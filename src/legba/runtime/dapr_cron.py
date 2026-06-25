# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cron-expression → Dapr-Reminder-timing adapter (per legba_runtime_spec.md §4.3).

Dapr Reminders take a ``due_time`` (datetime.timedelta until first fire)
and a ``period`` (datetime.timedelta between fires). They do NOT accept
cron strings — Dapr uses Go's ``time.Duration`` shape.

For each cron expression we use ``croniter`` to compute:

  * ``due_time`` — interval from now until the next cron fire. If the
    next fire is already past (clock skew, etc.), 0s.
  * ``period``   — interval between the next two cron fires.

Constant-period schedules (``*/5 * * * *``, hourly, daily) map cleanly to
a single Reminder period. Variable-period schedules (``0 9 * * 1-5`` —
weekdays only; monthly; yearly) are handled by **Dapr Jobs** (cron-native)
in the source-first runtime, not here — see ``docs/PIVOT_PROPOSAL.md`` §5.4.
(The prior Temporal ``CronTickerWorkflow`` path was retired in the pivot —
P-03.)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from croniter import croniter


def cron_to_reminder_timing(
    expression: str,
    *,
    base_time: datetime | None = None,
    fallback_period: timedelta = timedelta(minutes=15),
) -> tuple[timedelta, timedelta]:
    """Convert a cron expression to (due_time, period) for Dapr Reminders.

    Correct for constant-period crons. For variable-period schedules the
    runtime routes to Dapr Jobs (cron-native) instead — this function
    tolerates such input (uses the first computed gap) but the resulting
    Reminder would fire at that fixed gap forever, which is wrong for
    weekday-only / monthly / yearly schedules.

    Args:
        expression: Standard 5-field cron (``min hour dom mon dow``).
        base_time: Reference time for "now". Defaults to UTC now.
        fallback_period: Period to use when the cron has only one
            computable next fire (very rare; defensive).

    Returns:
        ``(due_time, period)`` as ``timedelta`` pair suitable for
        ``Actor.register_reminder(due_time=..., period=...)``.

    Raises:
        ValueError: cron expression invalid.
    """
    if not croniter.is_valid(expression):
        raise ValueError(f"invalid cron expression: {expression!r}")
    now = base_time or datetime.now(tz=timezone.utc)
    it = croniter(expression, start_time=now)
    first = it.get_next(datetime)
    second = it.get_next(datetime)
    # croniter returns naive datetime when start_time is naive — we pass
    # tz-aware, so this is safe.
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    if second.tzinfo is None:
        second = second.replace(tzinfo=timezone.utc)
    due = first - now
    if due < timedelta(0):
        due = timedelta(0)
    period = second - first
    if period <= timedelta(0):
        period = fallback_period
    return due, period


__all__ = ["cron_to_reminder_timing"]
