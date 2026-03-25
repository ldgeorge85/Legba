"""
Event Storage & Query Tools

Store events to both Postgres (structured queries) and OpenSearch
(full-text search). Query and search events across both stores.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID

logger = logging.getLogger(__name__)

from ....shared.schemas.tools import ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from ...memory.structured import StructuredStore
    from ...memory.opensearch import OpenSearchStore
    from ...tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Duplicate detection helpers
# ---------------------------------------------------------------------------

def _title_words(title: str) -> set[str]:
    """Extract normalised significant words from a title."""
    stopwords = {"a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "is", "was", "by", "from", "with"}
    return {w for w in title.lower().split() if w not in stopwords and len(w) > 1}


def _title_similarity(a: str, b: str) -> float:
    """Jaccard similarity of title words."""
    wa, wb = _title_words(a), _title_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


# ---------------------------------------------------------------------------
# Index management
# ---------------------------------------------------------------------------

_SIGNALS_INDEX_MAPPINGS = {
    "properties": {
        "title": {"type": "text", "analyzer": "standard"},
        "summary": {"type": "text", "analyzer": "standard"},
        "full_content": {"type": "text", "analyzer": "standard"},
        "category": {"type": "keyword"},
        "actors": {"type": "keyword"},
        "locations": {"type": "keyword"},
        "tags": {"type": "keyword"},
        "language": {"type": "keyword"},
        "source_id": {"type": "keyword"},
        "source_url": {"type": "keyword"},
        "confidence": {"type": "float"},
        "event_timestamp": {"type": "date"},
        "created_at": {"type": "date"},
        "geo_countries": {"type": "keyword"},
    }
}

_ensured_indices: set[str] = set()


async def _ensure_signals_index(opensearch: OpenSearchStore, index_name: str) -> None:
    """Idempotent index creation with proper mappings."""
    if index_name in _ensured_indices:
        return
    await opensearch.create_index(
        index_name,
        mappings=_SIGNALS_INDEX_MAPPINGS,
        settings={"number_of_shards": 1, "number_of_replicas": 0},
    )
    _ensured_indices.add(index_name)


def _signals_index_name() -> str:
    """Generate time-partitioned index name: legba-signals-YYYY.MM"""
    now = datetime.now(timezone.utc)
    return f"legba-signals-{now.strftime('%Y.%m')}"


# ---------------------------------------------------------------------------
# Post-store hooks (best-effort, non-blocking)
# ---------------------------------------------------------------------------

async def _check_watchlist_matches(structured: StructuredStore, event) -> list[dict]:
    """Check if a newly stored event triggers any active watch patterns.

    Matching is AND across criteria types (all non-empty must match),
    OR within each type (at least one entity/keyword/region must hit).
    """
    try:
        async with structured._pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'watchlist')"
            )
            if not exists:
                return []

            rows = await conn.fetch(
                "SELECT id, data, name, priority FROM watchlist WHERE active = true"
            )
            if not rows:
                return []

            matches = []
            event_text = f"{event.title} {event.summary or ''}".lower()
            event_actors_lower = {a.lower() for a in event.actors}
            event_locations_lower = {loc.lower() for loc in event.locations}
            event_geo_lower = {g.lower() for g in (event.geo_countries or [])}

            for row in rows:
                raw = row["data"]
                data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}

                matched_criteria = []
                failed = False

                # Entity matching
                watch_entities = [e.lower() for e in data.get("entities", [])]
                if watch_entities:
                    hit = next(
                        (we for we in watch_entities
                         if we in event_actors_lower or we in event_locations_lower or we in event_text),
                        None,
                    )
                    if hit:
                        matched_criteria.append(f"entity:{hit}")
                    else:
                        failed = True

                # Keyword matching
                if not failed:
                    watch_keywords = [k.lower() for k in data.get("keywords", [])]
                    if watch_keywords:
                        hit = next((kw for kw in watch_keywords if kw in event_text), None)
                        if hit:
                            matched_criteria.append(f"keyword:{hit}")
                        else:
                            failed = True

                # Category matching
                if not failed:
                    watch_categories = [c.lower() for c in data.get("categories", [])]
                    if watch_categories:
                        if str(event.category.value if hasattr(event.category, 'value') else event.category).lower() in watch_categories:
                            matched_criteria.append(f"category:{str(event.category.value if hasattr(event.category, 'value') else event.category)}")
                        else:
                            failed = True

                # Region matching
                if not failed:
                    watch_regions = [r.lower() for r in data.get("regions", [])]
                    if watch_regions:
                        all_locs = event_locations_lower | event_geo_lower
                        hit = next(
                            (wr for wr in watch_regions if wr in all_locs or wr in event_text),
                            None,
                        )
                        if hit:
                            matched_criteria.append(f"region:{hit}")
                        else:
                            failed = True

                if not failed and matched_criteria:
                    from uuid import uuid4
                    await conn.execute(
                        "INSERT INTO watch_triggers "
                        "(id, watch_id, event_id, watch_name, event_title, match_reasons, priority) "
                        "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)",
                        uuid4(), row["id"], event.id,
                        row["name"], event.title,
                        json.dumps(matched_criteria), row["priority"],
                    )
                    await conn.execute(
                        "UPDATE watchlist SET trigger_count = trigger_count + 1, "
                        "last_triggered_at = NOW() WHERE id = $1",
                        row["id"],
                    )
                    matches.append({
                        "watch_id": str(row["id"]),
                        "watch_name": row["name"],
                        "priority": row["priority"],
                        "matched": matched_criteria,
                    })

            return matches
    except Exception:
        return []


async def _suggest_situations(structured: StructuredStore, event) -> list[dict]:
    """Suggest active situations that may be related to this event."""
    try:
        async with structured._pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'situations')"
            )
            if not exists:
                return []

            rows = await conn.fetch(
                "SELECT id, data, name, status, category FROM situations "
                "WHERE status != 'resolved' "
                "ORDER BY intensity_score DESC LIMIT 50"
            )
            if not rows:
                return []

            suggestions = []
            event_actors_lower = {a.lower() for a in event.actors}
            event_locations_lower = {loc.lower() for loc in event.locations}
            event_geo_lower = {g.lower() for g in (event.geo_countries or [])}

            for row in rows:
                raw = row["data"]
                data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}

                score = 0.0
                reasons = []

                # Entity overlap
                sit_entities = {e.lower() for e in data.get("key_entities", [])}
                entity_overlap = sit_entities & (event_actors_lower | event_locations_lower)
                if entity_overlap:
                    score += 0.4 * min(len(entity_overlap), 3)
                    reasons.append(f"entities: {', '.join(sorted(entity_overlap)[:3])}")

                # Region overlap
                sit_regions = {r.lower() for r in data.get("regions", [])}
                region_overlap = sit_regions & (event_locations_lower | event_geo_lower)
                if region_overlap:
                    score += 0.3
                    reasons.append(f"regions: {', '.join(sorted(region_overlap)[:3])}")

                # Category match
                if row["category"] and row["category"] == str(event.category.value if hasattr(event.category, 'value') else event.category):
                    score += 0.2
                    reasons.append(f"category: {str(event.category.value if hasattr(event.category, 'value') else event.category)}")

                if score >= 0.3:
                    suggestions.append({
                        "situation_id": str(row["id"]),
                        "situation_name": row["name"],
                        "relevance": round(min(score, 1.0), 2),
                        "reasons": reasons,
                    })

            suggestions.sort(key=lambda x: x["relevance"], reverse=True)
            return suggestions[:3]
    except Exception:
        return []


async def _compute_novelty(structured: StructuredStore, event) -> dict | None:
    """Score how novel/unexpected this event is given existing knowledge."""
    try:
        async with structured._pool.acquire() as conn:
            score = 0.5  # baseline
            factors = []

            # Actor novelty: are actors known entities?
            if event.actors:
                known = 0
                for actor in event.actors[:5]:
                    exists = await conn.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM entity_profiles "
                        "WHERE lower(canonical_name) = $1)",
                        actor.lower(),
                    )
                    if exists:
                        known += 1
                unknown = len(event.actors[:5]) - known
                if unknown > 0:
                    score += min(0.2, 0.1 * unknown)
                    factors.append(f"{unknown}_new_actors")

            # Category rarity
            total = await conn.fetchval("SELECT count(*) FROM signals")
            if total and total > 20:
                cat_count = await conn.fetchval(
                    "SELECT count(*) FROM signals WHERE category = $1",
                    str(event.category.value if hasattr(event.category, 'value') else event.category),
                )
                ratio = cat_count / total
                if ratio < 0.05:
                    score += 0.2
                    factors.append("rare_category")
                elif ratio < 0.15:
                    score += 0.1
                    factors.append("uncommon_category")

            return {
                "novelty_score": round(min(score, 1.0), 2),
                "novelty_factors": factors,
            }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

EVENT_STORE_DEF = ToolDefinition(
    name="event_store",
    description="Store a new event in both Postgres (structured) and OpenSearch (searchable). "
                "Dual-store ensures both structured queries and full-text search work.",
    parameters=[
        ToolParameter(name="title", type="string",
                      description="Event title/headline"),
        ToolParameter(name="summary", type="string",
                      description="Brief summary of the event",
                      required=False),
        ToolParameter(name="full_content", type="string",
                      description="Full article/event content",
                      required=False),
        ToolParameter(name="raw_content", type="string",
                      description="Original unprocessed content (for future translation)",
                      required=False),
        ToolParameter(name="event_timestamp", type="string",
                      description="When the event occurred (ISO 8601, e.g. 2026-03-02T14:30:00Z)",
                      required=False),
        ToolParameter(name="source_id", type="string",
                      description="UUID of the registered source",
                      required=False),
        ToolParameter(name="source_url", type="string",
                      description="URL where the event was found",
                      required=False),
        ToolParameter(name="category", type="string",
                      description="Category (MUST be one of these exact values): conflict, political, economic, technology, health, environment, social, disaster, other. Default: other. No other values are accepted.",
                      required=False),
        ToolParameter(name="confidence", type="number",
                      description="Confidence score 0.0-1.0 (default: 0.5)",
                      required=False),
        ToolParameter(name="actors", type="string",
                      description="Comma-separated actor names (people, orgs, countries)",
                      required=False),
        ToolParameter(name="locations", type="string",
                      description="Comma-separated location names",
                      required=False),
        ToolParameter(name="tags", type="string",
                      description="Comma-separated tags",
                      required=False),
        ToolParameter(name="language", type="string",
                      description="ISO 639-1 language code (default: en)",
                      required=False),
        ToolParameter(name="guid", type="string",
                      description="RSS GUID or Atom ID for fast dedup (from feed_parse)",
                      required=False),
    ],
)

EVENT_QUERY_DEF = ToolDefinition(
    name="event_query",
    description="Query events from Postgres with structured filters (category, source, "
                "time range, language). Returns events sorted by event_timestamp descending.",
    parameters=[
        ToolParameter(name="category", type="string",
                      description="Filter by category (e.g. conflict, political)",
                      required=False),
        ToolParameter(name="source_id", type="string",
                      description="Filter by source UUID",
                      required=False),
        ToolParameter(name="since", type="string",
                      description="Events after this ISO 8601 timestamp",
                      required=False),
        ToolParameter(name="until", type="string",
                      description="Events before this ISO 8601 timestamp",
                      required=False),
        ToolParameter(name="language", type="string",
                      description="Filter by language code",
                      required=False),
        ToolParameter(name="limit", type="number",
                      description="Max events to return (default: 20)",
                      required=False),
    ],
)

EVENT_SEARCH_DEF = ToolDefinition(
    name="event_search",
    description="Full-text search events in OpenSearch. Searches across title, summary, "
                "content, actors, locations, and tags with relevance scoring.",
    parameters=[
        ToolParameter(name="query", type="string",
                      description="Search query text"),
        ToolParameter(name="category", type="string",
                      description="Optional category filter",
                      required=False),
        ToolParameter(name="since", type="string",
                      description="Events after this ISO 8601 timestamp",
                      required=False),
        ToolParameter(name="until", type="string",
                      description="Events before this ISO 8601 timestamp",
                      required=False),
        ToolParameter(name="limit", type="number",
                      description="Max results (default: 20)",
                      required=False),
    ],
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(
    registry: ToolRegistry,
    *,
    structured: StructuredStore,
    opensearch: OpenSearchStore,
) -> None:
    """Register event tools with the given registry."""

    def _check_pg() -> str | None:
        if structured is None or not structured._available:
            return "Error: Structured store (Postgres) is not available"
        return None

    def _check_os() -> str | None:
        if opensearch is None or not opensearch.available:
            return "Error: OpenSearch is not available"
        return None

    async def event_store_handler(args: dict) -> str:
        pg_err = _check_pg()
        if pg_err:
            return pg_err

        title = args.get("title", "")
        if not title:
            return "Error: title is required"

        # --- Duplicate detection -------------------------------------------

        # Fast path 1: GUID exact match
        guid = args.get("guid", "")
        if guid:
            existing = await structured.check_signal_guid(guid)
            if existing:
                return json.dumps({
                    "status": "duplicate_detected",
                    "existing_event_id": str(existing["id"]),
                    "existing_title": existing["title"],
                    "reason": "GUID match",
                    "hint": "This feed item has already been ingested (same GUID).",
                }, indent=2)

        # Fast path 2: source_url exact match
        source_url = args.get("source_url", "")
        if source_url:
            existing = await structured.check_signal_source_url(source_url)
            if existing:
                return json.dumps({
                    "status": "duplicate_detected",
                    "existing_event_id": str(existing["id"]),
                    "existing_title": existing["title"],
                    "reason": "source_url match",
                    "hint": "An event from this exact URL already exists.",
                }, indent=2)

        # Fast path 3: exact title match (case-insensitive) within last 7 days
        try:
            async with structured._pool.acquire() as conn:
                from datetime import timedelta
                cutoff = datetime.now(timezone.utc) - timedelta(days=7)
                existing = await conn.fetchrow(
                    "SELECT id, title FROM signals WHERE lower(title) = $1 AND created_at >= $2 LIMIT 1",
                    title.lower(), cutoff,
                )
                if existing:
                    return json.dumps({
                        "status": "duplicate_detected",
                        "existing_event_id": str(existing["id"]),
                        "existing_title": existing["title"],
                        "reason": "exact_title_match",
                        "hint": "An event with this exact title already exists within the last 7 days.",
                    }, indent=2)
        except Exception:
            pass

        # Title similarity check — adaptive threshold for short titles
        title_words = _title_words(title)
        sim_threshold = 0.4 if len(title_words) <= 5 else 0.5

        event_ts_raw = args.get("event_timestamp")
        if event_ts_raw:
            # With timestamp: check ±1 day window
            try:
                check_dt = datetime.fromisoformat(event_ts_raw.replace("Z", "+00:00"))
                from datetime import timedelta
                day_start = (check_dt - timedelta(days=1)).isoformat()
                day_end = (check_dt + timedelta(days=1)).isoformat()
                candidates = await structured.query_signals(
                    since=datetime.fromisoformat(day_start),
                    until=datetime.fromisoformat(day_end),
                    limit=200,
                )
                for existing in candidates:
                    sim = _title_similarity(title, existing.title)
                    if sim >= sim_threshold:
                        return json.dumps({
                            "status": "duplicate_detected",
                            "existing_event_id": str(existing.id),
                            "existing_title": existing.title,
                            "similarity": round(sim, 2),
                            "hint": "An event with a very similar title already exists for this date. "
                                    "Use the existing event ID instead of creating a duplicate.",
                        }, indent=2)
            except (ValueError, Exception):
                pass  # If date parsing or query fails, proceed with store
        else:
            # Without timestamp: check last 300 events by title similarity
            try:
                candidates = await structured.get_recent_signals_for_dedup(limit=300)
                for existing in candidates:
                    sim = _title_similarity(title, existing.title)
                    if sim >= sim_threshold:
                        return json.dumps({
                            "status": "duplicate_detected",
                            "existing_event_id": str(existing.id),
                            "existing_title": existing.title,
                            "similarity": round(sim, 2),
                            "hint": "An event with a very similar title already exists in recent events.",
                        }, indent=2)
            except Exception:
                pass
        # -------------------------------------------------------------------

        from ....shared.schemas.signals import create_signal, SignalCategory

        kwargs: dict = {"title": title}

        if args.get("summary"):
            kwargs["summary"] = args["summary"]
        if args.get("full_content"):
            kwargs["full_content"] = args["full_content"]
        if args.get("raw_content"):
            kwargs["raw_content"] = args["raw_content"]
        if args.get("event_timestamp"):
            try:
                kwargs["event_timestamp"] = datetime.fromisoformat(
                    args["event_timestamp"].replace("Z", "+00:00")
                )
            except ValueError:
                return "Error: Invalid event_timestamp format (use ISO 8601)"
        if args.get("source_id"):
            try:
                kwargs["source_id"] = UUID(args["source_id"])
            except ValueError:
                return "Error: Invalid source_id format"
        if args.get("source_url"):
            kwargs["source_url"] = args["source_url"]
        if args.get("category"):
            try:
                kwargs["category"] = SignalCategory(args["category"])
            except ValueError:
                return f"Error: Invalid category '{args['category']}'"
        if args.get("confidence") is not None:
            kwargs["confidence"] = float(args["confidence"])
        if args.get("actors"):
            actors_val = args["actors"]
            if isinstance(actors_val, list):
                kwargs["actors"] = [str(a).strip() for a in actors_val if str(a).strip()]
            elif isinstance(actors_val, str):
                kwargs["actors"] = [a.strip() for a in actors_val.split(",") if a.strip()]
        if args.get("locations"):
            loc_val = args["locations"]
            if isinstance(loc_val, list):
                kwargs["locations"] = [str(l).strip() for l in loc_val if str(l).strip()]
            elif isinstance(loc_val, str):
                kwargs["locations"] = [l.strip() for l in loc_val.split(",") if l.strip()]
        if args.get("tags"):
            tags_val = args["tags"]
            if isinstance(tags_val, list):
                kwargs["tags"] = [str(t).strip() for t in tags_val if str(t).strip()]
            elif isinstance(tags_val, str):
                kwargs["tags"] = [t.strip() for t in tags_val.split(",") if t.strip()]
        if args.get("language"):
            kwargs["language"] = args["language"]
        if args.get("guid"):
            kwargs["guid"] = args["guid"]

        signal = create_signal(**kwargs)

        # Auto-resolve locations to geo data
        if signal.locations:
            try:
                from .geo import resolve_locations
                geo = resolve_locations(signal.locations)
                signal.geo_countries = geo["countries"]
                signal.geo_regions = geo["regions"]
                signal.geo_coordinates = geo["coordinates"]
            except Exception:
                pass  # Geo resolution is best-effort

        # Store in Postgres
        pg_ok = await structured.save_signal(signal)

        # Store in OpenSearch (best-effort)
        os_result = None
        os_err = _check_os()
        if not os_err:
            index_name = _signals_index_name()
            await _ensure_signals_index(opensearch, index_name)
            os_doc = {
                "title": signal.title,
                "summary": signal.summary,
                "full_content": signal.full_content,
                "category": str(signal.category.value if hasattr(signal.category, 'value') else signal.category),
                "actors": signal.actors,
                "locations": signal.locations,
                "tags": signal.tags,
                "language": signal.language,
                "source_id": str(signal.source_id) if signal.source_id else None,
                "source_url": signal.source_url,
                "confidence": signal.confidence,
                "event_timestamp": signal.event_timestamp.isoformat() if signal.event_timestamp else None,
                "created_at": signal.created_at.isoformat(),
                "geo_countries": signal.geo_countries,
            }
            os_result = await opensearch.index_document(
                index_name, os_doc, doc_id=str(signal.id),
            )

        # Increment events_produced_count on source (even if PG save
        # retried with source_id=NULL due to FK violation — the source
        # itself may still exist and should get credit).
        if signal.source_id:
            try:
                await structured.increment_source_event_count(signal.source_id)
            except Exception as e:
                logger.error("Failed to increment event count for source %s: %s", signal.source_id, e)

        result = {
            "status": "stored" if pg_ok else "partial_failure",
            "event_id": str(signal.id),
            "title": signal.title,
            "postgres": "ok" if pg_ok else "failed",
            "opensearch": os_result.get("result", "skipped") if os_result else "unavailable",
        }

        # Include geo resolution results
        if signal.geo_countries:
            result["geo_countries"] = signal.geo_countries
        if signal.geo_coordinates:
            result["geo_coordinates"] = signal.geo_coordinates

        # Hint: nudge agent to resolve actors/locations to entity profiles
        unresolved = []
        for actor in signal.actors:
            unresolved.append({"name": actor, "suggested_role": "actor"})
        for loc in signal.locations:
            unresolved.append({"name": loc, "suggested_role": "location"})
        if unresolved:
            result["unresolved_entities"] = unresolved
            result["hint"] = (
                "Use entity_resolve to link these names to entity profiles. "
                "This builds the world model and enables connection analysis."
            )

        # --- Post-store intelligence hooks (best-effort) ---
        if pg_ok:
            # Watchlist auto-matching
            watch_matches = await _check_watchlist_matches(structured, signal)
            if watch_matches:
                result["watchlist_triggers"] = watch_matches

            # Situation suggestions
            sit_suggestions = await _suggest_situations(structured, signal)
            if sit_suggestions:
                result["situation_suggestions"] = sit_suggestions
                result["situation_hint"] = (
                    "Consider linking this event to relevant situations "
                    "using situation_link_event."
                )

            # Novelty scoring
            novelty = await _compute_novelty(structured, signal)
            if novelty:
                result["novelty"] = novelty
        # ---------------------------------------------------

        return json.dumps(result, indent=2, default=str)

    async def event_query_handler(args: dict) -> str:
        pg_err = _check_pg()
        if pg_err:
            return pg_err

        kwargs: dict = {}
        if args.get("category"):
            kwargs["category"] = args["category"]
        if args.get("source_id"):
            try:
                kwargs["source_id"] = UUID(args["source_id"])
            except ValueError:
                return "Error: Invalid source_id format"
        if args.get("since"):
            try:
                kwargs["since"] = datetime.fromisoformat(
                    args["since"].replace("Z", "+00:00")
                )
            except ValueError:
                return "Error: Invalid 'since' timestamp"
        if args.get("until"):
            try:
                kwargs["until"] = datetime.fromisoformat(
                    args["until"].replace("Z", "+00:00")
                )
            except ValueError:
                return "Error: Invalid 'until' timestamp"
        if args.get("language"):
            kwargs["language"] = args["language"]
        kwargs["limit"] = int(args.get("limit", 20))

        events = await structured.query_signals(**kwargs)
        if not events:
            return "No events found matching filters"

        result = []
        for e in events:
            result.append({
                "id": str(e.id),
                "title": e.title,
                "summary": e.summary[:200] if e.summary else "",
                "category": str(e.category.value if hasattr(e.category, 'value') else e.category),
                "event_timestamp": e.event_timestamp.isoformat() if e.event_timestamp else None,
                "source_url": e.source_url,
                "actors": e.actors,
                "locations": e.locations,
                "geo_countries": e.geo_countries if hasattr(e, 'geo_countries') else [],
                "confidence": e.confidence,
                "language": e.language,
            })
        return json.dumps({"count": len(result), "events": result}, indent=2, default=str)

    async def event_search_handler(args: dict) -> str:
        os_err = _check_os()
        if os_err:
            return os_err

        query_text = args.get("query", "")
        if not query_text:
            return "Error: query is required"

        limit = int(args.get("limit", 20))

        # Build OpenSearch query
        must = [
            {
                "multi_match": {
                    "query": query_text,
                    "fields": ["title^3", "summary^2", "full_content", "actors", "locations", "tags"],
                }
            }
        ]
        filters = []

        if args.get("category"):
            filters.append({"term": {"category": args["category"]}})
        if args.get("since") or args.get("until"):
            time_range: dict = {}
            if args.get("since"):
                time_range["gte"] = args["since"]
            if args.get("until"):
                time_range["lte"] = args["until"]
            filters.append({"range": {"event_timestamp": time_range}})

        if filters:
            query = {"bool": {"must": must, "filter": filters}}
        else:
            query = {"bool": {"must": must}}

        result = await opensearch.search(
            "legba-signals-*",
            query,
            size=limit,
            sort=[{"_score": "desc"}, {"event_timestamp": {"order": "desc", "unmapped_type": "date"}}],
        )

        if result.get("error"):
            return f"Search error: {result['error']}"

        hits = []
        for hit in result.get("hits", []):
            hits.append({
                "id": hit.get("_id", ""),
                "score": hit.get("_score"),
                "title": hit.get("title", ""),
                "summary": (hit.get("summary", "") or "")[:200],
                "category": hit.get("category", ""),
                "event_timestamp": hit.get("event_timestamp"),
                "actors": hit.get("actors", []),
                "locations": hit.get("locations", []),
                "source_url": hit.get("source_url", ""),
            })

        return json.dumps({
            "total": result.get("total", 0),
            "count": len(hits),
            "hits": hits,
        }, indent=2, default=str)

    registry.register(EVENT_STORE_DEF, event_store_handler)
    registry.register(EVENT_QUERY_DEF, event_query_handler)
    registry.register(EVENT_SEARCH_DEF, event_search_handler)
