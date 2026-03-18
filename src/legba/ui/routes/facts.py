"""Fact Explorer routes — CRUD for facts."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse

from ..app import get_stores, templates

logger = logging.getLogger(__name__)

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
    except Exception as e:
        logger.warning("Facts query failed: %s", e)
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


# ------------------------------------------------------------------
# Write operations (U6: delete, U7: inline edit)
# ------------------------------------------------------------------

@router.delete("/api/facts/{fact_id}")
async def delete_fact(request: Request, fact_id: UUID):
    """Delete a fact permanently."""
    stores = get_stores(request)
    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute("DELETE FROM facts WHERE id = $1", fact_id)
        return HTMLResponse('<div class="text-green-400 text-sm p-2">Fact deleted.</div>')
    except Exception as e:
        return HTMLResponse(f'<div class="text-red-400 text-sm p-2">Error: {e}</div>', status_code=500)


@router.put("/api/facts/{fact_id}")
async def update_fact(
    request: Request,
    fact_id: UUID,
    subject: str = Form(...),
    predicate: str = Form(...),
    value: str = Form(...),
    confidence: float = Form(...),
):
    """Update a fact inline."""
    stores = get_stores(request)
    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute(
                "UPDATE facts SET subject = $1, predicate = $2, value = $3, "
                "confidence = $4 WHERE id = $5",
                subject, predicate, value, confidence, fact_id,
            )
        # Return the updated row HTML
        fact = {
            "id": fact_id,
            "subject": subject,
            "predicate": predicate,
            "value": value,
            "confidence": confidence,
        }
        return templates.TemplateResponse("facts/_row_single.html", {
            "request": request, "fact": fact,
        })
    except Exception as e:
        return HTMLResponse(f'<div class="text-red-400 text-sm p-2">Error: {e}</div>', status_code=500)
