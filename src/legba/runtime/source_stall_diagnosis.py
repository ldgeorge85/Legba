# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The WHY sentence of a source-cadence stall alert.

Split out of :mod:`.liveness_watchdog` on 2026-10-03 (W3 SRC-2) to make room
under that module's size ceiling for the staleness tripwire's wiring; the
watchdog re-imports it under the same name, so ``from
legba.runtime.liveness_watchdog import _source_stall_diagnosis`` still binds.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _source_stall_diagnosis(row: dict[str, Any]) -> str:
    """Turn the source's newest poll-outcome row (DQ-H5b #88) into a WHY
    sentence for the cadence alert body.

    ``row`` carries ``last_poll_outcome``
    ('success'|'gated'|'empty'|'error'|None), ``last_poll_health``
    ('healthy'|'degraded'|'unhealthy'|None), ``last_poll_signals_written``,
    ``last_poll_error`` and ``last_poll_at``.

    A missing/NULL outcome means NO poll was ever recorded — i.e. the poll
    reminder itself is likely not firing (the actor never ran). Since migration
    0114 that reading is finally SOUND: every poll writes a row, so an absence
    is an absence. Before it, only non-productive polls were logged, so a
    healthily-firing source that had simply never failed hit this same branch
    and was diagnosed "the poll reminder may be dead" — precisely backwards.
    """
    outcome = row.get("last_poll_outcome")
    when = row.get("last_poll_at")
    when_s = ""
    if isinstance(when, datetime):
        when_s = f" at {when.astimezone(timezone.utc).isoformat(timespec='seconds')}"
    if outcome == "success":
        # The last recorded poll PRODUCED. Reaching this diagnosis means the
        # cadence check nonetheless found the source stale, so the poll stopped
        # firing after that success — the fetch path is not the suspect.
        n = row.get("last_poll_signals_written")
        n_s = f" ({n} signals)" if isinstance(n, int) and n > 0 else ""
        return (
            f"Last recorded poll SUCCEEDED{n_s}{when_s} and nothing has been "
            "recorded since — the feed itself was working, so suspect the poll "
            "reminder / actor cadence rather than the fetch path."
        )
    if outcome == "error":
        health = row.get("last_poll_health") or "unknown"
        err = row.get("last_poll_error")
        err_s = f" — {err}" if err else ""
        return (
            f"Last poll FAILED ({health}){when_s}{err_s}. The fetch/parse path "
            "errored (4xx / parse-fail / timeout); fix the source, not the cron."
        )
    if outcome == "gated":
        # Migration 0226: the feed DELIVERED and the keyword gate dropped every
        # entry. Productive for the fetch path (it works); not a new item.
        n = row.get("last_poll_keyword_filtered")
        n_s = f" ({n} entries)" if isinstance(n, int) and n > 0 else ""
        return (
            f"Last poll fetched cleanly and the feed DELIVERED, but the "
            f"keyword gate dropped every entry as off-topic{n_s}{when_s} — "
            "0 signals written. The fetch path works; a narrow include-list "
            "feed is simply quiet on-topic. Check the gate's include/exclude "
            "terms only if on-topic items are expected."
        )
    if outcome == "empty":
        return (
            f"Last poll fetched cleanly but produced 0 signals (healthy "
            f"HTTP-200, no new items){when_s}. The feed may simply be quiet — "
            "verify upstream actually has nothing, or whether the cursor/filter "
            "is over-trimming."
        )
    # No poll-outcome row at all → the poll likely never ran this window.
    return (
        "No empty/error poll-outcome row was recorded — the poll reminder "
        "itself may be dead (the source actor isn't firing). Check the poll "
        "reminder liveness / actor activation before the fetch path."
    )


#: Lane N-U: how long a source whose polls fail with an upstream ``HTTP 5xx``
#: may go without a produced signal before its stall is a DEAD source (paged
#: high) rather than an intermittent upstream error (recorded medium, not
#: paged). Hours; ``LEGBA_SOURCE_5XX_DEAD_AFTER_HOURS`` overrides.
SOURCE_5XX_DEAD_AFTER_ENV = "LEGBA_SOURCE_5XX_DEAD_AFTER_HOURS"
DEFAULT_SOURCE_5XX_DEAD_AFTER_HOURS = 48.0

#: The alert state of a stall recorded as a transient upstream error: an open
#: episode (no re-record while it lasts, a recovery row when it clears) that
#: still escalates to a high ``entered`` page once the source is dead.
TRANSIENT_ENTERED_STATE = "entered_transient"


def is_transient_upstream_5xx(row: dict[str, Any], age_s: float, dead_after_s: float) -> bool:
    """True when the source's newest poll failed with an upstream ``HTTP 5xx``
    and its newest signal is younger than ``dead_after_s`` — an intermittent
    upstream outage, not a dead source."""
    err = row.get("last_poll_error")
    return (
        row.get("last_poll_outcome") == "error"
        and isinstance(err, str) and err.strip().upper().startswith("HTTP 5")
        and age_s < dead_after_s
    )
