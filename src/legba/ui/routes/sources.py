"""Source Registry routes — CRUD for sources."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

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
            "statuses": STATUSES,
            "source_types": SOURCE_TYPES,
        },
    )


# ------------------------------------------------------------------
# Write operations
# ------------------------------------------------------------------

@router.put("/api/sources/{source_id}/status")
async def update_source_status(
    request: Request,
    source_id: UUID,
    status: str = Form(...),
):
    """Change a source's status (active/paused/retired/error)."""
    if status not in STATUSES:
        return HTMLResponse(f'<span class="text-red-400 text-xs">Invalid status: {status}</span>', status_code=400)
    stores = get_stores(request)
    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute(
                "UPDATE sources SET status = $1, data = jsonb_set(data, '{status}', to_jsonb($1::text)), "
                "updated_at = now() WHERE id = $2",
                status, source_id,
            )
        status_colors = {'active': 'badge-green', 'paused': 'badge-yellow', 'error': 'badge-red', 'retired': 'badge-gray'}
        return HTMLResponse(
            f'<span class="badge {status_colors.get(status, "badge-gray")}">{status}</span>'
        )
    except Exception as e:
        return HTMLResponse(f'<span class="text-red-400 text-xs">Error: {e}</span>', status_code=500)


@router.post("/api/sources")
async def create_source(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    source_type: str = Form("rss"),
    reliability: float = Form(0.5),
    bias_label: str = Form("center"),
    tags: str = Form(""),
    description: str = Form(""),
):
    """Create a new source."""
    stores = get_stores(request)
    try:
        import json
        from uuid import uuid4
        from datetime import datetime, timezone
        source_id = uuid4()
        now = datetime.now(timezone.utc)
        data = {
            "id": str(source_id),
            "name": name,
            "url": url,
            "source_type": source_type,
            "status": "active",
            "reliability": reliability,
            "bias_label": bias_label,
            "tags": [t.strip() for t in tags.split(",") if t.strip()],
            "description": description,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "event_count": 0,
            "fetch_count": 0,
            "fail_count": 0,
        }
        async with stores.structured._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO sources (id, data, status, source_type, created_at, updated_at) "
                "VALUES ($1, $2::jsonb, $3, $4, $5, $5)",
                source_id, json.dumps(data, default=str), "active", source_type, now,
            )
        return RedirectResponse(url=f"/sources/{source_id}", status_code=303)
    except Exception as e:
        return HTMLResponse(f'<div class="text-red-400 text-sm p-2">Error creating source: {e}</div>', status_code=500)


@router.delete("/api/sources/{source_id}")
async def delete_source(request: Request, source_id: UUID):
    """Delete a source permanently."""
    stores = get_stores(request)
    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute("DELETE FROM sources WHERE id = $1", source_id)
        return HTMLResponse('<div class="text-green-400 text-sm p-2">Source deleted.</div>')
    except Exception as e:
        return HTMLResponse(f'<div class="text-red-400 text-sm p-2">Error: {e}</div>', status_code=500)
