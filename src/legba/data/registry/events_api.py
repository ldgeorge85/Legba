# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V3/P6 — the event read API (spec §6.2).

Three read-only GET endpoints mounted under ``/api/v1/v3`` beside the
timeline + graph-arcs routers (the SAME ``RegistryAPIDeps`` bundle +
``require_bearer`` gate, one leaf ``*_api.py`` per the wiring convention):

  * ``GET /events`` — the paged event list, filters mirroring the
    ``query_events`` tool (``target_id`` / ``geo`` / ``category`` /
    ``lifecycle_state`` / ``entity`` / ``since`` / ``until`` / ``as_of``)
    plus a route-only ``situation_id`` (the tracked-events read — the
    Situations panel's per-situation event list over
    ``situation_event_links``). House page contract: ``limit`` default 50 /
    max 500 and an opaque ``next_cursor`` (the ordering anchor is
    ``COALESCE(time_start, produced_at) DESC, id DESC``, so the cursor keys
    on that pair). ``next_cursor`` is ``null`` when the page filled short.
  * ``GET /events/{event_id}`` — the ``inspect_event`` shape: the event
    row, its ranked evidence signals, its actors with roles, its event
    edges, the situations tracking it, and the lifecycle ledger. A missing
    or malformed id is a named refusal (404 / 400), never an empty-looking
    success.
  * ``GET /events/{event_id}/lifecycle`` — the append-only ledger alone,
    OLDEST→NEWEST (the ``opened`` row first).

The default (no ``as_of``) read is the OPEN gate — the 0032 pair plus the
P7 ``origin_class`` leg — so a backfilled event can never read as live.
``as_of`` swaps to the validity predicate; only there does
``include_origin`` widen the class set (default ``LIVE_CLASSES`` — history
must be asked for). An unknown ``lifecycle_state`` or origin class is a
400 naming the vocabulary, not a silent empty page.

Registry-slim (the ``graph_arcs_api`` precedent): this module imports the
``legba.data.provenance.origin`` leaf only (stdlib + the closed
vocabulary) — never the analyst runtime — and the poisoned-import test
asserts that stays true.
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..provenance import origin as _origin
from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

#: Deploy-marker convention (GRAPH_ARCS_API_VERSION etc.): the roll
#: checklist greps for this exact line.
EVENTS_API_VERSION = "2026-09/p6"

DEFAULT_LIMIT = 50
MAX_LIMIT = 500

#: The event lifecycle CHECK vocabulary (migration 0202) — mirrored here
#: because the slim image reads no DDL. Drift-guarded in test_events_api.
EVENT_LIFECYCLE_STATES: tuple[str, ...] = (
    "emerging", "developing", "active", "evolving", "resolved",
)


# ---------------------------------------------------------------------------
# Page contract — the opaque (anchor_ts, id) cursor, substrate_reads_api's
# own scheme (each route module keeps its own, deliberately).
# ---------------------------------------------------------------------------


def _encode_cursor(anchor: datetime, row_id: UUID | str) -> str:
    """Pack the page-ordering anchor (COALESCE(time_start, produced_at), id)
    into an opaque base64 token."""
    payload = json.dumps(
        {"anchor": anchor.isoformat(), "id": str(row_id)},
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Reverse of ``_encode_cursor``. Raises HTTPException(400) on bad input."""
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii"))
        obj = json.loads(decoded)
        anchor = datetime.fromisoformat(obj["anchor"])
        row_id = UUID(obj["id"])
    except Exception as exc:  # pragma: no cover - validation path
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid cursor: {exc}",
        )
    return anchor, row_id


def _validate_limit(limit: int) -> int:
    """Clamp-validate the page size; a clear 400 rather than a silent cap."""
    if limit < 1 or limit > MAX_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"limit must be in [1, {MAX_LIMIT}]",
        )
    return limit


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class EventRow(BaseModel):
    """One event — the columns the map / timeline / dossier surfaces read."""

    id: str
    title: str
    summary: str = ""
    category: str = ""
    event_type: str = "incident"
    severity: str = "medium"
    lifecycle_state: str = "emerging"
    lifecycle_changed_at: datetime | None = None
    time_start: datetime | None = None
    time_end: datetime | None = None
    geo: list[str] = Field(default_factory=list)
    geo_lat: float | None = None
    geo_lon: float | None = None
    locations: list[str] = Field(default_factory=list)
    confidence: float | None = None
    signal_count: int = 0
    distinct_source_count: int = 0
    oversized: bool = False
    analyst_id: str | None = None
    target_id: str | None = None
    origin_class: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    produced_at: datetime | None = None


class EventsPage(BaseModel):
    """The paged envelope — house contract (``data`` + ``next_cursor``)."""

    data: list[EventRow] = Field(default_factory=list)
    next_cursor: str | None = None
    #: The as_of instant echoed back when the caller asked for one (honest
    #: provenance on a time-shifted read).
    as_of: datetime | None = None
    #: How many returned rows carry no recorded ``valid_from`` — the
    #: unbounded-start tally the as_of contract reports.
    unbounded_start: int = 0


class EventSignalRef(BaseModel):
    """One ranked evidence signal on an event."""

    signal_id: str
    title: str | None = None
    canonical_url: str | None = None
    relevance: float
    source_class: str = ""
    source_kind: str = ""
    source_id: str = ""
    linked_at: datetime | None = None


class EventActorRef(BaseModel):
    """One entity playing a role in the event."""

    entity_id: str
    canonical_name: str | None = None
    entity_type: str | None = None
    role: str = "actor"
    confidence: float


class EventEdgeRef(BaseModel):
    """One event edge touching this event (both directions)."""

    id: str
    src_event_id: str
    dst_event_id: str
    edge_type: str
    direction: str            # 'out' leaves this event; 'in' arrives
    confidence: float
    why: str = ""
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class EventSituationRef(BaseModel):
    """One situation frame tracking this event."""

    id: str
    name: str
    status: str
    category: str = ""
    intensity_score: float | None = None
    relevance: float
    linked_at: datetime | None = None


class EventLifecycleRow(BaseModel):
    """One ledger row — a transition with its why, oldest→newest."""

    id: str
    occurred_at: datetime
    transition: str
    state_from: str
    state_to: str
    why: str
    analyst_id: str | None = None
    created_at: datetime | None = None


class EventInspectResponse(BaseModel):
    """The ``inspect_event`` shape — one event plus its five sections."""

    found: bool = True
    event: EventRow
    event_signature: str | None = None
    source_method: str | None = None
    source_type: str | None = None
    signals: list[EventSignalRef] = Field(default_factory=list)
    actors: list[EventActorRef] = Field(default_factory=list)
    edges: list[EventEdgeRef] = Field(default_factory=list)
    situations: list[EventSituationRef] = Field(default_factory=list)
    lifecycle: list[EventLifecycleRow] = Field(default_factory=list)
    refs: list[str] = Field(default_factory=list)


class EventLifecycleResponse(BaseModel):
    """The lifecycle-ledger envelope for ``/events/{id}/lifecycle``."""

    event_id: str
    rows: list[EventLifecycleRow] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# SQL — index-driven reads over the 0202/0203/0204 tables.
# ---------------------------------------------------------------------------

_EVENT_COLS = (
    "e.id, e.title, e.summary, e.category, e.event_type, e.severity, "
    "e.lifecycle_state, e.lifecycle_changed_at, e.time_start, e.time_end, "
    "e.geo, e.geo_lat, e.geo_lon, e.locations, e.confidence, "
    "e.signal_count, e.distinct_source_count, e.oversized, e.analyst_id, "
    "e.target_id, e.origin_class, e.valid_from, e.valid_until, e.produced_at"
)


def _event_row(r: Any) -> EventRow:
    """One asyncpg row → the wire model."""
    return EventRow(
        id=str(r["id"]),
        title=r["title"],
        summary=r["summary"] or "",
        category=r["category"] or "",
        event_type=r["event_type"],
        severity=r["severity"],
        lifecycle_state=r["lifecycle_state"],
        lifecycle_changed_at=r["lifecycle_changed_at"],
        time_start=r["time_start"],
        time_end=r["time_end"],
        geo=list(r["geo"] or []),
        geo_lat=r["geo_lat"],
        geo_lon=r["geo_lon"],
        locations=list(r["locations"] or []),
        confidence=r["confidence"],
        signal_count=r["signal_count"],
        distinct_source_count=r["distinct_source_count"],
        oversized=bool(r["oversized"]),
        analyst_id=r["analyst_id"],
        target_id=r["target_id"],
        origin_class=r["origin_class"],
        valid_from=r["valid_from"],
        valid_until=r["valid_until"],
        produced_at=r["produced_at"],
    )


def _lifecycle_row(r: Any) -> EventLifecycleRow:
    """One ledger row → the wire model."""
    return EventLifecycleRow(
        id=str(r["id"]),
        occurred_at=r["occurred_at"],
        transition=r["transition"],
        state_from=r["state_from"],
        state_to=r["state_to"],
        why=r["why"],
        analyst_id=r["analyst_id"],
        created_at=r["created_at"],
    )


_LIFECYCLE_SQL = """
    SELECT id, occurred_at, transition, state_from, state_to, why,
           analyst_id, created_at
      FROM event_lifecycle_events
     WHERE event_id = $1
     ORDER BY occurred_at ASC, created_at ASC
"""


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_events_router(deps: RegistryAPIDeps) -> APIRouter:
    """``GET /events``, ``/events/{id}``, ``/events/{id}/lifecycle`` —
    the V3 event read surface (P6)."""
    router = APIRouter(tags=["events"])

    @router.get("/events", response_model=EventsPage)
    async def list_events(
        target_id: str | None = Query(default=None),
        geo: list[str] | None = Query(
            default=None, description="ISO2 code(s), repeatable/csv"),
        category: str | None = Query(default=None),
        lifecycle_state: str | None = Query(default=None),
        entity: str | None = Query(
            default=None, description="actor canonical-name substring"),
        situation_id: str | None = Query(
            default=None,
            description="events a situation tracks (situation_event_links)"),
        since: datetime | None = Query(
            default=None, description="occurrence window start (overlap)"),
        until: datetime | None = Query(
            default=None, description="occurrence window end (overlap)"),
        as_of: datetime | None = Query(
            default=None,
            description="read the events whose validity window covered this "
            "instant; omit for the open/live read"),
        include_origin: list[str] | None = Query(
            default=None,
            description="origin classes to include on the as_of read "
            "(repeatable/csv; default live/web_retrieval/seed)"),
        limit: int = Query(default=DEFAULT_LIMIT),
        cursor: str | None = Query(default=None),
        principal: str = Depends(require_bearer),
    ) -> EventsPage:
        limit = _validate_limit(limit)
        if lifecycle_state is not None and (
            lifecycle_state not in EVENT_LIFECYCLE_STATES
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"lifecycle_state must be one of "
                       f"{list(EVENT_LIFECYCLE_STATES)}",
            )
        if include_origin is not None:
            unknown = sorted(
                set(include_origin) - set(_origin.ORIGIN_CLASSES))
            if unknown:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"unknown origin_class {unknown} — the closed "
                           f"vocabulary is {list(_origin.ORIGIN_CLASSES)}",
                )
        situation_uuid: UUID | None = None
        if situation_id is not None:
            try:
                situation_uuid = UUID(situation_id)
            except (ValueError, AttributeError):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="situation_id must be a uuid",
                )

        clauses: list[str] = []
        params: list[Any] = []
        if as_of is not None:
            params.append(as_of)
            clauses.append(_origin.as_of_gate_sql("e", len(params)))
        else:
            clauses.append(_origin.live_gate_sql("e"))
        clauses.append(_origin.origin_class_clause("e", include_origin))
        if target_id is not None:
            params.append(target_id)
            clauses.append(f"e.target_id = ${len(params)}")
        if geo:
            params.append([str(g).upper() for g in geo])
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
                "WHERE sel.event_id = e.id "
                f"AND sel.situation_id = ${len(params)})"
            )
        # The occurrence-window overlap legs (NULL bounds fall back to
        # produced_at — an unstamped event still places honestly).
        if since is not None:
            params.append(since)
            clauses.append(
                f"COALESCE(e.time_end, e.time_start, e.produced_at) "
                f">= ${len(params)}"
            )
        if until is not None:
            params.append(until)
            clauses.append(
                f"COALESCE(e.time_start, e.produced_at) < ${len(params)}"
            )
        if cursor is not None:
            cur_anchor, cur_id = _decode_cursor(cursor)
            params.append(cur_anchor)
            params.append(cur_id)
            clauses.append(
                f"(COALESCE(e.time_start, e.produced_at), e.id) "
                f"< (${len(params) - 1}, ${len(params)})"
            )
        params.append(limit + 1)
        sql = (
            f"SELECT {_EVENT_COLS} FROM events e "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY COALESCE(e.time_start, e.produced_at) DESC, e.id DESC "
            f"LIMIT ${len(params)}"
        )
        async with deps.descriptor_registry.pg.acquire() as conn:
            records = await conn.fetch(sql, *params)

        rows = [_event_row(r) for r in records[:limit]]
        next_cursor: str | None = None
        if len(records) > limit and rows:
            last = records[limit - 1]
            anchor = last["time_start"] or last["produced_at"]
            next_cursor = _encode_cursor(anchor, last["id"])
        return EventsPage(
            data=rows,
            next_cursor=next_cursor,
            as_of=as_of,
            unbounded_start=(
                sum(1 for r in records[:limit] if r["valid_from"] is None)
                if as_of is not None else 0
            ),
        )

    @router.get("/events/{event_id}", response_model=EventInspectResponse)
    async def inspect_event(
        event_id: str,
        principal: str = Depends(require_bearer),
    ) -> EventInspectResponse:
        try:
            eid = UUID(event_id)
        except (ValueError, AttributeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="event_id must be a uuid",
            )
        async with deps.descriptor_registry.pg.acquire() as conn:
            event = await conn.fetchrow(
                f"SELECT {_EVENT_COLS}, e.event_signature, e.source_method, "
                "       e.source_type "
                "FROM events e WHERE e.id = $1",
                eid,
            )
            if event is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"event not found: {event_id}",
                )
            signal_rows = await conn.fetch(
                "SELECT l.signal_id, l.relevance, l.source_class, "
                "       l.source_kind, l.source_id, l.linked_at, "
                "       s.payload->>'title' AS title, s.canonical_url "
                "FROM signal_event_links l "
                "LEFT JOIN signals s ON s.id = l.signal_id "
                "WHERE l.event_id = $1 "
                "ORDER BY l.relevance DESC, l.linked_at DESC "
                "LIMIT 50",
                eid,
            )
            actor_rows = await conn.fetch(
                "SELECT l.entity_id, l.role, l.confidence, "
                "       ep.canonical_name, ep.entity_type "
                "FROM event_entity_links l "
                "JOIN entity_profiles ep ON ep.id = l.entity_id "
                "WHERE l.event_id = $1 "
                "ORDER BY l.confidence DESC, l.role, ep.canonical_name "
                "LIMIT 50",
                eid,
            )
            edge_rows = await conn.fetch(
                "SELECT id, src_event_id, dst_event_id, edge_type, "
                "       confidence, why, valid_from, valid_until "
                "FROM event_edges "
                "WHERE (src_event_id = $1 OR dst_event_id = $1) "
                "  AND superseded_by IS NULL "
                "  AND valid_until IS NULL "
                "ORDER BY edge_type, created_at "
                "LIMIT 50",
                eid,
            )
            situation_rows = await conn.fetch(
                "SELECT s.id, s.name, s.status, s.category, "
                "       s.intensity_score, l.relevance, "
                "       l.created_at AS linked_at "
                "FROM situation_event_links l "
                "JOIN situations s ON s.id = l.situation_id "
                "WHERE l.event_id = $1 "
                "ORDER BY l.relevance DESC, s.updated_at DESC "
                "LIMIT 50",
                eid,
            )
            ledger_rows = await conn.fetch(_LIFECYCLE_SQL, eid)

        refs: list[str] = [event_id]
        signals = [
            EventSignalRef(
                signal_id=str(r["signal_id"]),
                title=r["title"],
                canonical_url=r["canonical_url"],
                relevance=float(r["relevance"]),
                source_class=r["source_class"],
                source_kind=r["source_kind"],
                source_id=r["source_id"],
                linked_at=r["linked_at"],
            )
            for r in signal_rows
        ]
        refs += [s.signal_id for s in signals]
        actors = [
            EventActorRef(
                entity_id=str(r["entity_id"]),
                canonical_name=r["canonical_name"],
                entity_type=r["entity_type"],
                role=r["role"],
                confidence=float(r["confidence"]),
            )
            for r in actor_rows
        ]
        refs += [a.entity_id for a in actors]
        edges = [
            EventEdgeRef(
                id=str(r["id"]),
                src_event_id=str(r["src_event_id"]),
                dst_event_id=str(r["dst_event_id"]),
                edge_type=r["edge_type"],
                direction="out" if r["src_event_id"] == eid else "in",
                confidence=float(r["confidence"]),
                why=r["why"],
                valid_from=r["valid_from"],
                valid_until=r["valid_until"],
            )
            for r in edge_rows
        ]
        refs += [e.id for e in edges]
        situations = [
            EventSituationRef(
                id=str(r["id"]),
                name=r["name"],
                status=r["status"],
                category=r["category"] or "",
                intensity_score=r["intensity_score"],
                relevance=float(r["relevance"]),
                linked_at=r["linked_at"],
            )
            for r in situation_rows
        ]
        refs += [s.id for s in situations]
        lifecycle = [_lifecycle_row(r) for r in ledger_rows]
        refs += [row.id for row in lifecycle]

        return EventInspectResponse(
            event=_event_row(event),
            event_signature=event["event_signature"],
            source_method=event["source_method"],
            source_type=event["source_type"],
            signals=signals,
            actors=actors,
            edges=edges,
            situations=situations,
            lifecycle=lifecycle,
            refs=refs,
        )

    @router.get(
        "/events/{event_id}/lifecycle",
        response_model=EventLifecycleResponse,
    )
    async def event_lifecycle(
        event_id: str,
        principal: str = Depends(require_bearer),
    ) -> EventLifecycleResponse:
        try:
            eid = UUID(event_id)
        except (ValueError, AttributeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="event_id must be a uuid",
            )
        async with deps.descriptor_registry.pg.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM events WHERE id = $1", eid)
            if not exists:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"event not found: {event_id}",
                )
            ledger_rows = await conn.fetch(_LIFECYCLE_SQL, eid)
        return EventLifecycleResponse(
            event_id=event_id,
            rows=[_lifecycle_row(r) for r in ledger_rows],
        )

    return router
