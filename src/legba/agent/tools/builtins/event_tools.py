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

_EVENTS_INDEX_MAPPINGS = {
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


async def _ensure_events_index(opensearch: OpenSearchStore, index_name: str) -> None:
    """Idempotent index creation with proper mappings."""
    if index_name in _ensured_indices:
        return
    await opensearch.create_index(
        index_name,
        mappings=_EVENTS_INDEX_MAPPINGS,
        settings={"number_of_shards": 1, "number_of_replicas": 0},
    )
    _ensured_indices.add(index_name)


def _events_index_name() -> str:
    """Generate time-partitioned index name: legba-events-YYYY.MM"""
    now = datetime.now(timezone.utc)
    return f"legba-events-{now.strftime('%Y.%m')}"


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

        # Fast path: GUID exact match
        guid = args.get("guid", "")
        if guid:
            existing = await structured.check_event_guid(guid)
            if existing:
                return json.dumps({
                    "status": "duplicate_detected",
                    "existing_event_id": str(existing["id"]),
                    "existing_title": existing["title"],
                    "reason": "GUID match",
                    "hint": "This feed item has already been ingested (same GUID).",
                }, indent=2)

        # Title similarity check
        event_ts_raw = args.get("event_timestamp")
        if event_ts_raw:
            # With timestamp: check ±1 day window
            try:
                check_dt = datetime.fromisoformat(event_ts_raw.replace("Z", "+00:00"))
                from datetime import timedelta
                day_start = (check_dt - timedelta(days=1)).isoformat()
                day_end = (check_dt + timedelta(days=1)).isoformat()
                candidates = await structured.query_events(
                    since=datetime.fromisoformat(day_start),
                    until=datetime.fromisoformat(day_end),
                    limit=50,
                )
                for existing in candidates:
                    sim = _title_similarity(title, existing.title)
                    if sim >= 0.5:
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
            # Without timestamp: check last 100 events by title similarity
            try:
                candidates = await structured.get_recent_events_for_dedup(limit=100)
                for existing in candidates:
                    sim = _title_similarity(title, existing.title)
                    if sim >= 0.5:
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

        from ....shared.schemas.events import create_event, EventCategory

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
                kwargs["category"] = EventCategory(args["category"])
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

        event = create_event(**kwargs)

        # Auto-resolve locations to geo data
        if event.locations:
            try:
                from .geo import resolve_locations
                geo = resolve_locations(event.locations)
                event.geo_countries = geo["countries"]
                event.geo_regions = geo["regions"]
                event.geo_coordinates = geo["coordinates"]
            except Exception:
                pass  # Geo resolution is best-effort

        # Store in Postgres
        pg_ok = await structured.save_event(event)

        # Store in OpenSearch (best-effort)
        os_result = None
        os_err = _check_os()
        if not os_err:
            index_name = _events_index_name()
            await _ensure_events_index(opensearch, index_name)
            os_doc = {
                "title": event.title,
                "summary": event.summary,
                "full_content": event.full_content,
                "category": event.category.value,
                "actors": event.actors,
                "locations": event.locations,
                "tags": event.tags,
                "language": event.language,
                "source_id": str(event.source_id) if event.source_id else None,
                "source_url": event.source_url,
                "confidence": event.confidence,
                "event_timestamp": event.event_timestamp.isoformat() if event.event_timestamp else None,
                "created_at": event.created_at.isoformat(),
                "geo_countries": event.geo_countries,
            }
            os_result = await opensearch.index_document(
                index_name, os_doc, doc_id=str(event.id),
            )

        # Increment events_produced_count on source (even if PG save
        # retried with source_id=NULL due to FK violation — the source
        # itself may still exist and should get credit).
        if event.source_id:
            try:
                await structured.increment_source_event_count(event.source_id)
            except Exception as e:
                logger.error("Failed to increment event count for source %s: %s", event.source_id, e)

        result = {
            "status": "stored" if pg_ok else "partial_failure",
            "event_id": str(event.id),
            "title": event.title,
            "postgres": "ok" if pg_ok else "failed",
            "opensearch": os_result.get("result", "skipped") if os_result else "unavailable",
        }

        # Include geo resolution results
        if event.geo_countries:
            result["geo_countries"] = event.geo_countries
        if event.geo_coordinates:
            result["geo_coordinates"] = event.geo_coordinates

        # Hint: nudge agent to resolve actors/locations to entity profiles
        unresolved = []
        for actor in event.actors:
            unresolved.append({"name": actor, "suggested_role": "actor"})
        for loc in event.locations:
            unresolved.append({"name": loc, "suggested_role": "location"})
        if unresolved:
            result["unresolved_entities"] = unresolved
            result["hint"] = (
                "Use entity_resolve to link these names to entity profiles. "
                "This builds the world model and enables connection analysis."
            )

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

        events = await structured.query_events(**kwargs)
        if not events:
            return "No events found matching filters"

        result = []
        for e in events:
            result.append({
                "id": str(e.id),
                "title": e.title,
                "summary": e.summary[:200] if e.summary else "",
                "category": e.category.value,
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
            "legba-events-*",
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
