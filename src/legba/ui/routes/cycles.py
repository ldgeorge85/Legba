"""Cycle Monitor routes — GET /cycles + GET /cycles/{cycle_number}."""

from __future__ import annotations

import json
import os
from datetime import datetime

from fastapi import APIRouter, Request

from ..app import get_stores, templates

router = APIRouter()


def _load_cycle_response():
    shared = os.environ.get("LEGBA_SHARED", "/shared")
    path = os.path.join(shared, "response.json")
    try:
        with open(path) as f:
            data = json.load(f)
        from ...shared.schemas.cycle import CycleResponse
        return CycleResponse.model_validate(data)
    except Exception:
        return None


def _parse_dt(val) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val))
    except Exception:
        return None


@router.get("/cycles")
async def cycle_list(request: Request):
    stores = get_stores(request)
    current_cycle = await stores.registers.get_int("cycle_number", 0)

    # Try to get cycle aggregation from audit OpenSearch
    cycles = []
    audit_available = stores.audit.available

    if audit_available:
        try:
            agg_result = await stores.audit.aggregate(
                "legba-audit-*",
                aggs={
                    "cycles": {
                        "terms": {
                            "field": "cycle",
                            "size": 50,
                            "order": {"_key": "desc"},
                        },
                        "aggs": {
                            "event_count": {"value_count": {"field": "event"}},
                            "tool_count": {
                                "filter": {"term": {"event": "tool_call"}}
                            },
                            "error_count": {
                                "filter": {"term": {"event": "error"}}
                            },
                            "min_ts": {"min": {"field": "timestamp"}},
                            "max_ts": {"max": {"field": "timestamp"}},
                        },
                    }
                },
            )

            buckets = (
                agg_result.get("aggregations", {})
                .get("cycles", {})
                .get("buckets", [])
            )

            for b in buckets:
                cycle_num = b.get("key")
                min_ts = b.get("min_ts", {}).get("value_as_string")
                max_ts = b.get("max_ts", {}).get("value_as_string")

                status = "completed"
                if cycle_num == current_cycle:
                    status = "running"

                cycles.append({
                    "cycle_number": cycle_num,
                    "status": status,
                    "event_count": b.get("event_count", {}).get("value", 0),
                    "tool_count": b.get("tool_count", {}).get("doc_count", 0),
                    "error_count": b.get("error_count", {}).get("doc_count", 0),
                    "timestamp": _parse_dt(min_ts),
                })

        except Exception:
            audit_available = False

    context = {
        "request": request,
        "active_page": "cycles",
        "cycles": cycles,
        "current_cycle": current_cycle,
        "audit_available": audit_available,
    }

    if request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"):
        return templates.TemplateResponse("cycles/_rows.html", context)
    return templates.TemplateResponse("cycles/list.html", context)


@router.get("/cycles/{cycle_number}")
async def cycle_detail(request: Request, cycle_number: int):
    stores = get_stores(request)

    # Check if this is the current cycle and load response.json
    cycle_response = _load_cycle_response()
    if cycle_response and cycle_response.cycle_number != cycle_number:
        cycle_response = None

    # Query audit OpenSearch for this cycle's entries
    tool_calls = []
    llm_calls = []
    errors = []
    phases = []
    audit_entries = []
    audit_available = stores.audit.available

    if audit_available:
        try:
            result = await stores.audit.search(
                "legba-audit-*",
                query={"term": {"cycle": cycle_number}},
                size=500,
                sort=[{"timestamp": {"order": "asc"}}],
            )

            for hit in result.get("hits", []):
                audit_entries.append(hit)
                event_type = hit.get("event", "")
                ts = _parse_dt(hit.get("timestamp"))

                if event_type == "tool_call":
                    tool_calls.append({
                        "tool_name": hit.get("tool_name", hit.get("name", "")),
                        "duration_ms": hit.get("duration_ms"),
                        "result": hit.get("result", ""),
                        "timestamp": ts,
                    })
                elif event_type == "llm_call":
                    llm_calls.append({
                        "purpose": hit.get("purpose", hit.get("phase", "")),
                        "tokens": hit.get("tokens", hit.get("total_tokens")),
                        "latency_ms": hit.get("latency_ms"),
                        "timestamp": ts,
                    })
                elif event_type == "phase":
                    phases.append({
                        "phase": hit.get("phase", hit.get("new_phase", "")),
                        "timestamp": ts,
                    })
                elif event_type == "error":
                    errors.append({
                        "message": hit.get("message", hit.get("error", "")),
                        "timestamp": ts,
                    })

        except Exception:
            audit_available = False

    return templates.TemplateResponse(
        "cycles/detail.html",
        {
            "request": request,
            "active_page": "cycles",
            "cycle_number": cycle_number,
            "cycle_response": cycle_response,
            "tool_calls": tool_calls,
            "llm_calls": llm_calls,
            "errors": errors,
            "phases": phases,
            "audit_entries": audit_entries,
            "audit_available": audit_available,
        },
    )
