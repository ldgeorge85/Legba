# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The liveness watchdog's view of SUBSCRIBED sources.

A subscribed source writes the poll ledger's row once per window
(``runtime/stream_ingest.py``), so the watchdog's existing per-source checks
already read it: an ``empty`` window of a quiet connection carries
``newest_entry_ts`` and lands in the honest-quiet class, a ``reconnecting`` /
``stalled`` window is an ``error`` row. What a poll never needs is the check
this module adds: a held connection can be DOWN for several windows in a row
(the upstream refuses, keeps dropping, or stalls), or produce NO window at
all (the supervisor is off, crashed, or no replica holds the lease) — and a
source with no cron is invisible to the cadence check.

Classes (:func:`classify_stream_liveness`, pure):

* ``connected`` — events arriving; ``quiet`` — connected, keep-alives only
  (honest quiet: nothing to fix);
* ``starting`` — active, no window yet, still inside the grace;
  ``transient`` — down for fewer than :data:`STREAM_DOWN_WINDOWS` windows;
  ``stopped`` — the last session ended cleanly (a restart is under way);
* ALERTING: ``silent`` (no window row for :data:`SILENT_WINDOWS` windows),
  ``refused`` (the connect is refused: a 4xx, a blocked host, a missing
  credential, a restrictive licence), ``stalled`` / ``reconnecting`` (down
  for :data:`STREAM_DOWN_WINDOWS` consecutive windows).

Transition-edge alerting on the watchdog's durable path, channel (= alert
kind) ``source_stream_down``: one alert on entry, one on recovery. Takes the
watchdog duck-typed (``wd``), like the sibling ``*_watch`` modules.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "ALERTING_CLASSES",
    "ALERT_CHANNEL_STREAM_DOWN",
    "SILENT_WINDOWS",
    "STREAM_DOWN_WINDOWS",
    "check_stream_liveness_once",
    "classify_stream_liveness",
]

ALERT_CHANNEL_STREAM_DOWN = "source_stream_down"
#: Consecutive down windows (reconnecting / stalled) before the alert.
STREAM_DOWN_WINDOWS = 3
#: No window row for this many windows = the connection is not held at all.
SILENT_WINDOWS = 3
ALERTING_CLASSES = frozenset({"silent", "refused", "stalled", "reconnecting"})
MIN_INTERVAL_S = 60.0

_DOWN = frozenset({"reconnecting", "stalled", "connecting", "refused"})
_RECENT = STREAM_DOWN_WINDOWS + 2

_ROWS_SQL = f"""
SELECT d.descriptor_id AS source_id,
       GREATEST(d.created_at, COALESCE((SELECT max(a.occurred_at) FROM descriptor_audit_log a
                                          WHERE a.descriptor_id = d.descriptor_id), d.created_at))
                                                                          AS head_since,
       COALESCE((d.body #>> '{{subscribe,outcome_window_seconds}}')::int, 300) AS window_s,
       po.occurred_at, po.stream ->> 'state' AS state, po.error
  FROM source_descriptors d
  LEFT JOIN LATERAL (
       SELECT occurred_at, stream, error FROM source_poll_outcomes
        WHERE source_id = d.descriptor_id AND stream IS NOT NULL
        ORDER BY occurred_at DESC LIMIT {_RECENT}
  ) po ON TRUE
 WHERE d.is_head AND d.state = 'active' AND d.body ->> 'acquisition' = 'subscribe'
 ORDER BY d.descriptor_id, po.occurred_at DESC
"""


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def classify_stream_liveness(
    *, now: datetime, head_since: datetime, window_s: float, recent: list[dict[str, Any]],
) -> str:
    """The liveness class of one active subscribed source. ``recent`` is its
    newest window rows, newest first, each ``{occurred_at, state}``."""
    silent_after = SILENT_WINDOWS * float(window_s)
    rows = [r for r in recent if r.get("occurred_at") is not None]
    if not rows:
        return "silent" if (now - _aware(head_since)).total_seconds() > silent_after else "starting"
    if (now - _aware(rows[0]["occurred_at"])).total_seconds() > silent_after:
        return "silent"
    state = rows[0].get("state")
    if state in ("connected", "quiet", "stopped"):
        return str(state)
    if state == "refused":
        return "refused"
    run = 0
    for r in rows:
        if r.get("state") not in _DOWN:
            break
        run += 1
    if run >= STREAM_DOWN_WINDOWS:
        return "stalled" if state == "stalled" else "reconnecting"
    return "transient"


async def _rows(wd: Any) -> dict[str, dict[str, Any]]:
    async with wd._pg.acquire() as conn:
        raw = await conn.fetch(_ROWS_SQL)
    out: dict[str, dict[str, Any]] = {}
    for r in raw:
        row = dict(r)
        entry = out.setdefault(row["source_id"], {
            "head_since": row["head_since"], "window_s": row["window_s"], "recent": []})
        if row.get("occurred_at") is not None:
            entry["recent"].append({"occurred_at": row["occurred_at"], "state": row["state"],
                                    "error": row.get("error")})
    return out


async def check_stream_liveness_once(wd: Any, now_monotonic: float, *,
                                     min_interval_s: float = 0.0) -> list[str]:
    """One pass. Returns ``["<source_id>:<class>", ...]`` for entry edges.
    Never raises (a sibling of the watchdog's other ``*_watch`` legs)."""
    try:
        if wd._pg is None:
            return []
        if wd._is_leader is not None and not wd._is_leader():
            return []
        if (now_monotonic - wd._started_at) < wd._cfg.stall_after_s:
            return []
        last = getattr(wd, "_stream_checked_at", None)
        if last is not None and min_interval_s and (now_monotonic - last) < min_interval_s:
            return []
        wd._stream_checked_at = now_monotonic
        now = datetime.now(tz=timezone.utc)
        by_source = await _rows(wd)
        states = await wd._get_alert_states(ALERT_CHANNEL_STREAM_DOWN)
        alerted: list[str] = []
        bad: set[str] = set()
        for source_id, info in sorted(by_source.items()):
            cls = classify_stream_liveness(now=now, head_since=info["head_since"],
                                           window_s=info["window_s"], recent=info["recent"])
            if cls not in ALERTING_CLASSES:
                continue
            bad.add(source_id)
            if states.get(source_id) == "entered":
                continue
            latest = info["recent"][0] if info["recent"] else {}
            title = f"Subscribed source down: {source_id} ({cls})"
            body = (
                f"Subscribed source '{source_id}' is ACTIVE but its held connection is "
                f"'{cls}'. "
                + ("No window row has been written for "
                   f"{SILENT_WINDOWS} windows: no runtime holds the stream (is "
                   "LEGBA_SUBSCRIBE_SUPERVISOR on, and is a replica holding the lease?)."
                   if cls == "silent" else
                   f"Last window: {latest.get('error') or 'no detail'}.")
            )
            logger.error("liveness_watchdog.stream_down source=%s class=%s", source_id, cls)
            await wd._record_transition(
                channel=ALERT_CHANNEL_STREAM_DOWN, entity_id=source_id, state="entered",
                severity="high", kind=ALERT_CHANNEL_STREAM_DOWN, title=title, body=body,
                extra={"stream_class": cls},
            )
            alerted.append(f"{source_id}:{cls}")
        await wd._emit_recoveries(channel=ALERT_CHANNEL_STREAM_DOWN, kind="source_stream_down_recovered",
                                  noun="Subscribed source", still_bad=bad)
        return alerted
    except Exception as exc:  # never kill the watchdog loop
        logger.warning("liveness_watchdog.stream_check_error err=%s", exc)
        return []
