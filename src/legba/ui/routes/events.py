"""Event Explorer routes — CRUD for events."""

from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from ..app import get_stores, templates
from ...shared.schemas.events import EventCategory

router = APIRouter()

CATEGORIES = [c.value for c in EventCategory]


@router.get("/events")
async def event_list(
    request: Request,
    q: str | None = None,
    category: str | None = None,
):
    stores = get_stores(request)

    if q:
        # Full-text search via OpenSearch
        os_query = {
            "multi_match": {
                "query": q,
                "fields": ["title^2", "summary", "full_content", "actors", "locations"],
            }
        }
        result = await stores.opensearch.search("legba-events-*", os_query, size=50)
        from ...shared.schemas.events import Event
        events = []
        for hit in result.get("hits", []):
            try:
                events.append(Event.model_validate(hit))
            except Exception:
                continue
        total = result.get("total", len(events))
    else:
        events = await stores.structured.query_events(
            category=category or None,
            limit=50,
        )
        total = await stores.count_events()

    context = {
        "request": request,
        "active_page": "events",
        "events": events,
        "total": total,
        "q": q,
        "category": category,
        "categories": CATEGORIES,
    }

    if request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"):
        return templates.TemplateResponse("events/_rows.html", context)
    return templates.TemplateResponse("events/list.html", context)


@router.get("/events/{event_id}")
async def event_detail(request: Request, event_id: UUID):
    stores = get_stores(request)
    event = await stores.get_event(event_id)

    source_name = None
    linked_entities = []

    if event:
        # Fetch source name
        if event.source_id:
            source = await stores.structured.get_source(event.source_id)
            if source:
                source_name = source.name

        # Fetch linked entities
        raw_links = await stores.structured.get_event_entities(event_id)
        from ...shared.schemas.entity_profiles import EntityProfile
        for raw in raw_links:
            try:
                profile = EntityProfile.model_validate(raw["profile"])
                linked_entities.append({
                    "profile": profile,
                    "role": raw.get("role", "mentioned"),
                    "confidence": raw.get("confidence", 0.0),
                })
            except Exception:
                continue

    return templates.TemplateResponse(
        "events/detail.html",
        {
            "request": request,
            "active_page": "events",
            "event": event,
            "source_name": source_name,
            "linked_entities": linked_entities,
            "categories": CATEGORIES,
        },
    )


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
                "DELETE FROM event_entity_links WHERE event_id = $1", event_id,
            )
            # Delete the event
            await conn.execute("DELETE FROM events WHERE id = $1", event_id)

        # Also delete from OpenSearch (best-effort)
        if stores.opensearch and stores.opensearch.available:
            try:
                await stores.opensearch.delete_document("legba-events-*", str(event_id))
            except Exception:
                pass

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
                "SELECT data FROM events WHERE id = $1", event_id,
            )
            if not row:
                return HTMLResponse('<div class="text-red-400 text-sm p-2">Event not found.</div>', status_code=404)

            data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
            data["category"] = category
            data["confidence"] = confidence
            data["tags"] = [t.strip() for t in tags.split(",") if t.strip()]

            await conn.execute(
                "UPDATE events SET data = $1::jsonb, updated_at = now() WHERE id = $2",
                json.dumps(data, default=str), event_id,
            )

        return HTMLResponse(
            '<div class="text-green-400 text-sm p-2">Event updated.</div>'
            '<script>setTimeout(() => location.reload(), 500)</script>'
        )
    except Exception as e:
        return HTMLResponse(f'<div class="text-red-400 text-sm p-2">Error: {e}</div>', status_code=500)
