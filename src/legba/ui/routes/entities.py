"""Entity Explorer routes — GET /entities + GET /entities/{entity_id}."""

from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, Request

from ..app import get_stores, templates
from ...shared.schemas.entity_profiles import EntityType

router = APIRouter()

ENTITY_TYPES = [t.value for t in EntityType]


@router.get("/entities")
async def entity_list(
    request: Request,
    q: str | None = None,
    entity_type: str | None = None,
):
    stores = get_stores(request)
    entities = await stores.structured.search_entity_profiles(
        query=q or None,
        entity_type=entity_type or None,
        limit=50,
    )
    total = await stores.count_entities()

    context = {
        "request": request,
        "active_page": "entities",
        "entities": entities,
        "total": total,
        "q": q,
        "entity_type": entity_type,
        "entity_types": ENTITY_TYPES,
    }

    if request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"):
        return templates.TemplateResponse("entities/_rows.html", context)
    return templates.TemplateResponse("entities/list.html", context)


@router.get("/entities/{entity_id}")
async def entity_detail(request: Request, entity_id: UUID):
    stores = get_stores(request)

    entity, linked_events_raw, versions = await asyncio.gather(
        stores.structured.get_entity_profile(entity_id),
        stores.structured.get_entity_events(entity_id, limit=20),
        stores.list_entity_versions(entity_id),
    )

    if entity is None:
        return templates.TemplateResponse(
            "entities/detail.html",
            {
                "request": request,
                "active_page": "entities",
                "entity": None,
                "linked_events": [],
                "relationships": [],
                "versions": [],
            },
        )

    # Fetch graph relationships (needs canonical_name, may fail gracefully)
    relationships = await stores.graph.get_relationships(
        entity.canonical_name, direction="both", limit=50
    )

    # Parse linked events into template-friendly dicts
    from ...shared.schemas.events import Event
    linked_events = []
    for raw in linked_events_raw:
        try:
            evt = Event.model_validate(raw["event"])
            linked_events.append({
                "event": evt,
                "role": raw.get("role", "mentioned"),
                "confidence": raw.get("confidence", 0.0),
            })
        except Exception:
            continue

    return templates.TemplateResponse(
        "entities/detail.html",
        {
            "request": request,
            "active_page": "entities",
            "entity": entity,
            "linked_events": linked_events,
            "relationships": relationships,
            "versions": versions,
        },
    )
