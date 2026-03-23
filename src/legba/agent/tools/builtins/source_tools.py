"""
Source Management Tools

CRUD operations for the source registry. The agent uses these to
discover, register, update, and retire news/data sources.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import TYPE_CHECKING
from uuid import UUID

from ....shared.schemas.tools import ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from ...memory.structured import StructuredStore
    from ...tools.registry import ToolRegistry


def _coerce_enum(val, enum_cls: type[Enum]) -> Enum:
    """Match a string to an enum value, case-insensitive with prefix matching."""
    low = str(val).lower().strip().replace(" ", "_").replace("-", "_")
    # Exact match on value
    for member in enum_cls:
        if member.value == low:
            return member
    # Prefix match (e.g. "public" → "public_broadcast", "government" → state via alias)
    for member in enum_cls:
        if member.value.startswith(low):
            return member
    raise ValueError(val)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

SOURCE_ADD_DEF = ToolDefinition(
    name="source_add",
    description="Register a new data source in the source registry. "
                "IMPORTANT: Before adding, use source_list to check if a source "
                "with the same name or domain already exists. Duplicate detection "
                "will reject sources from the same feed domain. "
                "All trust dimensions are optional with sensible defaults.",
    parameters=[
        ToolParameter(name="name", type="string",
                      description="Human-readable source name (e.g. 'Reuters World News')"),
        ToolParameter(name="url", type="string",
                      description="Feed or API endpoint URL"),
        ToolParameter(name="source_type", type="string",
                      description="Source type: rss, api, scrape, manual (default: rss)",
                      required=False),
        ToolParameter(name="reliability", type="number",
                      description="Reliability score 0.0-1.0 (default: 0.5)",
                      required=False),
        ToolParameter(name="bias_label", type="string",
                      description="Bias: far_left, left, center_left, center, center_right, right, far_right (default: center)",
                      required=False),
        ToolParameter(name="ownership_type", type="string",
                      description="Ownership: state, corporate, nonprofit, public_broadcast, independent (default: independent)",
                      required=False),
        ToolParameter(name="geo_origin", type="string",
                      description="ISO 3166-1 alpha-2 country code (e.g. US, GB, QA)",
                      required=False),
        ToolParameter(name="language", type="string",
                      description="ISO 639-1 language code (default: en)",
                      required=False),
        ToolParameter(name="timeliness", type="number",
                      description="Timeliness score 0.0-1.0 (default: 0.5)",
                      required=False),
        ToolParameter(name="coverage_scope", type="string",
                      description="Coverage: global, regional, national, local (default: global)",
                      required=False),
        ToolParameter(name="description", type="string",
                      description="Brief description of the source",
                      required=False),
        ToolParameter(name="tags", type="string",
                      description="Comma-separated tags (e.g. 'news,conflict,middle-east')",
                      required=False),
        ToolParameter(name="fetch_interval_minutes", type="number",
                      description="How often to fetch in minutes (default: 60)",
                      required=False),
    ],
)

SOURCE_LIST_DEF = ToolDefinition(
    name="source_list",
    description="List registered sources. Defaults to active sources only. Set status='all' to see all.",
    parameters=[
        ToolParameter(name="status", type="string",
                      description="Filter by status: active (default), paused, error, retired, or 'all'",
                      required=False),
        ToolParameter(name="source_type", type="string",
                      description="Filter by type: rss, api, scrape, manual",
                      required=False),
        ToolParameter(name="limit", type="number",
                      description="Max sources to return (default: 50)",
                      required=False),
    ],
)

SOURCE_UPDATE_DEF = ToolDefinition(
    name="source_update",
    description="Update an existing source's trust dimensions, status, or metadata.",
    parameters=[
        ToolParameter(name="source_id", type="string",
                      description="UUID of the source to update"),
        ToolParameter(name="status", type="string",
                      description="New status: active, paused, error, retired",
                      required=False),
        ToolParameter(name="reliability", type="number",
                      description="Updated reliability score 0.0-1.0",
                      required=False),
        ToolParameter(name="bias_label", type="string",
                      description="Updated bias label",
                      required=False),
        ToolParameter(name="ownership_type", type="string",
                      description="Updated ownership type",
                      required=False),
        ToolParameter(name="timeliness", type="number",
                      description="Updated timeliness score 0.0-1.0",
                      required=False),
        ToolParameter(name="fetch_interval_minutes", type="number",
                      description="Updated fetch interval in minutes",
                      required=False),
        ToolParameter(name="last_error", type="string",
                      description="Record an error message from last fetch attempt",
                      required=False),
    ],
)

SOURCE_REMOVE_DEF = ToolDefinition(
    name="source_remove",
    description="Remove a source from the registry by ID.",
    parameters=[
        ToolParameter(name="source_id", type="string",
                      description="UUID of the source to remove"),
    ],
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(registry: ToolRegistry, *, structured: StructuredStore) -> None:
    """Register source management tools with the given registry."""

    def _check_available() -> str | None:
        if structured is None or not structured._available:
            return "Error: Structured store (Postgres) is not available"
        return None

    async def source_add_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err

        name = args.get("name", "")
        url = args.get("url", "")
        if not name or not url:
            return "Error: name and url are required"

        # --- Duplicate detection via direct DB query ---
        # Checks: exact name (case-insensitive), normalized URL, same domain.
        # Uses a direct query instead of get_sources() to avoid silent failures.
        from urllib.parse import urlparse

        def _norm_url(u: str) -> str:
            return u.lower().replace("https://", "").replace("http://", "").replace("www.", "").rstrip("/")

        def _feed_domain(u: str) -> str:
            try:
                return urlparse(u).netloc.lower().replace("www.", "")
            except Exception:
                return ""

        norm_name = name.lower().strip()
        norm_url = _norm_url(url)
        new_domain = _feed_domain(url)

        try:
            async with structured._pool.acquire() as conn:
                # Check by name (case-insensitive)
                row = await conn.fetchrow(
                    "SELECT id, name, url FROM sources WHERE lower(name) = $1 LIMIT 1",
                    norm_name,
                )
                if row:
                    return json.dumps({
                        "status": "duplicate_detected",
                        "existing_source_id": str(row["id"]),
                        "existing_name": row["name"],
                        "existing_url": row["url"],
                        "reason": "same source name already registered",
                        "hint": "Use source_update to modify the existing source.",
                    }, indent=2)

                # Check by normalized URL (strip protocol)
                row = await conn.fetchrow(
                    "SELECT id, name, url FROM sources "
                    "WHERE replace(replace(lower(url), 'https://', ''), 'http://', '') = $1 LIMIT 1",
                    norm_url.replace("www.", ""),
                )
                if not row:
                    # Also check without www. on stored URLs
                    row = await conn.fetchrow(
                        "SELECT id, name, url FROM sources "
                        "WHERE replace(replace(replace(lower(url), 'https://', ''), 'http://', ''), 'www.', '') = $1 LIMIT 1",
                        norm_url,
                    )
                if row:
                    return json.dumps({
                        "status": "duplicate_detected",
                        "existing_source_id": str(row["id"]),
                        "existing_name": row["name"],
                        "existing_url": row["url"],
                        "reason": "same URL (protocol-normalized)",
                        "hint": "Use source_update to modify the existing source.",
                    }, indent=2)

                # Check by domain — only block if an existing source URL is a
                # prefix of the new URL or vice versa (allows different feeds
                # from the same outlet, e.g. reuters.com/world vs reuters.com/tech)
                if new_domain:
                    domain_rows = await conn.fetch(
                        "SELECT id, name, url FROM sources "
                        "WHERE url ILIKE $1 LIMIT 10",
                        f"%{new_domain}%",
                    )
                    if domain_rows:
                        # Only block on path-prefix overlap (not just same domain)
                        blocking = []
                        for r in domain_rows:
                            existing_norm = _norm_url(r["url"])
                            if existing_norm.startswith(norm_url) or norm_url.startswith(existing_norm):
                                blocking.append(r)
                        if blocking:
                            return json.dumps({
                                "status": "duplicate_detected",
                                "existing_sources": [
                                    {"id": str(r["id"]), "name": r["name"], "url": r["url"]}
                                    for r in blocking[:3]
                                ],
                                "reason": f"overlapping feed URL path — {len(blocking)} source(s) cover the same path",
                                "hint": "A source with an overlapping URL path is already registered. "
                                        "Use source_list to review, or use a more specific feed URL.",
                            }, indent=2)
        except Exception:
            pass  # If DB check fails, proceed with creation (save_source will still dedupe on url)

        from ....shared.schemas.sources import create_source, SourceType, BiasLabel, OwnershipType, CoverageScope

        kwargs = {"name": name, "url": url}

        if args.get("source_type"):
            try:
                kwargs["source_type"] = _coerce_enum(args["source_type"], SourceType)
            except ValueError:
                return f"Error: Invalid source_type '{args['source_type']}'. Use: rss, api, scrape, manual"
        if args.get("reliability") is not None:
            kwargs["reliability"] = float(args["reliability"])
        if args.get("bias_label"):
            try:
                kwargs["bias_label"] = _coerce_enum(args["bias_label"], BiasLabel)
            except ValueError:
                return f"Error: Invalid bias_label '{args['bias_label']}'. Use: far_left, left, center_left, center, center_right, right, far_right"
        if args.get("ownership_type"):
            try:
                kwargs["ownership_type"] = _coerce_enum(args["ownership_type"], OwnershipType)
            except ValueError:
                return f"Error: Invalid ownership_type '{args['ownership_type']}'. Use: state, corporate, nonprofit, public_broadcast, independent"
        if args.get("geo_origin"):
            kwargs["geo_origin"] = args["geo_origin"]
        if args.get("language"):
            kwargs["language"] = args["language"]
        if args.get("timeliness") is not None:
            kwargs["timeliness"] = float(args["timeliness"])
        if args.get("coverage_scope"):
            try:
                kwargs["coverage_scope"] = _coerce_enum(args["coverage_scope"], CoverageScope)
            except ValueError:
                return f"Error: Invalid coverage_scope '{args['coverage_scope']}'. Use: global, regional, national, local"
        if args.get("description"):
            kwargs["description"] = args["description"]
        if args.get("tags"):
            kwargs["tags"] = [t.strip() for t in args["tags"].split(",") if t.strip()]
        if args.get("fetch_interval_minutes") is not None:
            kwargs["fetch_interval_minutes"] = int(args["fetch_interval_minutes"])

        source = create_source(**kwargs)
        ok = await structured.save_source(source)
        if not ok:
            return "Error: Failed to save source to database"

        return json.dumps({
            "status": "created",
            "source_id": str(source.id),
            "name": source.name,
            "url": source.url,
        }, indent=2)

    async def source_list_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err

        status = args.get("status", "active")
        if status == "all":
            status = None
        source_type = args.get("source_type")
        limit = int(args.get("limit", 50))

        sources = await structured.get_sources(
            status=status, source_type=source_type, limit=limit,
        )
        if not sources:
            return "No sources found matching filters"

        result = []
        for s in sources:
            result.append({
                "id": str(s.id),
                "name": s.name,
                "url": s.url,
                "source_type": s.source_type.value,
                "status": s.status.value,
                "reliability": s.reliability,
                "bias_label": s.bias_label.value,
                "ownership_type": s.ownership_type.value,
                "geo_origin": s.geo_origin,
                "language": s.language,
                "coverage_scope": s.coverage_scope.value,
                "tags": s.tags,
                "last_fetched_at": str(s.last_fetched_at) if s.last_fetched_at else None,
                "fetch_success_count": getattr(s, 'fetch_success_count', 0),
                "fetch_failure_count": getattr(s, 'fetch_failure_count', 0),
                "events_produced_count": getattr(s, 'events_produced_count', 0),
                "consecutive_failures": getattr(s, 'consecutive_failures', 0),
                "last_successful_fetch_at": str(s.last_successful_fetch_at) if getattr(s, 'last_successful_fetch_at', None) else None,
                "quality_score": round(getattr(s, 'source_quality_score', 0.0) or 0.0, 3),
            })
        return json.dumps({"count": len(result), "sources": result}, indent=2, default=str)

    async def source_update_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err

        source_id_str = args.get("source_id", "")
        if not source_id_str:
            return "Error: source_id is required"

        try:
            source_id = UUID(source_id_str)
        except ValueError:
            return "Error: Invalid source_id format"

        source = await structured.get_source(source_id)
        if source is None:
            return f"Error: Source {source_id_str} not found"

        from ....shared.schemas.sources import SourceStatus, BiasLabel, OwnershipType
        from datetime import datetime, timezone

        if args.get("status"):
            try:
                source.status = _coerce_enum(args["status"], SourceStatus)
            except ValueError:
                return f"Error: Invalid status '{args['status']}'. Use: active, paused, error, retired"
        if args.get("reliability") is not None:
            source.reliability = float(args["reliability"])
        if args.get("bias_label"):
            try:
                source.bias_label = _coerce_enum(args["bias_label"], BiasLabel)
            except ValueError:
                return f"Error: Invalid bias_label '{args['bias_label']}'"
        if args.get("ownership_type"):
            try:
                source.ownership_type = _coerce_enum(args["ownership_type"], OwnershipType)
            except ValueError:
                return f"Error: Invalid ownership_type '{args['ownership_type']}'. Use: state, corporate, nonprofit, public_broadcast, independent"
        if args.get("timeliness") is not None:
            source.timeliness = float(args["timeliness"])
        if args.get("fetch_interval_minutes") is not None:
            source.fetch_interval_minutes = int(args["fetch_interval_minutes"])
        if args.get("last_error"):
            source.last_error = args["last_error"]

        source.updated_at = datetime.now(timezone.utc)
        ok = await structured.save_source(source)
        if not ok:
            return "Error: Failed to update source"

        return json.dumps({
            "status": "updated",
            "source_id": str(source.id),
            "name": source.name,
        }, indent=2)

    async def source_remove_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err

        source_id_str = args.get("source_id", "")
        if not source_id_str:
            return "Error: source_id is required"

        try:
            source_id = UUID(source_id_str)
        except ValueError:
            return "Error: Invalid source_id format"

        ok = await structured.delete_source(source_id)
        if not ok:
            return f"Error: Source {source_id_str} not found or could not be deleted"

        return json.dumps({
            "status": "deleted",
            "source_id": source_id_str,
        }, indent=2)

    registry.register(SOURCE_ADD_DEF, source_add_handler)
    registry.register(SOURCE_LIST_DEF, source_list_handler)
    registry.register(SOURCE_UPDATE_DEF, source_update_handler)
    registry.register(SOURCE_REMOVE_DEF, source_remove_handler)
