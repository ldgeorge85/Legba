"""
Consultation Engine — Interactive "Working" Interface

Lets the operator converse with Legba directly, querying its knowledge
stores through an LLM-driven tool-calling loop. Uses the same providers
as the agent but keeps its own lightweight conversation management.

Design:
- Does NOT reuse LLMClient (too coupled to cycles). Uses providers directly.
- Reuses parse_tool_calls / has_tool_call from the agent's tool parser.
- Reuses strip_harmony_response for vLLM output cleaning.
- Sessions stored in Redis with 1-hour TTL.
- Max 10 tool steps per exchange to prevent runaway loops.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable
from uuid import uuid4

from ..agent.llm.provider import VLLMProvider, LLMResponse
from ..agent.llm.anthropic_provider import AnthropicProvider
from ..agent.llm.tool_parser import parse_tool_calls, has_tool_call
from ..agent.llm.format import strip_harmony_response
from ..shared.config import LLMConfig
from .stores import StoreHolder

log = logging.getLogger(__name__)

MAX_TOOL_STEPS = 10
SESSION_TTL_SECONDS = 3600  # 1 hour


# ======================================================================
# Tool definitions
# ======================================================================

CONSULT_TOOLS = [
    {
        "name": "search_events",
        "description": "Full-text search over indexed events (OpenSearch). Returns matching events with title, summary, category, timestamp, actors, locations.",
        "parameters": [
            {"name": "query", "type": "string", "required": True, "description": "Search query text"},
            {"name": "category", "type": "string", "required": False, "description": "Filter by category"},
            {"name": "since", "type": "string", "required": False, "description": "ISO date — events after this time"},
            {"name": "until", "type": "string", "required": False, "description": "ISO date — events before this time"},
            {"name": "limit", "type": "integer", "required": False, "description": "Max results (default 20)"},
        ],
    },
    {
        "name": "query_events",
        "description": "Structured query over events via SQL (Postgres). Filter by category and date range.",
        "parameters": [
            {"name": "category", "type": "string", "required": False, "description": "Filter by event category"},
            {"name": "since", "type": "string", "required": False, "description": "ISO date — events after this time"},
            {"name": "until", "type": "string", "required": False, "description": "ISO date — events before this time"},
            {"name": "limit", "type": "integer", "required": False, "description": "Max results (default 20)"},
        ],
    },
    {
        "name": "inspect_entity",
        "description": "Get full entity profile and known facts. Returns profile data + up to 20 facts about the entity.",
        "parameters": [
            {"name": "name", "type": "string", "required": True, "description": "Entity canonical name (case-insensitive)"},
        ],
    },
    {
        "name": "search_facts",
        "description": "Search the fact store. At least one of subject, predicate, or value is required.",
        "parameters": [
            {"name": "subject", "type": "string", "required": False, "description": "Fact subject (ILIKE match)"},
            {"name": "predicate", "type": "string", "required": False, "description": "Fact predicate (ILIKE match)"},
            {"name": "value", "type": "string", "required": False, "description": "Fact value (ILIKE match)"},
            {"name": "limit", "type": "integer", "required": False, "description": "Max results (default 30)"},
        ],
    },
    {
        "name": "query_graph",
        "description": "Execute a Cypher query on the entity relationship graph (Apache AGE). Use AGE syntax: MATCH (n:entity) WHERE n.name = 'X'. NOT Neo4j — no shorthand property predicates in MATCH.",
        "parameters": [
            {"name": "query", "type": "string", "required": True, "description": "A Cypher query string (AGE syntax)"},
        ],
    },
    {
        "name": "list_situations",
        "description": "List tracked situations (ongoing events/crises being monitored).",
        "parameters": [
            {"name": "status", "type": "string", "required": False, "description": "Filter by status (active, resolved, etc.)"},
            {"name": "limit", "type": "integer", "required": False, "description": "Max results (default 20)"},
        ],
    },
    {
        "name": "list_watchlist",
        "description": "List watch patterns — monitored keywords/patterns that trigger on new events.",
        "parameters": [
            {"name": "active_only", "type": "boolean", "required": False, "description": "Only active watches (default true)"},
            {"name": "limit", "type": "integer", "required": False, "description": "Max results (default 20)"},
        ],
    },
    {
        "name": "list_sources",
        "description": "List active intelligence sources (RSS feeds, APIs, etc.).",
        "parameters": [
            {"name": "limit", "type": "integer", "required": False, "description": "Max results (default 30)"},
        ],
    },
    {
        "name": "list_goals",
        "description": "List goals and objectives.",
        "parameters": [
            {"name": "status", "type": "string", "required": False, "description": "Filter by status (default 'active')"},
            {"name": "limit", "type": "integer", "required": False, "description": "Max results (default 20)"},
        ],
    },
    {
        "name": "search_memory",
        "description": "Semantic search over episodic memory (Qdrant vector store). Finds memories by meaning, not keywords.",
        "parameters": [
            {"name": "query", "type": "string", "required": True, "description": "Natural-language query"},
            {"name": "limit", "type": "integer", "required": False, "description": "Max results (default 10)"},
        ],
    },
    {
        "name": "update_situation",
        "description": "Modify a tracked situation's status, description, or intensity.",
        "parameters": [
            {"name": "situation_id", "type": "string", "required": True, "description": "UUID of the situation"},
            {"name": "status", "type": "string", "required": False, "description": "New status (active, resolved, dormant, escalated)"},
            {"name": "description", "type": "string", "required": False, "description": "Updated description text"},
            {"name": "intensity_score", "type": "number", "required": False, "description": "New intensity score (0.0-1.0)"},
        ],
    },
    {
        "name": "update_goal",
        "description": "Modify a goal's status, progress, or add notes.",
        "parameters": [
            {"name": "goal_id", "type": "string", "required": True, "description": "UUID of the goal"},
            {"name": "status", "type": "string", "required": False, "description": "New status (active, completed, paused, cancelled)"},
            {"name": "progress_pct", "type": "number", "required": False, "description": "Progress percentage (0-100)"},
            {"name": "notes", "type": "string", "required": False, "description": "Operator notes to append"},
        ],
    },
    {
        "name": "send_message",
        "description": "Send a message to the agent's inbox, to be read on its next cycle.",
        "parameters": [
            {"name": "message", "type": "string", "required": True, "description": "Message content for the agent"},
            {"name": "priority", "type": "string", "required": False, "description": "Priority: normal, urgent, directive (default normal)"},
        ],
    },
]

_TOOL_DESC_BLOCK = "\n".join(
    f"- **{t['name']}**: {t['description']}" for t in CONSULT_TOOLS
)


def _format_tool_defs_json() -> str:
    """Format tool definitions as compact JSON for the system prompt."""
    tools = []
    for t in CONSULT_TOOLS:
        params = []
        for p in t.get("parameters", []):
            params.append({
                "name": p["name"],
                "type": p.get("type", "string"),
                "required": p.get("required", True),
                **({"description": p["description"]} if p.get("description") else {}),
            })
        tools.append({
            "name": t["name"],
            "description": t["description"],
            "parameters": params,
        })
    return json.dumps({"tools": tools}, indent=2)


# ======================================================================
# System prompt builder
# ======================================================================

_SYSTEM_TEMPLATE = """\
You are Legba — a continuously operating autonomous intelligence platform. \
You run indefinitely, ingesting global event data, building a knowledge graph, \
and producing analytical products.

Your operator is consulting you directly. This is a conversation — answer their \
questions using your knowledge base, look things up with your tools, and help \
them understand the world as you see it.

## Current State
- Cycle: {cycle_number}
- Events: {event_count} | Entities: {entity_count} | Relationships: {rel_count}
- Sources: {source_count} active | Goals: {goal_count} active

## Recent Journal
{journal}

## Active Situations
{situations}

## Tools
```json
{tool_defs}
```

## Tool Calling
To use a tool, output exactly:
{{"actions": [{{"tool": "tool_name", "args": {{"param": "value"}}}}]}}

You can call multiple tools at once in the actions array.
After receiving tool results, continue reasoning. When you have your answer, respond in plain text (no tool call).

## Guidelines
- Look things up rather than guessing — ground your answers in data
- Be analytical and insightful
- If you don't have data on something, say so
- Speak as yourself — you are Legba, the intelligence at the crossroads"""


async def _build_system_prompt(stores: StoreHolder) -> str:
    """Assemble the system prompt with live data from stores."""
    # Gather counts
    event_count = await stores.count_events()
    entity_count = await stores.count_entities()
    rel_count = await stores.count_relationships()
    source_count = await stores.count_sources()
    goal_count = await stores.count_goals()

    # Cycle number from Redis
    cycle_number = "unknown"
    try:
        raw = await stores.registers.get("cycle_number")
        if raw:
            cycle_number = raw
    except Exception:
        pass

    # Journal consolidation
    journal = "No journal consolidation available yet."
    try:
        journal_data = await stores.registers.get_json("journal")
        if journal_data:
            consolidation = journal_data.get("consolidation", "")
            if consolidation:
                journal = consolidation
    except Exception:
        pass

    # Active situations summary
    situations = "No active situations tracked."
    try:
        if stores.structured._available:
            async with stores.structured._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT name, category, intensity_score, event_count "
                    "FROM situations WHERE status = 'active' "
                    "ORDER BY intensity_score DESC LIMIT 15"
                )
                if rows:
                    lines = []
                    for r in rows:
                        lines.append(
                            f"- {r['name']} [{r['category']}] "
                            f"intensity={r['intensity_score']:.1f}, events={r['event_count']}"
                        )
                    situations = "\n".join(lines)
    except Exception:
        pass

    return _SYSTEM_TEMPLATE.format(
        cycle_number=cycle_number,
        event_count=event_count,
        entity_count=entity_count,
        rel_count=rel_count,
        source_count=source_count,
        goal_count=goal_count,
        journal=journal,
        situations=situations,
        tool_defs=_format_tool_defs_json(),
    )


# ======================================================================
# Tool handlers
# ======================================================================

async def _handle_search_events(stores: StoreHolder, args: dict) -> str:
    """Full-text search via OpenSearch."""
    query_text = args.get("query", "")
    if not query_text:
        return "Error: 'query' argument is required."

    category = args.get("category")
    since = args.get("since")
    until = args.get("until")
    limit = int(args.get("limit", 20))

    must_clauses: list[dict] = [
        {"multi_match": {"query": query_text, "fields": ["title^2", "summary", "actors", "locations"]}}
    ]
    if category:
        must_clauses.append({"term": {"category": category}})

    range_filter: dict[str, Any] = {}
    if since:
        range_filter["gte"] = since
    if until:
        range_filter["lte"] = until
    if range_filter:
        must_clauses.append({"range": {"event_timestamp": range_filter}})

    os_query = {"bool": {"must": must_clauses}}
    try:
        result = await stores.opensearch.search(
            index="legba-events-*",
            query=os_query,
            size=limit,
            sort=[{"event_timestamp": {"order": "desc"}}],
        )
        if result.get("error"):
            return f"Search error: {result['error']}"

        hits = result.get("hits", [])
        if not hits:
            return f"No events found matching '{query_text}'."

        events = []
        for h in hits:
            events.append({
                "title": h.get("title", ""),
                "summary": (h.get("summary", "") or "")[:200],
                "category": h.get("category", ""),
                "event_timestamp": h.get("event_timestamp", ""),
                "actors": h.get("actors", []),
                "locations": h.get("locations", []),
            })
        return json.dumps({"total": result.get("total", 0), "events": events}, indent=2, default=str)
    except Exception as e:
        return f"Error searching events: {e}"


async def _handle_query_events(stores: StoreHolder, args: dict) -> str:
    """Structured event query via Postgres."""
    category = args.get("category")
    since = args.get("since")
    until = args.get("until")
    limit = int(args.get("limit", 20))

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    conditions = []
    params: list[Any] = []
    idx = 1

    if category:
        conditions.append(f"category = ${idx}")
        params.append(category)
        idx += 1
    if since:
        conditions.append(f"event_timestamp >= ${idx}::timestamptz")
        params.append(since)
        idx += 1
    if until:
        conditions.append(f"event_timestamp <= ${idx}::timestamptz")
        params.append(until)
        idx += 1

    where = " AND ".join(conditions) if conditions else "TRUE"
    sql = (
        f"SELECT title, category, event_timestamp, data "
        f"FROM events WHERE {where} "
        f"ORDER BY event_timestamp DESC LIMIT {limit}"
    )
    try:
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        events = []
        for r in rows:
            data = r["data"]
            if isinstance(data, str):
                data = json.loads(data)
            events.append({
                "title": r["title"],
                "category": r["category"],
                "event_timestamp": str(r["event_timestamp"]) if r["event_timestamp"] else None,
                "summary": (data.get("summary", "") or "")[:200],
                "actors": data.get("actors", []),
                "locations": data.get("locations", []),
            })
        if not events:
            return "No events found matching the criteria."
        return json.dumps({"count": len(events), "events": events}, indent=2, default=str)
    except Exception as e:
        return f"Error querying events: {e}"


async def _handle_inspect_entity(stores: StoreHolder, args: dict) -> str:
    """Get entity profile + facts."""
    name = args.get("name", "")
    if not name:
        return "Error: 'name' argument is required."

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    try:
        async with stores.structured._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data FROM entity_profiles WHERE lower(canonical_name) = $1",
                name.lower(),
            )
            if not row:
                return f"No entity profile found for '{name}'."

            profile_data = row["data"]
            if isinstance(profile_data, str):
                profile_data = json.loads(profile_data)

            facts_rows = await conn.fetch(
                "SELECT subject, predicate, value, confidence "
                "FROM facts WHERE lower(subject) = $1 "
                "ORDER BY confidence DESC LIMIT 20",
                name.lower(),
            )
            facts = [
                {
                    "subject": f["subject"],
                    "predicate": f["predicate"],
                    "value": f["value"],
                    "confidence": float(f["confidence"]),
                }
                for f in facts_rows
            ]

        result = {"profile": profile_data, "facts": facts}
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        return f"Error inspecting entity: {e}"


async def _handle_search_facts(stores: StoreHolder, args: dict) -> str:
    """Search fact store with ILIKE."""
    subject = args.get("subject")
    predicate = args.get("predicate")
    value = args.get("value")
    limit = int(args.get("limit", 30))

    if not any([subject, predicate, value]):
        return "Error: at least one of 'subject', 'predicate', or 'value' is required."

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    conditions = []
    params: list[Any] = []
    idx = 1

    if subject:
        conditions.append(f"subject ILIKE ${idx}")
        params.append(f"%{subject}%")
        idx += 1
    if predicate:
        conditions.append(f"predicate ILIKE ${idx}")
        params.append(f"%{predicate}%")
        idx += 1
    if value:
        conditions.append(f"value ILIKE ${idx}")
        params.append(f"%{value}%")
        idx += 1

    where = " AND ".join(conditions)
    sql = (
        f"SELECT subject, predicate, value, confidence "
        f"FROM facts WHERE {where} "
        f"ORDER BY confidence DESC LIMIT {limit}"
    )
    try:
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        facts = [
            {
                "subject": r["subject"],
                "predicate": r["predicate"],
                "value": r["value"],
                "confidence": float(r["confidence"]),
            }
            for r in rows
        ]
        if not facts:
            return "No facts found matching the criteria."
        return json.dumps({"count": len(facts), "facts": facts}, indent=2, default=str)
    except Exception as e:
        return f"Error searching facts: {e}"


async def _handle_query_graph(stores: StoreHolder, args: dict) -> str:
    """Execute Cypher on the entity graph."""
    query = args.get("query", "")
    if not query:
        return "Error: 'query' argument is required."

    if not stores.graph.available:
        return "Error: Graph store unavailable."

    try:
        results = await stores.graph.execute_cypher(query)
        if results and isinstance(results[0], dict) and "error" in results[0]:
            return f"Cypher error: {results[0]['error']}"
        return json.dumps({"results": results, "count": len(results)}, indent=2, default=str)
    except Exception as e:
        return f"Error executing Cypher: {e}"


async def _handle_list_situations(stores: StoreHolder, args: dict) -> str:
    """List tracked situations."""
    status = args.get("status")
    limit = int(args.get("limit", 20))

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    conditions = []
    params: list[Any] = []
    idx = 1

    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1

    where = " AND ".join(conditions) if conditions else "TRUE"
    sql = (
        f"SELECT id, name, status, category, event_count, intensity_score, data "
        f"FROM situations WHERE {where} "
        f"ORDER BY intensity_score DESC LIMIT {limit}"
    )
    try:
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        situations = []
        for r in rows:
            data = r["data"]
            if isinstance(data, str):
                data = json.loads(data)
            situations.append({
                "id": str(r["id"]),
                "name": r["name"],
                "status": r["status"],
                "category": r["category"],
                "event_count": r["event_count"],
                "intensity_score": float(r["intensity_score"]),
                "key_entities": data.get("key_entities", []),
                "regions": data.get("regions", []),
            })
        if not situations:
            return "No situations found."
        return json.dumps({"count": len(situations), "situations": situations}, indent=2, default=str)
    except Exception as e:
        return f"Error listing situations: {e}"


async def _handle_list_watchlist(stores: StoreHolder, args: dict) -> str:
    """List watch patterns."""
    active_only = args.get("active_only", True)
    # Handle string "false" from LLM
    if isinstance(active_only, str):
        active_only = active_only.lower() not in ("false", "0", "no")
    limit = int(args.get("limit", 20))

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    conditions = []
    params: list[Any] = []

    if active_only:
        conditions.append("active = TRUE")

    where = " AND ".join(conditions) if conditions else "TRUE"
    sql = (
        f"SELECT id, name, priority, active, trigger_count, data "
        f"FROM watchlist WHERE {where} "
        f"ORDER BY trigger_count DESC LIMIT {limit}"
    )
    try:
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        watches = []
        for r in rows:
            data = r["data"]
            if isinstance(data, str):
                data = json.loads(data)
            watches.append({
                "id": str(r["id"]),
                "name": r["name"],
                "priority": r["priority"],
                "active": r["active"],
                "trigger_count": r["trigger_count"],
                "pattern": data.get("pattern", ""),
                "description": data.get("description", ""),
            })
        if not watches:
            return "No watch patterns found."
        return json.dumps({"count": len(watches), "watches": watches}, indent=2, default=str)
    except Exception as e:
        return f"Error listing watchlist: {e}"


async def _handle_list_sources(stores: StoreHolder, args: dict) -> str:
    """List active sources."""
    limit = int(args.get("limit", 30))

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    sql = (
        "SELECT name, url, source_type, status, reliability, "
        "events_produced_count, last_successful_fetch_at "
        "FROM sources WHERE status = 'active' "
        "ORDER BY events_produced_count DESC NULLS LAST "
        f"LIMIT {limit}"
    )
    try:
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(sql)

        sources = [
            {
                "name": r["name"],
                "url": r["url"],
                "source_type": r["source_type"],
                "status": r["status"],
                "reliability": float(r["reliability"]),
                "events_produced_count": r["events_produced_count"] or 0,
                "last_successful_fetch_at": str(r["last_successful_fetch_at"]) if r["last_successful_fetch_at"] else None,
            }
            for r in rows
        ]
        if not sources:
            return "No active sources found."
        return json.dumps({"count": len(sources), "sources": sources}, indent=2, default=str)
    except Exception as e:
        return f"Error listing sources: {e}"


async def _handle_list_goals(stores: StoreHolder, args: dict) -> str:
    """List goals."""
    status = args.get("status", "active")
    limit = int(args.get("limit", 20))

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    conditions = []
    params: list[Any] = []
    idx = 1

    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1

    where = " AND ".join(conditions) if conditions else "TRUE"
    sql = (
        f"SELECT id, status, priority, data "
        f"FROM goals WHERE {where} "
        f"ORDER BY priority ASC LIMIT {limit}"
    )
    try:
        async with stores.structured._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        goals = []
        for r in rows:
            data = r["data"]
            if isinstance(data, str):
                data = json.loads(data)
            goals.append({
                "id": str(r["id"]),
                "status": r["status"],
                "priority": r["priority"],
                "name": data.get("name", ""),
                "description": data.get("description", ""),
                "progress_pct": data.get("progress_pct", 0),
                "goal_type": data.get("goal_type", "goal"),
            })
        if not goals:
            return "No goals found."
        return json.dumps({"count": len(goals), "goals": goals}, indent=2, default=str)
    except Exception as e:
        return f"Error listing goals: {e}"


async def _handle_search_memory(stores: StoreHolder, args: dict) -> str:
    """Semantic search over episodic memory."""
    query_text = args.get("query", "")
    if not query_text:
        return "Error: 'query' argument is required."

    limit = int(args.get("limit", 10))
    try:
        results = await stores.search_memories("legba_short_term", query_text, limit)
        if not results:
            return "No memories found matching the query."

        memories = []
        for r in results:
            memories.append({
                "content": r.get("content", ""),
                "phase": r.get("phase", ""),
                "cycle": r.get("cycle_number", ""),
                "timestamp": r.get("timestamp", ""),
                "score": r.get("score", 0),
            })
        return json.dumps({"count": len(memories), "memories": memories}, indent=2, default=str)
    except Exception as e:
        return f"Error searching memory: {e}"


async def _handle_update_situation(stores: StoreHolder, args: dict) -> str:
    """Update a situation's status/description/intensity."""
    situation_id = args.get("situation_id", "")
    if not situation_id:
        return "Error: 'situation_id' argument is required."

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    status = args.get("status")
    description = args.get("description")
    intensity_score = args.get("intensity_score")

    if not any([status, description, intensity_score is not None]):
        return "Error: provide at least one of 'status', 'description', or 'intensity_score' to update."

    try:
        from uuid import UUID as _UUID
        sid = _UUID(situation_id)
    except ValueError:
        return f"Error: invalid UUID '{situation_id}'."

    set_clauses = ["updated_at = NOW()"]
    params: list[Any] = []
    idx = 1

    if status:
        set_clauses.append(f"status = ${idx}")
        params.append(status)
        idx += 1
    if intensity_score is not None:
        set_clauses.append(f"intensity_score = ${idx}")
        params.append(float(intensity_score))
        idx += 1

    # Build JSONB updates for the data column
    jsonb_updates = []
    if description:
        jsonb_updates.append(f"data = jsonb_set(data, '{{description}}', ${idx}::jsonb)")
        params.append(json.dumps(description))
        idx += 1
    if status:
        jsonb_updates.append(f"data = jsonb_set(data, '{{status}}', ${idx}::jsonb)")
        params.append(json.dumps(status))
        idx += 1

    all_updates = set_clauses + jsonb_updates
    params.append(sid)

    sql = f"UPDATE situations SET {', '.join(all_updates)} WHERE id = ${idx} RETURNING name"
    try:
        async with stores.structured._pool.acquire() as conn:
            row = await conn.fetchrow(sql, *params)
        if not row:
            return f"No situation found with id '{situation_id}'."
        return f"Situation '{row['name']}' updated successfully."
    except Exception as e:
        return f"Error updating situation: {e}"


async def _handle_update_goal(stores: StoreHolder, args: dict) -> str:
    """Update a goal's status/progress/notes."""
    goal_id = args.get("goal_id", "")
    if not goal_id:
        return "Error: 'goal_id' argument is required."

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    status = args.get("status")
    progress_pct = args.get("progress_pct")
    notes = args.get("notes")

    if not any([status, progress_pct is not None, notes]):
        return "Error: provide at least one of 'status', 'progress_pct', or 'notes' to update."

    try:
        from uuid import UUID as _UUID
        gid = _UUID(goal_id)
    except ValueError:
        return f"Error: invalid UUID '{goal_id}'."

    set_clauses = ["updated_at = NOW()"]
    params: list[Any] = []
    idx = 1

    if status:
        set_clauses.append(f"status = ${idx}")
        params.append(status)
        idx += 1

    # JSONB updates on the data column
    jsonb_updates = []
    if progress_pct is not None:
        jsonb_updates.append(f"data = jsonb_set(data, '{{progress_pct}}', ${idx}::jsonb)")
        params.append(json.dumps(float(progress_pct)))
        idx += 1
    if status:
        jsonb_updates.append(f"data = jsonb_set(data, '{{status}}', ${idx}::jsonb)")
        params.append(json.dumps(status))
        idx += 1
    if notes:
        # Append to existing notes or create new
        jsonb_updates.append(
            f"data = jsonb_set(data, '{{operator_notes}}', "
            f"COALESCE(data->'operator_notes', '[]'::jsonb) || ${idx}::jsonb)"
        )
        note_entry = json.dumps([{
            "text": notes,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }])
        params.append(note_entry)
        idx += 1

    all_updates = set_clauses + jsonb_updates
    params.append(gid)

    sql = f"UPDATE goals SET {', '.join(all_updates)} WHERE id = ${idx} RETURNING data->>'name' AS name"
    try:
        async with stores.structured._pool.acquire() as conn:
            row = await conn.fetchrow(sql, *params)
        if not row:
            return f"No goal found with id '{goal_id}'."
        return f"Goal '{row['name'] or goal_id}' updated successfully."
    except Exception as e:
        return f"Error updating goal: {e}"


async def _handle_send_message(
    stores: StoreHolder,
    args: dict,
    send_callback: Callable[[str, str], Awaitable[bool]] | None,
) -> str:
    """Send a message to the agent."""
    message = args.get("message", "")
    if not message:
        return "Error: 'message' argument is required."

    priority = args.get("priority", "normal")
    if priority not in ("normal", "urgent", "directive"):
        priority = "normal"

    if send_callback:
        try:
            ok = await send_callback(message, priority)
            if ok:
                return f"Message sent to agent inbox (priority: {priority}). It will be read on the next cycle."
            else:
                return "Failed to send message — NATS may be unavailable. Try the Messages page."
        except Exception as e:
            return f"Error sending message: {e}"
    else:
        return "Message sending not available in this context. Use the Messages page to send messages to the agent."


# Tool handler dispatch table
_TOOL_HANDLERS: dict[str, Callable] = {
    "search_events": _handle_search_events,
    "query_events": _handle_query_events,
    "inspect_entity": _handle_inspect_entity,
    "search_facts": _handle_search_facts,
    "query_graph": _handle_query_graph,
    "list_situations": _handle_list_situations,
    "list_watchlist": _handle_list_watchlist,
    "list_sources": _handle_list_sources,
    "list_goals": _handle_list_goals,
    "search_memory": _handle_search_memory,
    "update_situation": _handle_update_situation,
    "update_goal": _handle_update_goal,
    # send_message handled specially (needs callback)
}


# ======================================================================
# Consultation Engine
# ======================================================================

class ConsultationEngine:
    """
    Interactive consultation interface — operator converses with Legba,
    backed by tool-calling against the live knowledge stores.
    """

    def __init__(
        self,
        stores: StoreHolder,
        llm_config: LLMConfig,
        send_message_callback: Callable[[str, str], Awaitable[bool]] | None = None,
    ):
        self.stores = stores
        self.llm_config = llm_config
        self._send_callback = send_message_callback
        self._provider: VLLMProvider | AnthropicProvider | None = None

    # ------------------------------------------------------------------
    # Provider management
    # ------------------------------------------------------------------

    def _get_provider(self) -> VLLMProvider | AnthropicProvider:
        """Lazily create the LLM provider."""
        if self._provider is not None:
            return self._provider

        if self.llm_config.provider == "anthropic":
            self._provider = AnthropicProvider(
                api_key=self.llm_config.api_key,
                model=self.llm_config.model,
                timeout=self.llm_config.timeout,
                temperature=self.llm_config.temperature,
                max_tokens=self.llm_config.max_tokens,
            )
        else:
            self._provider = VLLMProvider(
                api_base=self.llm_config.api_base,
                api_key=self.llm_config.api_key,
                model=self.llm_config.model,
                timeout=self.llm_config.timeout,
                temperature=self.llm_config.temperature,
                top_p=self.llm_config.top_p,
            )
        return self._provider

    async def close(self) -> None:
        """Close the LLM provider's HTTP client."""
        if self._provider is not None:
            await self._provider.close()
            self._provider = None

    # ------------------------------------------------------------------
    # Session management (Redis-backed)
    # ------------------------------------------------------------------

    def _session_key(self, session_id: str) -> str:
        return f"consult:session:{session_id}"

    async def load_session(self, session_id: str) -> list[dict[str, str]]:
        """Load conversation history from Redis."""
        try:
            redis = self.stores.registers._redis
            if redis is None:
                return []
            raw = await redis.get(self._session_key(session_id))
            if raw:
                data = json.loads(raw)
                return data.get("messages", [])
        except Exception as e:
            log.warning("Failed to load session %s: %s", session_id, e)
        return []

    async def save_session(
        self, session_id: str, messages: list[dict[str, str]]
    ) -> None:
        """Persist conversation history to Redis with TTL."""
        try:
            redis = self.stores.registers._redis
            if redis is None:
                return
            payload = json.dumps({"messages": messages})
            await redis.set(
                self._session_key(session_id), payload, ex=SESSION_TTL_SECONDS
            )
        except Exception as e:
            log.warning("Failed to save session %s: %s", session_id, e)

    async def delete_session(self, session_id: str) -> None:
        """Delete a session from Redis."""
        try:
            redis = self.stores.registers._redis
            if redis is None:
                return
            await redis.delete(self._session_key(session_id))
        except Exception as e:
            log.warning("Failed to delete session %s: %s", session_id, e)

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tool(self, tool_name: str, args: dict) -> str:
        """Execute a consultation tool and return the result string."""
        if tool_name == "send_message":
            return await _handle_send_message(self.stores, args, self._send_callback)

        handler = _TOOL_HANDLERS.get(tool_name)
        if handler is None:
            return f"Unknown tool: '{tool_name}'. Available: {', '.join(_TOOL_HANDLERS.keys())}, send_message"

        try:
            return await handler(self.stores, args)
        except Exception as e:
            log.exception("Tool %s failed", tool_name)
            return f"Tool '{tool_name}' failed: {e}"

    # ------------------------------------------------------------------
    # Core exchange loop
    # ------------------------------------------------------------------

    async def exchange(
        self, session_id: str, user_message: str
    ) -> tuple[str, list[dict[str, str]]]:
        """
        Run one operator exchange: takes a user message, runs the
        tool-calling loop, returns (final_response, updated_messages).

        Returns the assistant's final text response and the full
        updated conversation history.
        """
        provider = self._get_provider()

        # Load existing session
        messages = await self.load_session(session_id)

        # Build system prompt
        system_prompt = await _build_system_prompt(self.stores)

        # Append user message
        messages.append({"role": "user", "content": user_message})

        # Tool-calling loop
        for step in range(MAX_TOOL_STEPS):
            response = await self._call_llm(provider, system_prompt, messages)
            content = response.content.strip()

            log.info(
                "Consult step %d/%d: tokens=%s, has_tool=%s",
                step + 1, MAX_TOOL_STEPS,
                response.usage, has_tool_call(content),
            )

            if not has_tool_call(content):
                # Final response — no tool call
                messages.append({"role": "assistant", "content": content})
                await self.save_session(session_id, messages)
                return content, messages

            # Parse and execute tool calls
            tool_calls = parse_tool_calls(content)
            if not tool_calls:
                # Parser found nothing despite has_tool_call hint — treat as final
                messages.append({"role": "assistant", "content": content})
                await self.save_session(session_id, messages)
                return content, messages

            # Record the assistant's tool-calling message
            messages.append({"role": "assistant", "content": content})

            # Execute each tool and collect results
            result_parts = []
            for tc in tool_calls:
                result = await self._execute_tool(tc.tool_name, tc.arguments)
                result_parts.append(f"[Tool Result: {tc.tool_name}]\n{result}")

            tool_results_text = "\n\n".join(result_parts)
            messages.append({"role": "user", "content": tool_results_text})

        # Exhausted tool steps — ask for a final answer
        messages.append({
            "role": "user",
            "content": (
                "You have used all available tool steps. "
                "Please provide your final answer now based on the results so far."
            ),
        })
        response = await self._call_llm(provider, system_prompt, messages)
        content = response.content.strip()
        messages.append({"role": "assistant", "content": content})
        await self.save_session(session_id, messages)
        return content, messages

    async def _call_llm(
        self,
        provider: VLLMProvider | AnthropicProvider,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> LLMResponse:
        """
        Call the LLM provider with the system prompt + conversation.

        For vLLM: combine system + conversation into a single user message.
        For Anthropic: use proper system + multi-turn messages.
        """
        if isinstance(provider, AnthropicProvider):
            # Anthropic: system is top-level, messages are user/assistant turns
            # Ensure first message is user role
            conv = list(messages)
            if not conv or conv[0]["role"] != "user":
                conv.insert(0, {"role": "user", "content": "(context follows)"})
            return await provider.chat_complete(
                messages=conv,
                system=system_prompt,
            )
        else:
            # vLLM / GPT-OSS: combine everything into a single user message
            parts = [system_prompt]
            for m in messages:
                role_label = "OPERATOR" if m["role"] == "user" else "LEGBA"
                parts.append(f"[{role_label}]\n{m['content']}")
            combined = "\n\n".join(parts)
            response = await provider.chat_complete(
                messages=[{"role": "user", "content": combined}],
            )
            # Strip Harmony artifacts from vLLM responses
            response.content = strip_harmony_response(response.content)
            return response
