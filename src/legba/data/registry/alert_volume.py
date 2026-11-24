# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ONE alert-volume rule (X4 ALERTS, W11-F17): how many pushes went out,
per topic and per kind — read by the volume route
(``GET /v3/system/alerts/volume``) and by the watchdog's ``alert_volume_high``
page (``runtime/alert_volume_watch.py``), so the number on the page and the
number that paged cannot disagree.

WHAT A PUSH IS. One ``alert_sink_deliveries`` row of an OUTWARD sink
(:data:`OUTWARD_SINK_KINDS`) with ``status='delivered'``. The internal rows are
NOT pushes and are never counted: the escalate pack's ``alert`` row records the
internal bus publish on ``channels.escalations`` (no phone hears it), and the
watchdog's ``liveness_watchdog`` rows record a transition the outward row then
delivers. An escalation is therefore ONE push with TWO rows (verified live on
10-14: 28 messages in the ntfy topic cache over a 10.7 h window, 28 ntfy
``delivered`` rows in the same window, 0 extra).

THE COLUMNS. Per (topic, kind): ``pushed`` (delivered), ``coalesced`` (the
per-sink cooldown folded it onto a later push), ``level_only`` (an escalation
whose severity held — never pushed), ``failed``, ``unconfigured``; and
``below_floor`` (under the sink's severity floor — never pushed),
``duplicate`` (the same alert row already had its series); and
``alerts_carried`` — alert rows the pushes carried (a batched push of nine band
moves is one push carrying nine).

TOPIC. ``payload_summary.topic`` (written since X4). Rows written before the
split carry none and report as ``pre_split``: every one of them went to the
single pre-split topic, and no topic is invented for them. KIND is
``payload_summary.kind``; a pre-split ``trigger_scan`` row takes its alert
row's trigger class, anything else its channel name.

Stdlib + ``legba.data.alerts.topics`` (stdlib-only) — registry-slim.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

from ..alerts.topics import (
    TOPIC_ENGINE, TOPIC_NIGHTLY, TOPIC_PRE_SPLIT, TOPIC_WORLD, TOPICS,
)

#: Sinks whose ``delivered`` row is a push someone receives.
OUTWARD_SINK_KINDS: tuple[str, ...] = ("ntfy", "webhook")

#: THE TUNABLES (docs/TUNABLES.md §5): pushes per topic in any trailing 24 h
#: above which the watchdog pages ``alert_volume_high`` (one page per topic per
#: transition, a recovery when it falls back to the cap or under).
CAP_ENV_BY_TOPIC: Mapping[str, str] = {
    TOPIC_WORLD: "LEGBA_ALERT_VOLUME_DAILY_CAP_WORLD",
    TOPIC_ENGINE: "LEGBA_ALERT_VOLUME_DAILY_CAP_ENGINE",
    # L-1: the nightly suite's pushes now ride the ledger, so they are counted.
    TOPIC_NIGHTLY: "LEGBA_ALERT_VOLUME_DAILY_CAP_NIGHTLY",
}
DEFAULT_DAILY_CAP = 50
CAP_WINDOW_HOURS = 24

#: status → column.
_STATUS_COLUMN: Mapping[str, str] = {
    "delivered": "pushed",
    "skipped_cooldown": "coalesced",
    "skipped_level_only": "level_only",
    "failed": "failed",
    "skipped_unconfigured": "unconfigured",
    # N-M (11-13): the two decisions that used to write no row.
    "skipped_below_severity": "below_floor",
    "skipped_duplicate": "duplicate",
}
COLUMNS: tuple[str, ...] = (
    "pushed", "coalesced", "level_only", "failed", "unconfigured",
    "below_floor", "duplicate", "other",
)

#: One row per (UTC day, topic, kind, status). ``idx_asd_attempted_at`` bounds
#: the window; the LEFT JOIN reads a pre-split trigger_scan row's alert row by
#: its primary key (live EXPLAIN in the X4 ALERTS report).
VOLUME_WINDOW_SQL = """
    SELECT to_char(d.attempted_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day,
           d.payload_summary ->> 'topic' AS topic,
           COALESCE(d.payload_summary ->> 'kind',
                    o.data -> 'data' ->> 'trigger_class',
                    d.channel_name, 'unknown') AS kind,
           d.status,
           count(*)::int AS n,
           count(*) FILTER (WHERE d.payload_summary ? 'batch_count')::int AS batches,
           COALESCE(sum((d.payload_summary ->> 'batch_count')::int), 0)::int
               AS batched_alerts,
           max(d.attempted_at) AS newest
      FROM alert_sink_deliveries d
      LEFT JOIN analyst_outputs o
             ON o.id = d.alert_row_id
            AND d.payload_summary ->> 'kind' IS NULL
            AND d.channel_name = 'trigger_scan'
     WHERE d.attempted_at >= $1
       AND d.sink_kind = ANY($2::text[])
     GROUP BY 1, 2, 3, 4
"""


def daily_cap(topic: str) -> int:
    env = CAP_ENV_BY_TOPIC.get(topic)
    raw = (os.environ.get(env) or "").strip() if env else ""
    try:
        value = int(raw) if raw else DEFAULT_DAILY_CAP
    except ValueError:
        return DEFAULT_DAILY_CAP
    return value if value > 0 else DEFAULT_DAILY_CAP


def caps() -> dict[str, int]:
    return {t: daily_cap(t) for t in TOPICS}


def _blank() -> dict[str, int]:
    out = {c: 0 for c in COLUMNS}
    out["alerts_carried"] = 0
    return out


def _add(acc: dict[str, int], row: Mapping[str, Any]) -> None:
    column = _STATUS_COLUMN.get(str(row.get("status") or ""), "other")
    n = int(row.get("n") or 0)
    acc[column] += n
    if column == "pushed":
        acc["alerts_carried"] += (
            n - int(row.get("batches") or 0) + int(row.get("batched_alerts") or 0)
        )


def summarize(
    rows: Iterable[Mapping[str, Any]], *, window_days: Optional[float] = None,
) -> dict[str, Any]:
    """Fold the window rows into per-topic, per-(topic, kind) and per-day tallies.

    ``per_day`` (pushes ÷ window days) is only computed when the caller states
    the window; it is a mean over the WHOLE window, zero-push days included.
    """
    topics: dict[str, dict[str, int]] = {}
    kinds: dict[tuple[str, str], dict[str, int]] = {}
    days: dict[tuple[str, str, str], dict[str, int]] = {}
    newest: Optional[datetime] = None
    for r in rows:
        topic = str(r.get("topic") or TOPIC_PRE_SPLIT)
        kind = str(r.get("kind") or "unknown")
        day = str(r.get("day") or "")
        _add(topics.setdefault(topic, _blank()), r)
        _add(kinds.setdefault((topic, kind), _blank()), r)
        _add(days.setdefault((day, topic, kind), _blank()), r)
        ts = r.get("newest")
        if isinstance(ts, datetime) and (newest is None or ts > newest):
            newest = ts
    topic_rows = []
    for t in sorted(topics):
        row = {"topic": t, **topics[t]}
        if window_days:
            row["per_day"] = round(topics[t]["pushed"] / float(window_days), 1)
        topic_rows.append(row)
    return {
        "as_of": newest.isoformat() if newest else None,
        "topics": topic_rows,
        "kinds": [
            {"topic": t, "kind": k, **v}
            for (t, k), v in sorted(kinds.items(), key=lambda kv: (kv[0][0], -kv[1]["pushed"], kv[0][1]))
        ],
        "days": [
            {"day": d, "topic": t, "kind": k, **v}
            for (d, t, k), v in sorted(days.items())
        ],
    }


def pushed_by_topic(summary: Mapping[str, Any]) -> dict[str, int]:
    return {str(t["topic"]): int(t["pushed"]) for t in summary.get("topics") or []}


__all__ = [
    "CAP_ENV_BY_TOPIC",
    "CAP_WINDOW_HOURS",
    "COLUMNS",
    "DEFAULT_DAILY_CAP",
    "OUTWARD_SINK_KINDS",
    "VOLUME_WINDOW_SQL",
    "caps",
    "daily_cap",
    "pushed_by_topic",
    "summarize",
]
