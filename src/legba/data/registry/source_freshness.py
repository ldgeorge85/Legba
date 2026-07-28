# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A7 — per-source freshness grading against cadence-derived budgets.

Formalizes the System Status panel's "how fresh is this source?" read into a
CLOSED graded vocabulary computed per source against a budget derived from
that source's OWN declared cadence — never a hardcoded global window:

  * ``ok``       — freshest signal age ≤ ``budget_minutes``.
  * ``stale``    — over budget, but ≤ ``WARN_MULTIPLE`` × budget.
  * ``warn``     — over ``WARN_MULTIPLE`` × budget (badly overdue).
  * ``empty``    — an active, cadence-declared source that has NEVER landed a
    signal row (a first-class honest state, not a worst ``warn``).
  * ``ungraded`` — no honest grade exists: the descriptor declares no parsable
    cadence (nothing to derive a budget from — NEVER faked to ``ok``), or the
    head is not active (a paused/retired source has no live polling
    expectation for its data age to be graded against).

Budget derivation
-----------------
Source descriptors declare their poll cadence as a cron expression at
``body.cadence.schedule.raw`` (see ``descriptors/source_*.yaml``). The budget
is that cadence's expectation with grace:

    ``budget_minutes = max(interval × GRACE_MULTIPLE, MIN_BUDGET_MINUTES)``

where ``interval`` is the MAXIMUM gap between successive cron fires over a
deterministic scan horizon (so ``47 5-23/6 * * *`` reads 360min, not the
naive field step) — computed with :mod:`croniter` from a FIXED epoch, making
the result deterministic and cacheable per expression. ``GRACE_MULTIPLE``
covers jitter + a few legitimately-empty polls (these are news feeds: a poll
that finds nothing new is normal, so the budget grades signal *production*
lag, not poll success). ``MIN_BUDGET_MINUTES`` keeps fast pollers of sparse
event feeds (e.g. a 5-min USGS poll) from flapping ``stale`` minutes after a
quiet spell begins.

Honesty note: an event-driven feed (earthquakes, disasters) can legitimately
ride ``stale``/``warn`` during a quiet period — the grade states "no signal
within the cadence-derived budget", which is TRUE; it is a freshness read,
not a failure verdict (the route's ``status`` field carries the
error/silent/paused read).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

logger = logging.getLogger(__name__)

FreshnessGrade = Literal["ok", "stale", "warn", "empty", "ungraded"]

#: budget = cadence interval × this grace multiple …
GRACE_MULTIPLE = 4.0
#: … but never below this floor (fast pollers of sparse feeds must not flap).
MIN_BUDGET_MINUTES = 30
#: ``warn`` beyond this multiple of the budget.
WARN_MULTIPLE = 3.0

#: Deterministic croniter base (a Monday 00:00 UTC) — budgets depend only on
#: the expression, never on request time, so the per-expression cache is safe.
_CRON_EPOCH = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)
#: Scan horizon for the max-gap walk (covers weekly shapes with margin).
_CRON_SCAN = timedelta(days=15)
#: Hard iteration bound (a "* * * * *" walk over 15 days would be 21600 steps
#: — bounded, but there is no need to walk further than this to see the max
#: gap of any expression whose gaps fit the horizon).
_CRON_MAX_STEPS = 25_000

#: Per-expression memo — the descriptor set is small (tens of sources).
_BUDGET_CACHE: dict[str, Optional[int]] = {}


def cadence_interval_minutes(cron_expr: Optional[str]) -> Optional[float]:
    """The MAXIMUM fire-to-fire gap (minutes) of a cron expression, walked
    deterministically from a fixed epoch until past a 15-day horizon (at least
    one full gap is always measured, so month-scale shapes still resolve), or
    ``None`` when no honest interval exists (missing/blank/invalid expression,
    croniter unavailable, or the walk could not complete)."""
    if not cron_expr or not str(cron_expr).strip():
        return None
    expr = str(cron_expr).strip()
    try:
        from croniter import croniter
    except ImportError:  # pragma: no cover — croniter is a hard dep in-tree
        return None
    try:
        if not croniter.is_valid(expr):
            return None
        it = croniter(expr, start_time=_CRON_EPOCH)
        prev = it.get_next(datetime)
        horizon = _CRON_EPOCH + _CRON_SCAN
        max_gap: Optional[timedelta] = None
        for _ in range(_CRON_MAX_STEPS):
            nxt = it.get_next(datetime)
            gap = nxt - prev
            if max_gap is None or gap > max_gap:
                max_gap = gap
            prev = nxt
            if nxt >= horizon:
                break
        else:
            # Never reached the horizon inside the step bound — the walk is
            # incomplete, so the max gap seen is not trustworthy.
            return None
        if max_gap is None:
            return None
        return max_gap.total_seconds() / 60.0
    except Exception as exc:  # noqa: BLE001 — a junk expression must grade ungraded
        logger.info("source_freshness.cron_unparsable expr=%r err=%s", expr, exc)
        return None


def derive_budget_minutes(cron_expr: Optional[str]) -> Optional[int]:
    """The freshness budget (minutes) for one declared cadence, or ``None``
    when the source is ungradable (no/invalid cadence declaration)."""
    key = str(cron_expr).strip() if cron_expr else ""
    if not key:
        return None
    if key in _BUDGET_CACHE:
        return _BUDGET_CACHE[key]
    interval = cadence_interval_minutes(key)
    budget = (
        None
        if interval is None
        else max(int(round(interval * GRACE_MULTIPLE)), MIN_BUDGET_MINUTES)
    )
    _BUDGET_CACHE[key] = budget
    return budget


def grade_freshness(
    *,
    state: Optional[str],
    age_seconds: Optional[int],
    budget_minutes: Optional[int],
) -> FreshnessGrade:
    """The graded freshness verdict for one source row (see module docstring).

    ``state`` is the head descriptor's declared state (``None`` reads active —
    the same defaulting the route's ``status`` derivation uses);
    ``age_seconds`` is the age of the freshest signal (``None`` = never
    produced one); ``budget_minutes`` comes from :func:`derive_budget_minutes`.
    """
    if state is not None and state != "active":
        return "ungraded"
    if budget_minutes is None or budget_minutes <= 0:
        return "ungraded"
    if age_seconds is None:
        return "empty"
    age_minutes = age_seconds / 60.0
    if age_minutes <= budget_minutes:
        return "ok"
    if age_minutes <= budget_minutes * WARN_MULTIPLE:
        return "stale"
    return "warn"


__all__ = [
    "FreshnessGrade",
    "GRACE_MULTIPLE",
    "MIN_BUDGET_MINUTES",
    "WARN_MULTIPLE",
    "cadence_interval_minutes",
    "derive_budget_minutes",
    "grade_freshness",
]
