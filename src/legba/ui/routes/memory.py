"""Memory Explorer routes — GET /memory + GET /memory/rows."""

from __future__ import annotations

from fastapi import APIRouter, Request

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
