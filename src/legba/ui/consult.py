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

from ..agent.llm.provider import VLLMProvider, LLMResponse, LLMApiError
from ..agent.llm.anthropic_provider import AnthropicProvider
from ..agent.llm.tool_parser import parse_tool_calls, has_tool_call
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
        "description": "Modify a tracked situation's status, description, category, entities, regions, tags, or intensity.",
        "parameters": [
            {"name": "situation_id", "type": "string", "required": True, "description": "UUID of the situation"},
            {"name": "status", "type": "string", "required": False, "description": "New status (active, resolved, dormant, escalating, de_escalating)"},
            {"name": "description", "type": "string", "required": False, "description": "Updated description text"},
            {"name": "category", "type": "string", "required": False, "description": "Updated category"},
            {"name": "key_entities", "type": "string", "required": False, "description": "Comma-separated key entities"},
            {"name": "regions", "type": "string", "required": False, "description": "Comma-separated regions"},
            {"name": "tags", "type": "string", "required": False, "description": "Comma-separated tags"},
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
    {
        "name": "list_hypotheses",
        "description": "List competing hypotheses (ACH). Shows thesis vs counter-thesis pairs with evidence balance, linked situations, and diagnostic evidence status.",
        "parameters": [
            {"name": "status", "type": "string", "required": False, "description": "Filter: active, confirmed, refuted, stale (default active)"},
            {"name": "situation_id", "type": "string", "required": False, "description": "Filter by situation UUID"},
            {"name": "limit", "type": "integer", "required": False, "description": "Max results (default 20)"},
        ],
    },
    {
        "name": "list_briefs",
        "description": "List situation briefs produced by SYNTHESIZE cycles. Named intelligence documents with thesis, evidence, competing hypotheses, predictions, and recommendations.",
        "parameters": [
            {"name": "limit", "type": "integer", "required": False, "description": "Max results (default 20)"},
        ],
    },
    # --- Write tools ---
    {
        "name": "add_entity_assertion",
        "description": "Add an assertion to an entity profile's JSONB data.sections.",
        "parameters": [
            {"name": "entity_id", "type": "string", "required": True, "description": "UUID of the entity"},
            {"name": "section", "type": "string", "required": False, "description": "Section name (default 'general')"},
            {"name": "key", "type": "string", "required": True, "description": "Assertion key"},
            {"name": "value", "type": "string", "required": True, "description": "Assertion value"},
            {"name": "confidence", "type": "number", "required": False, "description": "Confidence (default 0.7)"},
        ],
    },
    {
        "name": "remove_entity_assertion",
        "description": "Remove a specific assertion from an entity profile.",
        "parameters": [
            {"name": "entity_id", "type": "string", "required": True, "description": "UUID of the entity"},
            {"name": "section", "type": "string", "required": True, "description": "Section name"},
            {"name": "key", "type": "string", "required": True, "description": "Assertion key"},
            {"name": "value", "type": "string", "required": True, "description": "Assertion value"},
        ],
    },
    {
        "name": "update_event",
        "description": "Update event metadata (category, tags, confidence).",
        "parameters": [
            {"name": "event_id", "type": "string", "required": True, "description": "UUID of the event"},
            {"name": "category", "type": "string", "required": False, "description": "New category"},
            {"name": "tags", "type": "string", "required": False, "description": "Comma-separated tags"},
            {"name": "confidence", "type": "number", "required": False, "description": "New confidence score"},
        ],
    },
    {
        "name": "delete_event",
        "description": "Delete an event and its entity links. Also removes from OpenSearch.",
        "parameters": [
            {"name": "event_id", "type": "string", "required": True, "description": "UUID of the event"},
        ],
    },
    {
        "name": "create_source",
        "description": "Create a new intelligence source.",
        "parameters": [
            {"name": "name", "type": "string", "required": True, "description": "Source name"},
            {"name": "url", "type": "string", "required": True, "description": "Source URL"},
            {"name": "source_type", "type": "string", "required": False, "description": "Type (rss, api, scrape, etc. — default 'rss')"},
            {"name": "reliability", "type": "number", "required": False, "description": "Reliability score 0-1 (default 0.5)"},
            {"name": "description", "type": "string", "required": False, "description": "Source description"},
        ],
    },
    {
        "name": "update_source",
        "description": "Update a source's metadata.",
        "parameters": [
            {"name": "source_id", "type": "string", "required": True, "description": "UUID of the source"},
            {"name": "name", "type": "string", "required": False, "description": "New name"},
            {"name": "url", "type": "string", "required": False, "description": "New URL"},
            {"name": "source_type", "type": "string", "required": False, "description": "New source type"},
            {"name": "reliability", "type": "number", "required": False, "description": "New reliability score"},
            {"name": "description", "type": "string", "required": False, "description": "New description"},
        ],
    },
    {
        "name": "delete_source",
        "description": "Delete a source permanently.",
        "parameters": [
            {"name": "source_id", "type": "string", "required": True, "description": "UUID of the source"},
        ],
    },
    {
        "name": "create_goal",
        "description": "Create a new operational goal.",
        "parameters": [
            {"name": "description", "type": "string", "required": True, "description": "Goal description"},
            {"name": "priority", "type": "integer", "required": False, "description": "Priority 1-5 (default 3)"},
            {"name": "parent_id", "type": "string", "required": False, "description": "UUID of parent goal"},
        ],
    },
    {
        "name": "delete_goal",
        "description": "Delete a goal permanently.",
        "parameters": [
            {"name": "goal_id", "type": "string", "required": True, "description": "UUID of the goal"},
        ],
    },
    {
        "name": "create_situation",
        "description": "Create a new tracked situation.",
        "parameters": [
            {"name": "name", "type": "string", "required": True, "description": "Situation name"},
            {"name": "description", "type": "string", "required": False, "description": "Description"},
            {"name": "category", "type": "string", "required": False, "description": "Category"},
            {"name": "key_entities", "type": "string", "required": False, "description": "Comma-separated key entities"},
            {"name": "regions", "type": "string", "required": False, "description": "Comma-separated regions"},
        ],
    },
    {
        "name": "delete_situation",
        "description": "Delete a situation and its event links.",
        "parameters": [
            {"name": "situation_id", "type": "string", "required": True, "description": "UUID of the situation"},
        ],
    },
    {
        "name": "link_event_to_situation",
        "description": "Link an event to a situation.",
        "parameters": [
            {"name": "situation_id", "type": "string", "required": True, "description": "UUID of the situation"},
            {"name": "event_id", "type": "string", "required": True, "description": "UUID of the event"},
            {"name": "relevance", "type": "number", "required": False, "description": "Relevance score 0-1 (default 0.8)"},
        ],
    },
    {
        "name": "create_watch",
        "description": "Create a new watch pattern. At least one criterion required.",
        "parameters": [
            {"name": "name", "type": "string", "required": True, "description": "Watch name"},
            {"name": "entities", "type": "string", "required": False, "description": "Comma-separated entity names"},
            {"name": "keywords", "type": "string", "required": False, "description": "Comma-separated keywords"},
            {"name": "categories", "type": "string", "required": False, "description": "Comma-separated event categories"},
            {"name": "priority", "type": "string", "required": False, "description": "Priority: normal, high, critical (default 'normal')"},
        ],
    },
    {
        "name": "delete_watch",
        "description": "Delete a watch pattern and its triggers.",
        "parameters": [
            {"name": "watch_id", "type": "string", "required": True, "description": "UUID of the watch"},
        ],
    },
    {
        "name": "update_fact",
        "description": "Update a fact's subject, predicate, value, and confidence.",
        "parameters": [
            {"name": "fact_id", "type": "string", "required": True, "description": "UUID of the fact"},
            {"name": "subject", "type": "string", "required": True, "description": "Fact subject"},
            {"name": "predicate", "type": "string", "required": True, "description": "Fact predicate"},
            {"name": "value", "type": "string", "required": True, "description": "Fact value"},
            {"name": "confidence", "type": "number", "required": True, "description": "Confidence 0-1"},
        ],
    },
    {
        "name": "delete_fact",
        "description": "Delete a fact permanently.",
        "parameters": [
            {"name": "fact_id", "type": "string", "required": True, "description": "UUID of the fact"},
        ],
    },
    {
        "name": "add_graph_edge",
        "description": "Add a relationship edge between two entities in the graph.",
        "parameters": [
            {"name": "from_entity", "type": "string", "required": True, "description": "Source entity name"},
            {"name": "to_entity", "type": "string", "required": True, "description": "Target entity name"},
            {"name": "relation_type", "type": "string", "required": True, "description": "Relationship type (e.g. AlliedWith, HostileTo, LeaderOf)"},
            {"name": "since", "type": "string", "required": False, "description": "Date since the relationship started"},
        ],
    },
    {
        "name": "remove_graph_edge",
        "description": "Remove a relationship edge between two entities in the graph.",
        "parameters": [
            {"name": "from_entity", "type": "string", "required": True, "description": "Source entity name"},
            {"name": "to_entity", "type": "string", "required": True, "description": "Target entity name"},
            {"name": "relation_type", "type": "string", "required": True, "description": "Relationship type"},
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

# System content: identity + tools + format rules (instructions-first, like the agent)
_SYSTEM_CONTENT = """\
reasoning: high

You are Legba — a persistent autonomous intelligence analyst. The operator is consulting you. Answer using your tools, then use the respond tool to deliver your answer.

You have both read and write tools. Write tools let you create, update, and delete entities, events, sources, goals, situations, watchlist items, facts, and graph edges on the operator's behalf. Confirm destructive actions (deletes) before executing when the intent is ambiguous.

# Tools

```json
{tool_defs}
```

When the operator asks about graph relationships, entities, or connections,
use the query_graph tool to answer. Translate natural language to Cypher queries.

Examples:
- "Show me all HostileTo edges" → query_graph("MATCH (a)-[r:HostileTo]->(b) RETURN a.name, b.name")
- "Who leads Iran?" → query_graph("MATCH (p)-[:LeaderOf]->(c {{name: 'Iran'}}) RETURN p.name")
- "What's connected to Hezbollah?" → query_graph("MATCH (h {{name: 'Hezbollah'}})-[r]-(n) RETURN type(r), n.name")
- "Entities added since cycle 1000" → query_graph("MATCH (n) WHERE n.source_cycle > 1000 RETURN n.name, labels(n)")

Remember: This is Apache AGE (Postgres), not Neo4j. Use ag_catalog functions.
Key syntax: MATCH, WHERE, RETURN. Labels use labels(n). Edge types use type(r).
Property predicates go in WHERE, not in MATCH pattern shorthand.

Respond with a SINGLE JSON object: {{"actions": [{{"tool": "name", "args": {{...}}}}]}}
Use the respond tool for your final answer: {{"actions": [{{"tool": "respond", "args": {{"message": "..."}}}}]}}
Output ONLY valid JSON. After the closing }}, STOP."""

# User content: data sections + task (data-last, like the agent)
_USER_TEMPLATE = """\
--- CONTEXT DATA ---

Cycle: {cycle_number} | Events: {event_count} | Entities: {entity_count} | Relationships: {rel_count} | Sources: {source_count} | Goals: {goal_count}

## Journal
{journal}

## Situations
{situations}

--- END CONTEXT ---"""


async def _build_prompts(stores: StoreHolder) -> tuple[str, str]:
    """Build system content and user context from stores. Returns (system, user_context)."""
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
    except Exception as e:
        log.warning("Failed to fetch cycle number from Redis: %s", e)

    # Journal consolidation (truncated to avoid overwhelming the model)
    journal = "(none)"
    try:
        journal_data = await stores.registers.get_json("journal")
        if journal_data:
            consolidation = journal_data.get("consolidation", "")
            if consolidation:
                journal = consolidation[:1500]
    except Exception as e:
        log.warning("Failed to fetch journal consolidation: %s", e)

    # Active situations summary
    situations = "(none)"
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
    except Exception as e:
        log.warning("Failed to fetch active situations: %s", e)

    system = _SYSTEM_CONTENT.format(tool_defs=_format_tool_defs_json())
    user_context = _USER_TEMPLATE.format(
        cycle_number=cycle_number,
        event_count=event_count,
        entity_count=entity_count,
        rel_count=rel_count,
        source_count=source_count,
        goal_count=goal_count,
        journal=journal,
        situations=situations,
    )
    return system, user_context


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
        f"FROM signals WHERE {where} "
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

    import re as _re
    if _re.search(r'\b(CREATE|MERGE|SET|DELETE|REMOVE|DETACH)\b', query, _re.IGNORECASE):
        return "Error: query_graph is read-only. Use add_graph_edge or remove_graph_edge for mutations."

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
    """Update a situation's status/description/category/entities/regions/tags/intensity."""
    situation_id = args.get("situation_id", "")
    if not situation_id:
        return "Error: 'situation_id' argument is required."

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    status = args.get("status")
    description = args.get("description")
    category = args.get("category")
    key_entities = args.get("key_entities")
    regions = args.get("regions")
    tags = args.get("tags")
    intensity_score = args.get("intensity_score")

    if not any([status, description, category, key_entities, regions, tags, intensity_score is not None]):
        return "Error: provide at least one field to update."

    try:
        from uuid import UUID as _UUID
        sid = _UUID(situation_id)
    except ValueError:
        return f"Error: invalid UUID '{situation_id}'."

    try:
        async with stores.structured._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT data, name FROM situations WHERE id = $1", sid)
            if not row:
                return f"No situation found with id '{situation_id}'."

            data = row["data"]
            if isinstance(data, str):
                data = json.loads(data)

            set_parts = ["updated_at = NOW()"]
            params: list[Any] = []
            idx = 1

            if status:
                set_parts.append(f"status = ${idx}")
                params.append(status)
                idx += 1
                data["status"] = status
            if category:
                set_parts.append(f"category = ${idx}")
                params.append(category)
                idx += 1
                data["category"] = category
            if intensity_score is not None:
                set_parts.append(f"intensity_score = ${idx}")
                params.append(float(intensity_score))
                idx += 1
                data["intensity_score"] = float(intensity_score)
            if description:
                data["description"] = description
            if key_entities:
                data["key_entities"] = [e.strip() for e in key_entities.split(",") if e.strip()]
            if regions:
                data["regions"] = [r.strip() for r in regions.split(",") if r.strip()]
            if tags:
                data["tags"] = [t.strip() for t in tags.split(",") if t.strip()]

            data["updated_at"] = datetime.now(timezone.utc).isoformat()

            set_parts.append(f"data = ${idx}::jsonb")
            params.append(json.dumps(data, default=str))
            idx += 1

            params.append(sid)
            sql = f"UPDATE situations SET {', '.join(set_parts)} WHERE id = ${idx} RETURNING name"
            result = await conn.fetchrow(sql, *params)

        return f"Situation '{result['name']}' updated successfully."
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


async def _handle_add_entity_assertion(stores: StoreHolder, args: dict) -> str:
    entity_id = args.get("entity_id", "")
    if not entity_id:
        return "Error: 'entity_id' is required."
    section = args.get("section", "general")
    key = args.get("key", "")
    value = args.get("value", "")
    if not key or not value:
        return "Error: 'key' and 'value' are required."
    confidence = float(args.get("confidence", 0.7))

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    try:
        from uuid import UUID as _UUID
        eid = _UUID(entity_id)
    except ValueError:
        return f"Error: invalid UUID '{entity_id}'."

    try:
        async with stores.structured._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT data FROM entity_profiles WHERE id = $1", eid)
            if not row:
                return f"No entity found with id '{entity_id}'."

            data = row["data"]
            if isinstance(data, str):
                data = json.loads(data)

            sections = data.get("sections", {})
            section_list = sections.get(section, [])
            section_list.append({
                "key": key,
                "value": value,
                "confidence": confidence,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "source": "operator",
                "superseded": False,
            })
            sections[section] = section_list
            data["sections"] = sections

            await conn.execute(
                "UPDATE entity_profiles SET data = $1::jsonb, updated_at = now() WHERE id = $2",
                json.dumps(data, default=str), eid,
            )
        return f"Assertion added: {key} = {value} (section: {section})"
    except Exception as e:
        return f"Error adding assertion: {e}"


async def _handle_remove_entity_assertion(stores: StoreHolder, args: dict) -> str:
    entity_id = args.get("entity_id", "")
    section = args.get("section", "")
    key = args.get("key", "")
    value = args.get("value", "")
    if not entity_id or not section or not key or not value:
        return "Error: 'entity_id', 'section', 'key', and 'value' are all required."

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    try:
        from uuid import UUID as _UUID
        eid = _UUID(entity_id)
    except ValueError:
        return f"Error: invalid UUID '{entity_id}'."

    try:
        async with stores.structured._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT data FROM entity_profiles WHERE id = $1", eid)
            if not row:
                return f"No entity found with id '{entity_id}'."

            data = row["data"]
            if isinstance(data, str):
                data = json.loads(data)

            sections = data.get("sections", {})
            section_list = sections.get(section, [])
            sections[section] = [
                a for a in section_list
                if not (a.get("key") == key and a.get("value") == value)
            ]
            if not sections[section]:
                del sections[section]
            data["sections"] = sections

            await conn.execute(
                "UPDATE entity_profiles SET data = $1::jsonb, updated_at = now() WHERE id = $2",
                json.dumps(data, default=str), eid,
            )
        return f"Assertion removed: {key} = {value} (section: {section})"
    except Exception as e:
        return f"Error removing assertion: {e}"


async def _handle_update_event(stores: StoreHolder, args: dict) -> str:
    event_id = args.get("event_id", "")
    if not event_id:
        return "Error: 'event_id' is required."

    category = args.get("category")
    tags = args.get("tags")
    confidence = args.get("confidence")

    if not any([category, tags, confidence is not None]):
        return "Error: provide at least one of 'category', 'tags', or 'confidence'."

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    try:
        from uuid import UUID as _UUID
        eid = _UUID(event_id)
    except ValueError:
        return f"Error: invalid UUID '{event_id}'."

    try:
        async with stores.structured._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT data FROM signals WHERE id = $1", eid)
            if not row:
                return f"No event found with id '{event_id}'."

            data = row["data"]
            if isinstance(data, str):
                data = json.loads(data)

            if category:
                data["category"] = category
            if confidence is not None:
                data["confidence"] = float(confidence)
            if tags:
                data["tags"] = [t.strip() for t in tags.split(",") if t.strip()]

            await conn.execute(
                "UPDATE signals SET data = $1::jsonb, updated_at = now() WHERE id = $2",
                json.dumps(data, default=str), eid,
            )
        return f"Event '{event_id}' updated."
    except Exception as e:
        return f"Error updating event: {e}"


async def _handle_delete_event(stores: StoreHolder, args: dict) -> str:
    event_id = args.get("event_id", "")
    if not event_id:
        return "Error: 'event_id' is required."

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    try:
        from uuid import UUID as _UUID
        eid = _UUID(event_id)
    except ValueError:
        return f"Error: invalid UUID '{event_id}'."

    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute("DELETE FROM signal_entity_links WHERE signal_id = $1", eid)
            await conn.execute("DELETE FROM signals WHERE id = $1", eid)

        if stores.opensearch and stores.opensearch.available:
            try:
                await stores.opensearch.delete_document("legba-events-*", str(eid))
            except Exception as e:
                log.warning("OpenSearch delete for event %s failed (non-fatal): %s", eid, e)

        return f"Event '{event_id}' deleted."
    except Exception as e:
        return f"Error deleting event: {e}"


async def _handle_create_source(stores: StoreHolder, args: dict) -> str:
    name = args.get("name", "").strip()
    url = args.get("url", "").strip()
    if not name or not url:
        return "Error: 'name' and 'url' are required."

    source_type = args.get("source_type", "rss")
    reliability = float(args.get("reliability", 0.5))
    description = args.get("description", "")

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    try:
        source_id = uuid4()
        now = datetime.now(timezone.utc)
        data = {
            "id": str(source_id),
            "name": name,
            "url": url,
            "source_type": source_type,
            "status": "active",
            "reliability": reliability,
            "bias_label": "center",
            "tags": [],
            "description": description,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "event_count": 0,
            "fetch_count": 0,
            "fail_count": 0,
        }
        async with stores.structured._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO sources (id, data, status, source_type, created_at, updated_at) "
                "VALUES ($1, $2::jsonb, $3, $4, $5, $5)",
                source_id, json.dumps(data, default=str), "active", source_type, now,
            )
        return f"Source '{name}' created (id: {source_id})."
    except Exception as e:
        return f"Error creating source: {e}"


async def _handle_update_source(stores: StoreHolder, args: dict) -> str:
    source_id = args.get("source_id", "")
    if not source_id:
        return "Error: 'source_id' is required."

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    try:
        from uuid import UUID as _UUID
        sid = _UUID(source_id)
    except ValueError:
        return f"Error: invalid UUID '{source_id}'."

    name = args.get("name")
    url = args.get("url")
    source_type = args.get("source_type")
    reliability = args.get("reliability")
    description = args.get("description")

    if not any([name, url, source_type, reliability is not None, description is not None]):
        return "Error: provide at least one field to update."

    try:
        async with stores.structured._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT data FROM sources WHERE id = $1", sid)
            if not row:
                return f"No source found with id '{source_id}'."

            data = row["data"]
            if isinstance(data, str):
                data = json.loads(data)

            col_updates = ["updated_at = now()"]
            params: list[Any] = []
            idx = 1

            if name:
                data["name"] = name
            if url:
                data["url"] = url
            if source_type:
                data["source_type"] = source_type
                col_updates.append(f"source_type = ${idx}")
                params.append(source_type)
                idx += 1
            if reliability is not None:
                data["reliability"] = float(reliability)
            if description is not None:
                data["description"] = description

            col_updates.append(f"data = ${idx}::jsonb")
            params.append(json.dumps(data, default=str))
            idx += 1

            params.append(sid)
            sql = f"UPDATE sources SET {', '.join(col_updates)} WHERE id = ${idx}"
            await conn.execute(sql, *params)

        return f"Source '{source_id}' updated."
    except Exception as e:
        return f"Error updating source: {e}"


async def _handle_delete_source(stores: StoreHolder, args: dict) -> str:
    source_id = args.get("source_id", "")
    if not source_id:
        return "Error: 'source_id' is required."

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    try:
        from uuid import UUID as _UUID
        sid = _UUID(source_id)
    except ValueError:
        return f"Error: invalid UUID '{source_id}'."

    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute("DELETE FROM sources WHERE id = $1", sid)
        return f"Source '{source_id}' deleted."
    except Exception as e:
        return f"Error deleting source: {e}"


async def _handle_create_goal(stores: StoreHolder, args: dict) -> str:
    description = args.get("description", "").strip()
    if not description:
        return "Error: 'description' is required."

    priority = int(args.get("priority", 3))
    parent_id = args.get("parent_id")

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    try:
        goal_id = uuid4()
        now = datetime.now(timezone.utc)
        data = {
            "id": str(goal_id),
            "name": description[:120],
            "description": description,
            "goal_type": "operational",
            "status": "active",
            "priority": priority,
            "progress_pct": 0,
            "parent_id": parent_id,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        async with stores.structured._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO goals (id, data, status, priority, goal_type, created_at, updated_at) "
                "VALUES ($1, $2::jsonb, 'active', $3, 'operational', $4, $4)",
                goal_id, json.dumps(data, default=str), priority, now,
            )
        return f"Goal created: '{description[:80]}' (id: {goal_id})."
    except Exception as e:
        return f"Error creating goal: {e}"


async def _handle_delete_goal(stores: StoreHolder, args: dict) -> str:
    goal_id = args.get("goal_id", "")
    if not goal_id:
        return "Error: 'goal_id' is required."

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    try:
        from uuid import UUID as _UUID
        gid = _UUID(goal_id)
    except ValueError:
        return f"Error: invalid UUID '{goal_id}'."

    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute("DELETE FROM goals WHERE id = $1", gid)
        return f"Goal '{goal_id}' deleted."
    except Exception as e:
        return f"Error deleting goal: {e}"


async def _handle_create_situation(stores: StoreHolder, args: dict) -> str:
    name = args.get("name", "").strip()
    if not name:
        return "Error: 'name' is required."

    description = args.get("description", "")
    category = args.get("category", "")
    key_entities_raw = args.get("key_entities", "")
    regions_raw = args.get("regions", "")

    ents = [e.strip() for e in key_entities_raw.split(",") if e.strip()] if key_entities_raw else []
    regs = [r.strip() for r in regions_raw.split(",") if r.strip()] if regions_raw else []

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    try:
        sit_id = uuid4()
        now = datetime.now(timezone.utc)
        data = {
            "id": str(sit_id),
            "name": name,
            "description": description,
            "status": "active",
            "category": category,
            "key_entities": ents,
            "regions": regs,
            "tags": [],
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "event_count": 0,
            "intensity_score": 0.0,
        }
        async with stores.structured._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO situations (id, data, name, status, category, created_at, updated_at) "
                "VALUES ($1, $2::jsonb, $3, 'active', $4, $5, $5)",
                sit_id, json.dumps(data, default=str), name, category, now,
            )
        return f"Situation '{name}' created (id: {sit_id})."
    except Exception as e:
        return f"Error creating situation: {e}"


async def _handle_delete_situation(stores: StoreHolder, args: dict) -> str:
    situation_id = args.get("situation_id", "")
    if not situation_id:
        return "Error: 'situation_id' is required."

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    try:
        from uuid import UUID as _UUID
        sid = _UUID(situation_id)
    except ValueError:
        return f"Error: invalid UUID '{situation_id}'."

    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute("DELETE FROM situation_signals WHERE situation_id = $1", sid)
            await conn.execute("DELETE FROM situations WHERE id = $1", sid)
        return f"Situation '{situation_id}' deleted."
    except Exception as e:
        return f"Error deleting situation: {e}"


async def _handle_link_event_to_situation(stores: StoreHolder, args: dict) -> str:
    situation_id = args.get("situation_id", "")
    event_id = args.get("event_id", "")
    if not situation_id or not event_id:
        return "Error: 'situation_id' and 'event_id' are required."

    relevance = float(args.get("relevance", 0.8))

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    try:
        from uuid import UUID as _UUID
        sid = _UUID(situation_id)
        eid = _UUID(event_id)
    except ValueError:
        return "Error: invalid UUID."

    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO situation_signals (situation_id, signal_id, relevance) "
                "VALUES ($1, $2, $3) ON CONFLICT (situation_id, signal_id) DO UPDATE SET relevance = $3, added_at = NOW()",
                sid, eid, relevance,
            )
            # Count actual rows instead of blind increment
            now = datetime.now(timezone.utc)
            actual_count = await conn.fetchval(
                "SELECT count(*) FROM situation_signals WHERE situation_id = $1",
                sid,
            )
            await conn.execute(
                "UPDATE situations SET "
                "event_count = $2, "
                "last_event_at = $3, updated_at = $3, "
                "data = jsonb_set(jsonb_set(data, '{event_count}', $4::jsonb), "
                "'{last_event_at}', $5::jsonb) "
                "WHERE id = $1",
                sid, actual_count, now,
                json.dumps(actual_count),
                json.dumps(now.isoformat()),
            )
        return f"Event '{event_id}' linked to situation '{situation_id}' (relevance: {relevance})."
    except Exception as e:
        return f"Error linking event to situation: {e}"


async def _handle_create_watch(stores: StoreHolder, args: dict) -> str:
    name = args.get("name", "").strip()
    if not name:
        return "Error: 'name' is required."

    entities_raw = args.get("entities", "")
    keywords_raw = args.get("keywords", "")
    categories_raw = args.get("categories", "")
    priority = args.get("priority", "normal")

    entity_list = [e.strip() for e in entities_raw.split(",") if e.strip()] if entities_raw else []
    keyword_list = [k.strip() for k in keywords_raw.split(",") if k.strip()] if keywords_raw else []
    category_list = [c.strip() for c in categories_raw.split(",") if c.strip()] if categories_raw else []

    if not entity_list and not keyword_list and not category_list:
        return "Error: at least one criterion (entities, keywords, or categories) is required."

    if priority not in ("normal", "high", "critical"):
        priority = "normal"

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    try:
        watch_id = uuid4()
        now = datetime.now(timezone.utc)
        data = {
            "id": str(watch_id),
            "name": name,
            "description": "",
            "entities": entity_list,
            "keywords": keyword_list,
            "categories": category_list,
            "regions": [],
            "priority": priority,
            "active": True,
            "created_at": now.isoformat(),
            "last_triggered_at": None,
            "trigger_count": 0,
        }
        async with stores.structured._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO watchlist (id, data, name, priority, active, created_at) "
                "VALUES ($1, $2::jsonb, $3, $4, true, $5)",
                watch_id, json.dumps(data, default=str), name, priority, now,
            )
        return f"Watch '{name}' created (id: {watch_id})."
    except Exception as e:
        return f"Error creating watch: {e}"


async def _handle_delete_watch(stores: StoreHolder, args: dict) -> str:
    watch_id = args.get("watch_id", "")
    if not watch_id:
        return "Error: 'watch_id' is required."

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    try:
        from uuid import UUID as _UUID
        wid = _UUID(watch_id)
    except ValueError:
        return f"Error: invalid UUID '{watch_id}'."

    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute("DELETE FROM watch_triggers WHERE watch_id = $1", wid)
            await conn.execute("DELETE FROM watchlist WHERE id = $1", wid)
        return f"Watch '{watch_id}' deleted."
    except Exception as e:
        return f"Error deleting watch: {e}"


async def _handle_update_fact(stores: StoreHolder, args: dict) -> str:
    fact_id = args.get("fact_id", "")
    subject = args.get("subject", "")
    predicate = args.get("predicate", "")
    value = args.get("value", "")
    confidence = args.get("confidence")

    if not fact_id or not subject or not predicate or not value or confidence is None:
        return "Error: 'fact_id', 'subject', 'predicate', 'value', and 'confidence' are all required."

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    try:
        from uuid import UUID as _UUID
        fid = _UUID(fact_id)
    except ValueError:
        return f"Error: invalid UUID '{fact_id}'."

    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute(
                "UPDATE facts SET subject = $1, predicate = $2, value = $3, "
                "confidence = $4 WHERE id = $5",
                subject, predicate, value, float(confidence), fid,
            )
        return f"Fact '{fact_id}' updated."
    except Exception as e:
        return f"Error updating fact: {e}"


async def _handle_delete_fact(stores: StoreHolder, args: dict) -> str:
    fact_id = args.get("fact_id", "")
    if not fact_id:
        return "Error: 'fact_id' is required."

    if not stores.structured._available:
        return "Error: Postgres unavailable."

    try:
        from uuid import UUID as _UUID
        fid = _UUID(fact_id)
    except ValueError:
        return f"Error: invalid UUID '{fact_id}'."

    try:
        async with stores.structured._pool.acquire() as conn:
            await conn.execute("DELETE FROM facts WHERE id = $1", fid)
        return f"Fact '{fact_id}' deleted."
    except Exception as e:
        return f"Error deleting fact: {e}"


async def _handle_add_graph_edge(stores: StoreHolder, args: dict) -> str:
    from_entity = args.get("from_entity", "")
    to_entity = args.get("to_entity", "")
    relation_type = args.get("relation_type", "")
    if not from_entity or not to_entity or not relation_type:
        return "Error: 'from_entity', 'to_entity', and 'relation_type' are required."

    if not stores.graph.available:
        return "Error: Graph store unavailable."

    try:
        props = {"source": "operator"}
        since = args.get("since", "")
        if since and since.strip():
            props["since"] = since.strip()

        await stores.graph.add_relationship(from_entity, to_entity, relation_type, props)
        return f"Edge added: {from_entity} --[{relation_type}]--> {to_entity}"
    except Exception as e:
        return f"Error adding graph edge: {e}"


async def _handle_remove_graph_edge(stores: StoreHolder, args: dict) -> str:
    from_entity = args.get("from_entity", "")
    to_entity = args.get("to_entity", "")
    relation_type = args.get("relation_type", "")
    if not from_entity or not to_entity or not relation_type:
        return "Error: 'from_entity', 'to_entity', and 'relation_type' are required."

    if not stores.graph.available:
        return "Error: Graph store unavailable."

    try:
        cypher = (
            f"MATCH (a {{name: '{from_entity}'}})-[r:{relation_type}]->(b {{name: '{to_entity}'}}) "
            f"DELETE r"
        )
        await stores.graph.execute_cypher(cypher)
        return f"Edge removed: {from_entity} --[{relation_type}]--> {to_entity}"
    except Exception as e:
        return f"Error removing graph edge: {e}"


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
    # --- Write tools ---
    "add_entity_assertion": _handle_add_entity_assertion,
    "remove_entity_assertion": _handle_remove_entity_assertion,
    "update_event": _handle_update_event,
    "delete_event": _handle_delete_event,
    "create_source": _handle_create_source,
    "update_source": _handle_update_source,
    "delete_source": _handle_delete_source,
    "create_goal": _handle_create_goal,
    "delete_goal": _handle_delete_goal,
    "create_situation": _handle_create_situation,
    "delete_situation": _handle_delete_situation,
    "link_event_to_situation": _handle_link_event_to_situation,
    "create_watch": _handle_create_watch,
    "delete_watch": _handle_delete_watch,
    "update_fact": _handle_update_fact,
    "delete_fact": _handle_delete_fact,
    "add_graph_edge": _handle_add_graph_edge,
    "remove_graph_edge": _handle_remove_graph_edge,
}


# --- Hypothesis + Brief handlers ---
# (defined here, then added to dispatch dict below)

async def _handle_list_hypotheses(stores, args: dict) -> str:
    """List competing hypotheses from Postgres."""
    status = args.get("status", "active")
    situation_id = args.get("situation_id")
    limit = int(args.get("limit", 20))
    try:
        items = await stores.structured.list_hypotheses(
            status=status, situation_id=situation_id, limit=limit,
        )
        if not items:
            return f"No hypotheses found (status={status})"
        import json
        return json.dumps({"count": len(items), "hypotheses": items}, indent=2, default=str)
    except Exception as e:
        return f"Error listing hypotheses: {e}"


async def _handle_list_briefs(stores, args: dict) -> str:
    """List situation briefs from Redis."""
    limit = int(args.get("limit", 20))
    try:
        import json
        raw = await stores.registers._redis.lrange("legba:situation_briefs", 0, limit - 1)
        if not raw:
            return "No situation briefs yet. SYNTHESIZE cycles produce these."
        briefs = []
        for item in raw:
            try:
                data = json.loads(item)
                briefs.append({
                    "title": data.get("title", "?"),
                    "cycle": data.get("cycle", "?"),
                    "timestamp": data.get("timestamp", "?"),
                    "content_preview": data.get("content", "")[:500],
                })
            except Exception:
                continue
        return json.dumps({"count": len(briefs), "briefs": briefs}, indent=2, default=str)
    except Exception as e:
        return f"Error listing briefs: {e}"


# Add hypothesis + brief handlers to dispatch dict
_TOOL_HANDLERS["list_hypotheses"] = _handle_list_hypotheses
_TOOL_HANDLERS["list_briefs"] = _handle_list_briefs


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

        # Build prompts (system instructions + user context)
        system_content, user_context = await _build_prompts(self.stores)

        # Append user message
        messages.append({"role": "user", "content": user_message})

        # Tool-calling loop
        for step in range(MAX_TOOL_STEPS):
            # Retry on stochastic multi-message 400s (inherent to GPT-OSS
            # reasoning mode — same retry pattern as agent client.py:317)
            response = None
            for attempt in range(3):
                try:
                    response = await self._call_llm(
                        provider, system_content, user_context, messages,
                    )
                    break
                except LLMApiError as e:
                    if e.status_code == 400 and attempt < 2:
                        log.warning("Consult 400 attempt %d: %s", attempt + 1, e.body[:120])
                        import asyncio
                        await asyncio.sleep(1)
                        continue
                    raise
            content = response.content.strip()

            log.info(
                "Consult step %d/%d: tokens=%s, has_tool=%s",
                step + 1, MAX_TOOL_STEPS,
                response.usage, has_tool_call(content),
            )

            # Empty content — model dumped everything into reasoning channel.
            # Re-prompt once before giving up.
            if not content:
                log.warning("Consult step %d: empty content (%s tokens), re-prompting",
                            step + 1, response.usage.get("completion_tokens", "?"))
                messages.append({
                    "role": "user",
                    "content": 'Your previous response was empty. Respond now: {"actions": [...]}',
                })
                continue

            if not has_tool_call(content):
                # Final response — no tool call (plain text)
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

            # Intercept respond tool — this is the model's final answer
            for tc in tool_calls:
                if tc.tool_name == "respond":
                    final_text = tc.arguments.get("message", "")
                    messages.append({"role": "assistant", "content": final_text})
                    await self.save_session(session_id, messages)
                    return final_text, messages

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
        response = await self._call_llm(provider, system_content, user_context, messages)
        content = response.content.strip()
        messages.append({"role": "assistant", "content": content})
        await self.save_session(session_id, messages)
        return content, messages

    async def _call_llm(
        self,
        provider: VLLMProvider | AnthropicProvider,
        system_prompt: str,
        user_context: str,
        messages: list[dict[str, str]],
    ) -> LLMResponse:
        """
        Call the LLM provider with the system prompt + conversation.

        For vLLM: single {"role": "user"} message (system+conversation joined).
                  No max_tokens — let the model/server handle the budget.
        For Anthropic: proper system field + multi-turn messages.
        """
        if isinstance(provider, AnthropicProvider):
            # Strip GPT-OSS reasoning directive from system prompt
            import re
            clean_system = re.sub(r'^reasoning:\s*(high|medium|low)\s*\n*', '', system_prompt).lstrip()
            # Prepend context as the first user message
            conv = [{"role": "user", "content": user_context}] + list(messages)
            return await provider.chat_complete(
                messages=conv,
                system=clean_system,
            )
        else:
            # vLLM / GPT-OSS: combine into single user message.
            # Same as format.py:to_chat_messages() — the agent's proven pattern.
            # system_prompt already contains "reasoning: high" at the top.
            parts = [system_prompt, user_context]
            for m in messages:
                parts.append(m["content"])
            parts.append('Output one JSON object: {"actions": [...]}')
            combined = "\n\n".join(parts)

            payload_msgs = [{"role": "user", "content": combined}]
            log.info("Consult LLM request: chars=%d, messages=%d", len(combined), len(payload_msgs))

            response = await provider.chat_complete(
                messages=payload_msgs,
            )

            log.info(
                "Consult LLM response: finish=%s, usage=%s, content_len=%d",
                response.finish_reason, response.usage, len(response.content),
            )

            return response
