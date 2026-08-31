# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The situation trajectory state machine + append-only ledger (continuity P2).

THE PROBLEM THIS SOLVES
-----------------------
The units are stateless slice-of-now analyzers and stay that way — replayability
is the product. Phase 1 built continuity AROUND them: a composition can now CITE
its own prior read and a register of open situations. What it still could not do
is answer "what changed since the prior read" as anything other than prose
reconstructed from two snapshots, every cycle, by a model that will always find
SOMETHING to call a change. This module makes trajectory a queryable property.

THE TWO AXES (and why the trajectory state is not ``situations.status``)
-----------------------------------------------------------------------
``situation_clustering`` re-derives ``situations.status`` every 20 minutes from
member RECENCY (active <= 2d, dormant <= 7d, closed beyond) and writes it
through an ON CONFLICT DO UPDATE. It is the right owner of that axis and it is
the only owner. The trajectory state is a DIFFERENT axis — DIRECTION, over a
14-day dormancy horizon, decided from verified evidence — and it lives on the
ledger row (``state_from``/``state_to``). The CURRENT state is the newest row's
``state_to``: derived from the log, so it cannot drift from the log, and no
20-minute re-materialization can stomp it.

THE RULE THE WHOLE MODULE EXISTS TO ENFORCE
-------------------------------------------
A delta REQUIRES new cited evidence. ``unchanged`` requires none — it asserts
nothing. This is checked three times, on purpose: here at construction
(:class:`TrajectoryEvent`), so a bad row never reaches SQL and the tracker can
count it; in the database (``situation_events_delta_requires_evidence``), so no
future writer can bypass it; and by the verify pass over the
``situation_update`` finding that carries the claim, so the PROSE is graded
against the same evidence.

DEGRADE-NOT-BREAK, LOUDLY
-------------------------
:func:`record_situation_events` runs inside the actor's output-write flow (the
``consumed_edges`` precedent) and must never fail a run — a tracker run whose
finding landed is not made better by throwing away the finding. So it logs at
WARNING with the situation ids and returns a count. Silence is covered from the
other side: the ``situation_trajectory_ledger`` backlog drain on the S-1
production gauge measures open situations with news and no ledger row, so a
writer that quietly stops writing pages instead of disappearing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vocabulary — mirrors the CHECK constraints in migration 0184
# ---------------------------------------------------------------------------

#: The situation moved UP: more intense, more escalatory, closer to the thing
#: the frame exists to watch for.
DELTA_ESCALATES: str = "escalates"
#: The situation moved DOWN. Paired with resolution-class evidence this is the
#: only delta that can CLOSE a situation.
DELTA_DE_ESCALATES: str = "de_escalates"
#: The situation grew in SCOPE (new actors, new theatre, new mechanism) without
#: a direction change. Deliberately not a direction: a widening crisis that is
#: not intensifying should not read as escalation.
DELTA_BROADENS: str = "broadens"
#: The tracker LOOKED and found no movement. The ONLY delta that asserts nothing
#: and therefore the only one that needs no evidence — and the vehicle for the
#: dormancy transition.
DELTA_UNCHANGED_CHECKPOINT: str = "unchanged_checkpoint"

DELTA_KINDS: frozenset[str] = frozenset({
    DELTA_ESCALATES,
    DELTA_DE_ESCALATES,
    DELTA_BROADENS,
    DELTA_UNCHANGED_CHECKPOINT,
})

STATE_WATCHING: str = "watching"
STATE_ESCALATING: str = "escalating"
STATE_DE_ESCALATING: str = "de_escalating"
STATE_DORMANT: str = "dormant"
STATE_CLOSED: str = "closed"

TRAJECTORY_STATES: frozenset[str] = frozenset({
    STATE_WATCHING,
    STATE_ESCALATING,
    STATE_DE_ESCALATING,
    STATE_DORMANT,
    STATE_CLOSED,
})

#: A situation the ledger has never spoken about is being WATCHED — not
#: escalating, not calm. The honest default.
INITIAL_STATE: str = STATE_WATCHING

#: No attached evidence for this many days => a dormancy checkpoint row.
#: DELIBERATELY not situation_clustering's 2-day active window: that axis asks
#: "did a member land recently", this one asks "has this thread gone quiet
#: enough that a reader should stop expecting movement". Two weeks is the plan's
#: number (D4) and is long enough that a slow-moving frame is not mislabelled.
DORMANCY_DAYS: int = 14

#: States a situation can be re-opened FROM by new attached evidence (D4:
#: "Re-open on new attachment"). A dormant thread that gets news is watched
#: again; a closed one that gets news was closed too early and reopens too —
#: closing is a judgment, not a tombstone.
_REOPENABLE: frozenset[str] = frozenset({STATE_DORMANT, STATE_CLOSED})


class TrajectoryTransitionError(ValueError):
    """A delta / state pair the state machine refuses.

    Raised rather than coerced. Every caller of :func:`next_state` is deciding
    what to write into an append-only ledger, and a silently-corrected
    transition is a permanent, unreviewable lie in that ledger.
    """


def delta_requires_evidence(delta: str) -> bool:
    """Does ``delta`` need non-empty ``derived_from``?

    Everything except :data:`DELTA_UNCHANGED_CHECKPOINT`. An unknown delta
    returns ``True`` — the safe answer for a value this module does not
    recognize is "it had better carry evidence" (and construction will reject
    the unknown delta anyway).
    """
    return delta != DELTA_UNCHANGED_CHECKPOINT


def next_state(
    current: str,
    delta: str,
    *,
    resolution_grounded: bool = False,
    dormant: bool = False,
) -> str:
    """The trajectory state after ``delta`` is applied to ``current``.

    ``resolution_grounded`` — the de-escalation is grounded in resolution-class
    evidence (a ceasefire signed, a blockade lifted, an election certified), not
    merely "things got quieter". It is the ONLY route to
    :data:`STATE_CLOSED` short of an operator, and it is deliberately paired
    with :data:`DELTA_DE_ESCALATES`, which cannot be written without evidence.

    ``dormant`` — the dormancy horizon (:data:`DORMANCY_DAYS`) has elapsed with
    no attached evidence. Only meaningful with
    :data:`DELTA_UNCHANGED_CHECKPOINT`; a situation is NEVER auto-closed by
    silence alone (D4), it goes dormant and waits.

    Raises :class:`TrajectoryTransitionError` on an unknown state or delta.
    """
    if current not in TRAJECTORY_STATES:
        raise TrajectoryTransitionError(f"unknown trajectory state: {current!r}")
    if delta not in DELTA_KINDS:
        raise TrajectoryTransitionError(f"unknown trajectory delta: {delta!r}")

    if delta == DELTA_UNCHANGED_CHECKPOINT:
        # Silence never closes and never re-opens. It can only settle an already
        # open thread into dormancy.
        if dormant and current not in (STATE_DORMANT, STATE_CLOSED):
            return STATE_DORMANT
        return current

    # Every remaining delta carries new evidence, so a dormant/closed thread is
    # live again before the direction is applied.
    base = STATE_WATCHING if current in _REOPENABLE else current

    if delta == DELTA_ESCALATES:
        return STATE_ESCALATING
    if delta == DELTA_DE_ESCALATES:
        return STATE_CLOSED if resolution_grounded else STATE_DE_ESCALATING
    # DELTA_BROADENS — scope, not direction. It re-opens a quiet thread (that is
    # what ``base`` did) but never overwrites a direction the evidence has not
    # actually turned.
    return base


# ---------------------------------------------------------------------------
# The ledger row
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrajectoryEvent:
    """One pending ``situation_events`` row, before it has a source finding.

    The tracker builds these during its run; the runtime materializes them right
    after the ``situation_update`` output row lands, stamping
    ``source_output_id`` with that row's id (the actor mints output ids, so the
    handler cannot know it in advance — the same shape ``consumed_edges`` uses).

    Validated at construction so a malformed delta is a countable drop inside
    the tracker rather than a database error inside the actor's write flow.
    """

    situation_id: UUID
    occurred_at: datetime
    delta: str
    why: str
    state_from: str
    state_to: str
    derived_from: tuple[UUID, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.delta not in DELTA_KINDS:
            raise TrajectoryTransitionError(
                f"unknown trajectory delta: {self.delta!r}"
            )
        for name in ("state_from", "state_to"):
            value = getattr(self, name)
            if value not in TRAJECTORY_STATES:
                raise TrajectoryTransitionError(
                    f"unknown trajectory state for {name}: {value!r}"
                )
        if not str(self.why or "").strip():
            raise TrajectoryTransitionError(
                "a ledger row must say why — an empty `why` is unreadable"
            )
        if delta_requires_evidence(self.delta) and not self.derived_from:
            raise TrajectoryTransitionError(
                f"delta {self.delta!r} asserts a change and therefore REQUIRES "
                f"new cited evidence (derived_from is empty); only "
                f"{DELTA_UNCHANGED_CHECKPOINT!r} may be evidence-free"
            )


_INSERT_EVENT_SQL = """
    INSERT INTO situation_events
        (situation_id, occurred_at, delta, state_from, state_to, why,
         derived_from, source_output_id)
    VALUES ($1, $2, $3, $4, $5, $6, $7::uuid[], $8)
    ON CONFLICT (situation_id, source_output_id) DO NOTHING
"""


#: The consumption floor a delta CLAIM must clear to be recorded. The house
#: number (scorecard_banding.FAITH_FLOOR / the reads-API gate): a claim whose
#: faithfulness sits below it is not consumed anywhere else either.
LEDGER_FAITHFULNESS_FLOOR: float = 0.50


def _admits(event: TrajectoryEvent, verification: Mapping[str, Any] | None) -> bool:
    """May this event be written, given the verify verdict on its source?

    An ``unchanged_checkpoint`` carries no evidence and asserts nothing about the
    world, so no verdict gates it — including the dormancy row, which must land
    even on a cycle that made no cited claim at all (and therefore was never
    graded, by the honest-empty contract).

    Every OTHER delta is a claim. It is admitted only when the verify pass
    actually produced a verdict at or above the floor. A missing verdict is a
    REFUSAL, not a pass: verification is None when the pass raised, and "we could
    not grade it" must not become a permanent row in a table with no DELETE.
    """
    if not delta_requires_evidence(event.delta):
        return True
    if not isinstance(verification, Mapping):
        return False
    try:
        score = float(verification.get("faithfulness_score"))
    except (TypeError, ValueError):
        return False
    return score >= LEDGER_FAITHFULNESS_FLOOR


async def record_situation_events(
    conn: Any,
    *,
    events: Sequence[TrajectoryEvent],
    source_output_id: UUID,
    verification: Mapping[str, Any] | None = None,
) -> int:
    """Append ``events`` to the ledger, bound to ``source_output_id``.

    ``verification`` is the faithfulness verdict on the source finding (the
    actor's ``verification_block``). Delta CLAIMS are written only when it
    clears :data:`LEDGER_FAITHFULNESS_FLOOR`; checkpoints are always written.
    See :func:`_admits` for why a missing verdict refuses rather than passes.

    Returns the number of rows that actually landed (a re-run over the same
    source finding lands 0 — the unique constraint makes the writer idempotent).

    NEVER raises. This runs inside the actor's output-write flow and a tracker
    run whose finding already landed is not improved by unwinding it. A failure
    is logged at WARNING naming the situations, and the resulting gap is what
    the ``situation_trajectory_ledger`` backlog drain on the production gauge
    measures — so the silent-writer failure mode is covered by a gauge rather
    than by an exception nobody would see.
    """
    if not events:
        return 0
    written = 0
    for event in events:
        if not _admits(event, verification):
            logger.warning(
                "situation_trajectory.delta_refused situation_id=%s delta=%s "
                "source_output_id=%s — the source claim did not clear the "
                "faithfulness floor (%s); no ledger row written",
                event.situation_id, event.delta, source_output_id,
                (verification or {}).get("faithfulness_score"),
            )
            continue
        try:
            tag = await conn.execute(
                _INSERT_EVENT_SQL,
                event.situation_id,
                event.occurred_at,
                event.delta,
                event.state_from,
                event.state_to,
                event.why,
                list(event.derived_from),
                source_output_id,
            )
        except Exception as exc:  # degrade-not-break, loudly
            logger.warning(
                "situation_trajectory.append_failed situation_id=%s delta=%s "
                "source_output_id=%s err=%s",
                event.situation_id, event.delta, source_output_id, exc,
            )
            continue
        # asyncpg returns e.g. "INSERT 0 1"; a DO NOTHING conflict yields
        # "INSERT 0 0". Count what landed, not what was attempted.
        if isinstance(tag, str) and tag.rsplit(" ", 1)[-1] != "0":
            written += 1
    return written


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

_CURRENT_STATE_SQL = """
    SELECT DISTINCT ON (situation_id)
           situation_id, state_to, occurred_at, delta
      FROM situation_events
     WHERE situation_id = ANY($1::uuid[])
     ORDER BY situation_id, occurred_at DESC, created_at DESC, id DESC
"""


async def read_current_states(
    conn: Any, situation_ids: Iterable[Any],
) -> dict[str, str]:
    """``{situation_id -> current trajectory state}`` for the ids given.

    A situation with NO ledger rows is ABSENT from the mapping rather than
    reported as :data:`INITIAL_STATE`. The caller decides what "never spoken
    about" means for its surface; manufacturing a state here would make "we have
    never assessed this frame" indistinguishable from "we assessed it and it is
    being watched".
    """
    ids = _coerce_uuids(situation_ids)
    if not ids:
        return {}
    rows = await conn.fetch(_CURRENT_STATE_SQL, ids)
    return {str(r["situation_id"]): str(r["state_to"]) for r in rows}


# ---------------------------------------------------------------------------
# THE CORROBORATION CLOCK (H1 — the register forgetting curve)
# ---------------------------------------------------------------------------
#
# THE DEFECT this read exists to close (CORRECTNESS-R2, M-1 / ATTRIBUTION §1).
# ``situations.intensity_score`` and ``situations.status`` are both derived by
# ``situation_clustering`` from the ``produced_at`` of the frame's MEMBER
# FINDINGS — every desk read that clustered into the frame, including the
# absence reads a desk emits whenever it looks and sees nothing. Seven country
# desks write into one country frame several times a day, so every member is
# minutes old, every half-life weight is ~1.0, and the intensity saturates near
# 60 while ``last_event_at`` is always today. The decay curve was real and never
# got to run: the pipeline refreshed its own inputs faster than the half-life.
#
# The consequence, verified in the live DB at the round's T0: the AR frame stood
# at ``intensity 60.09``, ``event_count 396``, ``last_event_at 2026-08-25``,
# ``status active`` — on a maritime pilots' strike that had ENDED on 5 August.
# The desks wrote "no material change" into the frame, the frame reported that
# back as high intensity and recent activity, and the escalation desk cited the
# register as its decisive evidence that the strike was live.
#
# THE FIX is to give the frame a SECOND clock that only the world can wind.
# A ``situation_events`` row that is not an ``unchanged_checkpoint`` cannot exist
# without cited evidence (:class:`TrajectoryEvent` refuses it, and so does
# ``situation_events_delta_requires_evidence``), and its ``occurred_at`` is by
# construction the ``produced_at`` of the newest item it cited — EVIDENCE time.
# So the newest SIGNIFICANT delta's ``occurred_at`` is exactly "the last time the
# world moved this frame", and the COUNT of significant deltas is exactly "how
# many independent times it has been corroborated". Those two numbers are what
# :mod:`situation_clustering` decays against.
#
# NOTHING IS INVENTED FOR A FRAME THE LEDGER HAS NEVER SPOKEN ABOUT. Such a
# frame is ABSENT from the mapping — the same honesty contract
# :func:`read_current_states` keeps — and the caller leaves its behaviour
# untouched rather than decaying a frame on a clock that has never been wound.

_CORROBORATION_SQL = """
    SELECT situation_id,
           MAX(occurred_at) AS last_corroborated_at,
           COUNT(*)         AS corroboration_count
      FROM situation_events
     WHERE situation_id = ANY($1::uuid[])
       AND delta <> 'unchanged_checkpoint'
     GROUP BY situation_id
"""


@dataclass(frozen=True)
class Corroboration:
    """What the ledger knows about a frame's LAST CONTACT WITH THE WORLD.

    ``last_corroborated_at`` — the ``occurred_at`` of the newest SIGNIFICANT
    delta, i.e. the ``produced_at`` of the newest finding that actually moved the
    frame. Never a checkpoint's timestamp: a checkpoint asserts nothing, so it
    cannot date anything.

    ``count`` — how many significant deltas the frame has, ever. This is the
    frame's EVIDENCE DENSITY: a frame the world has moved nine separate times has
    a stronger claim to persist through a quiet week than one built on a single
    article, and :mod:`situation_clustering` keys its half-life on exactly that.
    """

    last_corroborated_at: datetime
    count: int


def evidence_anchor(
    corroboration: Corroboration | None, opened_at: datetime | None,
) -> tuple[datetime | None, bool]:
    """``(anchor, corroborated)`` — WHICH CLOCK dates this frame's last contact
    with the world.

    THE ONE DEFINITION, and it has two consumers by design.
    :mod:`~legba.data.analysts.deterministic_handlers.situation_clustering`
    decays intensity and demotes ``situations.status`` against it (H1); the
    :mod:`~legba.data.analysts.situation_tracker` decides DORMANCY against it
    (#64). Those two asked the same question through two different clocks until
    #64 — the register on the evidence clock, the tracker on the MEMBER clock —
    and the tracker's answer was structurally unreachable as a result. One
    predicate, two consumers, no drift: the same shape the
    ``is_non_event_situation_name`` repair took.

    * A frame the ledger HAS moved anchors on its newest SIGNIFICANT delta —
      evidence time, by construction (a non-checkpoint row cannot exist without
      cited evidence), and unwindable by the pipeline's own cadence.
    * A frame the ledger has NEVER moved anchors on its OPENING, and is reported
      ``corroborated=False`` so a caller can render the distinction (the register
      prints NEVER-CORROBORATED rather than dating a corroboration that never
      happened). It still DECAYS: no recorded corroboration is not a weaker claim
      to currency than a stale one. Decay on what is known; label what is known.
    * Neither — a frame with no ledger row and no opening — yields
      ``(None, False)``. The caller must treat that as "no clock", never as
      "fresh": both consumers leave such a frame alone rather than guess.
    """
    if corroboration is not None:
        anchor = corroboration.last_corroborated_at
        if isinstance(anchor, datetime):
            return (
                anchor if anchor.tzinfo else anchor.replace(tzinfo=timezone.utc),
                True,
            )
    if isinstance(opened_at, datetime):
        return (
            opened_at if opened_at.tzinfo else opened_at.replace(tzinfo=timezone.utc),
            False,
        )
    return (None, False)


async def read_corroboration(
    conn: Any, situation_ids: Iterable[Any],
) -> dict[str, Corroboration]:
    """``{situation_id -> }`` :class:`Corroboration` for the ids given.

    ONE grouped query for the whole batch (the ``read_trajectories`` reason: the
    caller asks for every open frame on a 20-minute cadence, and a per-situation
    fan-out is how a cheap derivation becomes a latency regression).

    A situation with NO significant deltas — never adjudicated, or adjudicated
    and never moved — is ABSENT from the mapping. That is not the same as
    ``count=0``: "the world has never moved this frame" and "we have never
    looked" are different facts and the caller must be able to tell them apart.
    """
    ids = _coerce_uuids(situation_ids)
    if not ids:
        return {}
    rows = await conn.fetch(_CORROBORATION_SQL, ids)
    out: dict[str, Corroboration] = {}
    for r in rows:
        when = r["last_corroborated_at"]
        if not isinstance(when, datetime):
            continue
        out[str(r["situation_id"])] = Corroboration(
            last_corroborated_at=when, count=int(r["corroboration_count"] or 0),
        )
    return out


_TRAJECTORY_SQL = """
    SELECT id, situation_id, occurred_at, delta, state_from, state_to, why,
           derived_from, source_output_id, created_at
      FROM situation_events
     WHERE situation_id = $1
     ORDER BY occurred_at DESC, created_at DESC, id DESC
     LIMIT $2
"""


async def read_trajectory(
    conn: Any, situation_id: Any, *, limit: int = 50,
) -> list[dict[str, Any]]:
    """The ledger for ONE situation, newest first, bounded by ``limit``."""
    sid = _coerce_uuid(situation_id)
    if sid is None:
        return []
    rows = await conn.fetch(_TRAJECTORY_SQL, sid, max(int(limit), 0))
    return [_row_to_dict(r) for r in rows]


#: FRAME-2 §2.3.1 — how far back a SIGNIFICANT delta may be and still be shown
#: in the register. The fortnight, the same window the window ledger carries and
#: the same one the composition admits heads over: a register whose movement
#: record reached further back than the read's own window would date-anchor the
#: prose outside anything else it was shown.
SIGNIFICANT_WINDOW_HOURS: int = 336

#: How many trailing ``unchanged_checkpoint`` rows accompany the significant
#: deltas. ONE, and it is not decoration: without it a frame with real August
#: escalations and nothing since would read as though it were still moving, and
#: "the last time we looked, nothing had changed, and that was <date>" is a fact
#: the reader needs. It renders LAST, after the movement.
CHECKPOINT_LINES: int = 1

# FRAME-2 §2.3.1 — THE RENDER SELECTION REPAIR.
#
# THE DEFECT, from the DB diagnosis (all rows read-only, 2026-08-20). Fleet-wide
# ``situation_events`` holds 461 escalates / 117 broadens / 50 de_escalates and
# 742 ``unchanged_checkpoint`` rows, and the hourly tracker writes MULTIPLE
# checkpoints a day (AR: five at 13:31:42Z on 08-20 alone). The register showed
# the newest three rows of ANY kind — so at the round's T0 the three rendered AR
# lines were three same-day "unchanged_checkpoint: no coercive economic measures
# observed", while that same frame's THREE real dated August escalations (the
# 08-06 maritime-pilot strike halting grain exports, the 08-07/08-09 Ecuador-gang
# sanctions) sat below the render cut. The record of movement existed; the render
# selected the noise, and the composition then read the register as evidence of
# calm.
#
# THE FIX is a predicate, not new machinery: rank the SIGNIFICANT deltas
# (anything that is not a checkpoint) in their own window, keep at most one
# trailing checkpoint, and hand the caller the movement FIRST. Two ranks in one
# query — still ONE round trip for up to eight frames, which is why the windowed
# rank was chosen over a per-situation fan-out in the first place.
_TRAJECTORIES_SQL = """
    SELECT id, situation_id, occurred_at, delta, state_from, state_to, why,
           derived_from, source_output_id, created_at, row_class
      FROM (
        SELECT e.*,
               0 AS row_class,
               row_number() OVER (
                   PARTITION BY e.situation_id
                   ORDER BY e.occurred_at DESC, e.created_at DESC, e.id DESC
               ) AS rn,
               $2::int AS keep
          FROM situation_events e
         WHERE e.situation_id = ANY($1::uuid[])
           AND e.delta <> 'unchanged_checkpoint'
           AND e.occurred_at > NOW() - make_interval(hours => $3)
        UNION ALL
        SELECT e.*,
               1 AS row_class,
               row_number() OVER (
                   PARTITION BY e.situation_id
                   ORDER BY e.occurred_at DESC, e.created_at DESC, e.id DESC
               ) AS rn,
               $4::int AS keep
          FROM situation_events e
         WHERE e.situation_id = ANY($1::uuid[])
           AND e.delta = 'unchanged_checkpoint'
      ) ranked
     WHERE rn <= keep
     ORDER BY situation_id, row_class, occurred_at DESC, created_at DESC, id DESC
"""


async def read_trajectories(
    conn: Any,
    situation_ids: Iterable[Any],
    *,
    per_situation: int = 3,
    significant_window_hours: int = SIGNIFICANT_WINDOW_HOURS,
    checkpoint_lines: int = CHECKPOINT_LINES,
) -> dict[str, list[dict[str, Any]]]:
    """``{situation_id -> its MOVEMENT record, newest first, then one checkpoint}``.

    ``per_situation`` bounds the SIGNIFICANT deltas (escalates / de_escalates /
    broadens) within ``significant_window_hours``; ``checkpoint_lines`` bounds
    the trailing ``unchanged_checkpoint`` rows, which are unwindowed on purpose —
    "we last looked on <date> and nothing had changed" is worth saying however
    long ago that was, and a checkpoint asserts nothing about the world (it is
    the one delta the ledger writes without evidence), so showing an old one
    cannot mislead the way an old escalation could.

    REGISTER-1c (2026-08-29) — THE EXEMPTION ABOVE IS TRUE OF THE DELTA, NOT OF
    THE ROW, and this reader is why the distinction had to be made downstream.
    The row it returns also carries ``why``: free model prose, written under a
    prompt whose own instruction is "situations mostly continue". 34% of the
    fleet's 1,095 checkpoint rows carry currency language and 53 carry
    CORROBORATION language, so an UNWINDOWED checkpoint's prose could — and did,
    24 days after the strike ended — tell a desk that an event "continues".
    The reader still returns ``why`` (the ``/v3`` trajectory API and the ledger
    drill both want it, and it is a real ledger field), but
    ``window_ledger._render_situation_register_lines`` no longer PRINTS it for a
    checkpoint. The unwindowed-checkpoint decision above survives intact because
    what now renders — the date and the delta name — is exactly the fact the
    quoted rationale claims for it.

    ORDER IS THE PRODUCT HERE. Movement first, quiet last — a caller renders the
    list as it arrives, and the whole defect this repairs was a render that put
    the day's checkpoint chatter where the movement should have been.

    ONE query with two windowed ranks rather than a fan-out of per-situation
    reads: the composition register asks for the deltas of up to eight frames on
    every compose, and eight round-trips inside a prompt build is how a
    "best-effort enrichment" turns into a latency regression.

    Situations with no ledger rows are simply absent (same honesty contract as
    :func:`read_current_states`). ``per_situation <= 0`` with no checkpoints
    asked for returns ``{}`` and issues NO query.
    """
    ids = _coerce_uuids(situation_ids)
    keep_significant = max(int(per_situation), 0)
    keep_checkpoints = max(int(checkpoint_lines), 0)
    if not ids or (keep_significant <= 0 and keep_checkpoints <= 0):
        return {}
    rows = await conn.fetch(
        _TRAJECTORIES_SQL,
        ids,
        keep_significant,
        int(significant_window_hours),
        keep_checkpoints,
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(str(r["situation_id"]), []).append(_row_to_dict(r))
    return out


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def _coerce_uuid(raw: Any) -> UUID | None:
    """``UUID`` or ``None`` — never a fabricated id."""
    if isinstance(raw, UUID):
        return raw
    if raw is None:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def _coerce_uuids(raw: Iterable[Any]) -> list[UUID]:
    out: list[UUID] = []
    for item in raw or ():
        uid = _coerce_uuid(item)
        if uid is not None:
            out.append(uid)
    return out


def _row_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    """One ledger row as a plain JSON-friendly dict."""
    return {
        "id": str(row["id"]),
        "situation_id": str(row["situation_id"]),
        "occurred_at": row["occurred_at"],
        "delta": str(row["delta"]),
        "state_from": str(row["state_from"]),
        "state_to": str(row["state_to"]),
        "why": str(row["why"]),
        "derived_from": [str(u) for u in (row["derived_from"] or [])],
        "source_output_id": str(row["source_output_id"]),
        "created_at": row["created_at"],
    }


__all__ = [
    "CHECKPOINT_LINES",
    "DELTA_BROADENS",
    "DELTA_DE_ESCALATES",
    "DELTA_ESCALATES",
    "DELTA_KINDS",
    "DELTA_UNCHANGED_CHECKPOINT",
    "DORMANCY_DAYS",
    "SIGNIFICANT_WINDOW_HOURS",
    "INITIAL_STATE",
    "LEDGER_FAITHFULNESS_FLOOR",
    "STATE_CLOSED",
    "STATE_DE_ESCALATING",
    "STATE_DORMANT",
    "STATE_ESCALATING",
    "STATE_WATCHING",
    "TRAJECTORY_STATES",
    "Corroboration",
    "TrajectoryEvent",
    "TrajectoryTransitionError",
    "delta_requires_evidence",
    "evidence_anchor",
    "next_state",
    "read_corroboration",
    "read_current_states",
    "read_trajectories",
    "read_trajectory",
    "record_situation_events",
]
