# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P1 event lifecycle maintenance — evidence-time ledger transitions.

``events.lifecycle`` is intentionally pure. This module is the only place that
measures live ``signal_event_links`` and turns the pure FSM verdict into the
append-only ``event_lifecycle_events`` row plus the row's fast-state column.
The two writes share one caller transaction, so a transition and the state it
lands on cannot disagree.

All clocks are evidence clocks: ``linked_at`` is the signal's ``fetched_at``.
A silence transition's ``occurred_at`` is the deterministic crossing point —
``newest linked_at + that state's silence horizon`` — not the run's wall clock.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from uuid import UUID

from ...events import lifecycle as el


def _utc(value: Any) -> datetime | None:
    """Coerce a datetime/ISO value to aware UTC."""
    if isinstance(value, datetime):
        dt = value
    elif value:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    return (
        dt.replace(tzinfo=timezone.utc)
        if dt.tzinfo is None
        else dt.astimezone(timezone.utc)
    )


def _uuid(value: Any) -> UUID | None:
    """Coerce ``value`` to a UUID, returning ``None`` on malformed input."""
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


_SILENCE_HORIZON_BY_STATE = {
    el.STATE_EMERGING: timedelta(hours=el.EMERGING_SILENCE_HOURS),
    el.STATE_DEVELOPING: timedelta(hours=el.DEVELOPING_SILENCE_HOURS),
    el.STATE_ACTIVE: timedelta(hours=el.ACTIVE_SILENCE_HOURS),
    el.STATE_EVOLVING: timedelta(hours=el.EVOLVING_SILENCE_HOURS),
}


#: This scan LEFT JOINs BOTH link tables onto ``events`` in one FROM, so every
#: row is one (signal link x entity link) PAIR: an event with 7 members and 23
#: actors produces 161 rows, not 7. Every count below is therefore taken
#: ``DISTINCT sel.signal_id`` — ``signal_event_links`` is unique on
#: ``(signal_id, event_id)``, so the distinct signal ids under one event ARE
#: its link rows, whatever the pairing multiplies them into.
#:
#: A plain ``count(sel.signal_id)`` here is what wrote the live drift: on
#: 2026-09-25, 1,099 of 1,246 ``events`` rows disagreed with their real
#: membership and 1,110 of them carried EXACTLY ``n_links * greatest(n_actors,
#: 1)`` — the worst row 170,526 against a real count in the hundreds. Only the
#: DISTINCT-ed ``distinct_source_count`` and the ``array_agg(DISTINCT ...)``
#: bundles were right, which is why the drift read as a ``signal_count``-only
#: fault. The events with no actor link at all (the pair count degenerates to
#: the link count) are the 147 that were correct.
#:
#: The two rates are only ever read as a RATIO against each other
#: (``link_rate_24h >= 2x baseline_rate_7d``, and the stabilise band), and the
#: fan-out scaled both by the same factor, so de-fanning them moves no
#: transition — it only makes the numbers the ledger's ``why`` line quotes
#: true. A signal's ``linked_at`` is a property of its own link row, so it is
#: uniform across that link's pairs and the FILTER partitions the distinct
#: ids exactly as it partitioned the rows.
_OPEN_EVENT_STATS_SQL = """
    SELECT e.id, e.lifecycle_state, e.confidence, e.geo,
           count(DISTINCT sel.signal_id)::int AS signal_count,
           count(DISTINCT NULLIF(sel.source_id, ''))::int
               AS distinct_source_count,
           max(sel.linked_at) AS latest_linked_at,
           count(DISTINCT sel.signal_id) FILTER (
               WHERE sel.linked_at >= $1::timestamptz - interval '24 hours'
           )::float / 24.0 AS link_rate_24h,
           count(DISTINCT sel.signal_id) FILTER (
               WHERE sel.linked_at >= $1::timestamptz - interval '7 days'
           )::float / 168.0 AS baseline_rate_7d,
           COALESCE(array_agg(DISTINCT sel.signal_id)
                    FILTER (WHERE sel.signal_id IS NOT NULL), '{}'::uuid[])
               AS signal_ids,
           COALESCE(array_agg(DISTINCT eel.entity_id)
                    FILTER (WHERE eel.entity_id IS NOT NULL), '{}'::uuid[])
               AS entity_ids,
           fresh.link_since_ledger,
           fresh.entity_link_since_ledger
      FROM events e
      LEFT JOIN signal_event_links sel ON sel.event_id = e.id
      LEFT JOIN event_entity_links eel ON eel.event_id = e.id
      LEFT JOIN LATERAL (
          SELECT CASE WHEN e.lifecycle_state = 'resolved' THEN EXISTS (
                     SELECT 1 FROM signal_event_links sl
                      WHERE sl.event_id = e.id
                        AND sl.created_at > COALESCE(
                                (SELECT max(ele.created_at)
                                   FROM event_lifecycle_events ele
                                  WHERE ele.event_id = e.id),
                                '-infinity'::timestamptz)
                 ) ELSE FALSE END AS link_since_ledger,
                 CASE WHEN e.lifecycle_state IN ('active','evolving')
                      THEN EXISTS (
                     SELECT 1 FROM event_entity_links el2
                      WHERE el2.event_id = e.id
                        AND el2.created_at > COALESCE(
                                (SELECT max(ele.created_at)
                                   FROM event_lifecycle_events ele
                                  WHERE ele.event_id = e.id),
                                '-infinity'::timestamptz)
                 ) ELSE FALSE END AS entity_link_since_ledger
      ) fresh ON TRUE
     WHERE e.lifecycle_state <> 'resolved'
        OR e.id = ANY($2::uuid[])
        OR fresh.link_since_ledger
     GROUP BY e.id, e.lifecycle_state, e.confidence, e.geo,
              e.lifecycle_changed_at,
              fresh.link_since_ledger, fresh.entity_link_since_ledger
     ORDER BY e.lifecycle_changed_at ASC, e.id ASC
     LIMIT $3
"""


_EVENT_TRANSITION_SQL = """
    INSERT INTO event_lifecycle_events
        (event_id, occurred_at, transition, state_from, state_to,
         why, derived_from, analyst_id, analyst_version, run_id)
    VALUES ($1, $2, $3, $4, $5, $6, $7::uuid[], $8, $9, $10)
"""


_EVENT_STATE_SQL = """
    UPDATE events
       SET lifecycle_state = $2,
           lifecycle_changed_at = $3,
           signal_count = $4,
           distinct_source_count = $5,
           confidence = $6,
           updated_at = NOW()
     WHERE id = $1
"""


def _evidence_for(row: Mapping[str, Any], new_ids: Iterable[UUID]) -> list[UUID]:
    """Evidence for an evidence-bearing transition, newest-member first."""
    fresh = list(new_ids)
    if fresh:
        return fresh
    return [
        x for x in (_uuid(v) for v in (row.get("signal_ids") or ()))
        if x is not None
    ]


def _transition_why(
    transition: str, m: el.EventMeasurement, new_count: int
) -> str:
    """One readable ledger reason for a transition."""
    if transition == el.TRANSITION_RESOLVED:
        return (
            f"no new evidence for {m.silence_hours:.1f}h; "
            f"signal_count={m.signal_count}"
        )
    if transition == el.TRANSITION_REACTIVATED:
        return f"{new_count} new evidence link(s) after resolution"
    if transition == el.TRANSITION_ADVANCED:
        return (
            f"signal_count={m.signal_count}, "
            f"confidence={m.confidence:.3f}"
        )
    if transition == el.TRANSITION_ACCELERATED:
        if m.new_actor_or_geo:
            return "a new actor or ISO2 attached"
        return (
            f"24h link rate {m.link_rate_24h:.3f}/h is >= "
            f"{el.ACCELERATE_RATE_FACTOR}x the 7d baseline "
            f"{m.baseline_rate_7d:.3f}/h"
        )
    if transition == el.TRANSITION_STABILISED:
        return (
            f"24h link rate {m.link_rate_24h:.3f}/h returned to the "
            f"[{el.STABILISE_RATE_BAND[0]}x, {el.STABILISE_RATE_BAND[1]}x] "
            f"baseline band ({m.baseline_rate_7d:.3f}/h)"
        )
    return f"{transition}: signal_count={m.signal_count}"


async def maintain_event_lifecycle(
    conn: Any,
    *,
    now: datetime | None,
    analyst_ctx: Any,
    newly_linked: Mapping[UUID, Iterable[UUID]] | None = None,
    new_context: Mapping[UUID, bool] | None = None,
    max_events: int = 5000,
    budget: Any = None,
) -> dict[str, int]:
    """Evaluate every open event (plus resolved rows with fresh links).

    ``newly_linked`` maps event id to the signal ids attached by this run;
    ``new_context[event_id]`` is True when the attachment introduced a new
    actor or ISO2. P1b: a resolved event also enters the scan when the SQL
    sees a ``signal_event_links`` row written since the event's last ledger
    row — that is what makes a budget-truncated pass safe: a re-link the
    in-memory map lost is discovered by the next tick instead of leaving the
    event resolved forever. ``budget`` is the optional PassBudget checked
    between events; rows it defers are counted and re-evaluated next tick.
    """
    at = _utc(now) or datetime.now(timezone.utc)
    fresh_map = {
        _uuid(k): {x for x in (_uuid(v) for v in vals) if x is not None}
        for k, vals in (newly_linked or {}).items()
        if _uuid(k) is not None
    }
    touched = sorted(fresh_map)
    rows = await conn.fetch(_OPEN_EVENT_STATS_SQL, at, touched, max_events)
    transitions = Counter()
    orphans = 0
    deferred = 0
    for row in rows:
        if budget is not None and budget.exhausted():
            deferred += 1
            continue
        event_id = _uuid(row["id"])
        if event_id is None:
            continue
        latest = _utc(row["latest_linked_at"])
        signal_ids = [
            x for x in (_uuid(v) for v in (row["signal_ids"] or ()))
            if x is not None
        ]
        if latest is None or not signal_ids:
            # An orphan cannot honestly be measured. The P1 writer refuses
            # evidence-free promotion, so an orphan is a counted invariant
            # breach, not a lifecycle row to invent.
            orphans += 1
            continue
        new_ids = fresh_map.get(event_id, set())
        silence_hours = max(0.0, (at - latest).total_seconds() / 3600.0)
        measurement = el.EventMeasurement(
            signal_count=int(row["signal_count"] or 0),
            confidence=float(row["confidence"] or 0.0),
            link_rate_24h=float(row["link_rate_24h"] or 0.0),
            baseline_rate_7d=float(row["baseline_rate_7d"] or 0.0),
            new_actor_or_geo=bool((new_context or {}).get(event_id))
            or bool(row["entity_link_since_ledger"]),
            new_link_arrived=bool(new_ids) or bool(row["link_since_ledger"]),
            silence_hours=silence_hours,
        )
        transition = el.evaluate_transition(str(row["lifecycle_state"]), measurement)
        if transition is None:
            continue
        state_to = el.next_state(str(row["lifecycle_state"]), transition)
        if transition == el.TRANSITION_RESOLVED:
            occurred_at = latest + _SILENCE_HORIZON_BY_STATE[str(row["lifecycle_state"])]
            evidence: list[UUID] = []
        else:
            occurred_at = latest
            evidence = _evidence_for(row, new_ids)
        ledger = el.LifecycleEvent(
            event_id=event_id,
            occurred_at=occurred_at,
            transition=transition,
            why=_transition_why(transition, measurement, len(new_ids)),
            state_from=str(row["lifecycle_state"]),
            state_to=state_to,
            derived_from=tuple(evidence),
        )
        await conn.execute(
            _EVENT_TRANSITION_SQL,
            ledger.event_id,
            ledger.occurred_at,
            ledger.transition,
            ledger.state_from,
            ledger.state_to,
            ledger.why,
            list(ledger.derived_from),
            analyst_ctx.analyst_id,
            analyst_ctx.analyst_version,
            analyst_ctx.run_id,
        )
        await conn.execute(
            _EVENT_STATE_SQL,
            event_id,
            ledger.state_to,
            ledger.occurred_at,
            measurement.signal_count,
            int(row["distinct_source_count"] or 0),
            measurement.confidence,
        )
        transitions[transition] += 1
    return {
        "transitions": dict(transitions),
        "orphans": orphans,
        "deferred": deferred,
    }


__all__ = ["maintain_event_lifecycle"]
