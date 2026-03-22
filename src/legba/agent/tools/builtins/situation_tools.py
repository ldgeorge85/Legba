"""
Situation Tracking Tools

CRUD operations for tracked situations (persistent narratives).
Situations accumulate events and track status over time.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from ....shared.schemas.tools import ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from ...memory.structured import StructuredStore
    from ...tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Table setup
# ---------------------------------------------------------------------------

_tables_ensured = False


async def _ensure_tables(pool) -> None:
    """Create situation tables if they don't exist (idempotent)."""
    global _tables_ensured
    if _tables_ensured:
        return
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS situations (
                id UUID PRIMARY KEY,
                data JSONB NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                category TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_event_at TIMESTAMPTZ,
                event_count INTEGER NOT NULL DEFAULT 0,
                intensity_score REAL NOT NULL DEFAULT 0.0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS situation_events (
                situation_id UUID NOT NULL REFERENCES situations(id),
                event_id UUID NOT NULL REFERENCES events(id),
                relevance REAL NOT NULL DEFAULT 1.0,
                added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (situation_id, event_id)
            )
        """)
    _tables_ensured = True


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

SITUATION_CREATE_DEF = ToolDefinition(
    name="situation_create",
    description="Create a new tracked situation (persistent narrative). "
                "Situations group related events under a named storyline "
                "(e.g. 'Iran Nuclear Crisis', 'Ukraine-Russia War'). "
                "Use situation_list first to avoid duplicates.",
    parameters=[
        ToolParameter(name="name", type="string",
                      description="Situation name (e.g. 'Iran Nuclear Talks')"),
        ToolParameter(name="description", type="string",
                      description="Brief description of the situation",
                      required=False),
        ToolParameter(name="category", type="string",
                      description="Category: conflict, political, economic, technology, health, environment, social, disaster, other",
                      required=False),
        ToolParameter(name="key_entities", type="string",
                      description="Comma-separated key entity names involved",
                      required=False),
        ToolParameter(name="regions", type="string",
                      description="Comma-separated region/country names",
                      required=False),
        ToolParameter(name="tags", type="string",
                      description="Comma-separated tags",
                      required=False),
    ],
)

SITUATION_UPDATE_DEF = ToolDefinition(
    name="situation_update",
    description="Update a tracked situation's status, description, entities, or regions.",
    parameters=[
        ToolParameter(name="situation_id", type="string",
                      description="UUID of the situation to update"),
        ToolParameter(name="status", type="string",
                      description="New status: active, escalating, de_escalating, dormant, resolved",
                      required=False),
        ToolParameter(name="description", type="string",
                      description="Updated description",
                      required=False),
        ToolParameter(name="add_entities", type="string",
                      description="Comma-separated entity names to add to key_entities",
                      required=False),
        ToolParameter(name="add_regions", type="string",
                      description="Comma-separated regions to add",
                      required=False),
        ToolParameter(name="intensity_score", type="number",
                      description="Updated intensity score 0.0-1.0",
                      required=False),
    ],
)

SITUATION_LIST_DEF = ToolDefinition(
    name="situation_list",
    description="List tracked situations with event counts and intensity scores.",
    parameters=[
        ToolParameter(name="status", type="string",
                      description="Filter by status: active, escalating, de_escalating, dormant, resolved",
                      required=False),
        ToolParameter(name="limit", type="number",
                      description="Max situations to return (default: 50)",
                      required=False),
    ],
)

SITUATION_LINK_EVENT_DEF = ToolDefinition(
    name="situation_link_event",
    description="Link an event to a tracked situation. This builds the event "
                "timeline for the situation and updates its event count.",
    parameters=[
        ToolParameter(name="situation_id", type="string",
                      description="UUID of the situation"),
        ToolParameter(name="event_id", type="string",
                      description="UUID of the event to link"),
        ToolParameter(name="relevance", type="number",
                      description="Relevance score 0.0-1.0 (default: 1.0)",
                      required=False),
    ],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_STATUSES = {"active", "escalating", "de_escalating", "dormant", "resolved"}


def _parse_csv(val) -> list[str]:
    """Parse a comma-separated string or list into a clean list of strings."""
    if isinstance(val, list):
        return [str(v).strip() for v in val if str(v).strip()]
    if isinstance(val, str):
        return [v.strip() for v in val.split(",") if v.strip()]
    return []


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(registry: ToolRegistry, *, structured: StructuredStore) -> None:
    """Register situation tracking tools with the given registry."""

    def _check_available() -> str | None:
        if structured is None or not structured._available:
            return "Error: Structured store (Postgres) is not available"
        return None

    async def situation_create_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err

        name = args.get("name", "").strip()
        if not name:
            return "Error: name is required"

        await _ensure_tables(structured._pool)

        # Duplicate check: exact name + fuzzy name overlap
        try:
            async with structured._pool.acquire() as conn:
                # Exact name match
                existing = await conn.fetchrow(
                    "SELECT id, name FROM situations WHERE lower(name) = $1 LIMIT 1",
                    name.lower(),
                )
                if existing:
                    return json.dumps({
                        "status": "duplicate_detected",
                        "existing_id": str(existing["id"]),
                        "existing_name": existing["name"],
                        "hint": "A situation with this name already exists. "
                                "Use situation_update to modify it.",
                    }, indent=2)

                # Fuzzy name overlap: Jaccard on name words
                stop = {"a", "an", "the", "of", "in", "on", "at", "to", "for",
                        "and", "or", "is", "was", "by", "from", "with", "march",
                        "2026", "2025", "events", "event"}
                new_words = {w for w in name.lower().split() if w not in stop and len(w) > 2}
                if new_words:
                    active = await conn.fetch(
                        "SELECT id, name FROM situations WHERE status IN ('active', 'escalating')",
                    )
                    for row in active:
                        ex_words = {w for w in row["name"].lower().split() if w not in stop and len(w) > 2}
                        if not ex_words:
                            continue
                        overlap = len(new_words & ex_words)
                        union = len(new_words | ex_words)
                        if union > 0 and overlap / union >= 0.5:
                            return json.dumps({
                                "status": "duplicate_detected",
                                "existing_id": str(row["id"]),
                                "existing_name": row["name"],
                                "overlap_words": sorted(new_words & ex_words),
                                "hint": f"Similar situation exists (word overlap {overlap}/{union}). "
                                        "Use situation_update to modify it.",
                            }, indent=2)
        except Exception:
            pass  # Proceed with creation if check fails

        from ....shared.schemas.situations import Situation

        now = datetime.now(timezone.utc)
        sit_id = uuid4()

        key_entities = _parse_csv(args.get("key_entities", ""))
        regions = _parse_csv(args.get("regions", ""))
        tags = _parse_csv(args.get("tags", ""))

        situation = Situation(
            id=sit_id,
            name=name,
            description=args.get("description", ""),
            category=args.get("category", ""),
            key_entities=key_entities,
            regions=regions,
            tags=tags,
            created_at=now,
            updated_at=now,
        )

        data_json = situation.model_dump_json()

        try:
            async with structured._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO situations (id, data, name, status, category, created_at, updated_at) "
                    "VALUES ($1, $2::jsonb, $3, $4, $5, $6, $6)",
                    sit_id, data_json, name, situation.status, situation.category, now,
                )
        except Exception as e:
            return f"Error: Failed to create situation: {e}"

        return json.dumps({
            "status": "created",
            "situation_id": str(sit_id),
            "name": name,
        }, indent=2)

    async def situation_update_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err

        sit_id_str = args.get("situation_id", "")
        if not sit_id_str:
            return "Error: situation_id is required"

        try:
            sit_id = UUID(sit_id_str)
        except ValueError:
            return "Error: Invalid situation_id format"

        await _ensure_tables(structured._pool)

        try:
            async with structured._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT data FROM situations WHERE id = $1", sit_id,
                )
                if not row:
                    return f"Error: Situation {sit_id_str} not found"

                raw = row["data"]
                data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}

                now = datetime.now(timezone.utc)
                updated_fields = []

                # Status
                new_status = args.get("status")
                if new_status:
                    if new_status not in _VALID_STATUSES:
                        return f"Error: Invalid status '{new_status}'. Use: {', '.join(sorted(_VALID_STATUSES))}"
                    data["status"] = new_status
                    updated_fields.append("status")

                # Description
                if args.get("description"):
                    data["description"] = args["description"]
                    updated_fields.append("description")

                # Add entities (merge, deduplicate)
                if args.get("add_entities"):
                    new_ents = _parse_csv(args["add_entities"])
                    existing = set(data.get("key_entities", []))
                    existing.update(new_ents)
                    data["key_entities"] = sorted(existing)
                    updated_fields.append("key_entities")

                # Add regions (merge, deduplicate)
                if args.get("add_regions"):
                    new_regs = _parse_csv(args["add_regions"])
                    existing = set(data.get("regions", []))
                    existing.update(new_regs)
                    data["regions"] = sorted(existing)
                    updated_fields.append("regions")

                # Intensity score
                if args.get("intensity_score") is not None:
                    data["intensity_score"] = float(args["intensity_score"])
                    updated_fields.append("intensity_score")

                if not updated_fields:
                    return "Error: No fields to update"

                data["updated_at"] = now.isoformat()

                current_row = await conn.fetchrow(
                    "SELECT intensity_score, event_count FROM situations WHERE id = $1", sit_id
                )
                if current_row:
                    if "intensity_score" not in updated_fields:
                        data["intensity_score"] = float(current_row["intensity_score"])
                    if "event_count" not in updated_fields:
                        data["event_count"] = current_row["event_count"]

                # Update denormalized columns + JSONB
                await conn.execute(
                    "UPDATE situations SET data = $1::jsonb, "
                    "status = $2, category = $3, "
                    "intensity_score = $4, updated_at = $5 "
                    "WHERE id = $6",
                    json.dumps(data, default=str),
                    data.get("status", "active"),
                    data.get("category", ""),
                    float(data.get("intensity_score", 0.0)),
                    now,
                    sit_id,
                )

        except Exception as e:
            return f"Error: Failed to update situation: {e}"

        return json.dumps({
            "status": "updated",
            "situation_id": str(sit_id),
            "updated_fields": updated_fields,
        }, indent=2)

    async def situation_list_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err

        await _ensure_tables(structured._pool)

        status_filter = args.get("status")
        limit = int(args.get("limit", 50))

        try:
            async with structured._pool.acquire() as conn:
                if status_filter:
                    rows = await conn.fetch(
                        "SELECT id, data, name, status, category, event_count, "
                        "intensity_score, last_event_at, created_at, updated_at "
                        "FROM situations WHERE status = $1 "
                        "ORDER BY updated_at DESC LIMIT $2",
                        status_filter, limit,
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT id, data, name, status, category, event_count, "
                        "intensity_score, last_event_at, created_at, updated_at "
                        "FROM situations ORDER BY updated_at DESC LIMIT $1",
                        limit,
                    )
        except Exception as e:
            return f"Error: Failed to list situations: {e}"

        if not rows:
            return "No situations found"

        result = []
        for row in rows:
            raw = row["data"]
            data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
            result.append({
                "id": str(row["id"]),
                "name": row["name"],
                "status": row["status"],
                "category": row["category"],
                "description": data.get("description", ""),
                "key_entities": data.get("key_entities", []),
                "regions": data.get("regions", []),
                "tags": data.get("tags", []),
                "event_count": row["event_count"],
                "intensity_score": row["intensity_score"],
                "last_event_at": str(row["last_event_at"]) if row["last_event_at"] else None,
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            })

        return json.dumps({"count": len(result), "situations": result}, indent=2, default=str)

    async def situation_link_event_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err

        sit_id_str = args.get("situation_id", "")
        event_id_str = args.get("event_id", "")
        if not sit_id_str or not event_id_str:
            return "Error: situation_id and event_id are required"

        try:
            sit_id = UUID(sit_id_str)
        except ValueError:
            return "Error: Invalid situation_id format"

        try:
            event_id = UUID(event_id_str)
        except ValueError:
            return "Error: Invalid event_id format"

        relevance = float(args.get("relevance", 1.0))

        await _ensure_tables(structured._pool)

        try:
            async with structured._pool.acquire() as conn:
                # Verify situation exists
                sit_row = await conn.fetchrow(
                    "SELECT id FROM situations WHERE id = $1", sit_id,
                )
                if not sit_row:
                    return f"Error: Situation {sit_id_str} not found"

                # Verify event exists
                evt_row = await conn.fetchrow(
                    "SELECT id FROM events WHERE id = $1", event_id,
                )
                if not evt_row:
                    return f"Error: Event {event_id_str} not found"

                # Insert link (ON CONFLICT to handle re-links gracefully)
                await conn.execute(
                    "INSERT INTO situation_events (situation_id, event_id, relevance) "
                    "VALUES ($1, $2, $3) "
                    "ON CONFLICT (situation_id, event_id) "
                    "DO UPDATE SET relevance = $3, added_at = NOW()",
                    sit_id, event_id, relevance,
                )

                # Update situation counters
                now = datetime.now(timezone.utc)
                actual_count = await conn.fetchval(
                    "SELECT count(*) FROM situation_events WHERE situation_id = $1",
                    sit_id,
                )
                await conn.execute(
                    "UPDATE situations SET "
                    "event_count = $2, "
                    "last_event_at = $3, updated_at = $3, "
                    "data = jsonb_set(jsonb_set(data, '{event_count}', $4::jsonb), "
                    "'{last_event_at}', $5::jsonb) "
                    "WHERE id = $1",
                    sit_id, actual_count, now,
                    json.dumps(actual_count),
                    json.dumps(now.isoformat()),
                )

        except Exception as e:
            return f"Error: Failed to link event: {e}"

        return json.dumps({
            "status": "linked",
            "situation_id": str(sit_id),
            "event_id": str(event_id),
            "relevance": relevance,
        }, indent=2)

    registry.register(SITUATION_CREATE_DEF, situation_create_handler)
    registry.register(SITUATION_UPDATE_DEF, situation_update_handler)
    registry.register(SITUATION_LIST_DEF, situation_list_handler)
    registry.register(SITUATION_LINK_EVENT_DEF, situation_link_event_handler)
