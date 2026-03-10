"""Entity Explorer routes — CRUD for entities."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse

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


# ------------------------------------------------------------------
# Write operations (U9: add/remove assertions)
# ------------------------------------------------------------------

@router.post("/api/entities/{entity_id}/assertions")
async def add_assertion(
    request: Request,
    entity_id: UUID,
    section: str = Form("general"),
    key: str = Form(...),
    value: str = Form(...),
    confidence: float = Form(0.7),
):
    """Add an assertion to an entity profile."""
    stores = get_stores(request)
    try:
        async with stores.structured._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data FROM entity_profiles WHERE id = $1", entity_id,
            )
            if not row:
                return HTMLResponse('<div class="text-red-400 text-sm p-2">Entity not found.</div>', status_code=404)

            data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
            sections = data.get("sections", {})
            section_list = sections.get(section, [])

            from datetime import datetime, timezone
            section_list.append({
                "key": key,
                "value": value,
                "confidence": confidence,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "source": "operator",
                "superseded": False,
            })
            sections[section] = section_list
            data["sections"] = sections

            await conn.execute(
                "UPDATE entity_profiles SET data = $1::jsonb, updated_at = now() WHERE id = $2",
                json.dumps(data, default=str), entity_id,
            )

        return HTMLResponse(
            f'<div class="text-green-400 text-sm p-2">Assertion added: {key} = {value}</div>'
            f'<script>setTimeout(() => location.reload(), 500)</script>'
        )
    except Exception as e:
        return HTMLResponse(f'<div class="text-red-400 text-sm p-2">Error: {e}</div>', status_code=500)


@router.delete("/api/entities/{entity_id}/assertions")
async def remove_assertion(
    request: Request,
    entity_id: UUID,
    section: str = "",
    key: str = "",
    value: str = "",
):
    """Remove a specific assertion from an entity profile."""
    stores = get_stores(request)
    try:
        async with stores.structured._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data FROM entity_profiles WHERE id = $1", entity_id,
            )
            if not row:
                return HTMLResponse('<div class="text-red-400 text-sm p-2">Entity not found.</div>', status_code=404)

            data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
            sections = data.get("sections", {})
            section_list = sections.get(section, [])

            # Filter out the matching assertion
            sections[section] = [
                a for a in section_list
                if not (a.get("key") == key and a.get("value") == value)
            ]
            # Remove section if empty
            if not sections[section]:
                del sections[section]
            data["sections"] = sections

            await conn.execute(
                "UPDATE entity_profiles SET data = $1::jsonb, updated_at = now() WHERE id = $2",
                json.dumps(data, default=str), entity_id,
            )

        return HTMLResponse('<div class="text-green-400 text-sm p-2">Assertion removed.</div>')
    except Exception as e:
        return HTMLResponse(f'<div class="text-red-400 text-sm p-2">Error: {e}</div>', status_code=500)
