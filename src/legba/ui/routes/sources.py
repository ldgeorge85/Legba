"""Source Registry routes — GET /sources + GET /sources/{source_id}."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request

from ..app import get_stores, templates
from ...shared.schemas.sources import SourceStatus, SourceType

router = APIRouter()

STATUSES = [s.value for s in SourceStatus]
SOURCE_TYPES = [t.value for t in SourceType]


@router.get("/sources")
async def source_list(
    request: Request,
    status: str | None = None,
    source_type: str | None = None,
):
    stores = get_stores(request)
    sources = await stores.structured.get_sources(
        status=status or "active",
        source_type=source_type or None,
        limit=200,
    )
    total = await stores.count_sources()

    context = {
        "request": request,
        "active_page": "sources",
        "sources": sources,
        "total": total,
        "status": status,
        "source_type": source_type,
        "statuses": STATUSES,
        "source_types": SOURCE_TYPES,
    }

    if request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"):
        return templates.TemplateResponse("sources/_rows.html", context)
    return templates.TemplateResponse("sources/list.html", context)


@router.get("/sources/{source_id}")
async def source_detail(request: Request, source_id: UUID):
    stores = get_stores(request)
    source = await stores.structured.get_source(source_id)

    recent_events = []
    if source:
        recent_events = await stores.structured.query_events(
            source_id=source_id, limit=10
        )

    return templates.TemplateResponse(
        "sources/detail.html",
        {
            "request": request,
            "active_page": "sources",
            "source": source,
            "recent_events": recent_events,
        },
    )
