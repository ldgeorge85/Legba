"""Fact Explorer routes — GET /facts + GET /facts/rows."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..app import get_stores, templates

router = APIRouter()

PAGE_SIZE = 50


async def _query_facts(stores, q: str | None, min_confidence: float | None, offset: int):
    """Query facts from Postgres with optional search and confidence filter."""
    if not stores.structured._available:
        return [], 0

    conditions = []
    params = []
    idx = 1

    if q:
        conditions.append(f"(subject ILIKE ${idx} OR predicate ILIKE ${idx})")
        params.append(f"%{q}%")
        idx += 1

    if min_confidence is not None:
        conditions.append(f"confidence >= ${idx}")
        params.append(min_confidence)
        idx += 1

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    try:
        async with stores.structured._pool.acquire() as conn:
            count_sql = f"SELECT count(*) FROM facts{where}"
            total = await conn.fetchval(count_sql, *params)

            query_sql = (
                f"SELECT id, subject, predicate, value, confidence, source_cycle, created_at "
                f"FROM facts{where} "
                f"ORDER BY created_at DESC "
                f"LIMIT ${idx} OFFSET ${idx + 1}"
            )
            rows = await conn.fetch(query_sql, *params, PAGE_SIZE, offset)

            facts = [dict(row) for row in rows]
            return facts, total
    except Exception:
        return [], 0


@router.get("/facts")
async def fact_list(
    request: Request,
    q: str | None = None,
    min_confidence: str | None = None,
    offset: int = 0,
):
    stores = get_stores(request)

    conf_val = float(min_confidence) if min_confidence else None
    facts, total = await _query_facts(stores, q or None, conf_val, offset)

    context = {
        "request": request,
        "active_page": "facts",
        "facts": facts,
        "total": total,
        "q": q,
        "min_confidence": min_confidence or "",
        "offset": offset,
        "page_size": PAGE_SIZE,
        "has_more": (offset + PAGE_SIZE) < total,
        "next_offset": offset + PAGE_SIZE,
    }

    if request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"):
        return templates.TemplateResponse("facts/_rows.html", context)
    return templates.TemplateResponse("facts/list.html", context)


@router.get("/facts/rows")
async def fact_rows(
    request: Request,
    q: str | None = None,
    min_confidence: str | None = None,
    offset: int = 0,
):
    stores = get_stores(request)

    conf_val = float(min_confidence) if min_confidence else None
    facts, total = await _query_facts(stores, q or None, conf_val, offset)

    context = {
        "request": request,
        "active_page": "facts",
        "facts": facts,
        "total": total,
        "q": q,
        "min_confidence": min_confidence or "",
        "offset": offset,
        "page_size": PAGE_SIZE,
        "has_more": (offset + PAGE_SIZE) < total,
        "next_offset": offset + PAGE_SIZE,
    }

    return templates.TemplateResponse("facts/_rows.html", context)
