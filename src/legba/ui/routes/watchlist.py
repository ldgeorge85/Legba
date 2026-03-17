"""Watchlist routes — CRUD for persistent watch patterns."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse

from ..app import get_stores, templates

router = APIRouter()

PRIORITIES = ["normal", "high", "critical"]

# ---------------------------------------------------------------------------
# DDL — ensure tables exist (idempotent)
# ---------------------------------------------------------------------------

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS watchlist (
    id UUID PRIMARY KEY,
    data JSONB NOT NULL,
    name TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_triggered_at TIMESTAMPTZ,
    trigger_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS watch_triggers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    watch_id UUID NOT NULL REFERENCES watchlist(id) ON DELETE CASCADE,
    signal_id UUID NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
    watch_name TEXT NOT NULL DEFAULT '',
    event_title TEXT NOT NULL DEFAULT '',
    match_reasons JSONB NOT NULL DEFAULT '[]',
    priority TEXT NOT NULL DEFAULT 'normal',
    triggered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_watch_triggers_watch_id ON watch_triggers(watch_id);
CREATE INDEX IF NOT EXISTS idx_watch_triggers_triggered_at ON watch_triggers(triggered_at DESC);
"""

_table_ensured = False


async def _ensure_tables(conn) -> None:
    global _table_ensured
    if _table_ensured:
        return
    await conn.execute(_TABLE_DDL)
    _table_ensured = True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/watchlist")
async def watchlist_page(request: Request):
    """List all watches with trigger counts."""
    stores = get_stores(request)
    watches = []
    total = 0
    active_count = 0
    total_triggers = 0

    if stores.structured._available:
        try:
            async with stores.structured._pool.acquire() as conn:
                await _ensure_tables(conn)
                rows = await conn.fetch(
                    "SELECT id, data, name, priority, active, created_at, "
                    "last_triggered_at, trigger_count "
                    "FROM watchlist ORDER BY "
                    "CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, "
                    "created_at DESC"
                )
                for row in rows:
                    raw = row["data"]
                    data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
                    watches.append({
                        "id": str(row["id"]),
                        "name": row["name"],
                        "priority": row["priority"],
                        "active": row["active"],
                        "entities": data.get("entities", []),
                        "keywords": data.get("keywords", []),
                        "categories": data.get("categories", []),
                        "regions": data.get("regions", []),
                        "description": data.get("description", ""),
                        "trigger_count": row["trigger_count"],
                        "last_triggered_at": row["last_triggered_at"],
                        "created_at": row["created_at"],
                    })
                total = len(watches)
                active_count = sum(1 for w in watches if w["active"])
                total_triggers = sum(w["trigger_count"] for w in watches)
        except Exception:
            pass

    context = {
        "request": request,
        "active_page": "watchlist",
        "watches": watches,
        "total": total,
        "active_count": active_count,
        "total_triggers": total_triggers,
        "priorities": PRIORITIES,
    }
    return templates.TemplateResponse("watchlist/list.html", context)


@router.get("/api/watchlist/triggers")
async def recent_triggers(request: Request):
    """Recent watch triggers (last 20) — returns partial HTML for htmx."""
    stores = get_stores(request)
    triggers = []

    if stores.structured._available:
        try:
            async with stores.structured._pool.acquire() as conn:
                await _ensure_tables(conn)
                rows = await conn.fetch(
                    "SELECT wt.id, wt.watch_id, wt.signal_id AS event_id, wt.data, wt.triggered_at, "
                    "w.name AS watch_name, w.priority "
                    "FROM watch_triggers wt "
                    "JOIN watchlist w ON w.id = wt.watch_id "
                    "ORDER BY wt.triggered_at DESC LIMIT 20"
                )
                for row in rows:
                    raw = row["data"]
                    data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
                    triggers.append({
                        "id": str(row["id"]),
                        "watch_id": str(row["watch_id"]),
                        "watch_name": row["watch_name"],
                        "event_id": str(row["event_id"]),
                        "event_title": data.get("event_title", "Unknown"),
                        "match_reasons": data.get("match_reasons", []),
                        "priority": row["priority"],
                        "triggered_at": row["triggered_at"],
                    })
        except Exception:
            pass

    context = {
        "request": request,
        "triggers": triggers,
    }
    return templates.TemplateResponse("watchlist/triggers.html", context)


@router.post("/api/watchlist")
async def create_watch(
    request: Request,
    name: str = Form(...),
    entities: str = Form(""),
    keywords: str = Form(""),
    categories: str = Form(""),
    regions: str = Form(""),
    priority: str = Form("normal"),
):
    """Create a new watch pattern."""
    stores = get_stores(request)

    name = name.strip()
    if not name:
        return HTMLResponse(
            '<div class="text-red-400 text-sm p-2">Name is required.</div>',
            status_code=400,
        )

    if priority not in PRIORITIES:
        priority = "normal"

    entity_list = [e.strip() for e in entities.split(",") if e.strip()]
    keyword_list = [k.strip() for k in keywords.split(",") if k.strip()]
    category_list = [c.strip() for c in categories.split(",") if c.strip()]
    region_list = [r.strip() for r in regions.split(",") if r.strip()]

    if not entity_list and not keyword_list and not category_list and not region_list:
        return HTMLResponse(
            '<div class="text-red-400 text-sm p-2">At least one criterion (entities, keywords, categories, or regions) is required.</div>',
            status_code=400,
        )

    watch_id = uuid4()
    now = datetime.now(timezone.utc)

    data = {
        "id": str(watch_id),
        "name": name,
        "description": "",
        "entities": entity_list,
        "keywords": keyword_list,
        "categories": category_list,
        "regions": region_list,
        "priority": priority,
        "active": True,
        "created_at": now.isoformat(),
        "last_triggered_at": None,
        "trigger_count": 0,
    }

    try:
        async with stores.structured._pool.acquire() as conn:
            await _ensure_tables(conn)
            await conn.execute(
                "INSERT INTO watchlist (id, data, name, priority, active, created_at) "
                "VALUES ($1, $2::jsonb, $3, $4, true, $5)",
                watch_id, json.dumps(data, default=str), name, priority, now,
            )
    except Exception as e:
        return HTMLResponse(
            f'<div class="text-red-400 text-sm p-2">Error creating watch: {e}</div>',
            status_code=500,
        )

    # Redirect back to the watchlist page
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/watchlist", status_code=303)


@router.delete("/api/watchlist/{watch_id}")
async def delete_watch(request: Request, watch_id: UUID):
    """Delete a watch pattern permanently."""
    stores = get_stores(request)
    try:
        async with stores.structured._pool.acquire() as conn:
            await _ensure_tables(conn)
            await conn.execute("DELETE FROM watchlist WHERE id = $1", watch_id)
        return HTMLResponse('<div class="text-green-400 text-sm p-2">Watch deleted.</div>')
    except Exception as e:
        return HTMLResponse(
            f'<div class="text-red-400 text-sm p-2">Error: {e}</div>',
            status_code=500,
        )
