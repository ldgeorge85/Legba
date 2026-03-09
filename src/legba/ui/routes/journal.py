"""Journal route — GET /journal."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..app import templates

router = APIRouter()


@router.get("/journal")
async def journal_page(request: Request):
    stores = request.app.state.stores

    journal_data = await stores.registers.get_json("journal") or {}

    consolidation = journal_data.get("consolidation", "")
    consolidation_cycle = journal_data.get("consolidation_cycle")
    consolidation_timestamp = journal_data.get("consolidation_timestamp", "")
    entry_count = len(journal_data.get("entries", []))

    return templates.TemplateResponse("journal/view.html", {
        "request": request,
        "active_page": "journal",
        "consolidation": consolidation,
        "consolidation_cycle": consolidation_cycle,
        "consolidation_timestamp": consolidation_timestamp[:19] if consolidation_timestamp else "",
        "pending_entries": entry_count,
    })
