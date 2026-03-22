"""
Watchlist Tools

CRUD operations for persistent watch patterns that fire on matching events.
The agent uses these to set up alerting criteria (entities, keywords,
categories, regions) and manage active watches.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID

from ....shared.schemas.tools import ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from ...memory.structured import StructuredStore
    from ...tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# SQL DDL — executed lazily on first use
# ---------------------------------------------------------------------------

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS watchlist (
    id UUID PRIMARY KEY,
    data JSONB NOT NULL,
    name TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_triggered_at TIMESTAMPTZ,
    trigger_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS watch_triggers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    watch_id UUID NOT NULL REFERENCES watchlist(id) ON DELETE CASCADE,
    event_id UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    watch_name TEXT NOT NULL DEFAULT '',
    event_title TEXT NOT NULL DEFAULT '',
    match_reasons JSONB NOT NULL DEFAULT '[]',
    priority TEXT NOT NULL DEFAULT 'normal',
    triggered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_watch_triggers_watch_id ON watch_triggers(watch_id);
CREATE INDEX IF NOT EXISTS idx_watch_triggers_triggered_at ON watch_triggers(triggered_at DESC);
"""

_table_ensured = False


async def _ensure_tables(conn) -> None:
    """Create watchlist tables if they don't exist (idempotent)."""
    global _table_ensured
    if _table_ensured:
        return
    await conn.execute(_TABLE_DDL)
    _table_ensured = True


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

WATCHLIST_ADD_DEF = ToolDefinition(
    name="watchlist_add",
    description="Create a persistent watch pattern that triggers when matching events "
                "are stored. At least one matching criterion (entities, keywords, "
                "categories, or regions) must be provided.",
    parameters=[
        ToolParameter(name="name", type="string",
                      description="Short descriptive name for this watch (e.g. 'Taiwan Strait Tensions')"),
        ToolParameter(name="entities", type="string",
                      description="Comma-separated entity names to match (e.g. 'China,Taiwan,United States')",
                      required=False),
        ToolParameter(name="keywords", type="string",
                      description="Comma-separated keywords to match in event title/summary (e.g. 'invasion,blockade,military')",
                      required=False),
        ToolParameter(name="categories", type="string",
                      description="Comma-separated event categories to match (e.g. 'conflict,political')",
                      required=False),
        ToolParameter(name="regions", type="string",
                      description="Comma-separated region/location names to match (e.g. 'East Asia,South China Sea')",
                      required=False),
        ToolParameter(name="priority", type="string",
                      description="Watch priority: normal, high, critical (default: normal)",
                      required=False),
        ToolParameter(name="description", type="string",
                      description="Longer description of what this watch is for",
                      required=False),
    ],
)

WATCHLIST_LIST_DEF = ToolDefinition(
    name="watchlist_list",
    description="List all watch patterns with their trigger counts and status.",
    parameters=[
        ToolParameter(name="active_only", type="boolean",
                      description="If true, only show active watches (default: true)",
                      required=False),
        ToolParameter(name="limit", type="number",
                      description="Max watches to return (default: 50)",
                      required=False),
    ],
)

WATCHLIST_REMOVE_DEF = ToolDefinition(
    name="watchlist_remove",
    description="Remove a watch pattern by ID. This also deletes its trigger history.",
    parameters=[
        ToolParameter(name="watch_id", type="string",
                      description="UUID of the watch to remove"),
    ],
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(registry: ToolRegistry, *, structured: StructuredStore) -> None:
    """Register watchlist management tools with the given registry."""

    def _check_available() -> str | None:
        if structured is None or not structured._available:
            return "Error: Structured store (Postgres) is not available"
        return None

    def _split_csv(val: str | None) -> list[str]:
        """Split a comma-separated string into a cleaned list."""
        if not val:
            return []
        return [v.strip() for v in val.split(",") if v.strip()]

    async def watchlist_add_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err

        name = args.get("name", "").strip()
        if not name:
            return "Error: name is required"

        entities = _split_csv(args.get("entities"))
        keywords = _split_csv(args.get("keywords"))
        categories = _split_csv(args.get("categories"))
        regions = _split_csv(args.get("regions"))
        priority = args.get("priority", "normal").strip().lower()
        description = args.get("description", "").strip()

        if priority not in ("normal", "high", "critical"):
            return f"Error: Invalid priority '{priority}'. Use: normal, high, critical"

        if not entities and not keywords and not categories and not regions:
            return "Error: At least one matching criterion (entities, keywords, categories, or regions) is required"

        from uuid import uuid4
        watch_id = uuid4()
        now = datetime.now(timezone.utc)

        data = {
            "id": str(watch_id),
            "name": name,
            "description": description,
            "entities": entities,
            "keywords": keywords,
            "categories": categories,
            "regions": regions,
            "priority": priority,
            "active": True,
            "created_at": now.isoformat(),
            "last_triggered_at": None,
            "trigger_count": 0,
        }

        try:
            async with structured._pool.acquire() as conn:
                await _ensure_tables(conn)

                # Duplicate check: exact name match
                existing = await conn.fetchrow(
                    "SELECT id, name FROM watchlist WHERE lower(name) = $1 AND active = true LIMIT 1",
                    name.lower(),
                )
                if existing:
                    return json.dumps({
                        "status": "duplicate_detected",
                        "existing_watch_id": str(existing["id"]),
                        "existing_name": existing["name"],
                        "reason": "a watch with this name already exists",
                    }, indent=2)

                # Semantic overlap check: compare keywords+entities against active watches
                new_terms = {t.lower() for t in entities + keywords}
                if new_terms:
                    rows = await conn.fetch(
                        "SELECT id, name, data FROM watchlist WHERE active = true",
                    )
                    for row in rows:
                        raw = row["data"]
                        d = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
                        existing_terms = {t.lower() for t in d.get("entities", []) + d.get("keywords", [])}
                        if not existing_terms:
                            continue
                        overlap = len(new_terms & existing_terms)
                        union = len(new_terms | existing_terms)
                        if union > 0 and overlap / union >= 0.5:
                            return json.dumps({
                                "status": "duplicate_detected",
                                "existing_watch_id": str(row["id"]),
                                "existing_name": row["name"],
                                "overlap": sorted(new_terms & existing_terms),
                                "reason": f"overlaps {overlap}/{union} terms with existing watch — update the existing watch instead of creating a new one",
                            }, indent=2)

                await conn.execute(
                    "INSERT INTO watchlist (id, data, name, priority, active, created_at) "
                    "VALUES ($1, $2::jsonb, $3, $4, true, $5)",
                    watch_id, json.dumps(data, default=str), name, priority, now,
                )
        except Exception as e:
            return f"Error: Failed to create watch — {e}"

        return json.dumps({
            "status": "created",
            "watch_id": str(watch_id),
            "name": name,
            "priority": priority,
            "entities": entities,
            "keywords": keywords,
            "categories": categories,
            "regions": regions,
        }, indent=2)

    async def watchlist_list_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err

        active_only = args.get("active_only", True)
        if isinstance(active_only, str):
            active_only = active_only.lower() in ("true", "1", "yes")
        limit = int(args.get("limit", 50))

        try:
            async with structured._pool.acquire() as conn:
                await _ensure_tables(conn)

                if active_only:
                    rows = await conn.fetch(
                        "SELECT id, data, name, priority, active, created_at, "
                        "last_triggered_at, trigger_count "
                        "FROM watchlist WHERE active = true "
                        "ORDER BY priority DESC, created_at DESC LIMIT $1",
                        limit,
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT id, data, name, priority, active, created_at, "
                        "last_triggered_at, trigger_count "
                        "FROM watchlist ORDER BY priority DESC, created_at DESC LIMIT $1",
                        limit,
                    )
        except Exception as e:
            return f"Error: Failed to list watches — {e}"

        if not rows:
            return "No watches found"

        result = []
        for row in rows:
            raw = row["data"]
            data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
            result.append({
                "id": str(row["id"]),
                "name": row["name"],
                "priority": row["priority"],
                "active": row["active"],
                "entities": data.get("entities", []),
                "keywords": data.get("keywords", []),
                "categories": data.get("categories", []),
                "regions": data.get("regions", []),
                "description": data.get("description", ""),
                "trigger_count": row["trigger_count"],
                "last_triggered_at": str(row["last_triggered_at"]) if row["last_triggered_at"] else None,
                "created_at": str(row["created_at"]),
            })

        return json.dumps({"count": len(result), "watches": result}, indent=2, default=str)

    async def watchlist_remove_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err

        watch_id_str = args.get("watch_id", "").strip()
        if not watch_id_str:
            return "Error: watch_id is required"

        try:
            watch_id = UUID(watch_id_str)
        except ValueError:
            return "Error: Invalid watch_id format"

        try:
            async with structured._pool.acquire() as conn:
                await _ensure_tables(conn)
                result = await conn.execute(
                    "DELETE FROM watchlist WHERE id = $1", watch_id,
                )
                # asyncpg returns "DELETE N" where N is the count
                if result == "DELETE 0":
                    return f"Error: Watch {watch_id_str} not found"
        except Exception as e:
            return f"Error: Failed to delete watch — {e}"

        return json.dumps({
            "status": "deleted",
            "watch_id": watch_id_str,
        }, indent=2)

    registry.register(WATCHLIST_ADD_DEF, watchlist_add_handler)
    registry.register(WATCHLIST_LIST_DEF, watchlist_list_handler)
    registry.register(WATCHLIST_REMOVE_DEF, watchlist_remove_handler)
