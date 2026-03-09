"""Goals route — GET /goals."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request

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
