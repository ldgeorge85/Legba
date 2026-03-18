"""Cycle Monitor routes — GET /cycles + GET /cycles/{cycle_number}."""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import datetime

from fastapi import APIRouter, Request

from ..app import get_stores, templates

logger = logging.getLogger(__name__)

router = APIRouter()

# Phase names that indicate cycle type (matched case-insensitively against phase events)
_CYCLE_TYPE_KEYWORDS = {
    "evolve": "EVOLVE",
    "introspection": "INTROSPECTION",
    "analysis": "ANALYSIS",
    "analyze": "ANALYSIS",
    "research": "RESEARCH",
    "acquire": "ACQUIRE",
}

# Phase colors for timeline bar (matched against lowercase phase name)
PHASE_COLORS = {
    "wake": {"bg": "#374151", "text": "#9ca3af", "label": "WAKE"},       # gray
    "orient": {"bg": "#1e3a5f", "text": "#60a5fa", "label": "ORIENT"},   # blue
    "evolve": {"bg": "#4a1d00", "text": "#fb923c", "label": "EVOLVE"},   # orange
    "plan": {"bg": "#422006", "text": "#facc15", "label": "PLAN"},       # yellow
    "act": {"bg": "#083344", "text": "#22d3ee", "label": "ACT"},         # cyan
    "reflect": {"bg": "#2e1065", "text": "#a78bfa", "label": "REFLECT"}, # violet
    "narrate": {"bg": "#1e1b4b", "text": "#818cf8", "label": "NARRATE"}, # indigo
    "persist": {"bg": "#052e16", "text": "#4ade80", "label": "PERSIST"}, # green
}


def _load_cycle_response():
    shared = os.environ.get("LEGBA_SHARED", "/shared")
    path = os.path.join(shared, "response.json")
    try:
        with open(path) as f:
            data = json.load(f)
        from ...shared.schemas.cycle import CycleResponse
        return CycleResponse.model_validate(data)
    except Exception as e:
        logger.debug("Failed to load cycle response.json: %s", e)
        return None


def _parse_dt(val) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val))
    except Exception as e:
        logger.debug("_parse_dt failed for %r: %s", val, e)
        return None


def _detect_cycle_type(phase_names: list[str]) -> str:
    """Detect cycle type from phase event names.

    Looks for keywords like 'acquire', 'research', 'analysis', 'introspection'
    in the phase names. Returns the highest-priority match or 'NORMAL'.
    """
    priority = ["EVOLVE", "INTROSPECTION", "ANALYSIS", "RESEARCH", "ACQUIRE"]
    detected = set()
    for name in phase_names:
        lower = name.lower()
        for keyword, ctype in _CYCLE_TYPE_KEYWORDS.items():
            if keyword in lower:
                detected.add(ctype)
    for p in priority:
        if p in detected:
            return p
    return "NORMAL"


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
                            "llm_count": {
                                "filter": {"term": {"event": "llm_call"}}
                            },
                            "error_count": {
                                "filter": {"term": {"event": "error"}}
                            },
                            "min_ts": {"min": {"field": "timestamp"}},
                            "max_ts": {"max": {"field": "timestamp"}},
                            "phase_names": {
                                "filter": {"term": {"event": "phase"}},
                                "aggs": {
                                    "names": {
                                        "terms": {
                                            "field": "phase",
                                            "size": 20,
                                        }
                                    }
                                },
                            },
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

                # Extract phase names for cycle type detection
                phase_buckets = (
                    b.get("phase_names", {})
                    .get("names", {})
                    .get("buckets", [])
                )
                phase_names = [pb.get("key", "") for pb in phase_buckets]
                cycle_type = _detect_cycle_type(phase_names)

                # Compute duration from min/max timestamps
                dt_min = _parse_dt(min_ts)
                dt_max = _parse_dt(max_ts)
                duration_s = None
                if dt_min and dt_max:
                    duration_s = int((dt_max - dt_min).total_seconds())

                cycles.append({
                    "cycle_number": cycle_num,
                    "status": status,
                    "cycle_type": cycle_type,
                    "event_count": b.get("event_count", {}).get("value", 0),
                    "tool_count": b.get("tool_count", {}).get("doc_count", 0),
                    "llm_count": b.get("llm_count", {}).get("doc_count", 0),
                    "error_count": b.get("error_count", {}).get("doc_count", 0),
                    "timestamp": dt_min,
                    "duration_s": duration_s,
                })

        except Exception as e:
            logger.warning("Cycle list audit query failed: %s", e)
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

        except Exception as e:
            logger.warning("Cycle detail audit query failed: %s", e)
            audit_available = False

    # --- Compute summary stats for the detail page ---
    total_tool_calls = len(tool_calls)
    total_llm_calls = len(llm_calls)
    total_errors = len(errors)

    # Token totals
    total_tokens = 0
    for lc in llm_calls:
        t = lc.get("tokens")
        if t and isinstance(t, (int, float)):
            total_tokens += int(t)

    # Average LLM latency
    latencies = [lc["latency_ms"] for lc in llm_calls if lc.get("latency_ms")]
    avg_latency_ms = int(sum(latencies) / len(latencies)) if latencies else None
    max_latency_ms = max(latencies) if latencies else 1  # for bar normalization

    # Duration from first to last audit entry timestamp
    timestamps = [e.get("timestamp") for e in audit_entries if e.get("timestamp")]
    parsed_timestamps = [_parse_dt(t) for t in timestamps]
    parsed_timestamps = [t for t in parsed_timestamps if t is not None]
    duration_s = None
    if len(parsed_timestamps) >= 2:
        duration_s = int((max(parsed_timestamps) - min(parsed_timestamps)).total_seconds())

    # Tool call frequency summary
    tool_counter = Counter(tc["tool_name"] for tc in tool_calls if tc.get("tool_name"))
    tool_summary = tool_counter.most_common(10)  # top 10 tools

    # Detect cycle type from phases
    phase_names = [p["phase"] for p in phases if p.get("phase")]
    cycle_type = _detect_cycle_type(phase_names)

    # Compute phase durations for timeline bar
    phase_timeline = []
    for i, p in enumerate(phases):
        phase_name = (p.get("phase") or "").lower()
        # Duration: time until next phase, or None for last
        dur_ms = None
        if i < len(phases) - 1 and p.get("timestamp") and phases[i + 1].get("timestamp"):
            dur_ms = int((phases[i + 1]["timestamp"] - p["timestamp"]).total_seconds() * 1000)
        elif i == len(phases) - 1 and p.get("timestamp") and parsed_timestamps:
            # Last phase: duration until last audit entry
            dur_ms = int((max(parsed_timestamps) - p["timestamp"]).total_seconds() * 1000)

        color_info = PHASE_COLORS.get(phase_name, {"bg": "#374151", "text": "#9ca3af", "label": phase_name.upper()})
        phase_timeline.append({
            "phase": p.get("phase", ""),
            "label": color_info["label"],
            "bg": color_info["bg"],
            "text": color_info["text"],
            "duration_ms": dur_ms,
            "timestamp": p.get("timestamp"),
        })

    # Compute proportional widths for timeline bar
    total_phase_dur = sum(pt["duration_ms"] for pt in phase_timeline if pt["duration_ms"])
    for pt in phase_timeline:
        if total_phase_dur > 0 and pt["duration_ms"]:
            pt["width_pct"] = max(8, int(pt["duration_ms"] / total_phase_dur * 100))
        else:
            # Equal width fallback
            pt["width_pct"] = max(8, int(100 / max(len(phase_timeline), 1)))

    return templates.TemplateResponse(
        "cycles/detail.html",
        {
            "request": request,
            "active_page": "cycles",
            "cycle_number": cycle_number,
            "cycle_type": cycle_type,
            "cycle_response": cycle_response,
            "tool_calls": tool_calls,
            "llm_calls": llm_calls,
            "errors": errors,
            "phases": phases,
            "phase_timeline": phase_timeline,
            "audit_entries": audit_entries,
            "audit_available": audit_available,
            # Summary stats
            "total_tool_calls": total_tool_calls,
            "total_llm_calls": total_llm_calls,
            "total_tokens": total_tokens,
            "avg_latency_ms": avg_latency_ms,
            "max_latency_ms": max_latency_ms,
            "total_errors": total_errors,
            "duration_s": duration_s,
            "tool_summary": tool_summary,
        },
    )
