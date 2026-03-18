"""Entity Explorer routes — CRUD for entities."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from uuid import UUID

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse

from ..app import get_stores, templates
from ...shared.schemas.entity_profiles import EntityType

logger = logging.getLogger(__name__)

router = APIRouter()

ENTITY_TYPES = [t.value for t in EntityType]

# Predicates used to build the profile summary card per entity type.
# Keys are assertion keys (case-insensitive match), values are display labels.
_PROFILE_KEYS: dict[str, dict[str, str]] = {
    "country": {
        "capital": "Capital",
        "capitalof": "Capital",
        "population": "Population",
        "populationof": "Population",
        "region": "Region",
        "continent": "Continent",
        "leader": "Leader",
        "leaderof": "Leader",
        "headofstate": "Head of State",
        "government_type": "Government",
        "governmenttype": "Government",
        "gdp": "GDP",
        "currency": "Currency",
        "official_language": "Language",
        "language": "Language",
    },
    "person": {
        "role": "Role",
        "title": "Title",
        "position": "Position",
        "affiliation": "Affiliation",
        "memberof": "Affiliation",
        "nationality": "Nationality",
        "citizenship": "Citizenship",
        "birthdate": "Born",
        "age": "Age",
    },
    "organization": {
        "type": "Type",
        "orgtype": "Type",
        "headquarters": "HQ",
        "headquarteredin": "HQ",
        "leader": "Leader",
        "leaderof": "Leader",
        "ceo": "CEO",
        "founded": "Founded",
        "members": "Members",
        "sector": "Sector",
    },
}
# Aliases for types that share the same profile keys
for _alias in ("international_org", "corporation", "armed_group"):
    _PROFILE_KEYS[_alias] = _PROFILE_KEYS["organization"]


def _extract_profile_summary(
    entity_type: str, sections: dict[str, list],
) -> list[dict[str, str]]:
    """Extract key profile facts from sections for the summary card.

    Returns a list of {"label": ..., "value": ...} dicts, deduplicated by label.
    """
    key_map = _PROFILE_KEYS.get(entity_type, {})
    if not key_map:
        return []

    seen_labels: set[str] = set()
    results: list[dict[str, str]] = []

    for _section_name, assertions in sections.items():
        for a in assertions:
            if isinstance(a, dict):
                akey = a.get("key", "")
                aval = a.get("value", "")
                superseded = a.get("superseded", False)
                confidence = a.get("confidence", 0.0)
            else:
                akey = a.key
                aval = a.value
                superseded = a.superseded
                confidence = a.confidence

            if superseded:
                continue

            normalized = akey.lower().replace(" ", "").replace("_", "")
            label = key_map.get(normalized)
            if label and label not in seen_labels:
                seen_labels.add(label)
                results.append({"label": label, "value": str(aval), "confidence": confidence})

    return results


def _group_facts_by_key(sections: dict[str, list]) -> list[dict]:
    """Group all active assertions by their key for card display.

    Returns a list of {"key": ..., "facts": [...]} sorted by key.
    """
    groups: dict[str, list] = defaultdict(list)

    for section_name, assertions in sections.items():
        for a in assertions:
            if isinstance(a, dict):
                if a.get("superseded", False):
                    continue
                groups[a.get("key", "unknown")].append({
                    "section": section_name,
                    "value": a.get("value", ""),
                    "confidence": a.get("confidence", 0.0),
                    "source_event_id": a.get("source_event_id"),
                    "source_url": a.get("source_url", ""),
                    "observed_at": a.get("observed_at"),
                    "key": a.get("key", ""),
                })
            else:
                if a.superseded:
                    continue
                groups[a.key].append({
                    "section": section_name,
                    "value": a.value,
                    "confidence": a.confidence,
                    "source_event_id": a.source_event_id,
                    "source_url": a.source_url,
                    "observed_at": a.observed_at,
                    "key": a.key,
                })

    return sorted(
        [{"key": k, "facts": v} for k, v in groups.items()],
        key=lambda g: g["key"].lower(),
    )


async def _fetch_entity_situations(pool, entity_name: str, limit: int = 5) -> list[dict]:
    """Fetch situations where key_entities includes this entity name."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, status, category, intensity_score, updated_at "
                "FROM situations "
                "WHERE data->'key_entities' @> $1::jsonb "
                "ORDER BY intensity_score DESC NULLS LAST, updated_at DESC "
                "LIMIT $2",
                json.dumps([entity_name]),
                limit,
            )
            return [
                {
                    "id": str(row["id"]),
                    "name": row["name"],
                    "status": row["status"],
                    "category": row["category"],
                    "intensity_score": row["intensity_score"] or 0.0,
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]
    except Exception as exc:
        logger.debug("Situation fetch for entity %r failed: %s", entity_name, exc)
        return []


PAGE_SIZE = 50


async def _query_entities_paged(stores, q, entity_type, offset):
    """Query entities with pagination. Returns (entities, total)."""
    from ...shared.schemas.entity_profiles import EntityProfile

    if not stores.structured._available:
        return [], 0

    conditions = []
    params = []
    idx = 1

    if q:
        conditions.append(f"LOWER(canonical_name) LIKE LOWER(${idx})")
        params.append(f"%{q}%")
        idx += 1
    if entity_type:
        conditions.append(f"entity_type = ${idx}")
        params.append(entity_type)
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    try:
        async with stores.structured._pool.acquire() as conn:
            total = await conn.fetchval(
                f"SELECT count(*) FROM entity_profiles {where}", *params,
            )
            rows = await conn.fetch(
                f"SELECT data FROM entity_profiles {where} "
                f"ORDER BY updated_at DESC "
                f"LIMIT ${idx} OFFSET ${idx + 1}",
                *params, PAGE_SIZE, offset,
            )
            entities = []
            for row in rows:
                try:
                    entities.append(EntityProfile.model_validate_json(row["data"]))
                except Exception as e:
                    logger.debug("Entity profile parse failed: %s", e)
                    total -= 1  # Don't count invalid rows
            return entities, total
    except Exception as e:
        logger.warning("Entity paged query failed: %s", e)
        return [], 0


def _entity_context(request, entities, total, q, entity_type, offset):
    return {
        "request": request,
        "active_page": "entities",
        "entities": entities,
        "total": total,
        "q": q,
        "entity_type": entity_type,
        "entity_types": ENTITY_TYPES,
        "offset": offset,
        "page_size": PAGE_SIZE,
        "has_more": (offset + PAGE_SIZE) < total,
        "next_offset": offset + PAGE_SIZE,
    }


@router.get("/entities")
async def entity_list(
    request: Request,
    q: str | None = None,
    entity_type: str | None = None,
    offset: int = 0,
):
    stores = get_stores(request)
    entities, total = await _query_entities_paged(stores, q, entity_type, offset)
    context = _entity_context(request, entities, total, q, entity_type, offset)

    if request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"):
        return templates.TemplateResponse("entities/_rows.html", context)
    return templates.TemplateResponse("entities/list.html", context)


@router.get("/entities/rows")
async def entity_rows(
    request: Request,
    q: str | None = None,
    entity_type: str | None = None,
    offset: int = 0,
):
    stores = get_stores(request)
    entities, total = await _query_entities_paged(stores, q, entity_type, offset)
    context = _entity_context(request, entities, total, q, entity_type, offset)
    return templates.TemplateResponse("entities/_rows.html", context)


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
                "situations": [],
                "profile_summary": [],
                "fact_groups": [],
                "total_facts": 0,
            },
        )

    # Parallel: graph relationships + situations for this entity
    relationships_task = stores.graph.get_relationships(
        entity.canonical_name, direction="both", limit=50
    )
    async def _empty_list() -> list:
        return []

    situations_task = (
        _fetch_entity_situations(stores.structured._pool, entity.canonical_name, limit=5)
        if stores.structured._available
        else _empty_list()
    )
    relationships, situations = await asyncio.gather(
        relationships_task, situations_task, return_exceptions=True,
    )
    if isinstance(relationships, BaseException):
        logger.debug("Relationship fetch failed: %s", relationships)
        relationships = []
    if isinstance(situations, BaseException):
        logger.debug("Situation fetch failed: %s", situations)
        situations = []

    # Parse linked events into template-friendly dicts
    from ...shared.schemas.signals import Signal as Event
    linked_events = []
    for raw in linked_events_raw:
        try:
            evt = Event.model_validate(raw["event"])
            linked_events.append({
                "event": evt,
                "role": raw.get("role", "mentioned"),
                "confidence": raw.get("confidence", 0.0),
            })
        except Exception as e:
            logger.debug("Linked event parse failed: %s", e)
            continue

    # Build profile summary and grouped facts for the new layout
    entity_type_val = entity.entity_type.value if entity.entity_type else "other"
    profile_summary = _extract_profile_summary(entity_type_val, entity.sections)
    fact_groups = _group_facts_by_key(entity.sections)
    total_facts = sum(len(g["facts"]) for g in fact_groups)

    return templates.TemplateResponse(
        "entities/detail.html",
        {
            "request": request,
            "active_page": "entities",
            "entity": entity,
            "linked_events": linked_events,
            "relationships": relationships,
            "versions": versions,
            "situations": situations,
            "profile_summary": profile_summary,
            "fact_groups": fact_groups,
            "total_facts": total_facts,
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


# ------------------------------------------------------------------
# Entity merge
# ------------------------------------------------------------------

@router.post("/api/entities/merge")
async def merge_entities(request: Request):
    """Merge two entities. Body: {keep_id, remove_id, preview: bool}

    When preview=true (default), returns counts of what would change.
    When preview=false, executes the merge: reassigns event links and
    facts, deletes the old profile and versions, remaps graph edges.
    """
    stores = get_stores(request)

    try:
        body = await request.json()
    except Exception as e:
        logger.debug("Entity merge: invalid JSON body: %s", e)
        return JSONResponse({"success": False, "error": "Invalid JSON body"}, status_code=400)

    keep_id = body.get("keep_id")
    remove_id = body.get("remove_id")
    preview = body.get("preview", True)

    if not keep_id or not remove_id:
        return JSONResponse(
            {"success": False, "error": "Both keep_id and remove_id are required"},
            status_code=400,
        )

    result = await stores.structured.merge_entities(
        keep_id=keep_id,
        remove_id=remove_id,
        preview=preview,
        graph_store=stores.graph,
    )

    status = 200 if result.get("success") else 400
    return JSONResponse(result, status_code=status)
