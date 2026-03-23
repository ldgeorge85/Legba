"""
Event graph operations for Apache AGE.

Provides Cypher query builders and mutation helpers for managing events as
first-class vertices in the knowledge graph alongside entities.  Events are
connected to entities via INVOLVED_IN edges, to other events via causal /
hierarchical / temporal edges, and to situations via TRACKED_BY edges.

All functions are standalone — they accept an asyncpg pool and graph name,
reusing the same AGE Cypher patterns established in graph.py (GraphStore).

Usage example (inside an async context with access to the graph store)::

    from legba.shared.graph_events import (
        upsert_event_vertex,
        link_entity_to_event,
        event_actors_query,
    )

    pool = graph_store._pool
    graph = graph_store.GRAPH_NAME

    # Create an event vertex
    await upsert_event_vertex(pool, graph, 42, "Coup in Niger", "conflict", "developing")

    # Link an entity to the event
    await link_entity_to_event(pool, graph, "Wagner Group", "Coup in Niger", "participant", 0.85)

    # Query actors involved in an event
    results = await event_actors_query(pool, graph, "Coup in Niger")
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import asyncpg


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EVENT_EDGE_TYPES: dict[str, str] = {
    "INVOLVED_IN": "Entity participation in an event",
    "PART_OF": "Sub-event hierarchy",
    "CAUSED_BY": "Causal relationship",
    "EVOLVES_FROM": "Temporal sequence",
    "CORRELATED_WITH": "Co-occurrence without clear causation",
    "CONTRADICTS": "Conflicting reports",
    "TRACKED_BY": "Situation tracking",
}

# Edge type → (source vertex type, target vertex type, optional property keys)
EVENT_EDGE_SCHEMA: dict[str, dict[str, Any]] = {
    "INVOLVED_IN": {
        "source": "Entity",
        "target": "Event",
        "properties": ["role", "confidence"],
    },
    "PART_OF": {
        "source": "Event",
        "target": "Event",
        "properties": [],
    },
    "CAUSED_BY": {
        "source": "Event",
        "target": "Event",
        "properties": ["confidence", "evidence_source"],
    },
    "EVOLVES_FROM": {
        "source": "Event",
        "target": "Event",
        "properties": [],
    },
    "CORRELATED_WITH": {
        "source": "Event",
        "target": "Event",
        "properties": ["confidence"],
    },
    "CONTRADICTS": {
        "source": "Event",
        "target": "Event",
        "properties": [],
    },
    "TRACKED_BY": {
        "source": "Event",
        "target": "Situation",
        "properties": ["relevance"],
    },
}


# ---------------------------------------------------------------------------
# AGE helpers (mirroring GraphStore._escape / _cypher / _prepare patterns)
# ---------------------------------------------------------------------------

def _escape(val: str) -> str:
    """Escape a string for embedding in a Cypher single-quoted literal."""
    return val.replace("\\", "\\\\").replace("'", "\\'")


def _to_cypher_value(val: Any) -> str:
    """Convert a Python value to a Cypher literal."""
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    return f"'{_escape(str(val))}'"


def _dict_to_cypher_map(d: dict[str, Any]) -> str:
    """Convert a Python dict to a Cypher map literal: {k1: v1, k2: v2}."""
    items = [f"{k}: {_to_cypher_value(v)}" for k, v in d.items()]
    return "{" + ", ".join(items) + "}"


def _parse_agtype(val: Any) -> Any:
    """Parse an agtype text value into a Python object.

    Strips AGE type suffixes (::vertex, ::edge, ::path, etc.) and
    deserialises the underlying JSON.
    """
    if val is None:
        return None
    s = str(val).strip()
    for suffix in (
        "::path", "::vertex", "::edge",
        "::numeric", "::integer", "::float", "::boolean",
    ):
        s = s.replace(suffix, "")
    s = s.strip()
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return s


async def _prepare(conn: asyncpg.Connection) -> None:
    """Prepare a connection for AGE queries (search_path + extension)."""
    await conn.execute("LOAD 'age'")
    await conn.execute('SET search_path = ag_catalog, "$user", public')


async def _cypher(
    conn: asyncpg.Connection,
    graph_name: str,
    query: str,
    cols: str = "v agtype",
) -> list[dict[str, Any]]:
    """Execute a Cypher query via AGE and parse the results.

    Args:
        conn: An asyncpg connection (already acquired from a pool).
        graph_name: The AGE graph name (e.g. ``'legba_graph'``).
        query: The Cypher query string.
        cols: Column spec for the AS clause (e.g. ``'name agtype, cnt agtype'``).

    Returns:
        List of dicts keyed by column name with parsed agtype values.
    """
    await _prepare(conn)
    sql = f"SELECT * FROM cypher('{graph_name}', $$ {query} $$) AS ({cols})"
    rows = await conn.fetch(sql)
    col_names = [c.strip().split()[0] for c in cols.split(",")]
    return [
        {name: _parse_agtype(row[name]) for name in col_names}
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Named query operations
# ---------------------------------------------------------------------------

async def event_actors_query(
    pool: asyncpg.Pool,
    graph_name: str,
    event_title: str,
) -> list[dict[str, Any]]:
    """Find entities involved in an event via INVOLVED_IN edges.

    Returns list of dicts with keys: actor, type, role, confidence.

    Example::

        actors = await event_actors_query(pool, "legba_graph", "Coup in Niger")
        for a in actors:
            print(f"{a['actor']} ({a['type']}) — role={a['role']}, conf={a['confidence']}")
    """
    title_esc = _escape(event_title)
    cypher = f"""
        MATCH (e:Event {{name: '{title_esc}'}})<-[r:INVOLVED_IN]-(entity)
        RETURN entity.name AS actor, label(entity) AS type,
               r.role AS role, r.confidence AS confidence
        ORDER BY r.confidence DESC
    """
    async with pool.acquire() as conn:
        return await _cypher(
            conn, graph_name, cypher,
            cols="actor agtype, type agtype, role agtype, confidence agtype",
        )


async def event_chain_query(
    pool: asyncpg.Pool,
    graph_name: str,
    event_title: str,
    max_depth: int = 3,
) -> list[dict[str, Any]]:
    """Follow the CAUSED_BY chain upstream from an event.

    Uses a variable-length path (same pattern as ``find_path`` in graph.py).
    Returns list of dicts with key ``chain`` — a list of event names from
    effect to root cause.

    Example::

        chains = await event_chain_query(pool, "legba_graph", "Oil Price Spike", max_depth=4)
        for c in chains:
            print(" → ".join(c["chain"]))
    """
    title_esc = _escape(event_title)
    depth = min(int(max_depth), 10)
    cypher = f"""
        MATCH path = (e:Event {{name: '{title_esc}'}})<-[:CAUSED_BY*1..{depth}]-(cause)
        RETURN [n IN nodes(path) | n.name] AS chain
    """
    async with pool.acquire() as conn:
        return await _cypher(conn, graph_name, cypher, cols="chain agtype")


async def event_children_query(
    pool: asyncpg.Pool,
    graph_name: str,
    event_title: str,
) -> list[dict[str, Any]]:
    """Find sub-events (children) of a parent event via PART_OF edges.

    Returns list of dicts with keys: child_event, event_id.

    Example::

        children = await event_children_query(pool, "legba_graph", "Ukraine Conflict")
        for c in children:
            print(f"  sub-event: {c['child_event']} (id={c['event_id']})")
    """
    title_esc = _escape(event_title)
    cypher = f"""
        MATCH (child:Event)-[:PART_OF]->(parent:Event {{name: '{title_esc}'}})
        RETURN child.name AS child_event, child.event_id AS event_id
    """
    async with pool.acquire() as conn:
        return await _cypher(
            conn, graph_name, cypher,
            cols="child_event agtype, event_id agtype",
        )


async def event_situation_query(
    pool: asyncpg.Pool,
    graph_name: str,
    situation_name: str,
) -> list[dict[str, Any]]:
    """Find events tracked by a situation via TRACKED_BY edges.

    Returns list of dicts with keys: event, event_id, relevance — ordered
    by relevance descending.

    Example::

        events = await event_situation_query(pool, "legba_graph", "Middle East Energy Crisis")
        for e in events:
            print(f"  {e['event']} (relevance={e['relevance']})")
    """
    name_esc = _escape(situation_name)
    cypher = f"""
        MATCH (e:Event)-[r:TRACKED_BY]->(s:Situation {{name: '{name_esc}'}})
        RETURN e.name AS event, e.event_id AS event_id, r.relevance AS relevance
        ORDER BY r.relevance DESC
    """
    async with pool.acquire() as conn:
        return await _cypher(
            conn, graph_name, cypher,
            cols="event agtype, event_id agtype, relevance agtype",
        )


async def entity_events_query(
    pool: asyncpg.Pool,
    graph_name: str,
    entity_name: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Find events that an entity is involved in.

    Returns list of dicts with keys: event, event_id, role — ordered by
    the event's created_at descending.

    Example::

        events = await entity_events_query(pool, "legba_graph", "Wagner Group", limit=10)
        for e in events:
            print(f"  {e['event']} — role: {e['role']}")
    """
    name_esc = _escape(entity_name)
    lim = int(limit)
    cypher = f"""
        MATCH (entity {{name: '{name_esc}'}})-[r:INVOLVED_IN]->(e:Event)
        RETURN e.name AS event, e.event_id AS event_id, r.role AS role
        ORDER BY e.created_at DESC
        LIMIT {lim}
    """
    async with pool.acquire() as conn:
        return await _cypher(
            conn, graph_name, cypher,
            cols="event agtype, event_id agtype, role agtype",
        )


async def cross_situation_query(
    pool: asyncpg.Pool,
    graph_name: str,
    situation_a: str,
    situation_b: str,
) -> list[dict[str, Any]]:
    """Find events that bridge two situations (tracked by both).

    Returns list of dicts with keys: event, event_id.

    Example::

        shared = await cross_situation_query(
            pool, "legba_graph",
            "Ukraine Conflict", "European Energy Crisis",
        )
        for e in shared:
            print(f"  bridging event: {e['event']}")
    """
    a_esc = _escape(situation_a)
    b_esc = _escape(situation_b)
    cypher = f"""
        MATCH (e:Event)-[:TRACKED_BY]->(s1:Situation {{name: '{a_esc}'}}),
              (e)-[:TRACKED_BY]->(s2:Situation {{name: '{b_esc}'}})
        RETURN e.name AS event, e.event_id AS event_id
    """
    async with pool.acquire() as conn:
        return await _cypher(
            conn, graph_name, cypher,
            cols="event agtype, event_id agtype",
        )


# ---------------------------------------------------------------------------
# Mutation helpers (upsert vertices, create edges)
# ---------------------------------------------------------------------------

async def upsert_event_vertex(
    pool: asyncpg.Pool,
    graph_name: str,
    event_id: int,
    event_title: str,
    category: str | None = None,
    lifecycle_status: str | None = None,
) -> bool:
    """Create or update an Event vertex in the graph.

    Follows the same MATCH-first / CREATE-if-absent pattern used by
    ``GraphStore.upsert_entity`` to avoid duplicate vertices when the
    event is referenced multiple times.

    Args:
        pool: asyncpg connection pool.
        graph_name: AGE graph name.
        event_id: Numeric event ID from the events table.
        event_title: Human-readable event title (becomes the vertex ``name``).
        category: Optional event category (e.g. ``'conflict'``, ``'economic'``).
        lifecycle_status: Optional status (e.g. ``'developing'``, ``'resolved'``).

    Returns:
        True on success, False on error.

    Example::

        ok = await upsert_event_vertex(
            pool, "legba_graph", 42, "Coup in Niger", "conflict", "developing",
        )
    """
    try:
        title_esc = _escape(event_title)
        now = datetime.now(timezone.utc).isoformat()

        props: dict[str, Any] = {"event_id": event_id}
        if category:
            props["category"] = category
        if lifecycle_status:
            props["lifecycle_status"] = lifecycle_status
        props_map = _dict_to_cypher_map(props)

        async with pool.acquire() as conn:
            # Check for existing vertex with this name (any label)
            existing = await _cypher(conn, graph_name, f"""
                MATCH (n {{name: '{title_esc}'}})
                RETURN n
                LIMIT 1
            """, cols="n agtype")

            if existing:
                # Update existing vertex
                await _cypher(conn, graph_name, f"""
                    MATCH (n {{name: '{title_esc}'}})
                    SET n += {props_map}
                    SET n.updated_at = '{now}'
                    RETURN n
                """, cols="n agtype")
            else:
                # Create new Event vertex
                await _cypher(conn, graph_name, f"""
                    CREATE (n:Event {{name: '{title_esc}'}})
                    SET n += {props_map}
                    SET n.created_at = '{now}'
                    SET n.updated_at = '{now}'
                    RETURN n
                """, cols="n agtype")
        return True
    except Exception:
        return False


async def link_entity_to_event(
    pool: asyncpg.Pool,
    graph_name: str,
    entity_name: str,
    event_title: str,
    role: str | None = None,
    confidence: float | None = None,
) -> bool:
    """Create an INVOLVED_IN edge from an entity to an event.

    Uses MERGE to avoid duplicate edges.

    Args:
        pool: asyncpg connection pool.
        graph_name: AGE graph name.
        entity_name: Name of the entity vertex.
        event_title: Name of the event vertex.
        role: The entity's role in the event (e.g. ``'perpetrator'``, ``'target'``).
        confidence: Confidence score 0.0–1.0.

    Returns:
        True on success, False on error.

    Example::

        await link_entity_to_event(
            pool, "legba_graph", "Wagner Group", "Coup in Niger",
            role="participant", confidence=0.85,
        )
    """
    try:
        ent_esc = _escape(entity_name)
        evt_esc = _escape(event_title)
        props: dict[str, Any] = {}
        if role:
            props["role"] = role
        if confidence is not None:
            props["confidence"] = confidence
        props_map = _dict_to_cypher_map(props) if props else "{}"

        async with pool.acquire() as conn:
            await _cypher(conn, graph_name, f"""
                MATCH (entity {{name: '{ent_esc}'}}), (event:Event {{name: '{evt_esc}'}})
                MERGE (entity)-[r:INVOLVED_IN]->(event)
                SET r += {props_map}
                RETURN r
            """, cols="r agtype")
        return True
    except Exception:
        return False


async def link_event_hierarchy(
    pool: asyncpg.Pool,
    graph_name: str,
    child_title: str,
    parent_title: str,
) -> bool:
    """Create a PART_OF edge from a child event to a parent event.

    Uses MERGE to avoid duplicate edges.

    Args:
        pool: asyncpg connection pool.
        graph_name: AGE graph name.
        child_title: Name of the child (sub-event) vertex.
        parent_title: Name of the parent event vertex.

    Returns:
        True on success, False on error.

    Example::

        await link_event_hierarchy(
            pool, "legba_graph", "Battle of Bakhmut", "Ukraine Conflict",
        )
    """
    try:
        child_esc = _escape(child_title)
        parent_esc = _escape(parent_title)

        async with pool.acquire() as conn:
            await _cypher(conn, graph_name, f"""
                MATCH (child:Event {{name: '{child_esc}'}}),
                      (parent:Event {{name: '{parent_esc}'}})
                MERGE (child)-[r:PART_OF]->(parent)
                RETURN r
            """, cols="r agtype")
        return True
    except Exception:
        return False


async def link_event_causal(
    pool: asyncpg.Pool,
    graph_name: str,
    effect_title: str,
    cause_title: str,
    confidence: float | None = None,
    evidence_source: str | None = None,
) -> bool:
    """Create a CAUSED_BY edge from an effect event to its cause event.

    Direction: effect ←[CAUSED_BY]— cause  (the effect points back to its cause).
    Uses MERGE to avoid duplicate edges.

    Args:
        pool: asyncpg connection pool.
        graph_name: AGE graph name.
        effect_title: Name of the effect event vertex.
        cause_title: Name of the cause event vertex.
        confidence: Confidence in the causal link (0.0–1.0).
        evidence_source: Description of the evidence supporting the link.

    Returns:
        True on success, False on error.

    Example::

        await link_event_causal(
            pool, "legba_graph",
            "Oil Price Spike", "OPEC Production Cut",
            confidence=0.9, evidence_source="Reuters analysis",
        )
    """
    try:
        effect_esc = _escape(effect_title)
        cause_esc = _escape(cause_title)
        props: dict[str, Any] = {}
        if confidence is not None:
            props["confidence"] = confidence
        if evidence_source:
            props["evidence_source"] = evidence_source
        props_map = _dict_to_cypher_map(props) if props else "{}"

        async with pool.acquire() as conn:
            await _cypher(conn, graph_name, f"""
                MATCH (effect:Event {{name: '{effect_esc}'}}),
                      (cause:Event {{name: '{cause_esc}'}})
                MERGE (effect)<-[r:CAUSED_BY]-(cause)
                SET r += {props_map}
                RETURN r
            """, cols="r agtype")
        return True
    except Exception:
        return False


async def link_event_situation(
    pool: asyncpg.Pool,
    graph_name: str,
    event_title: str,
    situation_name: str,
    relevance: float | None = None,
) -> bool:
    """Create a TRACKED_BY edge from an event to a situation.

    Uses MERGE to avoid duplicate edges.

    Args:
        pool: asyncpg connection pool.
        graph_name: AGE graph name.
        event_title: Name of the event vertex.
        situation_name: Name of the situation vertex.
        relevance: Relevance score 0.0–1.0.

    Returns:
        True on success, False on error.

    Example::

        await link_event_situation(
            pool, "legba_graph",
            "Coup in Niger", "Sahel Instability Brief",
            relevance=0.95,
        )
    """
    try:
        evt_esc = _escape(event_title)
        sit_esc = _escape(situation_name)
        props: dict[str, Any] = {}
        if relevance is not None:
            props["relevance"] = relevance
        props_map = _dict_to_cypher_map(props) if props else "{}"

        async with pool.acquire() as conn:
            await _cypher(conn, graph_name, f"""
                MATCH (event:Event {{name: '{evt_esc}'}}),
                      (sit:Situation {{name: '{sit_esc}'}})
                MERGE (event)-[r:TRACKED_BY]->(sit)
                SET r += {props_map}
                RETURN r
            """, cols="r agtype")
        return True
    except Exception:
        return False
