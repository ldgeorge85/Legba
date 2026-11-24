# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the ingestion intake is doing — counts for the read route and the
source-health surface. A LEAF (stdlib only; it takes a connection), so the
slim registry image can import it.

* ``queue`` — pending requests (queued, claimed) now, plus terminal states
  (done, refused, failed, expired) of requests created in the window;
* ``by_reason`` — the same, per reason;
* ``refusals`` — refused requests in the window, per refusal reason;
* ``satisfied`` — requests served from a stored copy (no fetch), per kind;
* ``hosts`` — the busiest hosts in the window: requests, pending, claims in
  the last hour, last claim;
* ``oldest_queued_age_s`` — how long the oldest queued request has waited
  (``None`` when nothing is queued).

Absent migration 0249 (a registry rolled ahead of the runtime) every read
degrades to ``None`` — an honest "not there", never zeros.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

__all__ = ["MAX_WINDOW_HOURS", "intake_counts"]

MAX_WINDOW_HOURS = 168
_HOST_ROWS = 20

_STATE_SQL = """
SELECT state, reason, refusal_reason, satisfied_from, count(*) AS n
  FROM ingestion_requests WHERE state IN ('queued', 'claimed')
 GROUP BY 1, 2, 3, 4
UNION ALL
SELECT state, reason, refusal_reason, satisfied_from, count(*) AS n
  FROM ingestion_requests
 WHERE created_at >= now() - make_interval(hours => $1)
   AND state NOT IN ('queued', 'claimed')
 GROUP BY 1, 2, 3, 4
"""

_HOST_SQL = """
SELECT host,
       count(*) AS requests,
       count(*) FILTER (WHERE state IN ('queued', 'claimed')) AS pending,
       count(*) FILTER (WHERE claimed_at >= now() - interval '1 hour') AS claims_last_hour,
       max(claimed_at) AS last_claimed_at
  FROM ingestion_requests
 WHERE host IS NOT NULL AND created_at >= now() - make_interval(hours => $1)
 GROUP BY host
 ORDER BY requests DESC, host
 LIMIT $2
"""

_OLDEST_SQL = "SELECT min(created_at) FROM ingestion_requests WHERE state = 'queued'"


def _missing(exc: Exception) -> bool:
    return type(exc).__name__ in {"UndefinedTableError", "UndefinedColumnError"}


async def intake_counts(conn: Any, *, window_hours: int = 24) -> dict[str, Any] | None:
    """The intake's counts, or ``None`` when its table is not there."""
    window = max(1, min(int(window_hours), MAX_WINDOW_HOURS))
    try:
        rows = await conn.fetch(_STATE_SQL, window)
        hosts = await conn.fetch(_HOST_SQL, window, _HOST_ROWS)
        oldest = await conn.fetchval(_OLDEST_SQL)
    except Exception as exc:
        if _missing(exc):
            return None
        raise
    queue: dict[str, int] = {}
    by_reason: dict[str, dict[str, int]] = {}
    refusals: dict[str, int] = {}
    satisfied: dict[str, int] = {}
    for r in rows:
        n = int(r["n"])
        queue[r["state"]] = queue.get(r["state"], 0) + n
        per = by_reason.setdefault(r["reason"], {})
        per[r["state"]] = per.get(r["state"], 0) + n
        if r["refusal_reason"]:
            refusals[r["refusal_reason"]] = refusals.get(r["refusal_reason"], 0) + n
        if r["satisfied_from"]:
            satisfied[r["satisfied_from"]] = satisfied.get(r["satisfied_from"], 0) + n
    now = datetime.now(tz=timezone.utc)
    return {
        "window_hours": window,
        "queue": queue,
        "by_reason": by_reason,
        "refusals": refusals,
        "satisfied": satisfied,
        "hosts": [
            {
                "host": h["host"], "requests": int(h["requests"]), "pending": int(h["pending"]),
                "claims_last_hour": int(h["claims_last_hour"]),
                "last_claimed_at": h["last_claimed_at"].isoformat() if h["last_claimed_at"] else None,
            }
            for h in hosts
        ],
        "oldest_queued_age_s": (now - oldest).total_seconds() if oldest is not None else None,
    }
