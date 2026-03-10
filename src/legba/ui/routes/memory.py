"""Memory Explorer routes — GET /memory + DELETE /api/memory."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..app import get_stores, templates

router = APIRouter()

COLLECTION_MAP = {
    "short_term": "legba_short_term",
    "long_term": "legba_long_term",
}


@router.get("/memory")
async def memory_list(
    request: Request,
    collection: str = "short_term",
    q: str | None = None,
    offset: str | None = None,
):
    stores = get_stores(request)

    col_name = COLLECTION_MAP.get(collection, COLLECTION_MAP["short_term"])

    memories: list = []
    next_offset: str | None = None

    if q and q.strip():
        # Semantic search — no pagination (returns top-k ranked by similarity)
        memories = await stores.search_memories(col_name, q.strip(), limit=50)
    else:
        memories, next_offset = await stores.get_memories(
            col_name, limit=50, offset=offset or None
        )

    total_short = await stores.count_memories(COLLECTION_MAP["short_term"])
    total_long = await stores.count_memories(COLLECTION_MAP["long_term"])

    context = {
        "request": request,
        "active_page": "memory",
        "memories": memories,
        "collection": collection,
        "q": q,
        "offset": offset,
        "next_offset": next_offset,
        "total_short": total_short,
        "total_long": total_long,
    }

    if request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"):
        return templates.TemplateResponse("memory/_rows.html", context)
    return templates.TemplateResponse("memory/list.html", context)


# ------------------------------------------------------------------
# Write operations (U8: delete memory episode)
# ------------------------------------------------------------------

@router.delete("/api/memory/{collection}/{point_id}")
async def delete_memory(request: Request, collection: str, point_id: str):
    """Delete a memory episode from Qdrant."""
    stores = get_stores(request)
    col_name = COLLECTION_MAP.get(collection)
    if not col_name:
        return HTMLResponse('<div class="text-red-400 text-sm p-2">Invalid collection.</div>', status_code=400)

    if not stores._qdrant_available:
        return HTMLResponse('<div class="text-red-400 text-sm p-2">Qdrant unavailable.</div>', status_code=503)

    try:
        def _delete():
            stores._qdrant.delete(
                collection_name=col_name,
                points_selector=[point_id],
            )

        await asyncio.to_thread(_delete)
        return HTMLResponse('<div class="text-green-400 text-sm p-2">Memory deleted.</div>')
    except Exception as e:
        return HTMLResponse(f'<div class="text-red-400 text-sm p-2">Error: {e}</div>', status_code=500)
