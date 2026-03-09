"""Goals route — CRUD for goals."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from ..app import get_stores, templates

router = APIRouter()


async def _fetch_goals(stores) -> list[dict]:
    """Fetch all goals and organise into a tree structure."""
    if not stores.structured._available:
        return []
    try:
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, data, status, priority, parent_id, created_at, updated_at "
                "FROM goals ORDER BY priority, created_at"
            )
    except Exception:
        return []

    goals = []
    for row in rows:
        raw = row["data"]
        data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
        goals.append({
            "id": str(row["id"]),
            "description": data.get("description", "Untitled"),
            "status": row["status"],
            "priority": row["priority"],
            "progress": data.get("progress_pct", 0.0),
            "parent_id": str(row["parent_id"]) if row["parent_id"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "children": [],
        })
    return goals


def _build_tree(goals: list[dict]) -> list[dict]:
    """Nest children under parents. Returns top-level goals."""
    by_id = {g["id"]: g for g in goals}
    roots = []
    for g in goals:
        pid = g["parent_id"]
        if pid and pid in by_id:
            by_id[pid]["children"].append(g)
        else:
            roots.append(g)
    return roots


@router.get("/goals")
async def goal_list(request: Request, status: str | None = None):
    stores = get_stores(request)
    all_goals = await _fetch_goals(stores)

    if status:
        all_goals = [g for g in all_goals if g["status"] == status]

    tree = _build_tree(all_goals)
    total = len(all_goals)
    active = sum(1 for g in all_goals if g["status"] == "active")
    completed = sum(1 for g in all_goals if g["status"] == "completed")

    context = {
        "request": request,
        "active_page": "goals",
        "goal_tree": tree,
        "total": total,
        "active_count": active,
        "completed_count": completed,
        "status_filter": status,
    }

    if request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"):
        return templates.TemplateResponse("goals/_tree.html", context)
    return templates.TemplateResponse("goals/list.html", context)


# ------------------------------------------------------------------
# Write operations
# ------------------------------------------------------------------

@router.put("/api/goals/{goal_id}/status")
async def update_goal_status(
    request: Request,
    goal_id: str,
    status: str = Form(...),
):
    """Change a goal's status (active/paused/completed/abandoned)."""
    valid = ("active", "paused", "completed", "abandoned")
    if status not in valid:
        return HTMLResponse(f'<span class="text-red-400 text-xs">Invalid status</span>', status_code=400)
    stores = get_stores(request)
    try:
        from uuid import UUID
        gid = UUID(goal_id)
        async with stores.structured._pool.acquire() as conn:
            await conn.execute(
                "UPDATE goals SET status = $1, updated_at = now() WHERE id = $2",
                status, gid,
            )
        colors = {'active': 'badge-green', 'completed': 'badge-gray', 'paused': 'badge-yellow', 'abandoned': 'badge-red'}
        return HTMLResponse(f'<span class="badge {colors.get(status, "badge-gray")}">{status}</span>')
    except Exception as e:
        return HTMLResponse(f'<span class="text-red-400 text-xs">Error: {e}</span>', status_code=500)


@router.post("/api/goals")
async def create_goal(
    request: Request,
    description: str = Form(...),
    priority: int = Form(3),
    parent_id: str = Form(""),
):
    """Create a new goal."""
    stores = get_stores(request)
    try:
        from uuid import UUID, uuid4
        from datetime import datetime, timezone
        gid = uuid4()
        now = datetime.now(timezone.utc)
        pid = UUID(parent_id) if parent_id else None
        data = {
            "id": str(gid),
            "description": description,
            "progress_pct": 0.0,
            "milestones": [],
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        async with stores.structured._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO goals (id, data, status, goal_type, priority, parent_id, created_at, updated_at) "
                "VALUES ($1, $2::jsonb, 'active', 'operational', $3, $4, $5, $5)",
                gid, json.dumps(data, default=str), priority, pid, now,
            )
        return RedirectResponse(url="/goals", status_code=303)
    except Exception as e:
        return HTMLResponse(f'<div class="text-red-400 text-sm p-2">Error: {e}</div>', status_code=500)


@router.delete("/api/goals/{goal_id}")
async def delete_goal(request: Request, goal_id: str):
    """Delete a goal permanently."""
    stores = get_stores(request)
    try:
        from uuid import UUID
        gid = UUID(goal_id)
        async with stores.structured._pool.acquire() as conn:
            await conn.execute("DELETE FROM goals WHERE id = $1", gid)
        return HTMLResponse('<div class="text-green-400 text-sm p-2">Goal deleted.</div>')
    except Exception as e:
        return HTMLResponse(f'<div class="text-red-400 text-sm p-2">Error: {e}</div>', status_code=500)
