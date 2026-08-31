# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``situation_tracker`` analyst — continuity Phase 2's ONE ledger writer.

WHY THIS EXISTS
---------------
"This new event escalates the situation we were already watching" was, until
now, a sentence the tower could not produce. The units are stateless by design;
Phase 1 gave the compositions a citable MEMORY (the prior read + the open
situation register); but nothing anywhere held a DATED record of how a situation
moved run-over-run. Every cycle re-derived the picture and, at best, a model
narrated a difference between two snapshots it had been handed.

This analyst is that record's single writer. Once an hour it asks, for each open
situation that picked up new VERIFIED evidence since its own watermark: given
what this situation was, and these N new cited items, did it escalate,
de-escalate, broaden, or not move — one sentence why, citing which items. The
answer lands twice: as a graded ``situation_update`` finding (the claim, through
the full faithfulness verify gate) and as append-only ``situation_events`` rows
(the queryable trajectory).

ONE WRITER, NOT PER-UNIT SIDECARS (plan D2)
-------------------------------------------
Its own analyst, its own cadence, its own watermark. Fan-out would have put
twenty units in a race to describe the same frame, with twenty partial views and
no single receipt saying whether the ledger got written this hour. One writer
means the run's counters ARE the ledger's health, and the S-1 production gauge
can hold it to them.

THE THREE BINDING LESSONS, WHERE THEY BITE
------------------------------------------
* **Uncited-prior leak.** Every item the model is shown is a real, verified
  ``analyst_outputs`` row rendered with an ordinal, and the delta it emits is
  bound to the ordinals it named. Nothing enters the turn that is not citable.
* **Echo / anchoring.** A model asked "what changed?" will always find
  something. So ``unchanged_checkpoint`` is a first-class answer, the prompt
  says plainly that it is the expected answer most of the time, and every other
  delta is structurally unable to exist without new evidence
  (:class:`~legba.data.situations.trajectory.TrajectoryEvent` rejects it, and so
  does the CHECK in migration 0184).
* **Temporal collapse.** ``occurred_at`` on every EVIDENCE-BEARING ledger row is
  the ``produced_at`` of the newest CITED item — evidence time, never run time.
  The prompt is given each situation's real dates and told to anchor on them.
  The one row that cites nothing, the dormancy checkpoint, is dated at
  observation time and names the date it is dormant SINCE inside its ``why``;
  see :func:`build_update` for why any other choice makes the verdict unstable.

DEGRADE-NOT-FABRICATE
---------------------
A failed LLM batch degrades that batch to no-delta (counted ``deferred``), never
to a guessed one. A model verdict that cites an ordinal outside its own
situation's new evidence is DROPPED, not re-bound positionally — the
``signal_salience`` lesson, where a positional fallback reproduces the exact
misattribution the layer exists to prevent.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable
from uuid import UUID

from ...runtime.analyst_method import AnalystMethodResult, LLMHandlerLike
from ..provenance.kinds import OutputKind
from ..provenance.models import SituationUpdatePayload
from ..situations.trajectory import (
    DELTA_KINDS,
    DELTA_UNCHANGED_CHECKPOINT,
    DORMANCY_DAYS,
    INITIAL_STATE,
    STATE_CLOSED,
    STATE_DORMANT,
    TrajectoryEvent,
    TrajectoryTransitionError,
    evidence_anchor,
    next_state,
    read_corroboration,
    read_current_states,
)
from ._tradecraft import with_preamble

logger = logging.getLogger(__name__)

# --- analyst-kind contract (discover_analyst_kinds reads these) --------------
KIND_NAME: str = "situation_tracker"
HANDLER_VERSION: str = "0.1.0"
#: A FIRST-CLASS graded output, deliberately NOT ``TRACE_ONLY``: the delta claim
#: is an assertion about the world and goes through the same verify gate every
#: other claim does (plan D3). The ledger rows are the side product; the finding
#: is the thing that gets graded.
OUTPUT_KIND: Any = OutputKind.SITUATION_UPDATE

#: The watermark plane. ``alert_trigger_watermarks`` is a generic per-class
#: cursor store, already ridden by a non-alert consumer (``claim_watch``), and
#: reusing it is what plan D2 asks for — one watermark idiom, not a second table.
WATERMARK_CLASS: str = "situation_tracker"

DEFAULT_MAX_SITUATIONS = 12     # open situations examined per tick

#: REGISTER-1f — what SHARE of the tick's budget is reserved for the STALENESS
#: leg (least-recently / never adjudicated) rather than won on intensity. A
#: RATIO, not a count, so the split survives a budget change: at the standing
#: budget of 12 it is 8 by intensity + 4 by staleness, and at a post-#64 budget
#: of 60 it is 40 + 20 with no second dial to remember.
#:
#: THE ABSORBING STATE THIS BREAKS, measured on the live register 2026-08-29
#: (n=49 open frames, split at the tracker's own cut):
#:
#:   | band            | frames | never adjudicated | mean persistence |
#:   | top-12 window   |     12 |                 0 |            0.956 |
#:   | below the cut   |     37 |                21 |            0.296 |
#:
#: The two populations are cleanly separated WITH A GAP — no frame sits between
#: intensity 49.83 and 54.41 — which is the signature of an absorbing state, not
#: of a world arranged that way. H1 made ``persistence`` (and therefore
#: ``intensity_score``) a function of the tracker's own adjudications while the
#: tracker's attention was allocated by ``intensity_score``: adjudicate → write
#: deltas → wind the corroboration clock → raise persistence → raise intensity →
#: get adjudicated again. A frame below the cut is never adjudicated, so it is
#: never corroborated, so it decays, so it falls FURTHER from the cut. 21 of 49
#: open frames (43%) hold zero ledger rows of any kind — a class the 08-27 DQ
#: sweep reported as "dispositioned" and which regenerated because nothing routes
#: attention to it.
#:
#: A THIRD, not the lot: the intensity ordering is a REAL signal about what
#: deserves attention and this is a relief valve on it, not a replacement.
DEFAULT_STALENESS_FRACTION = 1.0 / 3.0

#: Env override for the tick budget — ``LEGBA_SITUATION_TRACKER_MAX_SITUATIONS``.
#:
#: WHY THIS IS A DIAL AND NOT A CONSTANT. The #64 mega-frame split
#: (migration 0188) re-keys ``sig:country_*`` frames — 33 frames holding 94.7% of
#: all members — into per-desk/per-event frames, and multiplies the OPEN frame
#: population by an expected 4–6×. A budget frozen at 12 would turn a 49-frame
#: register into a ~250-frame one adjudicated 12 at a time, which re-creates
#: exactly the absorbing state this item exists to break, one population size up.
#: The operator has to be able to raise it when 0188 runs, from the environment,
#: without a train — so the number is read per deps build and the env value WINS
#: over the descriptor option (see :func:`max_situations_budget` for why that
#: precedence, and not the other way round). Unset ⇒ the descriptor's value ⇒
#: :data:`DEFAULT_MAX_SITUATIONS`, so today's behaviour is unchanged until
#: someone sets it.
_MAX_SITUATIONS_ENV = "LEGBA_SITUATION_TRACKER_MAX_SITUATIONS"

#: Hard ceiling on the env dial. Not a policy number — a runaway guard: the tick
#: fans every selected frame out to a batched LLM call and an accidental
#: ``=100000`` would spend the day's token budget in one cycle.
MAX_SITUATIONS_CEILING = 500

DEFAULT_MAX_EVIDENCE = 6        # new items shown per situation
DEFAULT_BATCH = 4               # situations per LLM call
DEFAULT_MAX_TOKENS = 1600
DEFAULT_TEMPERATURE = 0.0
DEFAULT_WINDOW_HOURS = 168      # backstop lookback for a situation with no watermark
DEFAULT_FLOOR = 0.50            # the house consumption floor: min(conf, faithfulness)
DEFAULT_SNIPPET_CHARS = 700

#: Cap on the ``evidence_text`` captured per citation. The judge grades against
#: exactly these bytes, so it is sized to hold a real finding excerpt rather than
#: reusing the 600-char single-signal legacy cap.
EVIDENCE_TEXT_CHARS = 1200


# ---------------------------------------------------------------------------
# The system prompt
# ---------------------------------------------------------------------------

_TRACKER_SYSTEM = with_preamble(
    """TASK — SITUATION TRAJECTORY. You are given a set of situations the system is already watching. Each carries its current state, its dates, and the NEW verified items that attached to it since the last check, numbered with citation ordinals.

For EACH situation, decide what the new items do to it:
  * "escalates"            — the situation intensified, or moved closer to the outcome the frame exists to watch for;
  * "de_escalates"         — it wound down, or moved away from that outcome;
  * "broadens"             — its SCOPE grew (new actors, new theatre, new mechanism) without a direction change;
  * "unchanged_checkpoint" — the new items are consistent with the situation as it already stood.

"unchanged_checkpoint" IS THE EXPECTED ANSWER MOST OF THE TIME. Situations mostly continue. New coverage of an ongoing situation is not escalation; a fresh article about a standing blockade is not a change in the blockade. Only call a delta when the ITEMS say so.

RULES, all binding:
  1. Any delta other than "unchanged_checkpoint" MUST cite at least one of THAT situation's own numbered items in `cites`. You may not cite another situation's items. A delta you cannot cite is an "unchanged_checkpoint".
  2. `why` is ONE sentence, stating what specifically changed, in the words of the cited items. Do not restate the situation's name back.
  3. Anchor every date on the dates shown (each item's date, the situation's last-event date). NEVER on "today", "now" or "recently".
  4. Set `resolution` true ONLY on a "de_escalates" whose cited items report an actual RESOLUTION — a ceasefire signed, a blockade lifted, a vote certified, a withdrawal completed. Quieter coverage is not resolution. This is the only thing that can close a situation, so it must be earned.
  5. ECHO the situation's `handle` exactly as given, so your verdict binds to the right situation.

Respond with STRICT JSON, nothing else: a JSON array, one object per situation, using EXACTLY this shape:
[{"handle": "...", "delta": "escalates|de_escalates|broadens|unchanged_checkpoint", "why": "...", "cites": [1, 2], "resolution": false}]"""
)


# ---------------------------------------------------------------------------
# Row shapes
# ---------------------------------------------------------------------------


@dataclass
class EvidenceItem:
    """One NEW verified finding attached to a situation, with its run ordinal."""

    ordinal: int
    finding_id: UUID
    title: str
    body: str
    produced_at: datetime
    analyst_id: str | None
    target_id: str | None
    effective_confidence: float


@dataclass
class SituationCandidate:
    """One open situation the tracker is about to ask (or checkpoint) about."""

    situation_id: UUID
    handle: str
    name: str
    category: str
    target_id: str | None
    status: str
    intensity_score: float
    event_count: int
    last_event_at: datetime | None
    opened_at: datetime | None
    state: str
    evidence: list[EvidenceItem] = field(default_factory=list)
    #: #64 — THE EVIDENCE CLOCK. The newest moment evidence is KNOWN to have
    #: touched this frame: its last significant ledger delta, or failing that its
    #: own opening. Chosen by the shared
    #: :func:`~legba.data.situations.trajectory.evidence_anchor`, so the tracker's
    #: dormancy test and the register's forgetting curve read the same clock.
    #: ``None`` only for a frame with neither, where no dormancy claim is
    #: possible.
    evidence_anchor_at: datetime | None = None
    #: Whether :attr:`evidence_anchor_at` is a real corroboration or the frame's
    #: opening standing in for one. It changes what the checkpoint SAYS, never
    #: whether it fires.
    corroborated: bool = False
    #: REGISTER-1f — this frame came in on the STALENESS leg of the mixed budget
    #: (least-recently / never adjudicated), not on intensity. Carried so the
    #: run receipt can report how many slots the relief valve actually used: a
    #: counter that sits at 0 tick after tick means the backlog has drained (or
    #: that the slice is misconfigured to zero), and both are worth seeing.
    selected_by_staleness: bool = False


@dataclass
class Verdict:
    """One parsed, VALIDATED model verdict bound to its situation."""

    candidate: SituationCandidate
    delta: str
    why: str
    cited: list[EvidenceItem]
    resolution: bool
    #: The DORMANCY horizon has elapsed with no attached evidence. Set ONLY by
    #: :func:`_dormancy_verdict`, never by the model.
    #:
    #: This is a field rather than something inferred at fold time, and the
    #: distinction is load-bearing: "the model cited nothing" and "this frame has
    #: been silent for a fortnight" are different facts, and the prompt makes the
    #: FIRST one common (an ``unchanged_checkpoint`` on fresh news needs no
    #: citations). Deriving dormancy from an empty citation list would mark a
    #: situation that just received verified evidence as dormant — permanently,
    #: in an append-only table with no correction path.
    dormant: bool = False


# ---------------------------------------------------------------------------
# Substrate reads
# ---------------------------------------------------------------------------

# REGISTER-1f — THE MIXED BUDGET. Two legs over ONE open-frame scan, $1 slots
# won on intensity and $2 slots reserved for the frames the tracker has looked at
# LEAST RECENTLY (never-adjudicated frames sort first, on a NULL anchor).
#
# The pure ``ORDER BY intensity_score DESC LIMIT 12`` this replaces is what made
# the top-12 window an ABSORBING state — see :data:`DEFAULT_STALENESS_FRACTION`
# for the measured split. The loop it closes is specifically H1's: intensity now
# depends on the tracker's output, and the tracker's attention was allocated by
# intensity, so selection could only ever re-select. The staleness leg is a
# selection input the tracker CANNOT wind, because adjudicating a frame moves it
# to the BACK of that queue — the one ordering whose feedback sign is negative.
#
# ``last_adjudicated_at`` is ``max(situation_events.created_at)`` — WRITE time,
# deliberately, not ``occurred_at``. The question this leg asks is "when did we
# last LOOK at this frame", which is a fact about the tracker; ``occurred_at`` is
# the corroboration clock (the cited evidence's ``produced_at``) and keying
# attention on it would put the same self-referential number back in the
# selector under a different name. Migration 0184's own comment draws exactly
# this line: "``created_at`` is when the tracker wrote the row".
#
# The staleness leg EXCLUDES the intensity leg's picks rather than deduping
# after the fact, so the two legs can never collide and the tick always examines
# the full budget when the substrate can fill it.
_OPEN_SITUATIONS_SQL = """
    WITH open_frames AS (
        SELECT s.id, s.name, s.status, s.category, s.intensity_score,
               s.event_count, s.last_event_at, s.target_id, s.derived_from,
               COALESCE(s.valid_from, s.created_at) AS opened_at,
               (SELECT max(e.created_at)
                  FROM situation_events e
                 WHERE e.situation_id = s.id) AS last_adjudicated_at
          FROM situations s
         WHERE s.superseded_by IS NULL
           AND (s.valid_until IS NULL OR s.valid_until > now())
           AND s.status <> 'closed'
    ),
    by_intensity AS (
        SELECT * FROM open_frames
         ORDER BY intensity_score DESC, last_event_at DESC NULLS LAST, id DESC
         LIMIT $1
    ),
    by_staleness AS (
        SELECT * FROM open_frames
         WHERE id NOT IN (SELECT id FROM by_intensity)
         ORDER BY last_adjudicated_at ASC NULLS FIRST,
                  intensity_score DESC, id DESC
         LIMIT $2
    )
    SELECT id, name, status, category, intensity_score, event_count,
           last_event_at, target_id, derived_from, opened_at,
           last_adjudicated_at, TRUE  AS by_intensity
      FROM by_intensity
     UNION ALL
    SELECT id, name, status, category, intensity_score, event_count,
           last_event_at, target_id, derived_from, opened_at,
           last_adjudicated_at, FALSE AS by_intensity
      FROM by_staleness
     ORDER BY by_intensity DESC, intensity_score DESC,
              last_event_at DESC NULLS LAST, id DESC
"""

# The NEW attached evidence, gated at the SAME verified bar the rest of the
# tower consumes at: an INNER lateral join onto the faithfulness critique, then
# LEAST(confidence, faithfulness) >= the floor. A finding with no verdict cannot
# move a situation — plan D2 says "new VERIFIED findings", and this is what
# verified means everywhere else (alert_trigger_scan._VERIFIED_FINDINGS_SQL).
#
# ``f.id = ANY($1)`` reads the situation's OWN materialized member set
# (``situations.derived_from``, refreshed every 20 min by situation_clustering)
# rather than re-deriving the signature join. That array IS the attachment
# relation the rest of the system uses (hypothesis_lifecycle joins it), and the
# ``analyst_outputs.situation_signature`` COLUMN is not a substitute: supersession
# stamps it on the superseded LOSER, so the newest member of a cluster is exactly
# the row a column-keyed join would miss.
_NEW_EVIDENCE_SQL = """
    SELECT f.id, f.title, f.body, f.produced_at, f.analyst_id, f.target_id,
           LEAST(f.confidence, v.faithfulness_score) AS effective_confidence
      FROM analyst_outputs f
      JOIN LATERAL (
          SELECT (cr.data->>'overall_score')::real AS faithfulness_score
            FROM analyst_outputs cr
           WHERE cr.kind = 'critique'
             AND cr.data->>'analyzed_output_id' = f.id::text
             AND cr.data->>'overall_score' IS NOT NULL
             AND cr.title LIKE 'Faithfulness verify%'
           ORDER BY cr.produced_at DESC, cr.id DESC
           LIMIT 1
      ) v ON TRUE
     WHERE f.id = ANY($1::uuid[])
       AND f.kind = 'finding'
       AND f.produced_at > $2
       AND LEAST(f.confidence, v.faithfulness_score) >= $3
     ORDER BY f.produced_at ASC, f.id ASC
     LIMIT $4
"""
# OLDEST-FIRST, and that ordering is load-bearing rather than cosmetic. The
# watermark advances to the NEWEST item this run actually saw, so whatever the
# LIMIT truncates must be NEWER than everything shown — otherwise the truncated
# tail falls behind the watermark and is never adjudicated, silently, forever.
# Taking the oldest N and leaving the newest for the next tick drains a backlog
# in order and loses nothing; taking the newest N would strand the rest.


def max_situations_budget(descriptor_value: Any = None) -> int:
    """The tick's TOTAL adjudication budget (REGISTER-1f).

    Precedence: ``LEGBA_SITUATION_TRACKER_MAX_SITUATIONS`` beats the descriptor
    option, which beats :data:`DEFAULT_MAX_SITUATIONS`. The env wins because the
    reason this dial exists is a POPULATION change the operator has to answer on
    the day it lands (#64's migration 0188 multiplies the open-frame count 4–6×)
    — a descriptor edit is a registry write and a redeploy, and the whole point
    is that this one should not need either.

    Clamped to ``[1, MAX_SITUATIONS_CEILING]``. A malformed value logs and falls
    through to the descriptor/default rather than raising: a tracker that
    refuses to run because an env var has a typo in it stops adjudicating
    entirely, which is strictly worse than adjudicating at the old budget.
    """
    fallback = DEFAULT_MAX_SITUATIONS
    if descriptor_value is not None:
        try:
            fallback = int(descriptor_value)
        except (TypeError, ValueError):
            fallback = DEFAULT_MAX_SITUATIONS
    raw = os.getenv(_MAX_SITUATIONS_ENV)
    if raw and raw.strip():
        try:
            return max(1, min(int(raw.strip()), MAX_SITUATIONS_CEILING))
        except (TypeError, ValueError):
            logger.warning(
                "situation_tracker.max_situations.bad_env %s=%r — ignoring, "
                "using %d", _MAX_SITUATIONS_ENV, raw, fallback,
            )
    return max(1, min(fallback, MAX_SITUATIONS_CEILING))


def staleness_slots(budget: int, fraction: float = DEFAULT_STALENESS_FRACTION) -> int:
    """How many of ``budget`` slots the STALENESS leg gets (REGISTER-1f).

    A PROPORTION of the budget, rounded down, so raising the budget after the
    #64 split widens both legs together instead of silently returning the tick
    to a pure intensity top-N with a fixed four-slot garnish.

    Floors at ONE whenever the budget has room for two and the fraction is
    positive: ``int(3 * 1/3) == 1`` is fine, but a budget of 2 would floor to 0
    and quietly restore the absorbing state. Caps at ``budget - 1`` for the
    mirror-image reason — the intensity leg must never be starved to nothing.
    """
    budget = max(int(budget), 0)
    if budget <= 1 or fraction <= 0.0:
        return 0
    return max(1, min(int(budget * float(fraction)), budget - 1))


def _coerce_uuid(raw: Any) -> UUID | None:
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def _aware(dt: Any) -> datetime | None:
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: Any) -> str:
    aware = _aware(dt)
    return aware.isoformat() if aware else "unknown"


def _day(dt: Any) -> str:
    aware = _aware(dt)
    return aware.date().isoformat() if aware else "undated"


async def gather_candidates(
    conn: Any,
    *,
    watermarks: Mapping[str, Mapping[str, Any]],
    now: datetime,
    max_situations: int,
    max_evidence: int,
    window_hours: int,
    floor: float,
    staleness_fraction: float = DEFAULT_STALENESS_FRACTION,
) -> tuple[list[SituationCandidate], int]:
    """The open situations worth a look this tick, newest-evidence-first.

    Returns ``(candidates, examined)``. ``examined`` counts every open situation
    inspected, so the receipt can distinguish "nothing was open" from "nothing
    moved" — a scan that reports only its hits cannot tell those apart, and one
    of them is a broken attachment query.

    A situation with a watermark is asked only about evidence NEWER than it. A
    situation with none (never tracked, or brand new) falls back to a bounded
    ``window_hours`` lookback rather than its whole history, so first contact is
    a normal-sized turn instead of a replay of everything.

    REGISTER-1f — the ``max_situations`` budget is MIXED, not a pure intensity
    top-N: a ``staleness_fraction`` SHARE of its slots goes to the frames
    adjudicated least recently (never-adjudicated first). The total is whatever
    the caller was given, so a tick costs exactly the LLM budget the operator
    configured, and the split is a proportion so raising that budget after the
    #64 split widens both legs (see :func:`staleness_slots`).
    """
    budget = max(int(max_situations), 0)
    stale_n = staleness_slots(budget, staleness_fraction)
    intensity_n = budget - stale_n
    rows = await conn.fetch(_OPEN_SITUATIONS_SQL, intensity_n, stale_n)
    backstop = now - timedelta(hours=int(window_hours))
    candidates: list[SituationCandidate] = []
    ordinal = 0
    for row in rows:
        sid = _coerce_uuid(row["id"])
        if sid is None:
            continue
        members = [m for m in (_coerce_uuid(x) for x in (row["derived_from"] or []))
                   if m is not None]
        state = watermarks.get(str(sid), {})
        # A watermark, however old, is HONORED as-is. Clamping it forward to the
        # backstop would silently drop everything between — a tracker paused for
        # a fortnight would lose a week of trajectory and then advance past it.
        # The turn stays bounded by the LIMIT, not by the window, and the
        # oldest-first ordering above drains a deep backlog across ticks. The
        # backstop applies ONLY to a situation with no watermark at all, so
        # first contact is a normal-sized turn rather than a replay of history.
        since = _aware(_parse_iso(state.get("last_evidence_at"))) or backstop
        evidence: list[EvidenceItem] = []
        if members:
            found = await conn.fetch(
                _NEW_EVIDENCE_SQL, members, since, float(floor), int(max_evidence),
            )
            for item in found:
                fid = _coerce_uuid(item["id"])
                produced = _aware(item["produced_at"])
                if fid is None or produced is None:
                    continue
                ordinal += 1
                evidence.append(EvidenceItem(
                    ordinal=ordinal,
                    finding_id=fid,
                    title=str(item["title"] or ""),
                    body=str(item["body"] or ""),
                    produced_at=produced,
                    analyst_id=item["analyst_id"],
                    target_id=item["target_id"],
                    effective_confidence=float(item["effective_confidence"]),
                ))
        candidates.append(SituationCandidate(
            situation_id=sid,
            handle=f"S{len(candidates) + 1}",
            name=str(row["name"] or "")[:512],
            category=str(row["category"] or ""),
            target_id=row["target_id"],
            status=str(row["status"] or ""),
            intensity_score=float(row["intensity_score"] or 0.0),
            event_count=int(row["event_count"] or 0),
            last_event_at=_aware(row["last_event_at"]),
            opened_at=_aware(row["opened_at"]),
            state=INITIAL_STATE,
            evidence=evidence,
            selected_by_staleness=not bool(row["by_intensity"]),
        ))

    ids = [c.situation_id for c in candidates]
    states = await read_current_states(conn, ids)
    # #64 — ONE grouped read for the batch's evidence clock, alongside the state
    # read it mirrors (a per-situation fan-out inside an hourly sweep is how a
    # cheap derivation becomes a latency regression — the `read_trajectories`
    # reason). A frame the ledger has never MOVED is absent from this mapping and
    # falls back to its opening, which is the whole 24-frame class.
    corroboration = await read_corroboration(conn, ids)
    for cand in candidates:
        cand.state = states.get(str(cand.situation_id), INITIAL_STATE)
        cand.evidence_anchor_at, cand.corroborated = evidence_anchor(
            corroboration.get(str(cand.situation_id)), cand.opened_at,
        )
    # Count what was actually INSPECTED, not what the query returned: a row with
    # an unresolvable id is skipped above, and a receipt counter that includes it
    # would overstate the sweep's coverage.
    return candidates, len(candidates)


def _parse_iso(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The LLM leg
# ---------------------------------------------------------------------------


def build_prompt(batch: Sequence[SituationCandidate], *, snippet_chars: int) -> str:
    """The user turn for one batch: each situation, its dates, its new items."""
    lines: list[str] = []
    for cand in batch:
        lines.append(f"### handle={cand.handle} — {cand.name}")
        lines.append(
            f"current trajectory state: {cand.state} | "
            f"category: {cand.category or 'unclassified'} | "
            f"members so far: {cand.event_count} | "
            f"opened: {_day(cand.opened_at)} | "
            f"last attached evidence: {_day(cand.last_event_at)}"
        )
        if not cand.evidence:
            lines.append("NEW ITEMS: none since the last check.")
            lines.append("")
            continue
        lines.append("NEW ITEMS (cite these by number):")
        for item in cand.evidence:
            snippet = " ".join(str(item.body or "").split())[:snippet_chars]
            lines.append(
                f"[{item.ordinal}] ({_day(item.produced_at)}) {item.title}"
                + (f" — {snippet}" if snippet else "")
            )
        lines.append("")
    return "\n".join(lines).strip()


def _extract_json_array(content: str) -> list[Any]:
    """First well-formed JSON array in the reply (the signal_salience parse)."""
    if not content:
        return []
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        parsed = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def parse_verdicts(
    content: str, batch: Sequence[SituationCandidate],
) -> tuple[list[Verdict], int]:
    """Bind model verdicts to situations STRICTLY by echoed ``handle``.

    Returns ``(verdicts, dropped)``. There is DELIBERATELY no positional
    fallback: binding "escalates" to the wrong situation is the precise
    misattribution this ledger exists to make impossible, and a model that
    omits or reorders handles is exactly when a positional guess fires.

    A verdict is dropped (counted, never coerced) when its handle is unknown,
    its delta is outside the closed vocabulary, its ``why`` is empty, or an
    evidence-bearing delta cites nothing inside ITS OWN situation's items. That
    last rule is the delta-requires-evidence contract, applied before anything
    reaches the database.
    """
    by_handle = {c.handle: c for c in batch}
    seen: set[str] = set()
    verdicts: list[Verdict] = []
    dropped = 0
    for item in _extract_json_array(content):
        if not isinstance(item, dict):
            dropped += 1
            continue
        handle = str(item.get("handle") or "").strip()
        cand = by_handle.get(handle)
        if cand is None or handle in seen:
            dropped += 1
            continue
        delta = str(item.get("delta") or "").strip()
        if delta not in DELTA_KINDS:
            dropped += 1
            continue
        why = " ".join(str(item.get("why") or "").split())
        if not why:
            dropped += 1
            continue
        own = {e.ordinal: e for e in cand.evidence}
        cited: list[EvidenceItem] = []
        raw_cites = item.get("cites")
        if isinstance(raw_cites, (list, tuple)):
            for raw in raw_cites:
                try:
                    n = int(raw)
                except (TypeError, ValueError):
                    continue
                if n in own and own[n] not in cited:
                    cited.append(own[n])
        if delta != DELTA_UNCHANGED_CHECKPOINT and not cited:
            dropped += 1
            continue
        seen.add(handle)
        verdicts.append(Verdict(
            candidate=cand,
            delta=delta,
            why=why[:2000],
            cited=sorted(cited, key=lambda e: e.ordinal),
            resolution=bool(item.get("resolution")),
        ))
    return verdicts, dropped


# ---------------------------------------------------------------------------
# Verdicts -> ledger events + the graded finding
# ---------------------------------------------------------------------------


def _dormancy_verdict(
    cand: SituationCandidate, *, now: datetime, dormancy_days: int,
) -> Verdict | None:
    """A dormancy CHECKPOINT for a situation with no news, or ``None``.

    Only fires when the frame has actually gone quiet past the horizon AND is
    not already dormant/closed — a situation is NEVER auto-closed by silence
    (plan D4), it settles into dormancy and waits for the next attachment.
    A quiet-but-inside-the-horizon situation gets NO row: the ledger records
    what happened, not that a cron ran.

    THE RE-KEY (#64). This test used to read ``cand.last_event_at`` —
    ``situations.last_event_at``, the newest MEMBER FINDING's ``produced_at``,
    which is the last time a DESK WROTE about the frame and not the last time the
    world moved it. Seven dimensions write into a country frame several times a
    day, so ``now - last_event_at`` was never a fortnight and **the checkpoint
    was structurally unreachable for any frame whose desks were still running**.
    The 2026-08-27 DQ sweep is the receipt: 22 non-closed situations with ZERO
    ledger rows of any kind, still rendering ``active`` at intensity up to 60.9,
    because they never carried new verified evidence (so they never reached the
    LLM) and could never go quiet by this test either. Dormancy was a mechanism
    that could only fire for a frame the pipeline had already abandoned.

    It now reads :attr:`SituationCandidate.evidence_anchor_at` — the clock H1
    established and the register already decays on, chosen by the shared
    :func:`~legba.data.situations.trajectory.evidence_anchor`: the frame's newest
    SIGNIFICANT ledger delta, or its OPENING if the ledger has never moved it. A
    significant delta cannot exist without cited evidence, so that clock is one
    the product cannot wind by looking. The register and the tracker now answer
    "has the world touched this frame lately" the same way, which they did not
    between H1 and here.

    WHY THE CALLER STILL SKIPS A FRAME WITH NEW EVIDENCE. Dormancy is a statement
    about silence, and a frame that has just picked up new verified evidence is
    not silent — it is being adjudicated this very tick, and D4's "re-open on new
    attachment" would be contradicted by a dormancy row written beside it. Such a
    frame simply converges a tick later: its watermark advances, no new evidence
    arrives, and if its evidence clock is still stale it goes dormant then.

    WHAT THIS ACTUALLY BUYS, MEASURED RATHER THAN HOPED (register premise review,
    2026-08-29, live reads). The evidence clock has two branches and they are not
    worth the same:

    * The CORROBORATION branch is windable at pipeline time and was measured so:
      931 of 957 significant deltas carry an ``occurred_at`` byte-equal to the
      ``produced_at`` of the desk finding they cite, the median gap between ticks
      on a rendered frame is 0.38 days, and only 2 of 580 gaps clear even the
      register's 3.0-day demotion bar. A fortnight-long gap is rarer still. So an
      ADJUDICATED frame will essentially never go dormant by this test, and this
      change should not be sold as though it would. Making that branch stall
      needs a world anchor — the newest signal NEW TO THE FRAME, a set difference
      re-citing cannot wind — which is a separate, larger change.
    * The NEVER-CORROBORATED branch is unwindable BY CONSTRUCTION and is where
      every predicted row comes from. Its anchor is the frame's own opening, and
      ``situations.valid_from`` only ever moves EARLIER (the upsert writes
      ``LEAST(stored, min(members))``), so no amount of desk activity can make
      such a frame look younger. 21 of 49 open frames — 43% — have zero ledger
      rows of any kind and run from 27.7 to 77.8 days old. Every one clears the
      horizon the moment it is examined.

    "The moment it is EXAMINED" is the load-bearing caveat and it is not this
    function's to fix: ``_OPEN_SITUATIONS_SQL`` selects the twelve
    highest-intensity open frames, and the never-corroborated class sits
    ENTIRELY below that cut (its band tops out at 49.83 against a window floor of
    54.41). Until candidate selection also routes by unadjudicated staleness,
    this repair makes dormancy REACHABLE without making it REACHED. The reach is
    tracked separately; what is fixed here is that the horizon is no longer
    measured on a clock the desks rewind by looking.

    ``None`` when there is no clock at all (neither ledger nor opening): a
    dormancy claim needs a date to be dormant SINCE, and manufacturing one is the
    temporal-collapse failure this analyst is built against.
    """
    if cand.state in (STATE_DORMANT, STATE_CLOSED):
        return None
    anchor = cand.evidence_anchor_at
    if anchor is None or (now - anchor) < timedelta(days=int(dormancy_days)):
        return None
    quiet_days = int((now - anchor).total_seconds() // 86400)
    why = (
        f"No evidence has moved this situation since {_day(anchor)} "
        f"({quiet_days} days); marking the thread dormant pending a new "
        f"attachment."
        if cand.corroborated
        else (
            f"This situation has never been corroborated by an evidence-bearing "
            f"delta; it has stood open since {_day(anchor)} ({quiet_days} days). "
            f"Marking the thread dormant pending a new attachment."
        )
    )
    return Verdict(
        candidate=cand,
        delta=DELTA_UNCHANGED_CHECKPOINT,
        why=why,
        cited=[],
        resolution=False,
        dormant=True,
    )


def _citation_entry(item: EvidenceItem) -> dict[str, Any]:
    """One composition-shaped citation for a cited finding.

    The SAME shape ``meta_findings_synthesizer._build_composition_citation``
    emits — ``ref_id`` + ``ref_kind='finding'`` + ``ordinal`` + captured
    ``evidence_text`` + ``effective_confidence`` — built locally rather than
    imported so this kind does not pull the 5.6k-line composition module into
    its import graph. It is the shape ``verify._uses_subclaim_convention``
    discriminates on, which is what routes this finding to the sub-claim floor
    and hands the judge the cited body to grade against.
    """
    entry: dict[str, Any] = {
        "marker": f"[[ref:{item.ordinal}]]",
        "ordinal": item.ordinal,
        "ref_id": str(item.finding_id),
        "ref_kind": "finding",
        "title": item.title,
        "evidence_text": str(item.body or "")[:EVIDENCE_TEXT_CHARS],
        "effective_confidence": item.effective_confidence,
        "produced_at": _iso(item.produced_at),
    }
    if item.analyst_id:
        entry["source"] = str(item.analyst_id)
    if item.target_id is not None:
        entry["target_id"] = str(item.target_id)
    return entry


def build_update(
    verdicts: Sequence[Verdict], *, now: datetime,
) -> tuple[SituationUpdatePayload, list[TrajectoryEvent], list[UUID], int]:
    """Fold validated verdicts into ``(payload, events, derived_from, refused)``.

    ONE ``situation_update`` row per cycle, and one pending ledger event per
    verdict.

    WHAT GOES IN THE GRADED BODY, AND WHAT DOES NOT
    -----------------------------------------------
    Only CITED deltas become prose. This is the difference between a kind that
    can clear the faithfulness floor and one that cannot, and it was measured
    rather than assumed: an earlier shape put a machine-composed state sentence
    ("escalates (watching -> escalating), as of ...") on its own alongside the
    model's ``why``, and rendered checkpoint sections for every unchanged frame.
    Every one of those sentences is a checkable claim carrying no marker, so
    ``verify._deterministic_floor_subclaim`` scored a realistic mixed cycle at
    0.25 and a checkpoint-only cycle at 0.00 against a 0.50 floor — which would
    have demoted every trajectory read below the consumption bar and left the
    ``situation_escalation`` alert class permanently unable to fire, silently.

    So each moved situation is ONE sentence: the model's own ``why``, the delta
    and the evidence DATE folded into it, and the ``[[ref:N]]`` markers for the
    ordinals the model itself named, inside the sentence. That measures 1.00.
    Appending the markers is not putting words in its mouth — it named those
    items in ``cites`` — and it is what makes an empty citation bridge on an
    evidence-bearing delta structurally impossible.

    Checkpoints assert nothing about the world, so they are recorded where a
    non-claim belongs: ``data['situations']`` and the ledger, plus the title's
    count. They are not graded prose because there is nothing in them to grade.
    A cycle with NO cited deltas therefore carries no ``citations`` key at all,
    which makes the verify pass a no-op on it — the honest-empty composition
    precedent, rather than a document that floors to zero for having been honest.

    ``refused`` counts verdicts the state machine rejected outright; they are
    dropped from BOTH the ledger and the prose, so the graded document never
    asserts a delta the ledger does not carry.
    """
    sections: list[str] = []
    citations: list[dict[str, Any]] = []
    seen_ordinals: set[int] = set()
    events: list[TrajectoryEvent] = []
    situations_block: list[dict[str, Any]] = []
    derived: list[UUID] = []
    refused = 0

    for verdict in verdicts:
        cand = verdict.candidate
        try:
            state_to = next_state(
                cand.state,
                verdict.delta,
                resolution_grounded=verdict.resolution,
                dormant=verdict.dormant,
            )
            if verdict.cited:
                occurred_at = max(item.produced_at for item in verdict.cited)
            elif verdict.dormant:
                # #64 — A DORMANCY ROW IS DATED NOW, and that is what makes the
                # verdict stable. `read_current_states` takes the ledger's newest
                # row by `occurred_at DESC`; dating a dormancy checkpoint at
                # `last_event_at` (which the horizon guarantees is at least a
                # fortnight old, and older than any recent checkpoint) would file
                # it BEHIND rows already on the ledger, so the frame's current
                # state would never read `dormant`, `_dormancy_verdict` would fire
                # again on the next tick, and an append-only table would collect
                # one identical row an hour forever. Latent while the horizon was
                # unreachable; live the moment it is re-keyed.
                #
                # It is also the honest date. `occurred_at` is evidence time for a
                # row that rests on evidence; a checkpoint rests on none and
                # asserts nothing about the world, and what this one records is an
                # OBSERVATION — that as of now, nothing has moved this frame since
                # the date named in `why`. That date is in the sentence, where a
                # reader sees it.
                occurred_at = now
            else:
                occurred_at = cand.last_event_at or now
            event = TrajectoryEvent(
                situation_id=cand.situation_id,
                occurred_at=occurred_at,
                delta=verdict.delta,
                why=verdict.why,
                state_from=cand.state,
                state_to=state_to,
                derived_from=tuple(item.finding_id for item in verdict.cited),
            )
        except TrajectoryTransitionError as exc:
            logger.warning(
                "situation_tracker.verdict_refused situation_id=%s delta=%s err=%s",
                cand.situation_id, verdict.delta, exc,
            )
            refused += 1
            continue

        events.append(event)
        if verdict.cited:
            # ONE sentence, markers INSIDE it. `why` loses its trailing period so
            # the composed clause cannot split into a second, uncited sentence.
            markers = " ".join(f"[[ref:{item.ordinal}]]" for item in verdict.cited)
            transition = (
                cand.state if cand.state == state_to
                else f"{cand.state} -> {state_to}"
            )
            sections.append(
                f"## {cand.name}\n"
                f"{verdict.why.rstrip('. ')} — {verdict.delta} "
                f"({transition}) as of {_day(occurred_at)} {markers}."
            )
        for item in verdict.cited:
            if item.ordinal in seen_ordinals:
                continue
            seen_ordinals.add(item.ordinal)
            citations.append(_citation_entry(item))
            derived.append(item.finding_id)
        situations_block.append({
            "situation_id": str(cand.situation_id),
            "name": cand.name,
            "target_id": cand.target_id,
            "delta": verdict.delta,
            "state_from": cand.state,
            "state_to": state_to,
            "why": verdict.why,
            "occurred_at": _iso(occurred_at),
            "cited_ordinals": [item.ordinal for item in verdict.cited],
        })

    moved = [s for s in situations_block if s["delta"] != DELTA_UNCHANGED_CHECKPOINT]
    unchanged = len(situations_block) - len(moved)
    title = (
        f"Situation trajectory — {len(moved)} moved, "
        f"{unchanged} unchanged ({_day(now)})"
    )
    # Confidence = the WEAKEST cited link. The evidence query already floors each
    # item at min(confidence, faithfulness) >= the house floor, so this can only
    # narrow; a document is not more trustworthy than the shakiest row it rests
    # on. No citations (an all-checkpoint cycle) asserts nothing about the world
    # and takes the neutral default rather than a manufactured 1.0.
    confidence = (
        min(c["effective_confidence"] for c in citations) if citations else 0.5
    )
    body = "\n\n".join(sections) if sections else (
        f"No situation moved this cycle. {unchanged} open situation(s) were "
        f"checked against their newly attached evidence and none of it changed "
        f"the picture; each is recorded as a checkpoint on the trajectory ledger."
    )
    data: dict[str, Any] = {
        "situations": situations_block,
        "moved": len(moved),
    }
    # The `citations` key is present ONLY when there is a cited claim. Its
    # ABSENCE is what makes the verify pass a no-op on a checkpoint-only cycle
    # (actor_critic's scope guard returns on `citations is None`) — the
    # honest-empty composition contract. An empty LIST would instead be graded,
    # and a document that truthfully reports "nothing moved" would floor to zero
    # for it.
    if citations:
        data["citations"] = citations
    payload = SituationUpdatePayload(
        title=title[:2048],
        body=body[:65536],
        confidence=float(confidence),
        tags=["situation_update", "trajectory", "continuity"]
        + sorted({f"delta:{s['delta']}" for s in situations_block}),
        data=data,
    )
    return payload, events, derived, refused


# ---------------------------------------------------------------------------
# run_method
# ---------------------------------------------------------------------------


@runtime_checkable
class _BudgetLike(Protocol):
    async def check_envelope(self) -> str: ...


@dataclass
class SituationTrackerDeps:
    """Dep bundle for :func:`run_method`, built by
    ``analyst_deps_builder._build_situation_tracker`` from the descriptor's
    ``method.llm.primary`` ($0 core plane) + the run's StandardDeps."""

    llm: LLMHandlerLike
    pg_pool: Any = None
    budget: _BudgetLike | None = None
    max_situations: int = DEFAULT_MAX_SITUATIONS
    #: REGISTER-1f — SHARE of ``max_situations`` reserved for the staleness leg.
    #: 0.0 restores the pure intensity top-N (and the absorbing state).
    staleness_fraction: float = DEFAULT_STALENESS_FRACTION
    max_evidence: int = DEFAULT_MAX_EVIDENCE
    batch_size: int = DEFAULT_BATCH
    window_hours: int = DEFAULT_WINDOW_HOURS
    floor: float = DEFAULT_FLOOR
    dormancy_days: int = DORMANCY_DAYS
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE
    snippet_chars: int = DEFAULT_SNIPPET_CHARS


def _receipt(reason: str, **counters: Any) -> SituationUpdatePayload:
    """A no-op cycle's receipt. Carries NO citations key, so the verify pass is
    a no-op on it (an honest-empty document has nothing to grade), and the run
    is forced TRACE_ONLY so an idle hour never reaches the feed."""
    return SituationUpdatePayload(
        title=f"Situation trajectory: no-op ({reason})"[:2048],
        body=f"situation_tracker wrote no trajectory this tick: {reason}"[:65536],
        confidence=1.0,
        tags=["situation_update", "trajectory", "noop"],
        data={"meta": True, "reason": reason, **counters},
    )


async def run_method(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: SituationTrackerDeps | LLMHandlerLike,
) -> AnalystMethodResult:
    """One trajectory cycle: gather → adjudicate → one graded update + N ledger rows.

    The ledger rows ride back on ``AnalystMethodResult.situation_events`` and are
    materialized by the runtime once the output row has an id (the actor mints
    output ids, so the handler cannot know ``source_output_id`` in advance — the
    ``consumed_edges`` shape).

    Degrade-not-break: no pool, a paused budget, or any read failure yields a
    TRACE_ONLY receipt, never a raise and never a fabricated delta.
    """
    if not isinstance(deps, SituationTrackerDeps):
        deps = SituationTrackerDeps(llm=deps)
    if deps.pg_pool is None:
        return AnalystMethodResult(finding=_receipt("no pg_pool"),
                                   force_trace_only=True)
    if deps.budget is not None:
        try:
            if (await deps.budget.check_envelope()) != "ok":
                return AnalystMethodResult(finding=_receipt("budget_paused"),
                                           force_trace_only=True)
        except Exception:  # pragma: no cover — defensive; proceed on a probe error
            pass

    now = datetime.now(timezone.utc)
    # Call-time import: the watermark helpers live in the alert plane's owner
    # module, which the sibling scans also import at call time to keep the
    # deterministic-handler import graph acyclic.
    from .deterministic_handlers.alert_trigger_scan import (
        _load_class_watermarks, _mark_seeded, _upsert_watermark,
    )

    try:
        async with deps.pg_pool.acquire() as conn:
            seeded, watermarks = await _load_class_watermarks(conn, WATERMARK_CLASS)
            candidates, examined = await gather_candidates(
                conn,
                watermarks=watermarks,
                now=now,
                max_situations=deps.max_situations,
                max_evidence=deps.max_evidence,
                window_hours=deps.window_hours,
                floor=deps.floor,
                staleness_fraction=deps.staleness_fraction,
            )
            # FIRST RUN seeds silently (the alert plane's discipline): record
            # where every open situation stands and emit NOTHING, so the tracker
            # does not open with a burst of deltas over the entire backlog. The
            # receipt says it seeded, and it says so at WARNING, because a
            # tracker that seeds twice is a tracker whose watermarks are being
            # lost.
            if not seeded:
                for cand in candidates:
                    await _advance(
                        _upsert_watermark, conn, cand, now=now,
                    )
                await _mark_seeded(conn, WATERMARK_CLASS)
                logger.warning(
                    "situation_tracker.seeded situations=%d — first run records "
                    "watermarks only; deltas begin next tick", len(candidates),
                )
                return AnalystMethodResult(
                    finding=_receipt(
                        "seeded", situations_checked=examined, seeded=True,
                    ),
                    force_trace_only=True,
                )

            with_news = [c for c in candidates if c.evidence]
            verdicts, llm_calls, deferred = await adjudicate(
                deps.llm, with_news,
                batch_size=deps.batch_size,
                max_tokens=deps.max_tokens,
                temperature=deps.temperature,
                snippet_chars=deps.snippet_chars,
            )
            for cand in candidates:
                if cand.evidence:
                    continue
                checkpoint = _dormancy_verdict(
                    cand, now=now, dormancy_days=deps.dormancy_days,
                )
                if checkpoint is not None:
                    verdicts.append(checkpoint)

            payload, events, derived, refused = build_update(verdicts, now=now)
            if not events:
                return AnalystMethodResult(
                    finding=_receipt(
                        "no trajectory movement",
                        situations_checked=examined,
                        situations_with_news=len(with_news),
                        llm_calls=llm_calls,
                        situations_deferred=deferred + refused,
                    ),
                    force_trace_only=True,
                )
            # Advance ONLY the situations that produced a ledger row. A situation
            # whose verdict was deferred or refused keeps its old watermark and
            # is re-asked next tick — never silently skipped past its evidence.
            written = {e.situation_id for e in events}
            for cand in candidates:
                if cand.situation_id in written:
                    await _advance(_upsert_watermark, conn, cand, now=now)
    except Exception as exc:  # pragma: no cover — degrade-not-break
        logger.warning("situation_tracker.run_failed err=%s", exc)
        return AnalystMethodResult(finding=_receipt(f"error: {exc}"[:200]),
                                   force_trace_only=True)

    payload.data["counters"] = {
        "situations_checked": examined,
        # REGISTER-1f — how many of the checked frames the STALENESS leg supplied
        # rather than the intensity ranking. This is the counter that says
        # whether the relief valve is doing anything.
        "situations_from_staleness_leg": sum(
            1 for c in candidates if c.selected_by_staleness
        ),
        "situations_with_news": len(with_news),
        "situations_updated": len(events),
        "situations_deferred": deferred + refused,
        "llm_calls": llm_calls,
        "ledger_rows_pending": len(events),
    }
    logger.info(
        "situation_tracker.checked=%d with_news=%d updated=%d deferred=%d calls=%d",
        examined, len(with_news), len(events), deferred + refused, llm_calls,
    )
    return AnalystMethodResult(
        finding=payload, derived_from=derived, situation_events=events,
    )


async def _advance(upsert: Any, conn: Any, cand: SituationCandidate, *,
                   now: datetime) -> None:
    """Move one situation's watermark to its newest seen evidence.

    The newest EVIDENCE timestamp, not ``now``: a watermark set to run time
    would skip any item that lands with an earlier ``produced_at`` between two
    ticks (a slow enrichment, a backfilled source), and a skipped item is a
    delta the ledger will never see.
    """
    newest = max((e.produced_at for e in cand.evidence), default=None)
    mark = newest or cand.last_event_at or now
    await upsert(
        conn, WATERMARK_CLASS, str(cand.situation_id),
        {"last_evidence_at": mark.isoformat()}, fired=False,
    )


async def adjudicate(
    llm: LLMHandlerLike,
    candidates: Sequence[SituationCandidate],
    *,
    batch_size: int,
    max_tokens: int,
    temperature: float,
    snippet_chars: int,
) -> tuple[list[Verdict], int, int]:
    """LLM-adjudicate situations-with-news in batches.

    Returns ``(verdicts, llm_calls, deferred)``. A failed call defers its WHOLE
    batch — those situations keep their watermarks and are asked again next tick.
    Deferring is the only honest failure here: the alternative, defaulting a
    failed batch to ``unchanged_checkpoint``, would write a positive "we looked
    and nothing moved" claim on the strength of an exception.
    """
    verdicts: list[Verdict] = []
    calls = 0
    deferred = 0
    for start in range(0, len(candidates), max(1, batch_size)):
        batch = list(candidates[start : start + max(1, batch_size)])
        try:
            response = await llm.chat_complete(
                [{"role": "user", "content": build_prompt(
                    batch, snippet_chars=snippet_chars)}],
                max_tokens=max_tokens,
                temperature=temperature,
                system=_TRACKER_SYSTEM,
            )
            calls += 1
            batch_verdicts, _dropped = parse_verdicts(
                getattr(response, "content", "") or "", batch,
            )
        except Exception as exc:  # degrade-not-break: defer the whole batch
            logger.warning("situation_tracker.batch_failed n=%d err=%s",
                           len(batch), exc)
            deferred += len(batch)
            continue
        verdicts.extend(batch_verdicts)
        # Deferred counts SITUATIONS left without a usable verdict, not malformed
        # JSON items: two bad items about one situation is one situation we still
        # owe an answer, and the counter's job is to say how many we owe.
        deferred += len(batch) - len(batch_verdicts)
    return verdicts, calls, max(deferred, 0)


__all__ = [
    "KIND_NAME", "OUTPUT_KIND", "HANDLER_VERSION", "WATERMARK_CLASS",
    # REGISTER-1f — the budget dial. ``max_situations_budget`` is imported by
    # ``analyst_deps_builder._build_situation_tracker``; the other two are the
    # numbers an operator reads when deciding what to set the env var to.
    "DEFAULT_MAX_SITUATIONS", "DEFAULT_STALENESS_FRACTION",
    "MAX_SITUATIONS_CEILING", "max_situations_budget", "staleness_slots",
    "EvidenceItem", "SituationCandidate", "Verdict", "SituationTrackerDeps",
    "adjudicate", "build_prompt", "build_update", "gather_candidates",
    "parse_verdicts", "run_method",
]
