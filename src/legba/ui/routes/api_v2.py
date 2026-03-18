"""
API v2 — JSON endpoints for the React UI.

All endpoints return JSON. Mounted under /api/v2/.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Request, Query, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..app import get_stores

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2", tags=["api-v2"])


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _json(data, status=200):
    return JSONResponse(content=data, status_code=status)


def _serialize_event(ev) -> dict:
    """Convert Event pydantic model to JSON-safe dict."""
    return {
        "event_id": str(ev.id),
        "title": ev.title,
        "category": ev.category if isinstance(ev.category, str) else ev.category.value,
        "confidence": ev.confidence,
        "timestamp": ev.event_timestamp.isoformat() if ev.event_timestamp else None,
        "source_name": None,  # No source_name on Event; resolved via source_id join if needed
        "source_url": ev.source_url or None,
        "source_id": str(ev.source_id) if ev.source_id else None,
        "description": ev.summary or "",
        "tags": ev.tags if hasattr(ev, "tags") and ev.tags else [],
        "created_at": ev.created_at.isoformat() if hasattr(ev, "created_at") and ev.created_at else None,
    }


def _serialize_entity(ep) -> dict:
    """Convert EntityProfile pydantic model to JSON-safe dict."""
    return {
        "entity_id": str(ep.id),
        "name": ep.canonical_name,
        "entity_type": ep.entity_type if isinstance(ep.entity_type, str) else ep.entity_type.value,
        "first_seen": ep.created_at.isoformat() if hasattr(ep, "created_at") and ep.created_at else None,
        "last_seen": ep.updated_at.isoformat() if hasattr(ep, "updated_at") and ep.updated_at else None,
        "event_count": getattr(ep, "event_link_count", 0) or 0,
        "completeness": getattr(ep, "completeness_score", None),
        "aliases": list(ep.aliases) if hasattr(ep, "aliases") and ep.aliases else [],
    }


# ------------------------------------------------------------------
# Dashboard
# ------------------------------------------------------------------

@router.get("/dashboard")
async def dashboard(request: Request):
    stores = get_stores(request)

    # Parallel count queries
    import asyncio
    counts = await asyncio.gather(
        stores.count_entities(),
        stores.count_signals(),
        stores.count_sources(),
        stores.count_goals(),
        stores.count_facts(),
        stores.count_situations(),
        stores.count_watchlist(),
        stores.count_relationships(),
        return_exceptions=True,
    )

    def _safe(val):
        return val if not isinstance(val, Exception) else 0

    # Current cycle from Redis
    cycle = 0
    agent_status = "idle"
    try:
        cycle_val = await stores.registers._redis.get("legba:cycle_number")
        if cycle_val:
            cycle = int(cycle_val) if isinstance(cycle_val, int) else int(cycle_val.decode() if isinstance(cycle_val, bytes) else cycle_val)
    except Exception as e:
        logger.warning("Dashboard: cycle number fetch failed: %s", e)
    # Derive agent status from most recent audit entry timestamp
    try:
        from datetime import timezone
        latest = await stores.audit.search(
            "legba-audit-*",
            {"match_all": {}},
            size=1,
            sort=[{"timestamp": "desc"}],
        )
        if latest.get("hits"):
            ts_str = latest["hits"][0].get("timestamp", "")
            if ts_str:
                last_ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - last_ts).total_seconds()
                agent_status = "running" if age < 300 else "idle"
    except Exception as e:
        logger.warning("Dashboard: agent status derivation failed: %s", e)

    # Recent events
    recent_events = []
    try:
        from ...shared.schemas.signals import Signal as Event
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT data FROM signals ORDER BY event_timestamp DESC NULLS LAST LIMIT 15"
            )
            for row in rows:
                try:
                    ev = Event.model_validate_json(row["data"])
                    recent_events.append(_serialize_event(ev))
                except Exception as e:
                    logger.debug("Dashboard: event parse failed: %s", e)
                    continue
    except Exception as e:
        logger.warning("Dashboard: recent events query failed: %s", e)

    # Ingestion status from Redis
    ingestion = {}
    try:
        hb = await stores.registers._redis.get("legba:ingest:heartbeat")
        now = time.time()
        signals_1h = await stores.registers._redis.zcount("legba:ingest:signals_1h", now - 3600, now)
        signals_24h = await stores.registers._redis.zcount("legba:ingest:signals_24h", now - 86400, now)
        errors_1h = await stores.registers._redis.zcount("legba:ingest:errors_1h", now - 3600, now)
        ingestion = {
            "active": hb is not None,
            "signals_1h": signals_1h,
            "signals_24h": signals_24h,
            "errors_1h": errors_1h,
        }
    except Exception as e:
        logger.warning("Dashboard: ingestion status fetch failed: %s", e)
        ingestion = {"active": False, "signals_1h": 0, "signals_24h": 0, "errors_1h": 0}

    # Active situations
    active_situations = []
    try:
        sits = await stores.fetch_active_situations(limit=5)
        active_situations = [
            {
                "situation_id": s["id"],
                "title": s["name"],
                "status": s["status"],
                "severity": s.get("category", "medium"),
                "event_count": 0,
                "created_at": s.get("updated_at", "").isoformat() if hasattr(s.get("updated_at", ""), "isoformat") else str(s.get("updated_at", "")),
                "updated_at": s.get("updated_at", "").isoformat() if hasattr(s.get("updated_at", ""), "isoformat") else str(s.get("updated_at", "")),
            }
            for s in sits
        ]
    except Exception as e:
        logger.warning("Dashboard: active situations fetch failed: %s", e)

    # Count derived events separately
    event_count = 0
    try:
        event_count = await stores.count_events()
    except Exception as e:
        logger.warning("Dashboard: event count failed: %s", e)

    # Count active hypotheses
    hypothesis_count = 0
    try:
        async with stores.structured._pool.acquire() as conn:
            hypothesis_count = await conn.fetchval(
                "SELECT count(*) FROM hypotheses WHERE status = 'active'"
            ) or 0
    except Exception as e:
        logger.debug("Dashboard: hypothesis count failed: %s", e)

    # Count situation briefs
    brief_count = 0
    try:
        brief_count = await stores.registers._redis.llen("legba:situation_briefs") or 0
    except Exception as e:
        logger.debug("Dashboard: brief count failed: %s", e)

    return _json({
        "entities": _safe(counts[0]),
        "signals": _safe(counts[1]),
        "events": event_count,
        "sources": _safe(counts[2]),
        "goals": _safe(counts[3]),
        "facts": _safe(counts[4]),
        "situations": _safe(counts[5]),
        "watchlist": _safe(counts[6]),
        "relationships": _safe(counts[7]),
        "hypotheses": hypothesis_count,
        "briefs": brief_count,
        "current_cycle": cycle,
        "agent_status": agent_status,
        "recent_signals": recent_events,
        "active_situations": active_situations,
        "ingestion": ingestion,
    })


# ------------------------------------------------------------------
# Signals (raw ingested material, formerly "events")
# ------------------------------------------------------------------

@router.get("/signals/facets")
async def signal_facets(request: Request):
    """Aggregated facets for signal filtering."""
    stores = get_stores(request)
    facets: dict = {}

    if not stores.structured._available:
        return _json({"categories": {}, "sources": {}, "timeline": {}})

    try:
        async with stores.structured._pool.acquire() as conn:
            # Category counts
            rows = await conn.fetch(
                "SELECT category, COUNT(*) as cnt FROM signals GROUP BY category ORDER BY cnt DESC"
            )
            facets["categories"] = {r["category"]: r["cnt"] for r in rows}

            # Source counts (top 20)
            rows = await conn.fetch(
                "SELECT s.name, COUNT(e.id) as cnt FROM signals e "
                "JOIN sources s ON e.source_id = s.id "
                "GROUP BY s.name ORDER BY cnt DESC LIMIT 20"
            )
            facets["sources"] = {r["name"]: r["cnt"] for r in rows}

            # Time distribution (events per day, last 30 days)
            rows = await conn.fetch(
                "SELECT DATE(created_at) as day, COUNT(*) as cnt FROM signals "
                "WHERE created_at > NOW() - INTERVAL '30 days' GROUP BY day ORDER BY day"
            )
            facets["timeline"] = {str(r["day"]): r["cnt"] for r in rows}

            # Confidence distribution (buckets)
            rows = await conn.fetch(
                "SELECT "
                "  CASE "
                "    WHEN confidence >= 0.9 THEN '0.9-1.0' "
                "    WHEN confidence >= 0.7 THEN '0.7-0.9' "
                "    WHEN confidence >= 0.5 THEN '0.5-0.7' "
                "    WHEN confidence >= 0.3 THEN '0.3-0.5' "
                "    ELSE '0.0-0.3' "
                "  END as bucket, COUNT(*) as cnt "
                "FROM signals GROUP BY bucket ORDER BY bucket"
            )
            facets["confidence_buckets"] = {r["bucket"]: r["cnt"] for r in rows}

    except Exception as exc:
        logger.warning("Event facets query failed: %s", exc)
        facets = {"categories": {}, "sources": {}, "timeline": {}, "confidence_buckets": {}}

    return _json(facets)


@router.get("/signals")
async def list_signals(
    request: Request,
    offset: int = 0,
    limit: int = Query(default=50, le=200),
    category: str | None = None,
    q: str | None = None,
    source: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    min_confidence: float | None = None,
):
    stores = get_stores(request)
    from ...shared.schemas.signals import Signal as Event

    if q:
        os_query = {
            "multi_match": {
                "query": q,
                "fields": ["title^2", "summary", "full_content", "actors", "locations"],
            }
        }
        result = await stores.opensearch.search("legba-signals-*,legba-events-*", os_query, size=limit)
        events = []
        for hit in result.get("hits", []):
            try:
                events.append(_serialize_event(Event.model_validate(hit)))
            except Exception as e:
                logger.debug("Signal parse from OpenSearch failed: %s", e)
                continue
        return _json({
            "items": events,
            "total": result.get("total", len(events)),
            "offset": offset,
            "limit": limit,
        })

    if not stores.structured._available:
        return _json({"items": [], "total": 0, "offset": offset, "limit": limit})

    conditions, params, idx = [], [], 1
    if category:
        cats = [c.strip() for c in category.split(",") if c.strip()]
        if len(cats) == 1:
            conditions.append(f"category = ${idx}")
            params.append(cats[0])
            idx += 1
        elif cats:
            placeholders = ", ".join(f"${idx + i}" for i in range(len(cats)))
            conditions.append(f"category IN ({placeholders})")
            params.extend(cats)
            idx += len(cats)
    if source:
        conditions.append(f"source_id IN (SELECT id FROM sources WHERE LOWER(name) = LOWER(${idx}))")
        params.append(source)
        idx += 1
    if start_date:
        try:
            conditions.append(f"event_timestamp >= ${idx}")
            params.append(datetime.fromisoformat(start_date))
            idx += 1
        except ValueError:
            pass
    if end_date:
        try:
            conditions.append(f"event_timestamp <= ${idx}")
            params.append(datetime.fromisoformat(end_date))
            idx += 1
        except ValueError:
            pass
    if min_confidence is not None:
        conditions.append(f"confidence >= ${idx}")
        params.append(min_confidence)
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    try:
        async with stores.structured._pool.acquire() as conn:
            total = await conn.fetchval(f"SELECT count(*) FROM signals {where}", *params)
            rows = await conn.fetch(
                f"SELECT data FROM signals {where} "
                f"ORDER BY event_timestamp DESC NULLS LAST, created_at DESC "
                f"LIMIT ${idx} OFFSET ${idx + 1}",
                *params, limit, offset,
            )
            items = []
            for row in rows:
                try:
                    items.append(_serialize_event(Event.model_validate_json(row["data"])))
                except Exception as e:
                    logger.debug("Signal row parse failed: %s", e)
                    continue
            return _json({"items": items, "total": total, "offset": offset, "limit": limit})
    except Exception as exc:
        logger.warning("Events query failed: %s", exc)
        return _json({"items": [], "total": 0, "offset": offset, "limit": limit})


@router.get("/signals/geo")
async def signals_geo(request: Request):
    """Signals with geo coordinates for heatmap visualization."""
    stores = get_stores(request)
    try:
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, title, category, confidence, event_timestamp,
                       data->'geo_coordinates' AS geo_coords
                FROM signals
                WHERE data->'geo_coordinates' IS NOT NULL
                  AND jsonb_array_length(COALESCE(data->'geo_coordinates', '[]'::jsonb)) > 0
                ORDER BY created_at DESC
                LIMIT 2000
                """
            )

        features = []
        for r in rows:
            geo_coords = r["geo_coords"]
            if isinstance(geo_coords, str):
                geo_coords = json.loads(geo_coords)
            if not geo_coords or not isinstance(geo_coords, list):
                continue
            for coord in geo_coords:
                lat = coord.get("lat")
                lon = coord.get("lon")
                if lat is None or lon is None:
                    continue
                features.append({
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [float(lon), float(lat)],
                    },
                    "properties": {
                        "id": str(r["id"]),
                        "title": r["title"],
                        "category": r["category"],
                        "confidence": float(r["confidence"]) if r["confidence"] else 0.5,
                        "timestamp": r["event_timestamp"].isoformat() if r["event_timestamp"] else None,
                        "location_name": coord.get("name", ""),
                    },
                })

        return _json({
            "type": "FeatureCollection",
            "features": features,
        })
    except Exception as exc:
        logger.warning("events/geo failed: %s", exc)
        return _json({"type": "FeatureCollection", "features": []})


@router.get("/signals/{signal_id}")
async def get_signal(request: Request, signal_id: UUID):
    stores = get_stores(request)
    ev = await stores.get_event(signal_id)
    if not ev:
        return _json({"error": "not found"}, 404)

    data = _serialize_event(ev)

    # Get linked entities
    entities = []
    try:
        ents = await stores.structured.get_signal_entities(signal_id)
        entities = [
            {
                "entity_id": str(e.get("entity_id", "")),
                "name": e.get("name", ""),
                "entity_type": e.get("entity_type", ""),
                "role": e.get("role"),
            }
            for e in ents
        ]
    except Exception as e:
        logger.warning("Signal entity links fetch failed for %s: %s", signal_id, e)

    data["entities"] = entities
    return _json(data)


@router.delete("/signals/{signal_id}")
async def delete_signal(request: Request, signal_id: UUID):
    stores = get_stores(request)
    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute("DELETE FROM signal_entity_links WHERE signal_id = $1", signal_id)
            await conn.execute("DELETE FROM signals WHERE id = $1", signal_id)
        return _json({"status": "deleted"})
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


# ------------------------------------------------------------------
# Events (derived real-world occurrences)
# ------------------------------------------------------------------

@router.get("/events/facets")
async def event_facets(request: Request):
    """Aggregated facets for derived events."""
    stores = get_stores(request)
    if not stores.structured._available:
        return _json({"categories": {}, "severities": {}, "types": {}})

    try:
        async with stores.structured._pool.acquire() as conn:
            cats = await conn.fetch(
                "SELECT category, COUNT(*) as cnt FROM events GROUP BY category ORDER BY cnt DESC"
            )
            sevs = await conn.fetch(
                "SELECT severity, COUNT(*) as cnt FROM events GROUP BY severity ORDER BY cnt DESC"
            )
            types = await conn.fetch(
                "SELECT event_type, COUNT(*) as cnt FROM events GROUP BY event_type ORDER BY cnt DESC"
            )
        return _json({
            "categories": {r["category"]: r["cnt"] for r in cats},
            "severities": {r["severity"]: r["cnt"] for r in sevs},
            "types": {r["event_type"]: r["cnt"] for r in types},
        })
    except Exception as exc:
        logger.warning("Event facets query failed: %s", exc)
        return _json({"categories": {}, "severities": {}, "types": {}})


@router.get("/events")
async def list_events(
    request: Request,
    offset: int = 0,
    limit: int = Query(default=50, le=200),
    category: str | None = None,
    severity: str | None = None,
    event_type: str | None = None,
    min_signals: int | None = None,
    q: str | None = None,
):
    """List derived events with filters."""
    stores = get_stores(request)
    if not stores.structured._available:
        return _json({"items": [], "total": 0, "offset": offset, "limit": limit})

    conditions, params, idx = [], [], 1
    if q:
        conditions.append(f"title ILIKE ${idx}")
        params.append(f"%{q}%")
        idx += 1
    if category:
        cats = [c.strip() for c in category.split(",") if c.strip()]
        if len(cats) == 1:
            conditions.append(f"category = ${idx}")
            params.append(cats[0])
            idx += 1
        elif cats:
            placeholders = ", ".join(f"${idx + i}" for i in range(len(cats)))
            conditions.append(f"category IN ({placeholders})")
            params.extend(cats)
            idx += len(cats)
    if severity:
        conditions.append(f"severity = ${idx}")
        params.append(severity)
        idx += 1
    if event_type:
        conditions.append(f"event_type = ${idx}")
        params.append(event_type)
        idx += 1
    if min_signals is not None:
        conditions.append(f"signal_count >= ${idx}")
        params.append(min_signals)
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    try:
        async with stores.structured._pool.acquire() as conn:
            total = await conn.fetchval(f"SELECT count(*) FROM events {where}", *params)
            rows = await conn.fetch(
                f"SELECT data FROM events {where} "
                f"ORDER BY time_start DESC NULLS LAST, created_at DESC "
                f"LIMIT ${idx} OFFSET ${idx + 1}",
                *params, limit, offset,
            )
            items = []
            for row in rows:
                try:
                    d = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
                    items.append({
                        "event_id": d.get("id", ""),
                        "title": d.get("title", ""),
                        "category": d.get("category", "other"),
                        "event_type": d.get("event_type", "incident"),
                        "severity": d.get("severity", "medium"),
                        "signal_count": d.get("signal_count", 0),
                        "confidence": d.get("confidence", 0.5),
                        "source_method": d.get("source_method", "auto"),
                        "time_start": d.get("time_start"),
                        "time_end": d.get("time_end"),
                        "actors": (d.get("actors") or [])[:5],
                        "locations": (d.get("locations") or [])[:5],
                        "created_at": d.get("created_at"),
                    })
                except Exception as e:
                    logger.debug("Derived event row parse failed: %s", e)
                    continue
            return _json({"items": items, "total": total, "offset": offset, "limit": limit})
    except Exception as exc:
        logger.warning("Events query failed: %s", exc)
        return _json({"items": [], "total": 0, "offset": offset, "limit": limit})


@router.get("/events/geo")
async def events_geo(request: Request):
    """Derived events with geo coordinates for map visualization."""
    stores = get_stores(request)
    try:
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, data FROM events
                WHERE data->'geo_coordinates' IS NOT NULL
                  AND jsonb_array_length(COALESCE(data->'geo_coordinates', '[]'::jsonb)) > 0
                ORDER BY created_at DESC
                LIMIT 2000
                """
            )

        features = []
        for r in rows:
            d = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
            geo_coords = d.get("geo_coordinates") or []
            for coord in geo_coords:
                lat, lon = coord.get("lat"), coord.get("lon")
                if lat is None or lon is None:
                    continue
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
                    "properties": {
                        "id": str(r["id"]),
                        "title": d.get("title", ""),
                        "category": d.get("category", "other"),
                        "severity": d.get("severity", "medium"),
                        "signal_count": d.get("signal_count", 0),
                        "confidence": d.get("confidence", 0.5),
                        "timestamp": d.get("time_start"),
                        "location_name": coord.get("name", ""),
                    },
                })

        return _json({"type": "FeatureCollection", "features": features})
    except Exception as exc:
        logger.warning("events/geo failed: %s", exc)
        return _json({"type": "FeatureCollection", "features": []})


@router.get("/events/{event_id}")
async def get_event(request: Request, event_id: UUID):
    """Get a single derived event with linked signals."""
    stores = get_stores(request)
    if not stores.structured._available:
        return _json({"error": "not available"}, 503)

    try:
        async with stores.structured._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT data FROM events WHERE id = $1", event_id)
            if not row:
                return _json({"error": "not found"}, 404)

            d = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]

            # Get linked signals
            signal_rows = await conn.fetch(
                """
                SELECT s.title, s.category, s.confidence, s.event_timestamp, sel.relevance
                FROM signals s
                JOIN signal_event_links sel ON s.id = sel.signal_id
                WHERE sel.event_id = $1
                ORDER BY sel.relevance DESC, s.event_timestamp DESC NULLS LAST
                LIMIT 20
                """,
                event_id,
            )
            linked_signals = [
                {
                    "title": r["title"],
                    "category": r["category"],
                    "confidence": float(r["confidence"]) if r["confidence"] else 0.5,
                    "timestamp": r["event_timestamp"].isoformat() if r["event_timestamp"] else None,
                    "relevance": float(r["relevance"]),
                }
                for r in signal_rows
            ]

        d["linked_signals"] = linked_signals
        return _json(d)
    except Exception as exc:
        logger.warning("Event detail query failed: %s", exc)
        return _json({"error": str(exc)}, 500)


# ------------------------------------------------------------------
# Entities
# ------------------------------------------------------------------

@router.get("/entities/types")
async def entity_types(request: Request):
    """List distinct entity types with counts for filter dropdowns."""
    stores = get_stores(request)
    if not stores.structured._available:
        return _json([])
    try:
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT entity_type, COUNT(*) as cnt FROM entity_profiles "
                "GROUP BY entity_type ORDER BY cnt DESC"
            )
            return _json([{"type": r["entity_type"], "count": r["cnt"]} for r in rows])
    except Exception as exc:
        logger.warning("Entity types query failed: %s", exc)
        return _json([])


@router.get("/entities")
async def list_entities(
    request: Request,
    offset: int = 0,
    limit: int = Query(default=50, le=200),
    type: str | None = None,
    q: str | None = None,
    min_completeness: float | None = None,
    created_after: str | None = None,
):
    stores = get_stores(request)
    from ...shared.schemas.entity_profiles import EntityProfile

    if not stores.structured._available:
        return _json({"items": [], "total": 0, "offset": offset, "limit": limit})

    conditions, params, idx = [], [], 1
    if q:
        conditions.append(f"LOWER(canonical_name) LIKE LOWER(${idx})")
        params.append(f"%{q}%")
        idx += 1
    if type:
        conditions.append(f"entity_type = ${idx}")
        params.append(type)
        idx += 1
    if min_completeness is not None and min_completeness > 0:
        conditions.append(f"completeness_score >= ${idx}")
        params.append(min_completeness)
        idx += 1
    if created_after:
        try:
            dt = datetime.fromisoformat(created_after)
        except ValueError:
            dt = None
        if dt:
            conditions.append(f"created_at >= ${idx}")
            params.append(dt)
            idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    try:
        async with stores.structured._pool.acquire() as conn:
            total = await conn.fetchval(f"SELECT count(*) FROM entity_profiles {where}", *params)
            rows = await conn.fetch(
                f"SELECT data FROM entity_profiles {where} "
                f"ORDER BY updated_at DESC "
                f"LIMIT ${idx} OFFSET ${idx + 1}",
                *params, limit, offset,
            )
            items = []
            for row in rows:
                try:
                    ep = EntityProfile.model_validate_json(row["data"])
                    items.append(_serialize_entity(ep))
                except Exception as e:
                    logger.debug("Entity profile parse failed: %s", e)
                    total -= 1
            return _json({"items": items, "total": total, "offset": offset, "limit": limit})
    except Exception as exc:
        logger.warning("Entities query failed: %s", exc)
        return _json({"items": [], "total": 0, "offset": offset, "limit": limit})


@router.get("/entities/{entity_id}")
async def get_entity(request: Request, entity_id: str):
    stores = get_stores(request)

    ep = None
    try:
        entity_uuid = UUID(entity_id)
        ep = await stores.structured.get_entity_profile(entity_uuid)
    except (ValueError, AttributeError):
        pass  # Not a UUID — try name lookup below

    # Fallback: look up by canonical_name (supports graph panel clicks which pass names)
    if not ep and stores.structured._available:
        try:
            row = await stores.structured._pool.fetchrow(
                "SELECT data FROM entity_profiles WHERE canonical_name ILIKE $1 LIMIT 1",
                entity_id,
            )
            if row:
                from ...shared.schemas.entity_profiles import EntityProfile
                ep = EntityProfile.model_validate_json(row["data"])
        except Exception as e:
            logger.debug("Entity name lookup failed for %r: %s", entity_id, e)

    if not ep:
        return _json({"error": "not found"}, 404)

    data = _serialize_entity(ep)

    # Assertions
    assertions = []
    try:
        for section_name, section_assertions in (ep.sections or {}).items():
            for a in section_assertions:
                if isinstance(a, dict):
                    if a.get("superseded"):
                        continue
                    assertions.append({
                        "key": a.get("key", ""),
                        "value": str(a.get("value", "")),
                        "confidence": a.get("confidence", 0),
                        "source": a.get("source_url", ""),
                        "timestamp": str(a.get("observed_at", "")),
                    })
                else:
                    if getattr(a, "superseded", False):
                        continue
                    assertions.append({
                        "key": a.key,
                        "value": str(a.value),
                        "confidence": a.confidence,
                        "source": getattr(a, "source_url", ""),
                        "timestamp": str(getattr(a, "observed_at", "")),
                    })
    except Exception as e:
        logger.warning("Entity assertions parse failed: %s", e)
    data["assertions"] = assertions

    # Relationships from graph
    relationships = []
    try:
        if stores.graph.available:
            graph = await stores.graph.get_ego_graph(ep.canonical_name, depth=1)
            for edge in graph.get("edges", []):
                relationships.append({
                    "source": edge.get("source", ""),
                    "target": edge.get("target", ""),
                    "rel_type": edge.get("rel_type", ""),
                    "properties": {k: v for k, v in edge.items() if k not in ("source", "target", "rel_type")},
                })
    except Exception as e:
        logger.warning("Entity relationships fetch failed: %s", e)
    data["relationships"] = relationships

    return _json(data)


@router.delete("/entities/{entity_id}")
async def delete_entity(request: Request, entity_id: str):
    stores = get_stores(request)
    try:
        uid = UUID(entity_id)
    except (ValueError, AttributeError):
        return _json({"error": "invalid entity_id"}, 400)
    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute("DELETE FROM signal_entity_links WHERE entity_id = $1", uid)
            await conn.execute("DELETE FROM entity_profiles WHERE id = $1", uid)
        return _json({"status": "deleted"})
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


# ------------------------------------------------------------------
# Sources
# ------------------------------------------------------------------

@router.get("/sources")
async def list_sources(
    request: Request,
    offset: int = 0,
    limit: int = Query(default=50, le=200),
    status: str | None = None,
    q: str | None = None,
):
    stores = get_stores(request)

    if not stores.structured._available:
        return _json({"items": [], "total": 0, "offset": offset, "limit": limit})

    conditions, params, idx = [], [], 1
    if q:
        conditions.append(f"LOWER(name) LIKE LOWER(${idx})")
        params.append(f"%{q}%")
        idx += 1
    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    try:
        async with stores.structured._pool.acquire() as conn:
            total = await conn.fetchval(f"SELECT count(*) FROM sources {where}", *params)
            rows = await conn.fetch(
                f"SELECT id, name, url, source_type, status, "
                f"fetch_success_count, fetch_failure_count, "
                f"events_produced_count, last_successful_fetch_at, "
                f"source_quality_score "
                f"FROM sources {where} "
                f"ORDER BY events_produced_count DESC NULLS LAST "
                f"LIMIT ${idx} OFFSET ${idx + 1}",
                *params, limit, offset,
            )
            items = [
                {
                    "source_id": str(r["id"]),
                    "name": r["name"],
                    "url": r["url"],
                    "source_type": r["source_type"],
                    "status": r["status"],
                    "fetch_count": r["fetch_success_count"] or 0,
                    "fail_count": r["fetch_failure_count"] or 0,
                    "event_count": r["events_produced_count"] or 0,
                    "last_fetched": r["last_successful_fetch_at"].isoformat() if r["last_successful_fetch_at"] else None,
                    "quality_score": round(r["source_quality_score"] or 0.0, 3),
                }
                for r in rows
            ]
            return _json({"items": items, "total": total, "offset": offset, "limit": limit})
    except Exception as exc:
        logger.warning("Sources query failed: %s", exc)
        return _json({"items": [], "total": 0, "offset": offset, "limit": limit})


class SourceCreate(BaseModel):
    name: str
    url: str
    source_type: str = "rss"


@router.post("/sources")
async def create_source(request: Request, body: SourceCreate):
    stores = get_stores(request)
    from uuid import uuid4
    sid = uuid4()
    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO sources (id, name, url, source_type, status) VALUES ($1, $2, $3, $4, 'active')",
                sid, body.name, body.url, body.source_type,
            )
        return _json({"source_id": str(sid), "status": "created"}, 201)
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


@router.put("/sources/{source_id}")
async def update_source(request: Request, source_id: str, body: dict = Body(...)):
    stores = get_stores(request)
    try:
        uid = UUID(source_id)
    except (ValueError, AttributeError):
        return _json({"error": "invalid source_id"}, 400)
    allowed = {"name", "url", "source_type", "status"}
    sets, params, idx = [], [], 1
    for k, v in body.items():
        if k in allowed:
            sets.append(f"{k} = ${idx}")
            params.append(v)
            idx += 1
    if not sets:
        return _json({"error": "no valid fields"}, 400)
    params.append(uid)
    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute(
                f"UPDATE sources SET {', '.join(sets)} WHERE id = ${idx}", *params
            )
        return _json({"status": "updated"})
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


@router.delete("/sources/{source_id}")
async def delete_source(request: Request, source_id: str):
    stores = get_stores(request)
    try:
        uid = UUID(source_id)
    except (ValueError, AttributeError):
        return _json({"error": "invalid source_id"}, 400)
    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute("DELETE FROM sources WHERE id = $1", uid)
        return _json({"status": "deleted"})
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


# ------------------------------------------------------------------
# Goals
# ------------------------------------------------------------------

@router.get("/goals")
async def list_goals(request: Request):
    stores = get_stores(request)

    if not stores.structured._available:
        return _json([])

    try:
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, data, status, priority, parent_id, created_at, updated_at "
                "FROM goals ORDER BY priority ASC, created_at ASC"
            )

        goal_map = {}
        roots = []

        for r in rows:
            data = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
            item = {
                "goal_id": str(r["id"]),
                "description": data.get("description", ""),
                "status": r["status"],
                "priority": r["priority"],
                "progress_pct": data.get("progress_pct", 0) or 0,
                "parent_id": str(r["parent_id"]) if r["parent_id"] else None,
                "children": [],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            goal_map[str(r["id"])] = item

        for item in goal_map.values():
            if item["parent_id"] and item["parent_id"] in goal_map:
                goal_map[item["parent_id"]]["children"].append(item)
            else:
                roots.append(item)

        return _json(roots)
    except Exception as exc:
        logger.warning("Goals query failed: %s", exc)
        return _json([])


@router.put("/goals/{goal_id}")
async def update_goal(request: Request, goal_id: str, body: dict = Body(...)):
    stores = get_stores(request)
    try:
        uid = UUID(goal_id)
    except (ValueError, AttributeError):
        return _json({"error": "invalid goal_id"}, 400)
    try:
        async with stores.structured._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT data FROM goals WHERE id = $1", uid)
            if not row:
                return _json({"error": "not found"}, 404)
            data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
            # Update allowed fields
            for k in ("status", "priority", "description", "progress_pct"):
                if k in body:
                    data[k] = body[k]
            sets = ["data = $1"]
            params = [json.dumps(data)]
            if "status" in body:
                sets.append(f"status = ${len(params) + 1}")
                params.append(body["status"])
            if "priority" in body:
                sets.append(f"priority = ${len(params) + 1}")
                params.append(int(body["priority"]))
            params.append(uid)
            await conn.execute(
                f"UPDATE goals SET {', '.join(sets)}, updated_at = NOW() WHERE id = ${len(params)}",
                *params,
            )
        return _json({"status": "updated"})
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


@router.delete("/goals/{goal_id}")
async def delete_goal(request: Request, goal_id: str):
    stores = get_stores(request)
    try:
        uid = UUID(goal_id)
    except (ValueError, AttributeError):
        return _json({"error": "invalid goal_id"}, 400)
    try:
        async with stores.structured._pool.acquire() as conn:
            # Unparent children before deleting
            await conn.execute("UPDATE goals SET parent_id = NULL WHERE parent_id = $1", uid)
            await conn.execute("DELETE FROM goals WHERE id = $1", uid)
        return _json({"status": "deleted"})
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


# ------------------------------------------------------------------
# Situations
# ------------------------------------------------------------------

@router.get("/situations")
async def list_situations(request: Request, status: str | None = None):
    stores = get_stores(request)

    if not stores.structured._available:
        return _json([])

    conditions, params, idx = [], [], 1
    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    try:
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT id, name, status, category, intensity_score, created_at, updated_at "
                f"FROM situations {where} "
                f"ORDER BY intensity_score DESC NULLS LAST, updated_at DESC",
                *params,
            )
            items = [
                {
                    "situation_id": str(r["id"]),
                    "title": r["name"],
                    "status": r["status"],
                    "severity": r["category"] or "medium",
                    "event_count": 0,
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                }
                for r in rows
            ]
            return _json(items)
    except Exception as exc:
        logger.warning("Situations query failed: %s", exc)
        return _json([])


# ------------------------------------------------------------------
# Watchlist
# ------------------------------------------------------------------

@router.get("/watchlist")
async def list_watchlist(request: Request):
    stores = get_stores(request)

    if not stores.structured._available:
        return _json([])

    try:
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, data, priority, active, created_at, "
                "last_triggered_at, trigger_count "
                "FROM watchlist WHERE active = true ORDER BY created_at DESC"
            )
            items = []
            for r in rows:
                data = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
                items.append({
                    "watch_id": str(r["id"]),
                    "entity_name": r["name"],
                    "watch_type": r["priority"],
                    "description": data.get("description", ""),
                    "entities": data.get("entities", []),
                    "keywords": data.get("keywords", []),
                    "categories": data.get("categories", []),
                    "trigger_count": r["trigger_count"] or 0,
                    "triggers": {},
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                })
            return _json(items)
    except Exception as exc:
        logger.warning("Watchlist query failed: %s", exc)
        return _json([])


class WatchCreate(BaseModel):
    name: str
    description: str = ""
    entities: list[str] = []
    keywords: list[str] = []
    categories: list[str] = []
    priority: str = "medium"


@router.post("/watchlist")
async def create_watch(request: Request, body: WatchCreate):
    stores = get_stores(request)
    from uuid import uuid4
    wid = uuid4()
    data_obj = {
        "description": body.description,
        "entities": body.entities,
        "keywords": body.keywords,
        "categories": body.categories,
    }
    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO watchlist (id, name, data, priority, active) VALUES ($1, $2, $3, $4, true)",
                wid, body.name, json.dumps(data_obj), body.priority,
            )
        return _json({"watch_id": str(wid), "status": "created"}, 201)
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


@router.put("/watchlist/{watch_id}")
async def update_watch(request: Request, watch_id: str, body: dict = Body(...)):
    stores = get_stores(request)
    try:
        uid = UUID(watch_id)
    except (ValueError, AttributeError):
        return _json({"error": "invalid watch_id"}, 400)
    try:
        async with stores.structured._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT name, data, priority FROM watchlist WHERE id = $1", uid)
            if not row:
                return _json({"error": "not found"}, 404)
            data_obj = json.loads(row["data"]) if isinstance(row["data"], str) else (row["data"] or {})
            name = body.get("name", row["name"])
            priority = body.get("priority", row["priority"])
            for k in ("description", "entities", "keywords", "categories"):
                if k in body:
                    data_obj[k] = body[k]
            await conn.execute(
                "UPDATE watchlist SET name = $1, data = $2, priority = $3 WHERE id = $4",
                name, json.dumps(data_obj), priority, uid,
            )
        return _json({"status": "updated"})
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


@router.delete("/watchlist/{watch_id}")
async def delete_watch(request: Request, watch_id: str):
    stores = get_stores(request)
    try:
        uid = UUID(watch_id)
    except (ValueError, AttributeError):
        return _json({"error": "invalid watch_id"}, 400)
    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute("DELETE FROM watch_triggers WHERE watch_id = $1", uid)
            await conn.execute("DELETE FROM watchlist WHERE id = $1", uid)
        return _json({"status": "deleted"})
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


@router.get("/watchlist/triggers")
async def list_watch_triggers(request: Request):
    stores = get_stores(request)

    if not stores.structured._available:
        return _json([])

    try:
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT wt.id, wt.watch_name, wt.event_title, "
                "wt.match_reasons, wt.priority, wt.triggered_at "
                "FROM watch_triggers wt "
                "ORDER BY wt.triggered_at DESC LIMIT 20"
            )
            triggers = [
                {
                    "watch_id": str(r["id"]),
                    "entity_name": r["watch_name"],
                    "trigger_type": r["priority"],
                    "details": r["event_title"],
                    "triggered_at": r["triggered_at"].isoformat() if r["triggered_at"] else None,
                }
                for r in rows
            ]
            return _json(triggers)
    except Exception as exc:
        logger.warning("Watch triggers query failed: %s", exc)
        return _json([])


# ------------------------------------------------------------------
# Predictions / Hypothesis Tracking
# ------------------------------------------------------------------

class PredictionCreate(BaseModel):
    hypothesis: str
    source_cycle: int = 0
    source_type: str = "report"
    category: str = ""
    region: str = ""
    confidence: float = 0.5


class PredictionUpdate(BaseModel):
    status: str | None = None
    evidence_for: str | None = None
    evidence_against: str | None = None
    confidence: float | None = None
    resolution_note: str | None = None
    resolution_cycle: int | None = None


@router.get("/predictions")
async def list_predictions(request: Request, status: str | None = None, limit: int = 50):
    stores = get_stores(request)
    if not stores.structured._available:
        return _json([])
    try:
        items = await stores.structured.list_predictions(status=status, limit=limit)
        return _json(items)
    except Exception as exc:
        logger.warning("Predictions list failed: %s", exc)
        return _json([])


@router.post("/predictions")
async def create_prediction(request: Request, body: PredictionCreate):
    stores = get_stores(request)
    if not stores.structured._available:
        return _json({"error": "structured store unavailable"}, 503)
    try:
        pid = await stores.structured.create_prediction(
            hypothesis=body.hypothesis,
            source_cycle=body.source_cycle,
            source_type=body.source_type,
            category=body.category,
            region=body.region,
            confidence=body.confidence,
        )
        if not pid:
            return _json({"error": "failed to create prediction"}, 500)
        return _json({"prediction_id": pid, "status": "created"}, 201)
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


@router.get("/predictions/{prediction_id}")
async def get_prediction(request: Request, prediction_id: str):
    stores = get_stores(request)
    if not stores.structured._available:
        return _json({"error": "structured store unavailable"}, 503)
    try:
        item = await stores.structured.get_prediction(prediction_id)
        if not item:
            return _json({"error": "not found"}, 404)
        return _json(item)
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


@router.put("/predictions/{prediction_id}")
async def update_prediction(request: Request, prediction_id: str, body: PredictionUpdate):
    stores = get_stores(request)
    if not stores.structured._available:
        return _json({"error": "structured store unavailable"}, 503)
    try:
        ok = await stores.structured.update_prediction(
            prediction_id=prediction_id,
            status=body.status,
            evidence_for=body.evidence_for,
            evidence_against=body.evidence_against,
            confidence=body.confidence,
            resolution_note=body.resolution_note,
            resolution_cycle=body.resolution_cycle,
        )
        if not ok:
            return _json({"error": "not found or update failed"}, 404)
        return _json({"status": "updated"})
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


# ------------------------------------------------------------------
# Hypotheses (ACH — competing thesis/counter-thesis pairs)
# ------------------------------------------------------------------

@router.get("/hypotheses")
async def list_hypotheses(
    request: Request,
    status: str | None = "active",
    situation_id: str | None = None,
    limit: int = 20,
):
    stores = get_stores(request)
    if not stores.structured._available:
        return _json([])
    try:
        items = await stores.structured.list_hypotheses(
            status=status, situation_id=situation_id, limit=limit,
        )
        return _json(items)
    except Exception as exc:
        logger.warning("Hypotheses list failed: %s", exc)
        return _json([])


# ------------------------------------------------------------------
# Situation Briefs (SYNTHESIZE deliverables)
# ------------------------------------------------------------------

@router.get("/briefs")
async def list_briefs(request: Request, limit: int = 20):
    """List situation briefs from Redis (newest first)."""
    stores = get_stores(request)
    try:
        import json as _j
        raw = await stores.registers._redis.lrange("legba:situation_briefs", 0, limit - 1)
        briefs = []
        for item in raw or []:
            try:
                data = _j.loads(item)
                briefs.append(data)
            except Exception:
                continue
        return _json(briefs)
    except Exception as exc:
        logger.warning("Briefs list failed: %s", exc)
        return _json([])


# ------------------------------------------------------------------
# Facts
# ------------------------------------------------------------------

@router.get("/facts/predicates")
async def fact_predicates(request: Request):
    """List distinct predicates with counts for filter dropdowns."""
    stores = get_stores(request)
    if not stores.structured._available:
        return _json([])
    try:
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT predicate, COUNT(*) as cnt FROM facts "
                "GROUP BY predicate ORDER BY cnt DESC LIMIT 50"
            )
            return _json([{"predicate": r["predicate"], "count": r["cnt"]} for r in rows])
    except Exception as exc:
        logger.warning("Fact predicates query failed: %s", exc)
        return _json([])


@router.get("/facts")
async def list_facts(
    request: Request,
    offset: int = 0,
    limit: int = Query(default=50, le=200),
    q: str | None = None,
    predicate: str | None = None,
    min_confidence: float | None = None,
    subject: str | None = None,
):
    stores = get_stores(request)

    if not stores.structured._available:
        return _json({"items": [], "total": 0, "offset": offset, "limit": limit})

    conditions, params, idx = [], [], 1
    if q:
        conditions.append(f"(LOWER(subject) LIKE LOWER(${idx}) OR LOWER(value) LIKE LOWER(${idx}))")
        params.append(f"%{q}%")
        idx += 1
    if predicate:
        conditions.append(f"predicate = ${idx}")
        params.append(predicate)
        idx += 1
    if min_confidence is not None and min_confidence > 0:
        conditions.append(f"confidence >= ${idx}")
        params.append(min_confidence)
        idx += 1
    if subject:
        conditions.append(f"LOWER(subject) LIKE LOWER(${idx})")
        params.append(f"%{subject}%")
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    try:
        async with stores.structured._pool.acquire() as conn:
            total = await conn.fetchval(f"SELECT count(*) FROM facts {where}", *params)
            rows = await conn.fetch(
                f"SELECT id, subject, predicate, value, confidence, source_cycle, created_at "
                f"FROM facts {where} ORDER BY created_at DESC "
                f"LIMIT ${idx} OFFSET ${idx + 1}",
                *params, limit, offset,
            )
            items = [
                {
                    "fact_id": str(r["id"]),
                    "subject": r["subject"],
                    "predicate": r["predicate"],
                    "object": r["value"],
                    "confidence": float(r["confidence"]) if r["confidence"] else 0,
                    "source": f"cycle {r['source_cycle']}" if r["source_cycle"] else "",
                    "timestamp": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ]
            return _json({"items": items, "total": total, "offset": offset, "limit": limit})
    except Exception as exc:
        logger.warning("Facts query failed: %s", exc)
        return _json({"items": [], "total": 0, "offset": offset, "limit": limit})


@router.delete("/facts/{fact_id}")
async def delete_fact(request: Request, fact_id: str):
    stores = get_stores(request)
    try:
        uid = UUID(fact_id)
    except (ValueError, AttributeError):
        return _json({"error": "invalid fact_id"}, 400)
    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute("DELETE FROM facts WHERE id = $1", uid)
        return _json({"status": "deleted"})
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


# ------------------------------------------------------------------
# Memory (Qdrant)
# ------------------------------------------------------------------

@router.get("/memory")
async def list_memory(
    request: Request,
    collection: str = "legba_short_term",
    q: str | None = None,
    offset: str | None = None,
):
    stores = get_stores(request)

    if q:
        results = await stores.search_memories(collection, q, limit=50)
        return _json({"points": results, "next_offset": None})

    points, next_offset = await stores.get_memories(collection, limit=50, offset=offset)
    return _json({"points": points, "next_offset": next_offset})


@router.delete("/memory/{collection}/{point_id}")
async def delete_memory(request: Request, collection: str, point_id: str):
    stores = get_stores(request)
    try:
        from qdrant_client.models import PointIdsList
        stores._qdrant.delete(
            collection_name=collection,
            points_selector=PointIdsList(points=[point_id]),
        )
        return _json({"status": "deleted"})
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


# ------------------------------------------------------------------
# Cycles (Audit OpenSearch)
# ------------------------------------------------------------------

# Phase keywords that indicate cycle type (same logic as cycles.py)
_CYCLE_TYPE_KEYWORDS = {
    "evolve": "EVOLVE",
    "introspection": "INTROSPECTION",
    "synthesize": "SYNTHESIZE",
    "analysis": "ANALYSIS",
    "analyze": "ANALYSIS",
    "research": "RESEARCH",
    "curate": "CURATE",
    "survey": "SURVEY",
    "acquire": "ACQUIRE",
}


def _detect_cycle_type(phase_names: list[str]) -> str:
    """Detect cycle type from phase event names."""
    priority = ["EVOLVE", "INTROSPECTION", "SYNTHESIZE", "ANALYSIS", "RESEARCH", "CURATE", "SURVEY", "ACQUIRE"]
    detected = set()
    for name in phase_names:
        lower = name.lower()
        for keyword, ctype in _CYCLE_TYPE_KEYWORDS.items():
            if keyword in lower:
                detected.add(ctype)
    for p in priority:
        if p in detected:
            return p
    return "SURVEY"


@router.get("/cycles")
async def list_cycles(
    request: Request,
    offset: int = 0,
    limit: int = Query(default=50, le=200),
):
    stores = get_stores(request)

    try:
        # Aggregate audit entries by cycle number (field is "cycle" in the audit index)
        agg_result = await stores.audit.aggregate(
            "legba-audit-*",
            aggs={
                "cycles": {
                    "terms": {
                        "field": "cycle",
                        "size": limit,
                        "order": {"_key": "desc"},
                    },
                    "aggs": {
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

        items = []
        for b in buckets:
            cycle_num = b.get("key")
            min_ts = b.get("min_ts", {}).get("value_as_string")
            max_ts = b.get("max_ts", {}).get("value_as_string")

            # Compute duration
            duration_s = 0
            if min_ts and max_ts:
                try:
                    dt_min = datetime.fromisoformat(str(min_ts).replace("Z", "+00:00"))
                    dt_max = datetime.fromisoformat(str(max_ts).replace("Z", "+00:00"))
                    duration_s = int((dt_max - dt_min).total_seconds())
                except Exception as e:
                    logger.debug("Cycle duration parse failed: %s", e)

            # Extract phase names for cycle type detection
            phase_buckets = (
                b.get("phase_names", {})
                .get("names", {})
                .get("buckets", [])
            )
            phase_names = [pb.get("key", "") for pb in phase_buckets]
            cycle_type = _detect_cycle_type(phase_names)

            items.append({
                "cycle_number": cycle_num,
                "cycle_type": cycle_type,
                "started_at": min_ts or "",
                "duration_s": duration_s,
                "tool_calls": b.get("tool_count", {}).get("doc_count", 0),
                "llm_calls": b.get("llm_count", {}).get("doc_count", 0),
                "events_stored": 0,
                "errors": b.get("error_count", {}).get("doc_count", 0),
            })

        total = len(items)
        return _json({
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
        })
    except Exception as exc:
        logger.warning("Cycles query failed: %s", exc)
        return _json({"items": [], "total": 0, "offset": offset, "limit": limit})


@router.get("/cycles/{cycle_number}")
async def get_cycle(request: Request, cycle_number: int):
    stores = get_stores(request)

    try:
        # Query all audit entries for this cycle (field is "cycle")
        result = await stores.audit.search(
            "legba-audit-*",
            {"term": {"cycle": cycle_number}},
            size=500,
            sort=[{"timestamp": {"order": "asc"}}],
        )
        hits = result.get("hits", [])
        if not hits:
            return _json({"error": "not found"}, 404)

        # Aggregate stats from individual audit entries
        tool_calls = 0
        llm_calls = 0
        errors = 0
        total_tokens = 0
        phases = []
        timestamps = []

        for hit in hits:
            event_type = hit.get("event", "")
            ts = hit.get("timestamp")
            if ts:
                timestamps.append(ts)

            if event_type == "tool_call":
                tool_calls += 1
            elif event_type == "llm_call":
                llm_calls += 1
                usage = hit.get("usage")
                if isinstance(usage, dict):
                    total_tokens += usage.get("total_tokens", 0) or 0
            elif event_type == "error":
                errors += 1
            elif event_type == "phase":
                phase_name = hit.get("phase", "")
                if phase_name:
                    phases.append({"phase": phase_name, "timestamp": ts})

        # Detect cycle type from phase names
        phase_names = [p["phase"] for p in phases]
        cycle_type = _detect_cycle_type(phase_names)

        # Compute duration from first/last timestamps
        duration_s = 0
        started_at = ""
        if timestamps:
            started_at = timestamps[0]
            try:
                ts_list = []
                for t in timestamps:
                    try:
                        ts_list.append(datetime.fromisoformat(str(t).replace("Z", "+00:00")))
                    except Exception as e:
                        logger.debug("Cycle detail timestamp parse failed: %s", e)
                        continue
                if len(ts_list) >= 2:
                    duration_s = int((max(ts_list) - min(ts_list)).total_seconds())
            except Exception as e:
                logger.debug("Cycle detail duration computation failed: %s", e)

        return _json({
            "cycle_number": cycle_number,
            "cycle_type": cycle_type,
            "started_at": started_at,
            "duration_s": duration_s,
            "tool_calls": tool_calls,
            "llm_calls": llm_calls,
            "events_stored": 0,
            "errors": errors,
            "total_tokens": total_tokens,
            "phases": phases,
        })
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


# ------------------------------------------------------------------
# Graph edges (JSON mutations)
# ------------------------------------------------------------------

@router.post("/graph/edges")
async def add_edge(request: Request):
    body = await request.json()
    stores = get_stores(request)

    try:
        await stores.graph.store_relationship(
            source=body["source"],
            target=body["target"],
            rel_type=body["rel_type"],
            properties=body.get("properties", {}),
        )
        return _json({"status": "created"})
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


@router.delete("/graph/edges")
async def remove_edge(request: Request):
    body = await request.json()
    stores = get_stores(request)

    try:
        await stores.graph.remove_relationship(
            source=body["source"],
            target=body["target"],
            rel_type=body["rel_type"],
        )
        return _json({"status": "deleted"})
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


# ------------------------------------------------------------------
# Operator Scorecard
# ------------------------------------------------------------------

@router.get("/scorecard")
async def get_scorecard(request: Request):
    """Return the latest operator scorecard computed during INTROSPECTION."""
    stores = get_stores(request)
    try:
        scorecard = await stores.registers.get_json("scorecard")
        cycle = await stores.registers.get_int("scorecard_cycle", 0)
        return _json({"cycle": cycle, "data": scorecard or {}})
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


# ------------------------------------------------------------------
# Global Search
# ------------------------------------------------------------------

@router.get("/search")
async def global_search(request: Request, q: str = Query(..., min_length=2)):
    """Search across entities, events, facts, and situations."""
    stores = get_stores(request)
    results: dict = {"entities": [], "events": [], "facts": [], "situations": []}

    if not stores.structured._available:
        return _json(results)

    pattern = f"%{q}%"

    try:
        async with stores.structured._pool.acquire() as conn:
            # Entities: search canonical_name
            try:
                rows = await conn.fetch(
                    "SELECT id, canonical_name, data->>'entity_type' as entity_type "
                    "FROM entity_profiles WHERE canonical_name ILIKE $1 LIMIT 10",
                    pattern,
                )
                results["entities"] = [
                    {"id": str(r["id"]), "canonical_name": r["canonical_name"],
                     "entity_type": r["entity_type"] or "unknown"}
                    for r in rows
                ]
            except Exception as exc:
                logger.debug("Search entities failed: %s", exc)

            # Events: search title
            try:
                rows = await conn.fetch(
                    "SELECT id, title, category FROM signals "
                    "WHERE title ILIKE $1 ORDER BY created_at DESC LIMIT 10",
                    pattern,
                )
                results["events"] = [
                    {"id": str(r["id"]), "title": r["title"], "category": r["category"] or "other"}
                    for r in rows
                ]
            except Exception as exc:
                logger.debug("Search events failed: %s", exc)

            # Facts: search subject or value
            try:
                rows = await conn.fetch(
                    "SELECT id, subject, predicate, value FROM facts "
                    "WHERE (subject ILIKE $1 OR value ILIKE $1) "
                    "AND superseded_by IS NULL LIMIT 10",
                    pattern,
                )
                results["facts"] = [
                    {"id": str(r["id"]), "subject": r["subject"],
                     "predicate": r["predicate"], "value": r["value"]}
                    for r in rows
                ]
            except Exception as exc:
                logger.debug("Search facts failed: %s", exc)

            # Situations: search name
            try:
                rows = await conn.fetch(
                    "SELECT id, name, status FROM situations "
                    "WHERE name ILIKE $1 LIMIT 10",
                    pattern,
                )
                results["situations"] = [
                    {"id": str(r["id"]), "title": r["name"], "status": r["status"] or "active"}
                    for r in rows
                ]
            except Exception as exc:
                logger.debug("Search situations failed: %s", exc)

    except Exception as exc:
        logger.warning("Global search failed: %s", exc)

    return _json(results)


# ---------------------------------------------------------------------------
# Proposed Edges (relationship inference queue)
# ---------------------------------------------------------------------------


@router.get("/proposed-edges")
async def list_proposed_edges(request: Request, status: str = "pending", limit: int = 50):
    """List proposed graph edges by status (pending/approved/rejected)."""
    stores = request.app.state.stores
    try:
        items = await stores.structured.list_proposed_edges(status=status, limit=limit)
        return _json({"items": items, "total": len(items)})
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


@router.post("/proposed-edges/{edge_id}/review")
async def review_proposed_edge(request: Request, edge_id: str):
    """Approve or reject a proposed edge. Body: {"action": "approved"|"rejected"}"""
    stores = request.app.state.stores
    try:
        body = await request.json()
        action = body.get("action", "")
        if action not in ("approved", "rejected"):
            return _json({"error": "action must be 'approved' or 'rejected'"}, 400)

        ok = await stores.structured.review_proposed_edge(edge_id, action)
        if not ok:
            return _json({"error": "edge not found or already reviewed"}, 404)

        # If approved, commit to graph
        if action == "approved":
            try:
                edge_list = await stores.structured.list_proposed_edges(status="approved", limit=1)
                # Re-fetch the specific edge to get its data
                async with stores.structured._pool.acquire() as conn:
                    from uuid import UUID as _UUID
                    r = await conn.fetchrow(
                        "SELECT source_entity, target_entity, relationship_type, source_cycle "
                        "FROM proposed_edges WHERE id = $1",
                        _UUID(edge_id),
                    )
                    if r and stores.graph and stores.graph.available:
                        await stores.graph.add_relationship(
                            r["source_entity"], r["target_entity"], r["relationship_type"],
                            {"source_cycle": r["source_cycle"], "operator_approved": True},
                        )
            except Exception as exc:
                logger.warning("Graph commit for approved edge failed: %s", exc)

        return _json({"success": True, "action": action})
    except Exception as exc:
        return _json({"error": str(exc)}, 500)
