"""Dashboard route — GET / and GET /partials/stats."""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, Request

from ..app import get_stores, templates

router = APIRouter()


async def _fetch_stats(request: Request) -> dict:
    stores = get_stores(request)
    (
        cycle_number,
        entity_count,
        event_count,
        source_count,
        goal_count,
        rel_count,
        fact_count,
        situation_count,
        watchlist_count,
        last_ingestion_cycle,
    ) = await asyncio.gather(
        stores.registers.get_int("cycle_number", 0),
        stores.count_entities(),
        stores.count_events(),
        stores.count_sources(),
        stores.count_goals(),
        stores.count_relationships(),
        stores.count_facts(),
        stores.count_situations(statuses=("active", "escalating")),
        stores.count_watchlist(),
        stores.registers.get_int("last_ingestion_cycle", 0),
    )
    return {
        "cycle_number": cycle_number,
        "entity_count": entity_count,
        "event_count": event_count,
        "source_count": source_count,
        "goal_count": goal_count,
        "relationship_count": rel_count,
        "fact_count": fact_count,
        "situation_count": situation_count,
        "watchlist_count": watchlist_count,
        "last_ingestion_cycle": last_ingestion_cycle,
    }


def _load_cycle_response():
    shared = os.environ.get("LEGBA_SHARED", "/shared")
    path = os.path.join(shared, "response.json")
    try:
        with open(path) as f:
            data = json.load(f)
        from ...shared.schemas.cycle import CycleResponse
        return CycleResponse.model_validate(data)
    except Exception:
        return None


@router.get("/")
async def dashboard(request: Request):
    stores = get_stores(request)
    stats, active_situations = await asyncio.gather(
        _fetch_stats(request),
        stores.fetch_active_situations(limit=5),
    )
    cycle_response = _load_cycle_response()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "active_page": "dashboard",
            "cycle_response": cycle_response,
            "active_situations": active_situations,
            **stats,
        },
    )


@router.get("/partials/stats")
async def stats_partial(request: Request):
    stats = await _fetch_stats(request)
    return templates.TemplateResponse(
        "partials/_stats.html",
        {"request": request, **stats},
    )
