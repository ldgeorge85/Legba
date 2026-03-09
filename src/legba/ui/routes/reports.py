"""Reports route — GET /reports, GET /reports/{index}."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..app import templates

router = APIRouter()


async def _get_reports(request: Request) -> list[dict]:
    """Fetch report history from Redis."""
    stores = request.app.state.stores
    reports = await stores.registers.get_json("report_history") or []
    if isinstance(reports, list):
        return list(reversed(reports))  # Most recent first
    return []


@router.get("/reports")
async def reports_list(request: Request):
    reports = await _get_reports(request)
    return templates.TemplateResponse("reports/list.html", {
        "request": request,
        "active_page": "reports",
        "reports": reports,
    })


@router.get("/reports/{index}")
async def report_detail(request: Request, index: int):
    reports = await _get_reports(request)
    if index < 0 or index >= len(reports):
        return templates.TemplateResponse("reports/list.html", {
            "request": request,
            "active_page": "reports",
            "reports": reports,
        })
    report = reports[index]
    return templates.TemplateResponse("reports/detail.html", {
        "request": request,
        "active_page": "reports",
        "report": report,
        "index": index,
        "total": len(reports),
    })
