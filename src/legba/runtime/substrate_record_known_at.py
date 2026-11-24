# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The record readers' ``known_at`` cut — the MACHINERY (lane E2).

What each record reader does under ``known_at`` is classified in
:mod:`legba.data.raw_reader_known_at` (:data:`RECORD_KNOWN_AT_CUTS`, with the
limits of the cut in that module's docstring); this module is the shared code
those readers call. The record-entry predicate is E0's one rendering,
:func:`legba.runtime.substrate_known_at.recorded_before_sql` (the temporal
leaf's half-open anchor window, upper bound only).

Three things a ROW cut alone does not do, done here:

* CLAMP a reader's own clock to K (``as_of`` / ``believed_as_of``), so a
  planner cannot ask a reader for a later instant than the cut;
* REBUILD at K the event fields that have an append-only ledger: the lifecycle
  state (``event_lifecycle_events``) and the signal / source counts
  (``signal_event_links``);
* MASK every timestamp after K on a returned row (``None``), and count it —
  a row recorded before K may carry a close, an update or an end stamped later.

Read-only: every statement is a SELECT. Nothing here runs without a cut.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping
from uuid import UUID

from .substrate_known_at import cut_stamp, recorded_before_sql

__all__ = [
    "EVENT_TIME_FIELDS",
    "clamp",
    "created_before",
    "lifecycle_at_sql",
    "mask_after",
    "rebuild_events_at",
    "stamp",
]

#: The timestamp fields of an event row (list and dossier) masked after K.
EVENT_TIME_FIELDS = (
    "lifecycle_changed_at", "time_start", "time_end", "valid_from",
    "valid_until", "produced_at", "created_at", "updated_at",
)


def created_before(anchor: str, param: int) -> str:
    """``<anchor> < $<param>`` — the record-entry clause a port reader appends.

    The port renders the cut through this name rather than calling E0's helper
    directly, so the port (which also renders validity predicates) is not a
    cut render site; the inventory guard pins the sites that are.
    """
    return recorded_before_sql(anchor, param)


def clamp(instant: datetime | None, known_at: datetime) -> datetime:
    """``min(instant, K)`` — and K when the reader was given no instant."""
    if instant is None or instant > known_at:
        return known_at
    return instant


def _as_instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def mask_after(row: dict[str, Any], fields: Iterable[str], known_at: datetime) -> int:
    """Set every ``fields`` timestamp later than K to ``None``; the count masked.

    Works on rendered rows (ISO strings) and raw ones (datetimes) alike. A
    naive timestamp is never compared (it cannot be placed against K) and is
    left as it is.
    """
    masked = 0
    for f in fields:
        at = _as_instant(row.get(f))
        if at is not None and at.tzinfo is not None and at >= known_at:
            row[f] = None
            masked += 1
    return masked


def lifecycle_at_sql(alias: str, k_param: int) -> str:
    """The lifecycle state of event ``<alias>`` at K: the newest ledger row
    that both happened and was recorded before K (``NULL`` when none was)."""
    return (
        "(SELECT l.state_to FROM event_lifecycle_events l "
        f"WHERE l.event_id = {alias}.id "
        f"AND {recorded_before_sql('l.created_at', k_param)} "
        f"AND {recorded_before_sql('l.occurred_at', k_param)} "
        "ORDER BY l.occurred_at DESC, l.created_at DESC LIMIT 1)"
    )


_STATE_AT_SQL = (
    "SELECT DISTINCT ON (event_id) event_id, state_to, occurred_at "
    "FROM event_lifecycle_events "
    "WHERE event_id = ANY($1::uuid[]) "
    f"AND {recorded_before_sql('created_at', 2)} "
    f"AND {recorded_before_sql('occurred_at', 2)} "
    "ORDER BY event_id, occurred_at DESC, created_at DESC"
)

_COUNTS_AT_SQL = (
    "SELECT event_id, count(*) AS n, count(DISTINCT source_id) AS d "
    "FROM signal_event_links "
    "WHERE event_id = ANY($1::uuid[]) "
    f"AND {recorded_before_sql('created_at', 2)} "
    "GROUP BY event_id"
)


async def rebuild_events_at(
    conn: Any, events: list[dict[str, Any]], known_at: datetime,
) -> int:
    """Rebuild each rendered event dict at K, in place; the timestamps masked.

    ``lifecycle_state`` / ``lifecycle_changed_at`` come from the newest ledger
    row recorded before K (``None`` when there was none); ``signal_count`` /
    ``distinct_source_count`` are recounted over the links recorded before K;
    ``superseded_by`` (undated) is dropped; every timestamp after K is masked.
    """
    if not events:
        return 0
    ids = [UUID(str(e["id"])) for e in events]
    states = {str(r["event_id"]): r for r in await conn.fetch(_STATE_AT_SQL, ids, known_at)}
    counts = {str(r["event_id"]): r for r in await conn.fetch(_COUNTS_AT_SQL, ids, known_at)}
    masked = 0
    for e in events:
        st = states.get(str(e["id"]))
        e["lifecycle_state"] = st["state_to"] if st is not None else None
        e["lifecycle_changed_at"] = (
            st["occurred_at"].isoformat()
            if st is not None and isinstance(st["occurred_at"], datetime) else None
        )
        c = counts.get(str(e["id"]))
        e["signal_count"] = int(c["n"]) if c is not None else 0
        e["distinct_source_count"] = int(c["d"]) if c is not None else 0
        if "superseded_by" in e:
            e["superseded_by"] = None
        masked += mask_after(e, EVENT_TIME_FIELDS, known_at)
    return masked


def stamp(
    out: Mapping[str, Any], known_at: datetime, *, how: str, masked: int = 0,
    dropped: int = 0,
) -> dict[str, Any]:
    """``out`` with the ``known_at_cut`` block (E0's shape plus ``masked_after_known_at``)."""
    block = cut_stamp(known_at, dropped=dropped, how=how)
    block["masked_after_known_at"] = int(masked)
    return {**out, "known_at_cut": block}
