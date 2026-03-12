"""Analytics API routes — JSON endpoints for charts and dashboards."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from ..app import get_stores

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stats")

# Event categories used for timeseries breakdown
EVENT_CATEGORIES = [
    "conflict", "political", "economic", "technology",
    "health", "environment", "social", "disaster", "other",
]


@router.get("/events-timeseries")
async def events_timeseries(request: Request, days: int = Query(default=30, ge=1, le=365)):
    """Event counts by day with category breakdown."""
    stores = get_stores(request)
    if not stores.structured._available:
        return JSONResponse([])

    try:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    DATE(COALESCE(event_timestamp, created_at)) AS day,
                    category,
                    COUNT(*) AS cnt
                FROM events
                WHERE COALESCE(event_timestamp, created_at) >= $1
                GROUP BY day, category
                ORDER BY day
                """,
                since,
            )

        # Pivot: {date_str -> {category -> count}}
        daily: dict[str, dict[str, int]] = {}
        for row in rows:
            d = row["day"].isoformat()
            if d not in daily:
                daily[d] = {c: 0 for c in EVENT_CATEGORIES}
            cat = row["category"] or "other"
            if cat not in daily[d]:
                daily[d][cat] = 0
            daily[d][cat] += row["cnt"]

        result = []
        for date_str in sorted(daily.keys()):
            counts = daily[date_str]
            total = sum(counts.values())
            result.append({"date": date_str, "total": total, **counts})

        return JSONResponse(result)

    except Exception as exc:
        logger.warning("events-timeseries query failed: %s", exc)
        return JSONResponse([])


@router.get("/cycle-performance")
async def cycle_performance(request: Request, last: int = Query(default=100, ge=1, le=1000)):
    """Per-cycle performance metrics from OpenSearch audit index."""
    stores = get_stores(request)
    if not stores.audit.available:
        return JSONResponse([])

    try:
        agg_result = await stores.audit.aggregate(
            "legba-audit-*",
            aggs={
                "cycles": {
                    "terms": {
                        "field": "cycle",
                        "size": last,
                        "order": {"_key": "desc"},
                    },
                    "aggs": {
                        "tool_calls": {
                            "filter": {"term": {"event": "tool_call"}},
                        },
                        "llm_calls": {
                            "filter": {"term": {"event": "llm_call"}},
                            "aggs": {
                                "total_tokens": {"sum": {"field": "tokens"}},
                            },
                        },
                        "errors": {
                            "filter": {"term": {"event": "error"}},
                        },
                        "avg_latency": {
                            "avg": {"field": "latency_ms"},
                        },
                    },
                },
            },
        )

        buckets = (
            agg_result.get("aggregations", {})
            .get("cycles", {})
            .get("buckets", [])
        )

        result = []
        for b in buckets:
            total_tokens_val = (
                b.get("llm_calls", {})
                .get("total_tokens", {})
                .get("value", 0)
            )
            avg_latency_val = b.get("avg_latency", {}).get("value")

            result.append({
                "cycle": b.get("key"),
                "tool_calls": b.get("tool_calls", {}).get("doc_count", 0),
                "llm_calls": b.get("llm_calls", {}).get("doc_count", 0),
                "errors": b.get("errors", {}).get("doc_count", 0),
                "avg_latency_ms": round(avg_latency_val) if avg_latency_val is not None else None,
                "total_tokens": int(total_tokens_val) if total_tokens_val else 0,
            })

        # Sort ascending by cycle number for charting
        result.sort(key=lambda x: x["cycle"] or 0)
        return JSONResponse(result)

    except Exception as exc:
        logger.warning("cycle-performance query failed: %s", exc)
        return JSONResponse([])


@router.get("/entity-distribution")
async def entity_distribution(request: Request):
    """Entity counts grouped by entity_type."""
    stores = get_stores(request)
    if not stores.structured._available:
        return JSONResponse({})

    try:
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT entity_type, COUNT(*) AS cnt
                FROM entity_profiles
                GROUP BY entity_type
                ORDER BY cnt DESC
                """
            )
        result = {row["entity_type"]: row["cnt"] for row in rows}
        return JSONResponse(result)

    except Exception as exc:
        logger.warning("entity-distribution query failed: %s", exc)
        return JSONResponse({})


@router.get("/source-health")
async def source_health(request: Request):
    """Source health overview with fetch/event counts, sorted by event_count descending."""
    stores = get_stores(request)
    if not stores.structured._available:
        return JSONResponse([])

    try:
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    s.name,
                    s.status,
                    s.source_type,
                    COALESCE(s.fetch_success_count, 0) AS fetch_count,
                    COALESCE(s.fetch_failure_count, 0) AS fail_count,
                    COALESCE(s.events_produced_count, 0) AS event_count
                FROM sources s
                ORDER BY COALESCE(s.events_produced_count, 0) DESC
                """
            )

        result = [
            {
                "name": row["name"],
                "status": row["status"],
                "source_type": row["source_type"],
                "fetch_count": row["fetch_count"],
                "fail_count": row["fail_count"],
                "event_count": row["event_count"],
            }
            for row in rows
        ]
        return JSONResponse(result)

    except Exception as exc:
        logger.warning("source-health query failed: %s", exc)
        return JSONResponse([])


@router.get("/fact-distribution")
async def fact_distribution(request: Request):
    """Fact counts by confidence bucket and top predicates."""
    stores = get_stores(request)
    if not stores.structured._available:
        return JSONResponse({"by_confidence": [], "top_predicates": []})

    try:
        async with stores.structured._pool.acquire() as conn:
            # Confidence buckets
            bucket_rows = await conn.fetch(
                """
                SELECT
                    CASE
                        WHEN confidence >= 0.9 THEN '0.9-1.0'
                        WHEN confidence >= 0.8 THEN '0.8-0.9'
                        WHEN confidence >= 0.7 THEN '0.7-0.8'
                        WHEN confidence >= 0.6 THEN '0.6-0.7'
                        WHEN confidence >= 0.5 THEN '0.5-0.6'
                        ELSE '0.0-0.5'
                    END AS range,
                    COUNT(*) AS cnt
                FROM facts
                GROUP BY range
                ORDER BY range DESC
                """
            )

            # Top predicates
            predicate_rows = await conn.fetch(
                """
                SELECT predicate, COUNT(*) AS cnt
                FROM facts
                GROUP BY predicate
                ORDER BY cnt DESC
                LIMIT 20
                """
            )

        by_confidence = [
            {"range": row["range"], "count": row["cnt"]}
            for row in bucket_rows
        ]
        top_predicates = [
            {"predicate": row["predicate"], "count": row["cnt"]}
            for row in predicate_rows
        ]

        return JSONResponse({
            "by_confidence": by_confidence,
            "top_predicates": top_predicates,
        })

    except Exception as exc:
        logger.warning("fact-distribution query failed: %s", exc)
        return JSONResponse({"by_confidence": [], "top_predicates": []})


@router.get("/situation-timeline")
async def situation_timeline(request: Request, id: str = Query(...)):
    """Event timeline for a specific situation."""
    stores = get_stores(request)
    if not stores.structured._available:
        return JSONResponse([])

    try:
        situation_id = UUID(id)
    except ValueError:
        return JSONResponse({"error": "Invalid situation ID"}, status_code=400)

    try:
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    e.id AS event_id,
                    e.title,
                    e.category,
                    e.event_timestamp,
                    se.relevance
                FROM situation_events se
                JOIN events e ON e.id = se.event_id
                WHERE se.situation_id = $1
                ORDER BY e.event_timestamp ASC NULLS LAST
                """,
                situation_id,
            )

        result = [
            {
                "event_id": str(row["event_id"]),
                "title": row["title"],
                "category": row["category"],
                "event_timestamp": row["event_timestamp"].isoformat() if row["event_timestamp"] else None,
                "relevance": float(row["relevance"]),
            }
            for row in rows
        ]
        return JSONResponse(result)

    except Exception as exc:
        logger.warning("situation-timeline query failed: %s", exc)
        return JSONResponse([])
