"""Event Explorer routes — CRUD for events."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from ..app import get_stores, templates

logger = logging.getLogger(__name__)
from ...shared.schemas.signals import SignalCategory as EventCategory

router = APIRouter()

CATEGORIES = [c.value for c in EventCategory]
PAGE_SIZE = 50


async def _query_events_paged(stores, q, category, offset):
    """Query events with pagination. Returns (events, total)."""
    from ...shared.schemas.signals import Signal as Event

    if q:
        # Full-text search via OpenSearch (no offset — refine query instead)
        os_query = {
            "multi_match": {
                "query": q,
                "fields": ["title^2", "summary", "full_content", "actors", "locations"],
            }
        }
        result = await stores.opensearch.search(
            "legba-events-*", os_query, size=PAGE_SIZE,
        )
        events = []
        for hit in result.get("hits", []):
            try:
                events.append(Event.model_validate(hit))
            except Exception as e:
                logger.debug("Event parse from OpenSearch failed: %s", e)
                continue
        total = result.get("total", len(events))
        return events, total

    # Postgres query with offset
    if not stores.structured._available:
        return [], 0

    conditions = []
    params = []
    idx = 1

    if category:
        conditions.append(f"category = ${idx}")
        params.append(category)
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    try:
        async with stores.structured._pool.acquire() as conn:
            total = await conn.fetchval(f"SELECT count(*) FROM signals {where}", *params)
            rows = await conn.fetch(
                f"SELECT data FROM signals {where} "
                f"ORDER BY event_timestamp DESC NULLS LAST, created_at DESC "
                f"LIMIT ${idx} OFFSET ${idx + 1}",
                *params, PAGE_SIZE, offset,
            )
            events = [Event.model_validate_json(row["data"]) for row in rows]
            return events, total
    except Exception as e:
        logger.warning("Events paged query failed: %s", e)
        return [], 0


def _event_context(request, events, total, q, category, offset):
    return {
        "request": request,
        "active_page": "events",
        "events": events,
        "total": total,
        "q": q,
        "category": category,
        "categories": CATEGORIES,
        "offset": offset,
        "page_size": PAGE_SIZE,
        "has_more": (offset + PAGE_SIZE) < total,
        "next_offset": offset + PAGE_SIZE,
    }


@router.get("/events")
async def event_list(
    request: Request,
    q: str | None = None,
    category: str | None = None,
    offset: int = 0,
):
    stores = get_stores(request)
    events, total = await _query_events_paged(stores, q, category, offset)
    context = _event_context(request, events, total, q, category, offset)

    if request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"):
        return templates.TemplateResponse("events/_rows.html", context)
    return templates.TemplateResponse("events/list.html", context)


@router.get("/events/rows")
async def event_rows(
    request: Request,
    q: str | None = None,
    category: str | None = None,
    offset: int = 0,
):
    stores = get_stores(request)
    events, total = await _query_events_paged(stores, q, category, offset)
    context = _event_context(request, events, total, q, category, offset)
    return templates.TemplateResponse("events/_rows.html", context)


@router.get("/events/{event_id}")
async def event_detail(request: Request, event_id: UUID):
    stores = get_stores(request)
    event = await stores.get_event(event_id)

    source_name = None
    source = None
    linked_entities = []
    linked_situations = []
    related_events = []

    if event:
        # Parallel fetches for source, entities, situations, related events
        tasks = {}

        # Source name
        if event.source_id:
            tasks["source"] = stores.structured.get_source(event.source_id)

        # Linked entities
        tasks["entities"] = stores.structured.get_event_entities(event_id)

        # Linked situations (via situation_events join table)
        tasks["situations"] = _fetch_linked_situations(stores, event_id)

        # Related events (same source or overlapping actors)
        tasks["related"] = _fetch_related_events(
            stores, event_id, event.source_id, event.actors
        )

        results = await asyncio.gather(
            *tasks.values(), return_exceptions=True
        )
        result_map = dict(zip(tasks.keys(), results))

        # Process source
        if "source" in result_map and not isinstance(result_map["source"], Exception):
            source = result_map["source"]
            if source:
                source_name = source.name

        # Process entities
        if not isinstance(result_map.get("entities"), Exception):
            raw_links = result_map.get("entities", [])
            from ...shared.schemas.entity_profiles import EntityProfile
            for raw in raw_links:
                try:
                    profile = EntityProfile.model_validate(raw["profile"])
                    linked_entities.append({
                        "profile": profile,
                        "role": raw.get("role", "mentioned"),
                        "confidence": raw.get("confidence", 0.0),
                    })
                except Exception as e:
                    logger.debug("Entity profile parse failed: %s", e)
                    continue

        # Process situations
        if not isinstance(result_map.get("situations"), Exception):
            linked_situations = result_map.get("situations", [])

        # Process related events
        if not isinstance(result_map.get("related"), Exception):
            related_events = result_map.get("related", [])

    return templates.TemplateResponse(
        "events/detail.html",
        {
            "request": request,
            "active_page": "events",
            "event": event,
            "source_name": source_name,
            "source": source,
            "linked_entities": linked_entities,
            "linked_situations": linked_situations,
            "related_events": related_events,
            "categories": CATEGORIES,
        },
    )


async def _fetch_linked_situations(stores, event_id: UUID) -> list[dict]:
    """Fetch situations linked to this event via situation_events."""
    if not stores.structured._available:
        return []
    try:
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT s.id, s.name, s.status, s.category, s.intensity_score "
                "FROM situations s "
                "JOIN situation_signals ss ON s.id = ss.situation_id "
                "WHERE ss.signal_id = $1 "
                "ORDER BY s.intensity_score DESC",
                event_id,
            )
            return [
                {
                    "id": str(row["id"]),
                    "name": row["name"],
                    "status": row["status"],
                    "category": row["category"] or "",
                    "intensity_score": row["intensity_score"] or 0.0,
                }
                for row in rows
            ]
    except Exception as e:
        logger.warning("_fetch_linked_situations query failed: %s", e)
        return []


def _parse_ts(val) -> datetime | None:
    """Best-effort parse of an ISO timestamp string to datetime."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        s = str(val).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception as e:
        logger.debug("_parse_ts failed for %r: %s", val, e)
        return None


async def _fetch_related_events(
    stores, event_id: UUID, source_id: UUID | None, actors: list[str]
) -> list[dict]:
    """Fetch related events — same source or overlapping actors."""
    if not stores.structured._available:
        return []
    try:
        from ...shared.schemas.signals import Signal as Event
        results = []
        seen_ids = set()

        async with stores.structured._pool.acquire() as conn:
            # Same source events
            if source_id:
                rows = await conn.fetch(
                    "SELECT id, data FROM signals "
                    "WHERE source_id = $1 AND id != $2 "
                    "ORDER BY created_at DESC LIMIT 5",
                    source_id, event_id,
                )
                for row in rows:
                    eid = row["id"]
                    if eid in seen_ids:
                        continue
                    seen_ids.add(eid)
                    try:
                        data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
                        results.append({
                            "id": str(eid),
                            "title": data.get("title", "Untitled"),
                            "category": data.get("category", "other"),
                            "event_timestamp": _parse_ts(data.get("event_timestamp")),
                            "relation": "same source",
                        })
                    except Exception as e:
                        logger.debug("Related event parse failed: %s", e)
                        continue

            # Overlapping actors (if we have room and actors exist)
            if len(results) < 5 and actors:
                remaining = 5 - len(results)
                # Use ANY to find events sharing at least one actor
                actor_rows = await conn.fetch(
                    "SELECT id, data FROM signals "
                    "WHERE data->'actors' ?| $1 AND id != $2 "
                    "ORDER BY created_at DESC LIMIT $3",
                    actors, event_id, remaining + len(results),
                )
                for row in actor_rows:
                    eid = row["id"]
                    if eid in seen_ids:
                        continue
                    if len(results) >= 5:
                        break
                    seen_ids.add(eid)
                    try:
                        data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
                        results.append({
                            "id": str(eid),
                            "title": data.get("title", "Untitled"),
                            "category": data.get("category", "other"),
                            "event_timestamp": _parse_ts(data.get("event_timestamp")),
                            "relation": "shared actors",
                        })
                    except Exception as e:
                        logger.debug("Related event (actor) parse failed: %s", e)
                        continue

        return results[:5]
    except Exception as e:
        logger.warning("_fetch_related_events query failed: %s", e)
        return []


# ------------------------------------------------------------------
# Write operations (U11: delete, U12: metadata edit)
# ------------------------------------------------------------------

@router.delete("/api/events/{event_id}")
async def delete_event(request: Request, event_id: UUID):
    """Delete an event and its entity links."""
    stores = get_stores(request)
    try:
        async with stores.structured._pool.acquire() as conn:
            # Delete entity links first (FK cascade)
            await conn.execute(
                "DELETE FROM signal_entity_links WHERE signal_id = $1", event_id,
            )
            # Delete the event
            await conn.execute("DELETE FROM signals WHERE id = $1", event_id)

        # Also delete from OpenSearch (best-effort)
        if stores.opensearch and stores.opensearch.available:
            try:
                await stores.opensearch.delete_document("legba-events-*", str(event_id))
            except Exception as e:
                logger.warning("OpenSearch delete for event %s failed (non-fatal): %s", event_id, e)

        return HTMLResponse(
            '<div class="text-green-400 text-sm p-2">Event deleted.</div>'
            '<script>setTimeout(() => window.location="/events", 800)</script>'
        )
    except Exception as e:
        return HTMLResponse(f'<div class="text-red-400 text-sm p-2">Error: {e}</div>', status_code=500)


@router.put("/api/events/{event_id}")
async def update_event_metadata(
    request: Request,
    event_id: UUID,
    category: str = Form(...),
    tags: str = Form(""),
    confidence: float = Form(0.5),
):
    """Update event metadata (category, tags, confidence)."""
    if category not in CATEGORIES:
        return HTMLResponse(f'<span class="text-red-400 text-xs">Invalid category: {category}</span>', status_code=400)

    stores = get_stores(request)
    try:
        async with stores.structured._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data FROM signals WHERE id = $1", event_id,
            )
            if not row:
                return HTMLResponse('<div class="text-red-400 text-sm p-2">Event not found.</div>', status_code=404)

            data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
            data["category"] = category
            data["confidence"] = confidence
            data["tags"] = [t.strip() for t in tags.split(",") if t.strip()]

            await conn.execute(
                "UPDATE signals SET data = $1::jsonb, updated_at = now() WHERE id = $2",
                json.dumps(data, default=str), event_id,
            )

        return HTMLResponse(
            '<div class="text-green-400 text-sm p-2">Event updated.</div>'
            '<script>setTimeout(() => location.reload(), 500)</script>'
        )
    except Exception as e:
        return HTMLResponse(f'<div class="text-red-400 text-sm p-2">Error: {e}</div>', status_code=500)
