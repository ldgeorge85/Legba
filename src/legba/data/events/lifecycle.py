# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The event lifecycle state machine (DATA MODEL V3 / P0 — spec §2.3).

THE SHAPE
---------
Five states — ``emerging``, ``developing``, ``active``, ``evolving``,
``resolved`` — a pure transition function, and an append-only ledger
(``event_lifecycle_events``, migration 0204). This module is a leaf, modeled
line-for-line on ``data/situations/trajectory.py``: vocabulary constants,
``next_state(...)`` raising rather than coercing, a frozen dataclass validated
at construction, and a DB CHECK mirroring the Python frozensets so no future
writer can bypass them. NO I/O in this module.

THE TABLE THE CHECKs MIRROR
---------------------------

::

    none      -> emerging    promotion (spec §2.2)                    opened
    emerging  -> developing  signal_count >= 3                        advanced
    developing-> active      signal_count >= 5 AND confidence >= 0.6  advanced
    active    -> evolving    24h link rate >= 2x the 7d baseline,      accelerated
                             or a new actor / new ISO2 attaches
    evolving  -> active      rate back inside [0.5x, 2x] baseline      stabilised
    emerging  -> resolved    no new link in 48 h                      resolved
    developing-> resolved    no new link in 72 h                      resolved
    active, evolving -> resolved   no new link in 7 days              resolved
    resolved  -> developing  a new signal links                       reactivated

THREE RULES THAT ARE NOT NEGOTIABLE (spec §2.3)
---------------------------------------------
1. Every clock runs on EVIDENCE time — ``signal_event_links.linked_at`` (the
   signal's ``fetched_at``), never run time. A backlog drain must not resolve
   a live event, and a re-ingest must not reactivate a dead one. That is why
   ``EventMeasurement.silence_hours`` is measured off the newest ``linked_at``,
   not ``now() - produced_at``.
2. ``reactivated`` is a TRANSITION, not a state — there is no sixth state.
   The court case months later reopens the event: the row is ``reactivated``
   in the ledger (indexed by ``idx_ele_reactivations``), and the state lands
   on ``developing``.
3. Silence never reactivates and never closes twice. ``resolved`` is the only
   evidence-free transition — it asserts that nothing ARRIVED — and the
   ``event_lifecycle_requires_evidence`` CHECK enforces that structurally.
   A resolved event that stays quiet writes nothing: no heartbeat rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID

#: Deploy marker (V3 plan §2 P0): the lifecycle vocabulary + FSM shipped under
#: this version string. Bump when the transition table changes.
EVENTS_LIFECYCLE_VERSION: str = "2026-09/p0"


# ---------------------------------------------------------------------------
# Vocabulary — mirrors the CHECK constraints in migration 0204
# ---------------------------------------------------------------------------

STATE_EMERGING: str = "emerging"
STATE_DEVELOPING: str = "developing"
STATE_ACTIVE: str = "active"
STATE_EVOLVING: str = "evolving"
STATE_RESOLVED: str = "resolved"

LIFECYCLE_STATES: frozenset[str] = frozenset({
    STATE_EMERGING,
    STATE_DEVELOPING,
    STATE_ACTIVE,
    STATE_EVOLVING,
    STATE_RESOLVED,
})

TRANSITION_OPENED: str = "opened"
TRANSITION_ADVANCED: str = "advanced"
TRANSITION_ACCELERATED: str = "accelerated"
TRANSITION_STABILISED: str = "stabilised"
TRANSITION_RESOLVED: str = "resolved"
TRANSITION_REACTIVATED: str = "reactivated"

LIFECYCLE_TRANSITIONS: frozenset[str] = frozenset({
    TRANSITION_OPENED,
    TRANSITION_ADVANCED,
    TRANSITION_ACCELERATED,
    TRANSITION_STABILISED,
    TRANSITION_RESOLVED,
    TRANSITION_REACTIVATED,
})

#: An event the ledger has never spoken about does not exist; the row the write
#: path opens with is ``emerging``.
INITIAL_STATE: str = STATE_EMERGING

#: The legal (state_from, transition) -> state_to table. Everything not listed
#: raises: a transition that does not move is a ledger row that lies about what
#: happened ('opened' is the deliberate exception — it records the birth, a
#: self-loop on emerging, so re-materialization of an un-advanced event is
#: idempotent rather than a refusal).
_NEXT_STATE: dict[tuple[str, str], str] = {
    (STATE_EMERGING, TRANSITION_OPENED): STATE_EMERGING,
    (STATE_EMERGING, TRANSITION_ADVANCED): STATE_DEVELOPING,
    (STATE_DEVELOPING, TRANSITION_ADVANCED): STATE_ACTIVE,
    (STATE_ACTIVE, TRANSITION_ACCELERATED): STATE_EVOLVING,
    (STATE_EVOLVING, TRANSITION_STABILISED): STATE_ACTIVE,
    (STATE_EMERGING, TRANSITION_RESOLVED): STATE_RESOLVED,
    (STATE_DEVELOPING, TRANSITION_RESOLVED): STATE_RESOLVED,
    (STATE_ACTIVE, TRANSITION_RESOLVED): STATE_RESOLVED,
    (STATE_EVOLVING, TRANSITION_RESOLVED): STATE_RESOLVED,
    (STATE_RESOLVED, TRANSITION_REACTIVATED): STATE_DEVELOPING,
}

# ---------------------------------------------------------------------------
# Trigger thresholds (spec §2.3) — evidence-time measurements, computed by the
# caller (P1's event_clustering) and handed in via EventMeasurement.
# ---------------------------------------------------------------------------

#: emerging advances once three signals evidence the occurrence.
EMERGING_ADVANCE_SIGNAL_COUNT: int = 3
#: developing activates on five signals AND a confidence floor — corroboration
#: alone is not activation.
DEVELOPING_ADVANCE_SIGNAL_COUNT: int = 5
DEVELOPING_ADVANCE_CONFIDENCE: float = 0.6
#: active accelerates when the trailing-24h link rate reaches this multiple of
#: the trailing-7d baseline (per-hour rates on both sides).
ACCELERATE_RATE_FACTOR: float = 2.0
#: evolving stabilises when the 24h rate re-enters [lo, hi] x baseline.
STABILISE_RATE_BAND: tuple[float, float] = (0.5, 2.0)
#: Silence horizons, in HOURS of evidence time (no new link's linked_at).
EMERGING_SILENCE_HOURS: float = 48.0
DEVELOPING_SILENCE_HOURS: float = 72.0
#: active and evolving share the seven-day horizon.
ACTIVE_SILENCE_HOURS: float = 7 * 24.0
EVOLVING_SILENCE_HOURS: float = 7 * 24.0


class EventTransitionError(ValueError):
    """A transition / state pair the state machine refuses.

    Raised rather than coerced. Every caller of :func:`next_state` is deciding
    what to write into an append-only ledger, and a silently-corrected
    transition is a permanent, unreviewable lie in that ledger.
    """


def transition_requires_evidence(transition: str) -> bool:
    """Does ``transition`` need non-empty ``derived_from``?

    Everything except :data:`TRANSITION_RESOLVED` — the silence transition,
    which asserts that nothing arrived. An unknown transition returns ``True``:
    the safe answer for a value this module does not recognize is "it had
    better carry evidence" (and construction rejects the unknown transition
    anyway).
    """
    return transition != TRANSITION_RESOLVED


def next_state(current: str, transition: str) -> str:
    """The lifecycle state after ``transition`` is applied to ``current``.

    Raises :class:`EventTransitionError` on an unknown state or transition, and
    on every (state, transition) pair the table does not bless — a transition
    that does not move is a ledger row that lies about what happened, and
    silence closing a resolved event twice is refused, not ignored.
    """
    if current not in LIFECYCLE_STATES:
        raise EventTransitionError(f"unknown lifecycle state: {current!r}")
    if transition not in LIFECYCLE_TRANSITIONS:
        raise EventTransitionError(f"unknown lifecycle transition: {transition!r}")
    try:
        return _NEXT_STATE[(current, transition)]
    except KeyError:
        raise EventTransitionError(
            f"transition {transition!r} is not legal from state {current!r}"
        ) from None


# ---------------------------------------------------------------------------
# The measurement bundle + the trigger evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventMeasurement:
    """What the caller measured about an event this tick — all EVIDENCE time.

    ``signal_count`` / ``confidence`` come off the events row's rollups.
    ``link_rate_24h`` and ``baseline_rate_7d`` are links-per-hour over
    ``signal_event_links.linked_at`` (the signal's fetched_at — never run
    time). ``new_actor_or_geo`` is whether a new actor or ISO2 attached this
    tick; ``new_link_arrived`` is whether at least one link landed this tick
    (the reactivation trigger). ``silence_hours`` is the evidence-time gap
    since the newest ``linked_at``.
    """

    signal_count: int = 0
    confidence: float = 0.0
    link_rate_24h: float = 0.0
    baseline_rate_7d: float = 0.0
    new_actor_or_geo: bool = False
    new_link_arrived: bool = False
    silence_hours: float = 0.0


def evaluate_transition(current: str, m: EventMeasurement) -> Optional[str]:
    """Which transition fires for ``current`` under measurement ``m``.

    Returns ``None`` when no trigger fires (no ledger row this tick — silence
    is only ever recorded once, as the ``resolved`` row itself). Raises
    :class:`EventTransitionError` on an unknown state.

    Ordering matters and is deliberate: SILENCE is evaluated first, because it
    asserts an absence — an event that has gone quiet resolves even if its
    rollup counts would otherwise look promotable. ``resolved`` events only
    ever leave on a new link, never on silence (rule 3).
    """
    if current not in LIFECYCLE_STATES:
        raise EventTransitionError(f"unknown lifecycle state: {current!r}")

    if current == STATE_RESOLVED:
        # Only a new link reactivates — silence never does (rule 3).
        return TRANSITION_REACTIVATED if m.new_link_arrived else None

    if current == STATE_EMERGING:
        if m.silence_hours >= EMERGING_SILENCE_HOURS:
            return TRANSITION_RESOLVED
        if m.signal_count >= EMERGING_ADVANCE_SIGNAL_COUNT:
            return TRANSITION_ADVANCED
        return None

    if current == STATE_DEVELOPING:
        if m.silence_hours >= DEVELOPING_SILENCE_HOURS:
            return TRANSITION_RESOLVED
        if (
            m.signal_count >= DEVELOPING_ADVANCE_SIGNAL_COUNT
            and m.confidence >= DEVELOPING_ADVANCE_CONFIDENCE
        ):
            return TRANSITION_ADVANCED
        return None

    if current == STATE_ACTIVE:
        if m.silence_hours >= ACTIVE_SILENCE_HOURS:
            return TRANSITION_RESOLVED
        if (
            m.link_rate_24h >= ACCELERATE_RATE_FACTOR * m.baseline_rate_7d
            and m.link_rate_24h > 0.0
        ) or m.new_actor_or_geo:
            return TRANSITION_ACCELERATED
        return None

    # STATE_EVOLVING
    if m.silence_hours >= EVOLVING_SILENCE_HOURS:
        return TRANSITION_RESOLVED
    lo, hi = STABILISE_RATE_BAND
    if lo * m.baseline_rate_7d <= m.link_rate_24h <= hi * m.baseline_rate_7d:
        return TRANSITION_STABILISED
    return None


# ---------------------------------------------------------------------------
# The ledger row
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LifecycleEvent:
    """One pending ``event_lifecycle_events`` row, validated at construction.

    Validated so a malformed transition is a countable drop inside the writer
    rather than a database error inside the write flow — and so a row whose
    ``state_to`` does not follow from ``state_from`` + ``transition`` (per
    :func:`next_state`) can never be constructed at all.
    """

    event_id: UUID
    occurred_at: datetime           # EVIDENCE time, never run time
    transition: str
    why: str
    state_from: str
    state_to: str
    derived_from: tuple[UUID, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.transition not in LIFECYCLE_TRANSITIONS:
            raise EventTransitionError(
                f"unknown lifecycle transition: {self.transition!r}"
            )
        for name in ("state_from", "state_to"):
            value = getattr(self, name)
            if value not in LIFECYCLE_STATES:
                raise EventTransitionError(
                    f"unknown lifecycle state for {name}: {value!r}"
                )
        # The declared to-state must be what the FSM produces for the pair —
        # a row that misreports where it landed corrupts every read of the
        # ledger's newest state_to.
        expected = next_state(self.state_from, self.transition)
        if expected != self.state_to:
            raise EventTransitionError(
                f"state_to {self.state_to!r} disagrees with the FSM: "
                f"{self.transition!r} from {self.state_from!r} lands on "
                f"{expected!r}"
            )
        if not str(self.why or "").strip():
            raise EventTransitionError(
                "a ledger row must say why — an empty `why` is unreadable"
            )
        if transition_requires_evidence(self.transition) and not self.derived_from:
            raise EventTransitionError(
                f"transition {self.transition!r} asserts a change and "
                f"therefore REQUIRES new cited evidence (derived_from is "
                f"empty); only {TRANSITION_RESOLVED!r} may be evidence-free"
            )
