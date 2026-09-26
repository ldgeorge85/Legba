# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Frame readers — the situation / timeline / event substrate reads
(V3/P6 extraction).

``substrate_query_port.py`` crossed its module-size headroom when the P6
event tools landed; per the gate's own instruction (extract, never raise),
the situation and timeline readers — the natural cohesive cut, the same
shape P3 made with ``substrate_temporal.py`` — moved here as module-level
functions taking the pool as their first argument. The port keeps thin
``self``-delegating wrappers (docstrings intact, signatures unchanged), so
the ``SubstrateQueryPort`` Protocol, the consult dispatcher, the pack
handlers and every test see the same surface.

P6 then adds the two event readers to this SAME sibling — an event is a
bounded frame (``[time_start, time_end]``) where a situation is an ongoing
one, so the cluster stays cohesive:

  * ``query_events`` — the filtered event list. Its default (no ``as_of``)
    read is the OPEN gate — :func:`origin.live_gate_sql` (the 0032 pair +
    the P7 origin-class leg), so a backfilled event can never read as live.
    ``as_of`` swaps to the validity predicate
    (:func:`origin.as_of_gate_sql`) — ``include_origin`` there, and only
    there, widens the class set (default ``LIVE_CLASSES`` — history must be
    asked for). ``since``/``until`` bound the event's OCCURRENCE span by
    overlap: an event that began before ``until`` and had not ended before
    ``since`` is in the window (an event's ``time_start``/``time_end`` may
    be NULL — it then falls back to ``produced_at`` on both legs).
  * ``inspect_event`` — the one-event dossier: the row itself, its ranked
    evidence signals, its actors with roles, its event edges (both
    directions), the situations tracking it, and its lifecycle ledger
    oldest→newest — spec §6.1's five sections, in one call.

Both take ``max_row_limit`` from the caller (the port's ``_MAX_ROW_LIMIT``)
so the sibling carries no policy constants of its own beyond the timeline
merge knobs it owns.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import asyncpg

from ..data.provenance import origin as _origin
from . import substrate_temporal as _temporal

logger = logging.getLogger(__name__)

# ``get_timeline`` pulls at most this many of each contributing stream
# (current facts, recent signals) before the merge + clamp to the caller's
# ``limit`` — bounds the per-stream scan when the merged limit is large.
_TIMELINE_PER_STREAM_CAP = 200
# DQ-#70/F5 — per-kind floor on the MERGED timeline. Signals are far denser than
# facts/situations, so a pure newest-first clamp buries the sparse-but-important
# kinds. Each kind is guaranteed up to this many of its newest items before the
# remaining budget is filled by overall recency.
_TIMELINE_PER_KIND_FLOOR = 6

#: The event lifecycle CHECK vocabulary (migration 0202) — an unknown
#: ``lifecycle_state`` filter is refused loud, not silently empty.
EVENT_LIFECYCLE_STATES: tuple[str, ...] = (
    "emerging", "developing", "active", "evolving", "resolved",
)


# ---------------------------------------------------------------------------
# list_situations — moved verbatim from substrate_query_port (P6 extraction)
# ---------------------------------------------------------------------------


async def list_situations(
    pool: asyncpg.Pool,
    *,
    status: str | None = None,
    target_id: str | None = None,
    since_hours: int | None = None,
    limit: int = 20,
    as_of: str | None = None,
    max_row_limit: int = 200,
) -> dict[str, Any]:
    """The ``list_situations`` body — see the port method's docstring (it
    owns the contract)."""
    try:
        as_of_dt = (
            _temporal.parse_instant(as_of, name="as_of")
            if as_of is not None else None
        )
    except _temporal.TemporalParameterError as exc:
        return {"rows": [], "refs": [], "error": str(exc)}
    clamped_limit = max(1, min(int(limit), max_row_limit))
    clauses: list[str] = []
    params: list[Any] = []
    if as_of_dt is not None:
        params.append(as_of_dt)
        clauses.append(_temporal.temporal_predicate("", len(params)))
    if status is not None:
        params.append(status)
        clauses.append(f"status = ${len(params)}")
    if target_id is not None:
        params.append(target_id)
        clauses.append(f"target_id = ${len(params)}")
    if since_hours is not None:
        params.append(datetime.now(timezone.utc) - timedelta(hours=int(since_hours)))
        # Recency = last ACTIVITY, not first creation. produced_at is frozen
        # at first-cluster (the upsert never bumps it), so filtering it would
        # silently drop a weeks-old frame that just took a fresh member.
        # updated_at is refreshed on every re-cluster (NOT NULL, now() default).
        clauses.append(f"updated_at >= ${len(params)}")
    where = (" AND ".join(clauses)) if clauses else "TRUE"
    params.append(clamped_limit)
    sql = (
        "SELECT id, name, status, category, "
        "       event_count, intensity_score, "
        "       target_id, analyst_id, produced_at, updated_at, "
        "       valid_from "
        "FROM situations "
        f"WHERE {where} "
        "ORDER BY updated_at DESC, produced_at DESC "
        f"LIMIT ${len(params)}"
    )
    async with pool.acquire() as conn:
        records = await conn.fetch(sql, *params)

    rows: list[dict[str, Any]] = []
    refs: list[str] = []
    for r in records:
        refs.append(str(r["id"]))
        rows.append({
            "id": str(r["id"]),
            "name": r["name"],
            "status": r["status"],
            "category": r["category"],
            "event_count": r["event_count"],
            "intensity_score": r["intensity_score"],
            "target_id": r["target_id"],
            "analyst_id": r["analyst_id"],
            "produced_at": r["produced_at"].isoformat()
                if isinstance(r["produced_at"], datetime) else None,
            "updated_at": r["updated_at"].isoformat()
                if isinstance(r["updated_at"], datetime) else None,
        })
    out: dict[str, Any] = {"rows": rows, "refs": refs, "count": len(rows)}
    if as_of_dt is not None:
        out["as_of"] = as_of_dt.isoformat()
        # Count over the raw records: the row dicts deliberately do not
        # carry valid_from (the response shape is unchanged); the NULL
        # tally is the honest "how much of this answer has no recorded
        # start" number.
        out["unbounded_start"] = sum(
            1 for r in records if r["valid_from"] is None)
    return out


# ---------------------------------------------------------------------------
# get_timeline — moved verbatim from substrate_query_port (P6 extraction)
# ---------------------------------------------------------------------------


async def get_timeline(
    pool: asyncpg.Pool,
    *,
    subject: str,
    limit: int = 40,
    since: str | None = None,
    until: str | None = None,
    max_row_limit: int = 200,
) -> dict[str, Any]:
    """The ``get_timeline`` body — see the port method's docstring (it owns
    the contract)."""
    try:
        since_dt = (
            _temporal.parse_instant(since, name="since")
            if since is not None else None
        )
        until_dt = (
            _temporal.parse_instant(until, name="until")
            if until is not None else None
        )
    except _temporal.TemporalParameterError as exc:
        return {
            "subject": subject, "items": [], "refs": [],
            "error": str(exc),
        }
    clamped_limit = max(1, min(int(limit), max_row_limit))
    s = (subject or "").strip()
    if not s:
        return {
            "subject": subject,
            "items": [],
            "refs": [],
            "error": "subject must be non-empty",
        }

    def _window(anchor: str, params: list[Any]) -> str:
        """Append the since/until bounds to ``params``; return the clause."""
        si = ui = None
        if since_dt is not None:
            params.append(since_dt)
            si = len(params)
        if until_dt is not None:
            params.append(until_dt)
            ui = len(params)
        clause = _temporal.anchor_window_clause(
            anchor, since_param=si, until_param=ui)
        return f" AND {clause}" if clause else ""

    async with pool.acquire() as conn:
        fact_params: list[Any] = [f"%{s}%"]
        fact_rows = await conn.fetch(
            f"""
            SELECT id, subject, predicate, value, confidence,
                   valid_from, produced_at, created_at
            FROM facts
            WHERE subject ILIKE $1
              AND {_origin.live_gate_sql("")}
              {_window("COALESCE(valid_from, produced_at, created_at)",
                       fact_params)}
            ORDER BY COALESCE(valid_from, produced_at, created_at) DESC
            LIMIT ${len(fact_params) + 1}
            """,
            *fact_params,
            _TIMELINE_PER_STREAM_CAP,
        )
        signal_params: list[Any] = [s]
        signal_rows = await conn.fetch(
            f"""
            SELECT id, payload->>'title' AS title,
                   payload->>'title_en' AS title_en,
                   payload->>'category' AS category,
                   canonical_url, fetched_at, created_at
            FROM signals
            WHERE to_tsvector('simple',
                      coalesce(payload->>'title','') || ' ' ||
                      coalesce(payload->>'summary',''))
                  @@ plainto_tsquery('simple', $1)
                  {_window("COALESCE(fetched_at, created_at)",
                           signal_params)}
            ORDER BY COALESCE(fetched_at, created_at) DESC
            LIMIT ${len(signal_params) + 1}
            """,
            *signal_params,
            _TIMELINE_PER_STREAM_CAP,
        )
        # Situation FRAMES on the subject (5b/5c — situations are the
        # persistent-frame substitute for an events table; see DATA_MODEL).
        # A frame is a span [valid_from, valid_until); it anchors on
        # valid_from so the timeline shows when the situation began alongside
        # the facts + signals. Closed (historical) frames are included — they
        # ARE the "events come and go" history.
        situation_params: list[Any] = [f"%{s}%"]
        situation_rows = await conn.fetch(
            f"""
            SELECT id, name, status, intensity_score,
                   valid_from, valid_until, produced_at, created_at
            FROM situations
            WHERE name ILIKE $1
              AND superseded_by IS NULL
              {_window("COALESCE(valid_from, produced_at, created_at)",
                       situation_params)}
            ORDER BY COALESCE(valid_from, produced_at, created_at) DESC
            LIMIT ${len(situation_params) + 1}
            """,
            *situation_params,
            _TIMELINE_PER_STREAM_CAP,
        )

    # Merge into one stream keyed on a single temporal anchor.  Skip
    # any row whose anchor is NULL — it can't be placed in time.
    merged: list[tuple[datetime, dict[str, Any]]] = []
    refs: list[str] = []
    for r in fact_rows:
        anchor = r["valid_from"] or r["produced_at"] or r["created_at"]
        if not isinstance(anchor, datetime):
            continue
        fid = r["id"]
        refs.append(str(fid))
        merged.append((anchor, {
            "kind": "fact",
            "id": str(fid),
            "at": anchor.isoformat(),
            "subject": r["subject"],
            "predicate": r["predicate"],
            "value": r["value"],
            "confidence": float(r["confidence"])
                if r["confidence"] is not None else None,
        }))
    for r in signal_rows:
        anchor = r["fetched_at"] or r["created_at"]
        if not isinstance(anchor, datetime):
            continue
        sid = r["id"]
        refs.append(str(sid))
        merged.append((anchor, {
            "kind": "signal",
            "id": str(sid),
            "at": anchor.isoformat(),
            "title": r["title"],
            # T-1b (M13): stored English title (translate route); absent else.
            "title_en": r["title_en"],
            "category": r["category"],
            "source_url": r["canonical_url"],
        }))
    for r in situation_rows:
        anchor = r["valid_from"] or r["produced_at"] or r["created_at"]
        if not isinstance(anchor, datetime):
            continue
        uid = r["id"]
        refs.append(str(uid))
        until_ = r["valid_until"]
        merged.append((anchor, {
            "kind": "situation",
            "id": str(uid),
            "at": anchor.isoformat(),
            "name": r["name"],
            "status": r["status"],
            "intensity_score": float(r["intensity_score"])
                if r["intensity_score"] is not None else None,
            # The span end — None while the frame is still open/ongoing.
            "until": until_.isoformat() if isinstance(until_, datetime) else None,
        }))

    merged.sort(key=lambda pair: pair[0], reverse=True)  # newest-first
    # DQ-#70/F5 — per-kind floor: guarantee each kind up to
    # ``_TIMELINE_PER_KIND_FLOOR`` of its NEWEST items (round-robin, so a
    # tight budget is shared fairly), then fill the remaining slots by
    # overall recency. Without this, a dense signal stream clamps the whole
    # window to signals and the sparse facts/situations vanish.
    by_kind: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
    for pair in merged:
        by_kind.setdefault(pair[1]["kind"], []).append(pair)
    selected: list[tuple[datetime, dict[str, Any]]] = []
    chosen: set[str] = set()
    for rank in range(_TIMELINE_PER_KIND_FLOOR):
        for rows in by_kind.values():
            if rank < len(rows) and len(selected) < clamped_limit:
                pair = rows[rank]
                if pair[1]["id"] not in chosen:
                    selected.append(pair)
                    chosen.add(pair[1]["id"])
    for pair in merged:  # fill the rest by overall recency
        if len(selected) >= clamped_limit:
            break
        if pair[1]["id"] not in chosen:
            selected.append(pair)
            chosen.add(pair[1]["id"])
    selected.sort(key=lambda pair: pair[0], reverse=True)
    items = [item for _, item in selected]

    out: dict[str, Any] = {
        "subject": subject,
        "items": items,
        "refs": [item["id"] for item in items],
        "counts": {
            "facts": sum(1 for i in items if i["kind"] == "fact"),
            "signals": sum(1 for i in items if i["kind"] == "signal"),
            "situations": sum(1 for i in items if i["kind"] == "situation"),
        },
    }
    if since_dt is not None:
        out["since"] = since_dt.isoformat()
    if until_dt is not None:
        out["until"] = until_dt.isoformat()
    return out


# ---------------------------------------------------------------------------
# query_events (V3/P6) — the filtered event list
# ---------------------------------------------------------------------------


def _event_row(r: Any) -> dict[str, Any]:
    """One events row → the tool's wire dict (timestamps ISO, floats real)."""
    def iso(v: Any) -> str | None:
        return v.isoformat() if isinstance(v, datetime) else None
    return {
        "id": str(r["id"]),
        "title": r["title"],
        "category": r["category"],
        "event_type": r["event_type"],
        "severity": r["severity"],
        "lifecycle_state": r["lifecycle_state"],
        "lifecycle_changed_at": iso(r["lifecycle_changed_at"]),
        "time_start": iso(r["time_start"]),
        "time_end": iso(r["time_end"]),
        "geo": list(r["geo"] or []),
        "geo_lat": r["geo_lat"],
        "geo_lon": r["geo_lon"],
        "locations": list(r["locations"] or []),
        "confidence": float(r["confidence"])
            if r["confidence"] is not None else None,
        "signal_count": r["signal_count"],
        "distinct_source_count": r["distinct_source_count"],
        "oversized": bool(r["oversized"]),
        "analyst_id": r["analyst_id"],
        "target_id": r["target_id"],
        "origin_class": r["origin_class"],
        "produced_at": iso(r["produced_at"]),
        "valid_from": iso(r["valid_from"]),
        "valid_until": iso(r["valid_until"]),
    }


async def query_events(
    pool: asyncpg.Pool,
    *,
    target_id: str | None = None,
    geo: str | list[str] | None = None,
    category: str | None = None,
    lifecycle_state: str | None = None,
    entity: str | None = None,
    since: str | None = None,
    until: str | None = None,
    as_of: str | None = None,
    include_origin: list[str] | None = None,
    situation_id: str | None = None,
    limit: int = 20,
    max_row_limit: int = 200,
) -> dict[str, Any]:
    """The ``query_events`` body — see the port method's docstring."""
    try:
        as_of_dt = (
            _temporal.parse_instant(as_of, name="as_of")
            if as_of is not None else None
        )
        since_dt = (
            _temporal.parse_instant(since, name="since")
            if since is not None else None
        )
        until_dt = (
            _temporal.parse_instant(until, name="until")
            if until is not None else None
        )
    except _temporal.TemporalParameterError as exc:
        return {"rows": [], "refs": [], "error": str(exc)}
    if lifecycle_state is not None and lifecycle_state not in EVENT_LIFECYCLE_STATES:
        return {
            "rows": [], "refs": [],
            "error": f"unknown lifecycle_state {lifecycle_state!r} — the "
                     f"five-state vocabulary is {list(EVENT_LIFECYCLE_STATES)}",
        }
    if situation_id is not None:
        try:
            situation_uuid: UUID | None = UUID(str(situation_id))
        except (ValueError, AttributeError):
            return {
                "rows": [], "refs": [],
                "error": f"situation_id must be a uuid, got {situation_id!r}",
            }
    else:
        situation_uuid = None
    try:
        origin_clause = _origin.origin_class_clause("e", include_origin)
    except _origin.OriginClassError as exc:
        return {"rows": [], "refs": [], "error": str(exc)}

    clamped_limit = max(1, min(int(limit), max_row_limit))
    clauses: list[str] = []
    params: list[Any] = []
    if as_of_dt is not None:
        params.append(as_of_dt)
        clauses.append(_origin.as_of_gate_sql("e", len(params)))
    else:
        clauses.append(_origin.live_gate_sql("e"))
    clauses.append(origin_clause)
    if target_id is not None:
        params.append(target_id)
        clauses.append(f"e.target_id = ${len(params)}")
    if geo is not None:
        geo_codes = [geo] if isinstance(geo, str) else list(geo)
        params.append([str(g).upper() for g in geo_codes])
        clauses.append(f"e.geo && ${len(params)}::text[]")
    if category is not None:
        params.append(category)
        clauses.append(f"e.category = ${len(params)}")
    if lifecycle_state is not None:
        params.append(lifecycle_state)
        clauses.append(f"e.lifecycle_state = ${len(params)}")
    if entity is not None:
        params.append(f"%{entity}%")
        clauses.append(
            "EXISTS (SELECT 1 FROM event_entity_links eel "
            "JOIN entity_profiles ep ON ep.id = eel.entity_id "
            "WHERE eel.event_id = e.id "
            f"AND ep.canonical_name ILIKE ${len(params)})"
        )
    if situation_uuid is not None:
        params.append(situation_uuid)
        clauses.append(
            "EXISTS (SELECT 1 FROM situation_event_links sel "
            f"WHERE sel.event_id = e.id AND sel.situation_id = ${len(params)})"
        )
    # since/until bound the OCCURRENCE span by overlap — the event began
    # before `until` and had not ended before `since` (NULL time bounds fall
    # back to produced_at so an unstamped row still places honestly).
    if since_dt is not None:
        params.append(since_dt)
        clauses.append(
            f"COALESCE(e.time_end, e.time_start, e.produced_at) >= ${len(params)}")
    if until_dt is not None:
        params.append(until_dt)
        clauses.append(
            f"COALESCE(e.time_start, e.produced_at) < ${len(params)}")
    where = " AND ".join(clauses)
    params.append(clamped_limit)
    sql = (
        "SELECT e.id, e.title, e.category, e.event_type, e.severity, "
        "       e.lifecycle_state, e.lifecycle_changed_at, "
        "       e.time_start, e.time_end, e.geo, e.geo_lat, e.geo_lon, "
        "       e.locations, e.confidence, e.signal_count, "
        "       e.distinct_source_count, e.oversized, e.analyst_id, "
        "       e.target_id, e.origin_class, e.produced_at, "
        "       e.valid_from, e.valid_until "
        "FROM events e "
        f"WHERE {where} "
        "ORDER BY COALESCE(e.time_start, e.produced_at) DESC, e.id DESC "
        f"LIMIT ${len(params)}"
    )
    async with pool.acquire() as conn:
        records = await conn.fetch(sql, *params)

    rows = [_event_row(r) for r in records]
    refs = [row["id"] for row in rows]
    out: dict[str, Any] = {"rows": rows, "refs": refs, "count": len(rows)}
    if as_of_dt is not None:
        out["as_of"] = as_of_dt.isoformat()
        out["unbounded_start"] = sum(
            1 for r in records if r["valid_from"] is None)
    if since_dt is not None:
        out["since"] = since_dt.isoformat()
    if until_dt is not None:
        out["until"] = until_dt.isoformat()
    return out


# ---------------------------------------------------------------------------
# inspect_event (V3/P6) — the one-event dossier
# ---------------------------------------------------------------------------

#: Cap on each detail section (ranked signals / actors / edges / situations /
#: ledger) so a hot event cannot make one tool call unbounded.
_INSPECT_SECTION_CAP = 50


async def inspect_event(
    pool: asyncpg.Pool,
    *,
    event_id: str,
    section_cap: int = _INSPECT_SECTION_CAP,
) -> dict[str, Any]:
    """The ``inspect_event`` body — see the port method's docstring."""
    try:
        eid = UUID(str(event_id))
    except (ValueError, AttributeError):
        return {
            "found": False, "refs": [],
            "error": f"event_id must be a uuid, got {event_id!r}",
        }

    def iso(v: Any) -> str | None:
        return v.isoformat() if isinstance(v, datetime) else None

    async with pool.acquire() as conn:
        event = await conn.fetchrow(
            "SELECT e.id, e.event_signature, e.analyst_id, e.title, e.summary, "
            "       e.category, e.event_type, e.severity, "
            "       e.lifecycle_state, e.lifecycle_changed_at, "
            "       e.time_start, e.time_end, e.geo, e.geo_lat, e.geo_lon, "
            "       e.locations, e.confidence, e.signal_count, "
            "       e.distinct_source_count, e.oversized, e.source_method, "
            "       e.source_type, e.target_id, e.target_version, "
            "       e.analyst_version, e.run_id, e.origin_class, "
            "       e.collection_id, e.valid_from, e.valid_until, "
            "       e.superseded_by, e.produced_at, e.created_at, e.updated_at "
            "FROM events e WHERE e.id = $1",
            eid,
        )
        if event is None:
            return {
                "found": False, "refs": [],
                "error": f"event not found: {event_id}",
            }
        signal_rows = await conn.fetch(
            "SELECT l.signal_id, l.relevance, l.source_class, l.source_kind, "
            "       l.source_id, l.linked_at, "
            "       s.payload->>'title' AS title, s.canonical_url, "
            "       s.fetched_at "
            "FROM signal_event_links l "
            "LEFT JOIN signals s ON s.id = l.signal_id "
            "WHERE l.event_id = $1 "
            "ORDER BY l.relevance DESC, l.linked_at DESC "
            "LIMIT $2",
            eid, section_cap,
        )
        actor_rows = await conn.fetch(
            "SELECT l.entity_id, l.role, l.confidence, "
            "       ep.canonical_name, ep.entity_type "
            "FROM event_entity_links l "
            "JOIN entity_profiles ep ON ep.id = l.entity_id "
            "WHERE l.event_id = $1 "
            "ORDER BY l.confidence DESC, l.role, ep.canonical_name "
            "LIMIT $2",
            eid, section_cap,
        )
        edge_rows = await conn.fetch(
            "SELECT id, src_event_id, dst_event_id, edge_type, confidence, "
            "       why, valid_from, valid_until "
            "FROM event_edges "
            "WHERE (src_event_id = $1 OR dst_event_id = $1) "
            "  AND superseded_by IS NULL "
            "  AND valid_until IS NULL "
            "ORDER BY edge_type, created_at "
            "LIMIT $2",
            eid, section_cap,
        )
        situation_rows = await conn.fetch(
            "SELECT s.id, s.name, s.status, s.category, s.intensity_score, "
            "       l.relevance, l.created_at AS linked_at "
            "FROM situation_event_links l "
            "JOIN situations s ON s.id = l.situation_id "
            "WHERE l.event_id = $1 "
            "ORDER BY l.relevance DESC, s.updated_at DESC "
            "LIMIT $2",
            eid, section_cap,
        )
        ledger_rows = await conn.fetch(
            "SELECT id, occurred_at, transition, state_from, state_to, why, "
            "       analyst_id, created_at "
            "FROM event_lifecycle_events "
            "WHERE event_id = $1 "
            "ORDER BY occurred_at ASC, created_at ASC "
            "LIMIT $2",
            eid, section_cap,
        )

    refs: list[str] = [str(event["id"])]
    signals: list[dict[str, Any]] = []
    for r in signal_rows:
        refs.append(str(r["signal_id"]))
        signals.append({
            "signal_id": str(r["signal_id"]),
            "title": r["title"],
            "canonical_url": r["canonical_url"],
            "relevance": float(r["relevance"]),
            "source_class": r["source_class"],
            "source_kind": r["source_kind"],
            "source_id": r["source_id"],
            "linked_at": iso(r["linked_at"]),
            "fetched_at": iso(r["fetched_at"]),
        })
    actors: list[dict[str, Any]] = []
    for r in actor_rows:
        refs.append(str(r["entity_id"]))
        actors.append({
            "entity_id": str(r["entity_id"]),
            "canonical_name": r["canonical_name"],
            "entity_type": r["entity_type"],
            "role": r["role"],
            "confidence": float(r["confidence"]),
        })
    edges: list[dict[str, Any]] = []
    for r in edge_rows:
        refs.append(str(r["id"]))
        out_dir = "out" if r["src_event_id"] == eid else "in"
        edges.append({
            "id": str(r["id"]),
            "src_event_id": str(r["src_event_id"]),
            "dst_event_id": str(r["dst_event_id"]),
            "edge_type": r["edge_type"],
            "direction": out_dir,
            "confidence": float(r["confidence"]),
            "why": r["why"],
            "valid_from": iso(r["valid_from"]),
            "valid_until": iso(r["valid_until"]),
        })
    situations: list[dict[str, Any]] = []
    for r in situation_rows:
        refs.append(str(r["id"]))
        situations.append({
            "id": str(r["id"]),
            "name": r["name"],
            "status": r["status"],
            "category": r["category"],
            "intensity_score": float(r["intensity_score"])
                if r["intensity_score"] is not None else None,
            "relevance": float(r["relevance"]),
            "linked_at": iso(r["linked_at"]),
        })
    lifecycle: list[dict[str, Any]] = []
    for r in ledger_rows:
        refs.append(str(r["id"]))
        lifecycle.append({
            "id": str(r["id"]),
            "occurred_at": iso(r["occurred_at"]),
            "transition": r["transition"],
            "state_from": r["state_from"],
            "state_to": r["state_to"],
            "why": r["why"],
            "analyst_id": r["analyst_id"],
            "created_at": iso(r["created_at"]),
        })

    return {
        "found": True,
        "event": {
            "id": str(event["id"]),
            "event_signature": event["event_signature"],
            "title": event["title"],
            "summary": event["summary"],
            "category": event["category"],
            "event_type": event["event_type"],
            "severity": event["severity"],
            "lifecycle_state": event["lifecycle_state"],
            "lifecycle_changed_at": iso(event["lifecycle_changed_at"]),
            "time_start": iso(event["time_start"]),
            "time_end": iso(event["time_end"]),
            "geo": list(event["geo"] or []),
            "geo_lat": event["geo_lat"],
            "geo_lon": event["geo_lon"],
            "locations": list(event["locations"] or []),
            "confidence": float(event["confidence"])
                if event["confidence"] is not None else None,
            "signal_count": event["signal_count"],
            "distinct_source_count": event["distinct_source_count"],
            "oversized": bool(event["oversized"]),
            "source_method": event["source_method"],
            "source_type": event["source_type"],
            "analyst_id": event["analyst_id"],
            "analyst_version": event["analyst_version"],
            "run_id": str(event["run_id"]) if event["run_id"] else None,
            "target_id": event["target_id"],
            "target_version": event["target_version"],
            "origin_class": event["origin_class"],
            "collection_id": str(event["collection_id"])
                if event["collection_id"] else None,
            "valid_from": iso(event["valid_from"]),
            "valid_until": iso(event["valid_until"]),
            "superseded_by": str(event["superseded_by"])
                if event["superseded_by"] else None,
            "produced_at": iso(event["produced_at"]),
            "created_at": iso(event["created_at"]),
            "updated_at": iso(event["updated_at"]),
        },
        "signals": signals,
        "actors": actors,
        "edges": edges,
        "situations": situations,
        "lifecycle": lifecycle,
        "refs": refs,
    }
