# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""When is a cadence-bearing analyst (or poll source) STALE? — the pure decision.

Extracted from ``liveness_watchdog`` (roll 65), which calls it for both the
per-analyst and the per-source cadence checks.

THE EXPECTED GAP COMES FROM THE SCHEDULE, NOT A FLAT INTERVAL (roll 65). The
check used to take the gap between the next two fires after NOW as "the
interval" and alert past ``factor`` x that. For a WINDOWED cron that is wrong
outside the window: ``correctness_grader`` runs ``20 1-9 * * *`` (hourly,
01:20-09:20 UTC), so at 11:21Z its next two fires were 01:20 and 02:20, the
"interval" was 1 h, and a grader that last ran at 09:21 — exactly on schedule —
was called stalled at 2 h (the 2026-11-08 11:21Z alert).

Now the threshold is measured from the LAST RUN along the schedule itself:

    period   = gap between the first two scheduled fires after the last run
    deadline = max(last_run + factor x period,
                   next_fire_after_last_run + (factor - 1) x period)

On a uniform cron the first term always wins (the next fire is at most one
period after the last run), so every constant-period analyst and source keeps
exactly the threshold it had. On a windowed cron the second term carries the
deadline across the quiet hours: the grader's 09:21 run is next due at 01:20,
so it is stale only once 02:20 passes without a run — the same "missed two
fires" a continuous hourly analyst is held to, and never outside its window.

A RUN IN FLIGHT IS NOT A STALL (P-7, 2026-11-14). A run's ``analyst_traces``
row lands when the run ENDS, stamped with its START. An hourly analyst that
misses exactly one fire (a roll killed it) is due again at the next fire —
and at that instant ``age == 2 x period`` exactly, so for as long as the
next run takes (``graph_projector``: ~9 min at 11M arcs) the check saw a
2.0 h-old success and paged, then "recovered" when the trace landed (the
2026-11-13 04:07Z and 2026-11-14 09:07Z episodes). A row may therefore carry
``recent_run_s`` — the analyst's own longest successful run over the last
day — and the deadline moves out by that much, capped at one period so a
genuinely hung analyst still alerts within one extra period. Rows without
it keep exactly the threshold they had.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from croniter import croniter


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def schedule_threshold_s(
    cron: str, last_run: datetime, factor: float, run_grace_s: float = 0.0
) -> float | None:
    """Seconds after ``last_run`` at which the entity is stale by its own
    schedule (see the module docstring); ``None`` for an invalid cron or a
    schedule with no positive period. ``run_grace_s`` (the analyst's recent
    run duration) extends the deadline by at most one period."""
    if not croniter.is_valid(cron):
        return None
    last_run = _aware(last_run)
    it = croniter(cron, start_time=last_run)
    nxt = _aware(it.get_next(datetime))
    after = _aware(it.get_next(datetime))
    period = after - nxt
    if period <= timedelta(0):
        return None
    deadline = max(last_run + factor * period, nxt + (factor - 1.0) * period)
    if run_grace_s and run_grace_s > 0:
        deadline += min(timedelta(seconds=float(run_grace_s)), period)
    return (deadline - last_run).total_seconds()


def evaluate_cadence_staleness(
    rows: Any,
    *,
    now: datetime,
    factor: float,
    min_threshold_s: float,
    id_key: str = "analyst_id",
    ts_key: str = "last_run",
    run_key: str = "recent_run_s",
) -> list[tuple[str, float, float]]:
    """Pure staleness decision over cadence rows — no DB, unit-testable.

    ``rows``: iterable of mappings with an id (``id_key``), ``cron`` (5-field
    cron string), and a last-activity timestamp (``ts_key``, tz-aware datetime
    or None). Returns ``(id, age_s, threshold_s)`` for each entity whose age
    exceeds ``max(schedule_threshold_s(...), min_threshold_s)``. Rows with no
    prior activity (timestamp None) are skipped — a never-run analyst /
    never-produced source is a registration/activation failure (covered by the
    boot log), not a cadence stall, and skipping it avoids false-alarming fresh
    registrations. A row's optional ``run_key`` value (seconds) is the
    in-flight grace (module docstring). Generic over ``id_key``/``ts_key`` so
    it serves both per-analyst (run) and per-source (signal) liveness.
    """
    stale: list[tuple[str, float, float]] = []
    for r in rows:
        entity_id = r.get(id_key)
        cron = (r.get("cron") or "").strip()
        last_run = r.get(ts_key)
        if not entity_id or not cron or last_run is None:
            continue
        try:
            run_s = float(r.get(run_key) or 0.0)
        except (TypeError, ValueError):
            run_s = 0.0
        try:
            expected_s = schedule_threshold_s(cron, last_run, factor, run_s)
        except (ValueError, KeyError):  # an unparsable cron never crashes the sweep
            continue
        if expected_s is None:
            continue
        threshold_s = max(expected_s, min_threshold_s)
        age_s = (now - _aware(last_run)).total_seconds()
        if age_s > threshold_s:
            stale.append((entity_id, age_s, threshold_s))
    return stale


__all__ = ["evaluate_cadence_staleness", "schedule_threshold_s"]
