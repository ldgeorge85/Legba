"""Situations route — CRUD for tracked situations."""

from __future__ import annotations

import json
from uuid import UUID, uuid4
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from ..app import get_stores, templates

router = APIRouter()

_VALID_STATUSES = ("active", "escalating", "de_escalating", "dormant", "resolved")


async def _ensure_tables(pool) -> None:
    """Create situation tables if they don't exist."""
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS situations (
                id UUID PRIMARY KEY,
                data JSONB NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                category TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_event_at TIMESTAMPTZ,
                event_count INTEGER NOT NULL DEFAULT 0,
                intensity_score REAL NOT NULL DEFAULT 0.0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS situation_events (
                situation_id UUID NOT NULL REFERENCES situations(id),
                event_id UUID NOT NULL REFERENCES events(id),
                relevance REAL NOT NULL DEFAULT 1.0,
                added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (situation_id, event_id)
            )
        """)


async def _fetch_situations(stores, status: str | None = None) -> list[dict]:
    """Fetch all situations from Postgres."""
    if not stores.structured._available:
        return []
    try:
        await _ensure_tables(stores.structured._pool)
        async with stores.structured._pool.acquire() as conn:
            if status:
                rows = await conn.fetch(
                    "SELECT id, data, name, status, category, event_count, "
                    "intensity_score, last_event_at, created_at, updated_at "
                    "FROM situations WHERE status = $1 "
                    "ORDER BY updated_at DESC",
                    status,
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, data, name, status, category, event_count, "
                    "intensity_score, last_event_at, created_at, updated_at "
                    "FROM situations ORDER BY updated_at DESC"
                )
    except Exception:
        return []

    situations = []
    for row in rows:
        raw = row["data"]
        data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
        situations.append({
            "id": str(row["id"]),
            "name": row["name"],
            "status": row["status"],
            "category": row["category"],
            "description": data.get("description", ""),
            "key_entities": data.get("key_entities", []),
            "regions": data.get("regions", []),
            "tags": data.get("tags", []),
            "event_count": row["event_count"],
            "intensity_score": row["intensity_score"] or 0.0,
            "last_event_at": row["last_event_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
    return situations


@router.get("/situations")
async def situation_list(request: Request, status: str | None = None):
    stores = get_stores(request)
    situations = await _fetch_situations(stores, status=status)

    active = sum(1 for s in situations if s["status"] == "active")
    escalating = sum(1 for s in situations if s["status"] == "escalating")
    dormant = sum(1 for s in situations if s["status"] == "dormant")

    return templates.TemplateResponse("situations/list.html", {
        "request": request,
        "active_page": "situations",
        "situations": situations,
        "total": len(situations),
        "active_count": active,
        "escalating_count": escalating,
        "dormant_count": dormant,
        "status_filter": status,
    })


@router.get("/situations/{situation_id}")
async def situation_detail(request: Request, situation_id: UUID):
    stores = get_stores(request)

    situation = None
    linked_events = []

    if not stores.structured._available:
        return templates.TemplateResponse("situations/detail.html", {
            "request": request,
            "active_page": "situations",
            "situation": None,
            "linked_events": [],
        })

    try:
        await _ensure_tables(stores.structured._pool)
        async with stores.structured._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, data, name, status, category, event_count, "
                "intensity_score, last_event_at, created_at, updated_at "
                "FROM situations WHERE id = $1",
                situation_id,
            )
            if row:
                raw = row["data"]
                data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
                situation = {
                    "id": str(row["id"]),
                    "name": row["name"],
                    "status": row["status"],
                    "category": row["category"],
                    "description": data.get("description", ""),
                    "key_entities": data.get("key_entities", []),
                    "regions": data.get("regions", []),
                    "tags": data.get("tags", []),
                    "event_count": row["event_count"],
                    "intensity_score": row["intensity_score"] or 0.0,
                    "last_event_at": row["last_event_at"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }

                # Fetch linked events
                event_rows = await conn.fetch(
                    "SELECT e.id, e.data, se.relevance, se.added_at "
                    "FROM situation_events se "
                    "JOIN events e ON e.id = se.event_id "
                    "WHERE se.situation_id = $1 "
                    "ORDER BY se.added_at DESC",
                    situation_id,
                )
                for erow in event_rows:
                    eraw = erow["data"]
                    edata = eraw if isinstance(eraw, dict) else json.loads(eraw) if isinstance(eraw, str) else {}
                    linked_events.append({
                        "id": str(erow["id"]),
                        "title": edata.get("title", "Untitled"),
                        "category": edata.get("category", "other"),
                        "event_timestamp": edata.get("event_timestamp"),
                        "relevance": erow["relevance"],
                        "added_at": erow["added_at"],
                    })
    except Exception:
        pass

    return templates.TemplateResponse("situations/detail.html", {
        "request": request,
        "active_page": "situations",
        "situation": situation,
        "linked_events": linked_events,
    })


# ------------------------------------------------------------------
# Write operations
# ------------------------------------------------------------------

@router.post("/api/situations")
async def create_situation(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    category: str = Form(""),
    key_entities: str = Form(""),
    regions: str = Form(""),
    tags: str = Form(""),
):
    """Create a new situation."""
    stores = get_stores(request)
    try:
        await _ensure_tables(stores.structured._pool)
        sit_id = uuid4()
        now = datetime.now(timezone.utc)

        ents = [e.strip() for e in key_entities.split(",") if e.strip()] if key_entities else []
        regs = [r.strip() for r in regions.split(",") if r.strip()] if regions else []
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

        data = {
            "id": str(sit_id),
            "name": name,
            "description": description,
            "status": "active",
            "category": category,
            "key_entities": ents,
            "regions": regs,
            "tags": tag_list,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "event_count": 0,
            "intensity_score": 0.0,
        }

        async with stores.structured._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO situations (id, data, name, status, category, created_at, updated_at) "
                "VALUES ($1, $2::jsonb, $3, 'active', $4, $5, $5)",
                sit_id, json.dumps(data, default=str), name, category, now,
            )

        return RedirectResponse(url="/situations", status_code=303)
    except Exception as e:
        return HTMLResponse(f'<div class="text-red-400 text-sm p-2">Error: {e}</div>', status_code=500)


@router.put("/api/situations/{situation_id}")
async def update_situation_status(
    request: Request,
    situation_id: UUID,
    status: str = Form(...),
):
    """Change a situation's status."""
    if status not in _VALID_STATUSES:
        return HTMLResponse(
            f'<span class="text-red-400 text-xs">Invalid status. Use: {", ".join(_VALID_STATUSES)}</span>',
            status_code=400,
        )
    stores = get_stores(request)
    try:
        now = datetime.now(timezone.utc)
        async with stores.structured._pool.acquire() as conn:
            # Update denormalized column
            await conn.execute(
                "UPDATE situations SET status = $1, updated_at = $2 WHERE id = $3",
                status, now, situation_id,
            )
            # Update JSONB data
            row = await conn.fetchrow("SELECT data FROM situations WHERE id = $1", situation_id)
            if row:
                raw = row["data"]
                data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
                data["status"] = status
                data["updated_at"] = now.isoformat()
                await conn.execute(
                    "UPDATE situations SET data = $1::jsonb WHERE id = $2",
                    json.dumps(data, default=str), situation_id,
                )

        colors = {
            "active": "badge-blue",
            "escalating": "badge-red",
            "de_escalating": "badge-green",
            "dormant": "badge-gray",
            "resolved": "badge-gray",
        }
        label = status.replace("_", " ")
        return HTMLResponse(f'<span class="badge {colors.get(status, "badge-gray")}">{label}</span>')
    except Exception as e:
        return HTMLResponse(f'<span class="text-red-400 text-xs">Error: {e}</span>', status_code=500)


@router.delete("/api/situations/{situation_id}")
async def delete_situation(request: Request, situation_id: UUID):
    """Delete a situation and its event links."""
    stores = get_stores(request)
    try:
        async with stores.structured._pool.acquire() as conn:
            # Delete event links first (FK)
            await conn.execute(
                "DELETE FROM situation_events WHERE situation_id = $1", situation_id,
            )
            await conn.execute("DELETE FROM situations WHERE id = $1", situation_id)

        return HTMLResponse(
            '<div class="text-green-400 text-sm p-2">Situation deleted.</div>'
            '<script>setTimeout(() => window.location="/situations", 800)</script>'
        )
    except Exception as e:
        return HTMLResponse(f'<div class="text-red-400 text-sm p-2">Error: {e}</div>', status_code=500)
