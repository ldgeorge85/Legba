# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alert when an analyst's resume cursor lags real time (N-Z, 2026-11-14).

``event_clustering``'s signal cursor sat ~29 h behind for days and then
stopped, and nothing said so: the analyst ran on cadence (so the cadence
watchdog saw a healthy analyst) and every receipt was a success. A cursor
analyst now writes ``lag_minutes`` onto its watermark state
(``alert_trigger_watermarks.state``) every pass; this liveness-watchdog leg
reads every state that carries it and alerts on entry when the EFFECTIVE lag
— the stored lag plus the minutes since the state was written, so an analyst
that stopped writing keeps ageing — exceeds the threshold, with one recovery
row on exit (transition-edge, like the other watchdog legs).

Any analyst that stores ``lag_minutes`` on its watermark joins this check;
``event_clustering`` (``signal_slice``) is the first. When the single alert
path lands (``raise_alert`` with its closed list of kinds), the kind to add is
``analyst_cursor_lag`` (recovery: ``analyst_cursor_lag_recovered``).

``LEGBA_ANALYST_CURSOR_LAG_ALERT_MINUTES`` sets the threshold (default 180,
``0`` disables).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

ALERT_CHANNEL_CURSOR_LAG = "analyst_cursor_lag"
ENV_THRESHOLD = "LEGBA_ANALYST_CURSOR_LAG_ALERT_MINUTES"
DEFAULT_THRESHOLD_MINUTES = 180.0

_LAG_SQL = """
    SELECT trigger_class, watermark_key, state::text AS state, updated_at
      FROM alert_trigger_watermarks
     WHERE state::jsonb ? 'lag_minutes'
"""


def threshold_minutes() -> float:
    raw = os.getenv(ENV_THRESHOLD, "").strip()
    try:
        return float(raw) if raw else DEFAULT_THRESHOLD_MINUTES
    except ValueError:
        return DEFAULT_THRESHOLD_MINUTES


def evaluate(rows: list[dict[str, Any]], *, now: datetime, threshold: float) -> list[dict[str, Any]]:
    """The cursors over the threshold, with their effective lag."""
    if threshold <= 0:
        return []
    bad = []
    for row in rows:
        try:
            state = json.loads(row["state"]) if isinstance(row["state"], str) \
                else dict(row["state"] or {})
            lag = float(state.get("lag_minutes"))
        except (TypeError, ValueError):
            continue
        updated = row.get("updated_at")
        age = 0.0
        if isinstance(updated, datetime):
            updated = updated if updated.tzinfo else updated.replace(tzinfo=timezone.utc)
            age = max(0.0, (now - updated).total_seconds() / 60.0)
        effective = lag + age
        if effective > threshold:
            bad.append({
                "entity_id": f"{row['trigger_class']}:{row['watermark_key']}",
                "lag_minutes": round(effective, 1),
                "stored_lag_minutes": round(lag, 1),
                "backfill_open": "backfill" in state,
                "threshold_minutes": threshold,
            })
    return bad


def transition_kwargs(entry: dict[str, Any]) -> dict[str, Any]:
    hours = entry["lag_minutes"] / 60.0
    title = f"Analyst cursor lag: {entry['entity_id']} is {hours:.1f}h behind"
    body = (
        f"The resume cursor '{entry['entity_id']}' is {hours:.1f} h behind real "
        f"time (threshold {entry['threshold_minutes'] / 60.0:.1f} h). Rows past "
        "the cursor are not yet in the product — for event_clustering, signals "
        "with no event row. The analyst may still be running on cadence; "
        "check its receipts' slice block (lag, pages, backfill) and phase "
        "seconds. The alert clears when the lag falls under the threshold."
    )
    return {
        "channel": ALERT_CHANNEL_CURSOR_LAG, "entity_id": entry["entity_id"],
        "state": "entered", "severity": "high", "kind": "analyst_cursor_lag",
        "title": title, "body": body,
        "extra": {k: entry[k] for k in
                  ("lag_minutes", "stored_lag_minutes", "backfill_open",
                   "threshold_minutes")},
    }


async def check_once(wd: Any, now_monotonic: float) -> list[str]:
    """The watchdog leg (leader-gated, boot-graced, transition-edge)."""
    if wd._pg is None:
        return []
    if wd._is_leader is not None and not wd._is_leader():
        return []
    if (now_monotonic - wd._started_at) < wd._cfg.stall_after_s:
        return []
    async with wd._pg.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(_LAG_SQL)]
    bad = evaluate(rows, now=datetime.now(timezone.utc), threshold=threshold_minutes())
    states = await wd._get_alert_states(ALERT_CHANNEL_CURSOR_LAG)
    alerted: list[str] = []
    for entry in bad:
        if states.get(entry["entity_id"]) == "entered":
            continue
        logger.error("liveness_watchdog.cursor_lag entity=%s lag_minutes=%.0f",
                     entry["entity_id"], entry["lag_minutes"])
        await wd._record_transition(**transition_kwargs(entry))
        alerted.append(entry["entity_id"])
    await wd._emit_recoveries(
        channel=ALERT_CHANNEL_CURSOR_LAG, kind="analyst_cursor_lag_recovered",
        noun="Analyst cursor", still_bad={e["entity_id"] for e in bad},
    )
    return alerted


async def check_cursor_lag_once(wd: Any, now_monotonic: float) -> list[str]:
    """:func:`check_once` for the watchdog loop: never raises."""
    try:
        return await check_once(wd, now_monotonic)
    except Exception as exc:  # noqa: BLE001 — never kill the watchdog loop
        logger.warning("liveness_watchdog.cursor_lag_check_error err=%s", exc)
        return []


__all__ = [
    "ALERT_CHANNEL_CURSOR_LAG",
    "check_cursor_lag_once",
    "ENV_THRESHOLD",
    "check_once",
    "evaluate",
    "threshold_minutes",
    "transition_kwargs",
]
