"""
Entity graph tools: graph_store, graph_query

Let the agent explicitly manage entities and their relationships in the
knowledge graph (Apache AGE / Cypher). Used for tracking connections between
people, organizations, events, locations, concepts, etc.

graph_query supports named operations (top_connected, neighbors, path, etc.)
instead of raw Cypher — all queries are pre-built for AGE compatibility.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ....shared.schemas.tools import ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from ...memory.graph import GraphStore
    from ..registry import ToolRegistry


# Canonical relationship types and their common synonyms.
# The agent's prompt lists canonical types, but by tool-step 10+ the guidance
# is out of the sliding window. Normalize here at the storage layer.
# Expanded in L.1x to cover all observed and predicted synonyms.
RELATIONSHIP_ALIASES: dict[str, str] = {
    # CreatedBy
    "AuthoredBy": "CreatedBy", "BuiltBy": "CreatedBy", "DevelopedBy": "CreatedBy",
    "MadeBy": "CreatedBy", "WrittenBy": "CreatedBy", "DesignedBy": "CreatedBy",
    "FoundedBy": "CreatedBy", "InventedBy": "CreatedBy", "ProducedBy": "CreatedBy",
    # MaintainedBy
    "HostedBy": "MaintainedBy", "RunBy": "MaintainedBy", "ManagedBy": "MaintainedBy",
    "OperatedBy": "MaintainedBy", "SupportedBy": "MaintainedBy",
    # FundedBy
    "HasFunding": "FundedBy", "SponsoredBy": "FundedBy", "BackedBy": "FundedBy",
    "FinancedBy": "FundedBy", "InvestedBy": "FundedBy",
    # UsesArchitecture
    "HasArchitecture": "UsesArchitecture", "BuiltWith": "UsesArchitecture",
    "ImplementedWith": "UsesArchitecture", "UsesFramework": "UsesArchitecture",
    # UsesPersistence
    "HasPersistence": "UsesPersistence", "Persists": "UsesPersistence",
    "StoresIn": "UsesPersistence", "HasStorage": "UsesPersistence",
    "UsesDatabase": "UsesPersistence", "UsesStorage": "UsesPersistence",
    # HasSafety
    "HasSafetyFeature": "HasSafety", "HasGuardrail": "HasSafety",
    "HasSandbox": "HasSafety", "SafeguardedBy": "HasSafety",
    # HasLimitation
    "LimitedBy": "HasLimitation", "HasWeakness": "HasLimitation",
    "HasDrawback": "HasLimitation",
    # HasFeature
    "Supports": "HasFeature", "Provides": "HasFeature", "Includes": "HasFeature",
    "Enables": "HasFeature", "HasCapability": "HasFeature", "Implements": "HasFeature",
    # AffiliatedWith
    "AssociatedWith": "AffiliatedWith", "HasOrganization": "AffiliatedWith",
    "ConnectedTo": "AffiliatedWith", "WorksWith": "AffiliatedWith",
    "PartneredWith": "AffiliatedWith",
    # PartOf
    "BelongsTo": "PartOf", "ComponentOf": "PartOf",
    # DependsOn
    "Requires": "DependsOn", "ReliesOn": "DependsOn", "Uses": "DependsOn",
    "FeedsInto": "DependsOn", "UsedBy": "DependsOn",
    # Extends
    "ForkedFrom": "Extends", "DerivedFrom": "Extends", "BuildsUpon": "Extends",
    "InheritsFrom": "Extends",
    # AlternativeTo
    "CompetesWith": "AlternativeTo", "SimilarTo": "AlternativeTo",
    # InspiredBy
    "InfluencedBy": "InspiredBy", "MotivatedBy": "InspiredBy",
    # Former RelatedTo catch-all — now redirected to AffiliatedWith to avoid vague edges
    "RELATED_TO": "AffiliatedWith", "Related": "AffiliatedWith", "LinksTo": "AffiliatedWith",
    "Has": "AffiliatedWith", "Knows": "AffiliatedWith", "ReportsTo": "LeaderOf",
    # --- SA relationship aliases ---
    # AlliedWith
    "AlliedTo": "AlliedWith", "AllyOf": "AlliedWith",
    # HostileTo
    "EnemyOf": "HostileTo", "OpposedTo": "HostileTo",
    "AtWarWith": "HostileTo", "RivalOf": "HostileTo",
    # SanctionedBy
    "SanctionsAgainst": "SanctionedBy", "EmbargoedBy": "SanctionedBy",
    # MemberOf
    "BelongsToOrg": "MemberOf",
    # LeaderOf
    "HeadOf": "LeaderOf", "PresidentOf": "LeaderOf",
    "CommanderOf": "LeaderOf", "ChairOf": "LeaderOf",
    # LocatedIn
    "BasedIn": "LocatedIn", "HeadquarteredIn": "LocatedIn",
    "SituatedIn": "LocatedIn",
    # OperatesIn
    "ActiveIn": "OperatesIn", "DeployedIn": "OperatesIn",
    # BordersWith
    "AdjacentTo": "BordersWith", "NeighborOf": "BordersWith",
    # OccupiedBy
    "ControlledBy": "OccupiedBy", "AnnexedBy": "OccupiedBy",
    # SignatoryTo
    "PartyTo": "SignatoryTo", "RatifiedBy": "SignatoryTo",
    # SuppliesWeaponsTo
    "ArmsSupplier": "SuppliesWeaponsTo",
    # TradesWith
    "TradingPartner": "TradesWith", "CommercialRelation": "TradesWith",
}

# The closed set of valid relationship types. Anything not here and not in
# RELATIONSHIP_ALIASES gets fuzzy-matched or falls back to RelatedTo.
CANONICAL_RELATIONSHIP_TYPES: frozenset[str] = frozenset({
    # SA: geopolitical and institutional relationship types
    "AlliedWith", "HostileTo", "TradesWith", "SanctionedBy",
    "SuppliesWeaponsTo", "MemberOf", "LeaderOf",
    "OperatesIn", "LocatedIn", "BordersWith", "OccupiedBy",
    "SignatoryTo", "ProducesResource", "ImportsFrom", "ExportsTo",
    # General relationship types
    "AffiliatedWith", "PartOf", "FundedBy",
    "CreatedBy", "MaintainedBy",
    # Technical (kept for backwards compatibility)
    "UsesArchitecture", "UsesPersistence", "HasSafety", "HasLimitation", "HasFeature",
    "Extends", "DependsOn", "AlternativeTo", "InspiredBy",
})


def normalize_relationship_type(rel_type: str) -> tuple[str, str | None]:
    """Return (canonical_type, feedback_note_or_None).

    Pipeline: alias lookup → canonical passthrough → fuzzy match → fallback.
    """
    # 1. Alias lookup (fast path — covers known synonyms)
    canonical = RELATIONSHIP_ALIASES.get(rel_type)
    if canonical:
        return canonical, f"Note: '{rel_type}' normalized to canonical '{canonical}'."

    # 2. Already canonical — pass through silently
    if rel_type in CANONICAL_RELATIONSHIP_TYPES:
        return rel_type, None

    # 3. Fuzzy match against canonical types
    from difflib import SequenceMatcher

    best: str | None = None
    best_ratio = 0.0
    rel_lower = rel_type.lower()
    for ctype in CANONICAL_RELATIONSHIP_TYPES:
        ratio = SequenceMatcher(None, rel_lower, ctype.lower()).ratio()
        if ratio > best_ratio:
            best_ratio, best = ratio, ctype

    if best and best_ratio >= 0.7:
        return best, (
            f"Note: '{rel_type}' fuzzy-matched to canonical '{best}' "
            f"(score={best_ratio:.2f})."
        )

    # 4. Unrecognized — reject instead of defaulting to RelatedTo
    return None, (
        f"Error: '{rel_type}' is not a recognized relationship type. "
        f"Use a specific type: AlliedWith, HostileTo, TradesWith, SanctionedBy, "
        f"SuppliesWeaponsTo, MemberOf, LeaderOf, OperatesIn, LocatedIn, "
        f"BordersWith, AffiliatedWith, PartOf, FundedBy, or others from the canonical set."
    )


async def _find_similar_entity(
    graph: "GraphStore",
    name: str,
    entity_type: str | None = None,
) -> str | None:
    """Check for a fuzzy-matching entity already in the graph.

    Returns the existing entity's canonical name if a close match is found
    (SequenceMatcher ratio ≥ 0.85 after normalization), else None.
    Skips exact case-insensitive matches (handled by find_entity).
    """
    from difflib import SequenceMatcher
    import re as _re

    def _norm(n: str) -> str:
        """Lowercase and strip separators for comparison."""
        return _re.sub(r'[-_/\\\s]+', '', n.lower())

    norm = _norm(name)
    if not norm:
        return None

    candidates = await graph.search_entities(
        query=None,
        entity_type=entity_type if entity_type and entity_type != "unknown" else None,
        limit=500,
    )
    for c in candidates:
        if c.name.lower() == name.lower():
            continue  # exact match handled by find_entity
        c_norm = _norm(c.name)
        if norm == c_norm:
            return c.name
        if SequenceMatcher(None, norm, c_norm).ratio() >= 0.85:
            return c.name
    return None


def register(
    registry: ToolRegistry,
    *,
    graph: GraphStore,
) -> None:
    """Register graph tools wired to the live AGE graph store."""

    # ------------------------------------------------------------------
    # graph_store_nexus handler — reified relationships (Nexuses)
    # ------------------------------------------------------------------

    async def graph_store_nexus_handler(args: dict) -> str:
        """Store a reified relationship as a Nexus node in the graph.

        Creates: Postgres row + AGE Nexus node + PARTY_TO / TARGETS /
        CONDUCTED_VIA edges.
        """
        import json as _json
        from uuid import uuid4 as _uuid4
        from datetime import datetime as _dt, timezone as _tz

        op_type = args.get("nexus_type", "")
        if not op_type:
            return "Error: nexus_type is required."
        channel = args.get("channel", "direct")
        if channel not in ("direct", "proxy", "covert", "institutional"):
            return f"Error: channel must be one of: direct, proxy, covert, institutional. Got '{channel}'."
        intent = args.get("intent", "neutral")
        if intent not in ("supportive", "hostile", "dual-use", "neutral"):
            return f"Error: intent must be one of: supportive, hostile, dual-use, neutral. Got '{intent}'."
        description = args.get("description", "")
        actor = args.get("actor", "")
        target = args.get("target", "")
        if not actor or not target:
            return "Error: both actor and target are required."
        via = args.get("via", "")
        confidence = float(args.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))
        evidence_id = args.get("evidence_id", "")

        op_id = _uuid4()
        now = _dt.now(_tz.utc)

        # --- 1. Insert into Postgres nexuses table ---
        try:
            if not graph._pool:
                return "Error: graph store not available (no pool)."
            async with graph._pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO nexuses
                        (id, nexus_type, channel, intent, description,
                         actor_entity, target_entity, via_entity,
                         confidence, evidence_count, source_cycle, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 1, $10, $11)
                """,
                    op_id, op_type, channel, intent, description,
                    actor, target, via or None,
                    confidence, 0, now,
                )
        except Exception as exc:
            return f"Error inserting nexus into Postgres: {exc}"

        # --- 2. Create Nexus node in AGE graph ---
        desc_esc = graph._escape(description)
        type_esc = graph._escape(op_type)
        channel_esc = graph._escape(channel)
        intent_esc = graph._escape(intent)
        op_id_str = str(op_id)

        try:
            async with graph._pool.acquire() as conn:
                await graph._prepare(conn)

                # Create the Nexus node
                await graph._cypher(conn, f"""
                    CREATE (op:Nexus {{
                        op_id: '{op_id_str}',
                        type: '{type_esc}',
                        channel: '{channel_esc}',
                        intent: '{intent_esc}',
                        description: '{desc_esc}',
                        confidence: {confidence}
                    }})
                    RETURN op
                """, cols="op agtype")

                # Create PARTY_TO edge: actor -> Nexus
                actor_esc = graph._escape(actor)
                await graph._cypher(conn, f"""
                    MATCH (a {{name: '{actor_esc}'}}), (op:Nexus {{op_id: '{op_id_str}'}})
                    CREATE (a)-[:PartyTo {{role: 'actor'}}]->(op)
                    RETURN a
                """, cols="a agtype")

                # Create TARGETS edge: Nexus -> target
                target_esc = graph._escape(target)
                await graph._cypher(conn, f"""
                    MATCH (op:Nexus {{op_id: '{op_id_str}'}}), (t {{name: '{target_esc}'}})
                    CREATE (op)-[:Targets]->(t)
                    RETURN t
                """, cols="t agtype")

                # Create CONDUCTED_VIA edge if via is provided: Nexus -> via entity
                if via:
                    via_esc = graph._escape(via)
                    await graph._cypher(conn, f"""
                        MATCH (op:Nexus {{op_id: '{op_id_str}'}}), (v {{name: '{via_esc}'}})
                        CREATE (op)-[:ConductedVia]->(v)
                        RETURN v
                    """, cols="v agtype")

        except Exception as exc:
            # Postgres row already written; graph edges are best-effort
            return (
                f"Nexus {op_id_str} saved to Postgres but AGE graph "
                f"creation partially failed: {exc}. "
                f"Ensure actor '{actor}', target '{target}'"
                + (f", and via '{via}'" if via else "")
                + " exist as graph entities (use graph_store first)."
            )

        # Build result message
        parts = [f"Nexus stored: {op_id_str}"]
        parts.append(f"  type={op_type}, channel={channel}, intent={intent}")
        parts.append(f"  actor={actor} -> target={target}")
        if via:
            parts.append(f"  via={via}")
        parts.append(f"  confidence={confidence}")
        if evidence_id:
            parts.append(f"  evidence_id={evidence_id}")
        return "\n".join(parts)

    async def graph_store_handler(args: dict) -> str:
        import json as _json
        from ....shared.schemas.memory import Entity

        # --- Event vertex mode ---
        event_type = args.get("event_type")
        if event_type:
            from ....shared.graph_events import (
                upsert_event_vertex,
                link_entity_to_event,
                link_event_hierarchy,
                link_event_causal,
            )
            event_title = args.get("entity_name", "")
            event_id = int(args.get("event_id", 0))
            lifecycle = args.get("lifecycle_status")
            ok = await upsert_event_vertex(
                graph._pool, graph.GRAPH_NAME,
                event_id, event_title,
                category=event_type, lifecycle_status=lifecycle,
            )
            if not ok:
                return f"Error: failed to upsert Event vertex '{event_title}'."
            result = f"Event vertex '{event_title}' (id={event_id}, type={event_type}) stored."

            # Event-to-event edges (PART_OF, CAUSED_BY)
            relate_to = args.get("relate_to")
            rel_type = args.get("relation_type")
            if relate_to and rel_type:
                if rel_type.upper() == "PART_OF":
                    ok = await link_event_hierarchy(graph._pool, graph.GRAPH_NAME, event_title, relate_to)
                    if ok:
                        result += f" Edge: {event_title} --[PART_OF]--> {relate_to}."
                    else:
                        result += f" Warning: failed to create PART_OF edge to '{relate_to}'."
                elif rel_type.upper() == "CAUSED_BY":
                    props_str = args.get("relation_properties", "{}")
                    try:
                        rprops = _json.loads(props_str) if props_str else {}
                    except Exception:
                        rprops = {}
                    ok = await link_event_causal(
                        graph._pool, graph.GRAPH_NAME, event_title, relate_to,
                        confidence=rprops.get("confidence"),
                        evidence_source=rprops.get("evidence_source"),
                    )
                    if ok:
                        result += f" Edge: {event_title} <--[CAUSED_BY]-- {relate_to}."
                    else:
                        result += f" Warning: failed to create CAUSED_BY edge to '{relate_to}'."
                elif rel_type.upper() == "INVOLVED_IN":
                    # relate_to is the entity name that is involved in this event
                    role = None
                    confidence = None
                    props_str = args.get("relation_properties", "{}")
                    try:
                        rprops = _json.loads(props_str) if props_str else {}
                    except Exception:
                        rprops = {}
                    role = rprops.get("role")
                    confidence = rprops.get("confidence")
                    ok = await link_entity_to_event(
                        graph._pool, graph.GRAPH_NAME, relate_to, event_title,
                        role=role, confidence=confidence,
                    )
                    if ok:
                        result += f" Edge: {relate_to} --[INVOLVED_IN]--> {event_title}."
                    else:
                        result += f" Warning: failed to create INVOLVED_IN edge from '{relate_to}'."
                else:
                    result += f" Warning: unsupported event edge type '{rel_type}'. Use PART_OF, CAUSED_BY, or INVOLVED_IN."

            return result

        # --- Standard entity mode ---
        name = args.get("entity_name", "")
        etype = args.get("entity_type", "concept")
        props_str = args.get("properties", "{}")
        try:
            props = _json.loads(props_str) if props_str else {}
        except Exception:
            props = {}

        existing = await graph.find_entity(name)
        merge_note = ""
        if not existing:
            similar = await _find_similar_entity(graph, name, etype)
            if similar:
                existing = await graph.find_entity(similar)
                merge_note = f" Note: merged into existing '{similar}' (similar to '{name}')."
                name = similar
        if existing:
            existing.entity_type = etype
            existing.properties.update(props)
            entity = existing
        else:
            entity = Entity(name=name, entity_type=etype, properties=props)
        await graph.upsert_entity(entity)
        result = f"Entity '{name}' ({etype}) stored.{merge_note}"

        relate_to = args.get("relate_to")
        rel_type = args.get("relation_type")
        if relate_to and rel_type:
            # Check if this is an INVOLVED_IN edge (entity → event)
            if rel_type.upper() == "INVOLVED_IN":
                from ....shared.graph_events import link_entity_to_event
                props_str = args.get("relation_properties", "{}")
                try:
                    rprops = _json.loads(props_str) if props_str else {}
                except Exception:
                    rprops = {}
                ok = await link_entity_to_event(
                    graph._pool, graph.GRAPH_NAME, name, relate_to,
                    role=rprops.get("role"), confidence=rprops.get("confidence"),
                )
                if ok:
                    result += f" Edge: {name} --[INVOLVED_IN]--> {relate_to}."
                else:
                    result += f" Warning: failed to create INVOLVED_IN edge to event '{relate_to}'."
                return result

            rel_type, rel_note = normalize_relationship_type(rel_type)
            if rel_type is None:
                # Rejected — unrecognized relationship type
                result += f" {rel_note}"
                return result
            target = await graph.find_entity(relate_to)
            if not target:
                similar_target = await _find_similar_entity(graph, relate_to)
                if similar_target:
                    target = await graph.find_entity(similar_target)
                    relate_to = similar_target
            if not target:
                target = Entity(name=relate_to, entity_type="unknown")
                await graph.upsert_entity(target)

            rel_props_str = args.get("relation_properties", "{}")
            try:
                rel_props = _json.loads(rel_props_str) if rel_props_str else {}
            except Exception:
                rel_props = {}

            since = args.get("since")
            until = args.get("until")
            await graph.add_relationship(
                source_name=name,
                target_name=relate_to,
                relation_type=rel_type,
                properties=rel_props,
                since=since,
                until=until,
            )
            temporal = ""
            if since or until:
                parts = []
                if since:
                    parts.append(f"since {since}")
                if until:
                    parts.append(f"until {until}")
                temporal = f" ({', '.join(parts)})"
            result += f" Relationship: {name} --[{rel_type}]--> {relate_to}.{temporal}"
            if rel_note:
                result += f" {rel_note}"

        return result

    # ------------------------------------------------------------------
    # graph_query handler — named operations, no raw Cypher
    # ------------------------------------------------------------------

    async def graph_query_handler(args: dict) -> str:
        import json as _json

        query = args.get("query", "")
        mode = args.get("mode", "search")
        etype = args.get("entity_type")
        rel_type = args.get("relation_type")
        depth = int(args.get("depth", 2))
        limit = int(args.get("limit", 20))
        entity_b = args.get("entity_b", "")

        # ------ existing modes (unchanged) ------

        if mode == "search":
            entities = await graph.search_entities(
                query=query, entity_type=etype, limit=limit,
            )
            if not entities:
                return f"No entities found matching '{query}'."
            lines = []
            for e in entities:
                props = f" {e.properties}" if e.properties else ""
                lines.append(f"- {e.name} ({e.entity_type}){props}")
            return "\n".join(lines)

        elif mode == "relationships":
            rels = await graph.get_relationships(
                entity_name=query,
                relation_type=rel_type,
                limit=limit,
            )
            if not rels:
                return f"No relationships found for '{query}'."
            lines = []
            for r in rels:
                direction = r["direction"]
                arrow = "-->" if direction == "outgoing" else "<--"
                temporal = ""
                rel_props = r.get("rel_properties", {})
                if rel_props.get("since") or rel_props.get("until"):
                    parts = []
                    if rel_props.get("since"):
                        parts.append(f"since {rel_props['since']}")
                    if rel_props.get("until"):
                        parts.append(f"until {rel_props['until']}")
                    temporal = f" ({', '.join(parts)})"
                lines.append(f"- {query} {arrow}[{r['relation_type']}]{arrow} {r['entity_name']} ({r['entity_type']}){temporal}")
            return "\n".join(lines)

        elif mode == "subgraph":
            sg = await graph.query_subgraph(
                entity_name=query, depth=depth, limit=limit,
            )
            entities_list = sg.get("entities", [])
            rels = sg.get("relationships", [])
            if not entities_list:
                return f"No subgraph found around '{query}'."
            lines = [f"Subgraph around '{query}' (depth={depth}):"]
            lines.append(f"Entities ({len(entities_list)}):")
            for e in entities_list:
                lines.append(f"  - {e['name']} ({e['type']})")
            lines.append(f"Relationships ({len(rels)}):")
            for r in rels:
                lines.append(f"  - {r['source']} --[{r['relation']}]--> {r['target']}")
            return "\n".join(lines)

        # ------ new named operations ------

        elif mode == "top_connected":
            # Top N entities by edge count (replaces broken size() Cypher)
            try:
                async with graph._pool.acquire() as conn:
                    await graph._prepare(conn)
                    # Count outgoing + incoming edges per node
                    rows = await graph._cypher(conn, f"""
                        MATCH (n)-[r]-()
                        RETURN n.name AS name, count(r) AS degree
                        ORDER BY degree DESC
                        LIMIT {int(limit)}
                    """, cols="name agtype, degree agtype")
                if not rows:
                    return "No connected entities found."
                lines = [f"Top {len(rows)} entities by connection count:"]
                for r in rows:
                    lines.append(f"  {r['degree']:>4}  {r['name']}")
                return "\n".join(lines)
            except Exception as exc:
                return f"Error in top_connected: {exc}"

        elif mode == "by_type":
            # All entities of a given type
            target_type = etype or query
            if not target_type:
                return "Error: provide entity_type or query with the type name (e.g. 'country', 'person')."
            entities = await graph.search_entities(
                query=None, entity_type=target_type, limit=limit,
            )
            if not entities:
                return f"No entities of type '{target_type}'."
            lines = [f"Entities of type '{target_type}' ({len(entities)}):"]
            for e in entities:
                lines.append(f"  - {e.name}")
            return "\n".join(lines)

        elif mode == "edge_types":
            # Relationship type distribution
            try:
                async with graph._pool.acquire() as conn:
                    await graph._prepare(conn)
                    rows = await graph._cypher(conn, """
                        MATCH ()-[r]->()
                        RETURN label(r) AS rel_type, count(r) AS cnt
                        ORDER BY cnt DESC
                    """, cols="rel_type agtype, cnt agtype")
                if not rows:
                    return "No edges in the graph."
                total = sum(r["cnt"] for r in rows)
                lines = [f"Edge type distribution ({total} total edges):"]
                for r in rows:
                    pct = r["cnt"] / total * 100 if total else 0
                    lines.append(f"  {r['cnt']:>5}  ({pct:4.1f}%)  {r['rel_type']}")
                return "\n".join(lines)
            except Exception as exc:
                return f"Error in edge_types: {exc}"

        elif mode == "shared_connections":
            # Entities connected to both A and B
            if not query or not entity_b:
                return "Error: provide 'query' (entity A) and 'entity_b' (entity B)."
            try:
                a_esc = graph._escape(query)
                b_esc = graph._escape(entity_b)
                async with graph._pool.acquire() as conn:
                    await graph._prepare(conn)
                    rows = await graph._cypher(conn, f"""
                        MATCH (a {{name: '{a_esc}'}})-[r1]-(shared)-[r2]-(b {{name: '{b_esc}'}})
                        RETURN DISTINCT shared.name AS name, label(r1) AS rel_a, label(r2) AS rel_b
                        LIMIT {int(limit)}
                    """, cols="name agtype, rel_a agtype, rel_b agtype")
                if not rows:
                    return f"No shared connections between '{query}' and '{entity_b}'."
                lines = [f"Shared connections between '{query}' and '{entity_b}' ({len(rows)}):"]
                for r in rows:
                    lines.append(f"  - {r['name']}  (via {r['rel_a']} / {r['rel_b']})")
                return "\n".join(lines)
            except Exception as exc:
                return f"Error in shared_connections: {exc}"

        elif mode == "path":
            # Shortest path between A and B
            if not query or not entity_b:
                return "Error: provide 'query' (source entity) and 'entity_b' (target entity)."
            max_depth = min(depth, 5)
            path = await graph.find_path(query, entity_b, max_depth=max_depth)
            if not path:
                return f"No path found between '{query}' and '{entity_b}' within {max_depth} hops."
            lines = [f"Path from '{query}' to '{entity_b}' ({len(path)} steps):"]
            for step in path:
                lines.append(f"  {step.get('source', '?')} --[{step.get('relation', '?')}]--> {step.get('target', '?')}")
            return "\n".join(lines)

        elif mode == "triangles":
            # Find A→B→C relationship triangles (tension/alliance patterns)
            if rel_type:
                rl = graph._sanitize_label(rel_type)
                pattern = f"(a)-[r1:{rl}]->(b)-[r2]->(c)"
            else:
                pattern = "(a)-[r1]->(b)-[r2]->(c)"
            try:
                async with graph._pool.acquire() as conn:
                    await graph._prepare(conn)
                    rows = await graph._cypher(conn, f"""
                        MATCH {pattern}
                        RETURN a.name AS src, label(r1) AS r1_type, b.name AS mid,
                               label(r2) AS r2_type, c.name AS dst
                        LIMIT {int(limit)}
                    """, cols="src agtype, r1_type agtype, mid agtype, r2_type agtype, dst agtype")
                if not rows:
                    rel_note = f" starting with {rel_type}" if rel_type else ""
                    return f"No relationship triangles found{rel_note}."
                lines = [f"Relationship chains ({len(rows)} found):"]
                for r in rows:
                    lines.append(f"  {r['src']} --[{r['r1_type']}]--> {r['mid']} --[{r['r2_type']}]--> {r['dst']}")
                return "\n".join(lines)
            except Exception as exc:
                return f"Error in triangles: {exc}"

        elif mode == "isolated":
            # Entities with zero edges
            try:
                async with graph._pool.acquire() as conn:
                    await graph._prepare(conn)
                    # AGE doesn't support OPTIONAL MATCH well, so use set difference
                    # Get all nodes, then nodes with edges, subtract
                    all_rows = await graph._cypher(conn, f"""
                        MATCH (n)
                        RETURN n.name AS name, label(n) AS etype
                        LIMIT 5000
                    """, cols="name agtype, etype agtype")
                    connected_rows = await graph._cypher(conn, """
                        MATCH (n)-[]-()
                        RETURN DISTINCT n.name AS name
                    """, cols="name agtype")
                connected_names = {r["name"] for r in connected_rows}
                isolated = [r for r in all_rows if r["name"] not in connected_names]
                if not isolated:
                    return f"No isolated entities. All {len(all_rows)} entities have at least one edge."
                result = isolated[:limit]
                lines = [f"Isolated entities ({len(isolated)} of {len(all_rows)} total, showing {len(result)}):"]
                for r in result:
                    lines.append(f"  - {r['name']} ({r['etype']})")
                return "\n".join(lines)
            except Exception as exc:
                return f"Error in isolated: {exc}"

        elif mode == "recent_edges":
            # Edges with a 'since' property >= a given date
            since_val = query or ""
            if not since_val:
                return "Error: provide a date in 'query' (e.g. '2026-03-01') to find edges added since that date."
            try:
                since_esc = graph._escape(since_val)
                async with graph._pool.acquire() as conn:
                    await graph._prepare(conn)
                    rows = await graph._cypher(conn, f"""
                        MATCH (a)-[r]->(b)
                        WHERE r.since >= '{since_esc}'
                        RETURN a.name AS src, label(r) AS rel, b.name AS dst, r.since AS since
                        ORDER BY r.since DESC
                        LIMIT {int(limit)}
                    """, cols="src agtype, rel agtype, dst agtype, since agtype")
                if not rows:
                    return f"No edges with since >= '{since_val}'."
                lines = [f"Edges since '{since_val}' ({len(rows)} found):"]
                for r in rows:
                    lines.append(f"  {r['since']}  {r['src']} --[{r['rel']}]--> {r['dst']}")
                return "\n".join(lines)
            except Exception as exc:
                return f"Error in recent_edges: {exc}"

        # ------ event graph operations (from graph_events module) ------

        elif mode == "event_actors":
            # Entities involved in an event via INVOLVED_IN edges
            if not query:
                return "Error: provide the event title in 'query'."
            try:
                from ....shared.graph_events import event_actors_query
                rows = await event_actors_query(graph._pool, graph.GRAPH_NAME, query)
                if not rows:
                    return f"No actors found for event '{query}'."
                lines = [f"Actors involved in '{query}' ({len(rows)} found):"]
                for r in rows:
                    role = r.get('role') or 'unspecified'
                    conf = r.get('confidence')
                    conf_str = f", conf={conf:.2f}" if conf is not None else ""
                    lines.append(f"  - {r['actor']} ({r.get('type', '?')}) — role={role}{conf_str}")
                return "\n".join(lines)
            except Exception as exc:
                return f"Error in event_actors: {exc}"

        elif mode == "event_chain":
            # Causal chain following CAUSED_BY edges upstream from an event
            if not query:
                return "Error: provide the event title in 'query'."
            try:
                from ....shared.graph_events import event_chain_query
                rows = await event_chain_query(graph._pool, graph.GRAPH_NAME, query, max_depth=depth)
                if not rows:
                    return f"No causal chain found for event '{query}'."
                lines = [f"Causal chains for '{query}' ({len(rows)} paths):"]
                for r in rows:
                    chain = r.get('chain', [])
                    if isinstance(chain, list):
                        lines.append(f"  {' → '.join(str(c) for c in chain)}")
                    else:
                        lines.append(f"  {chain}")
                return "\n".join(lines)
            except Exception as exc:
                return f"Error in event_chain: {exc}"

        elif mode == "event_children":
            # Sub-events of a parent event via PART_OF edges
            if not query:
                return "Error: provide the parent event title in 'query'."
            try:
                from ....shared.graph_events import event_children_query
                rows = await event_children_query(graph._pool, graph.GRAPH_NAME, query)
                if not rows:
                    return f"No sub-events found for '{query}'."
                lines = [f"Sub-events of '{query}' ({len(rows)} found):"]
                for r in rows:
                    eid = r.get('event_id', '?')
                    lines.append(f"  - [{eid}] {r.get('child_event', '?')}")
                return "\n".join(lines)
            except Exception as exc:
                return f"Error in event_children: {exc}"

        elif mode == "event_situation":
            # Events tracked by a situation via TRACKED_BY edges
            if not query:
                return "Error: provide the situation name in 'query'."
            try:
                from ....shared.graph_events import event_situation_query
                rows = await event_situation_query(graph._pool, graph.GRAPH_NAME, query)
                if not rows:
                    return f"No events tracked by situation '{query}'."
                lines = [f"Events tracked by '{query}' ({len(rows)} found):"]
                for r in rows:
                    eid = r.get('event_id', '?')
                    rel = r.get('relevance')
                    rel_str = f" (relevance={rel:.2f})" if rel is not None else ""
                    lines.append(f"  - [{eid}] {r.get('event', '?')}{rel_str}")
                return "\n".join(lines)
            except Exception as exc:
                return f"Error in event_situation: {exc}"

        elif mode == "entity_events":
            # Events an entity participates in via INVOLVED_IN edges
            if not query:
                return "Error: provide the entity name in 'query'."
            try:
                from ....shared.graph_events import entity_events_query
                rows = await entity_events_query(graph._pool, graph.GRAPH_NAME, query, limit=limit)
                if not rows:
                    return f"No events found for entity '{query}'."
                lines = [f"Events involving '{query}' ({len(rows)} found):"]
                for r in rows:
                    eid = r.get('event_id', '?')
                    role = r.get('role') or 'unspecified'
                    lines.append(f"  - [{eid}] {r.get('event', '?')} — role={role}")
                return "\n".join(lines)
            except Exception as exc:
                return f"Error in entity_events: {exc}"

        elif mode == "cross_situation":
            # Events bridging two situations
            if not query or not entity_b:
                return "Error: provide 'query' (situation A) and 'entity_b' (situation B)."
            try:
                from ....shared.graph_events import cross_situation_query
                rows = await cross_situation_query(graph._pool, graph.GRAPH_NAME, query, entity_b)
                if not rows:
                    return f"No bridging events between situations '{query}' and '{entity_b}'."
                lines = [f"Events bridging '{query}' and '{entity_b}' ({len(rows)} found):"]
                for r in rows:
                    eid = r.get('event_id', '?')
                    lines.append(f"  - [{eid}] {r.get('event', '?')}")
                return "\n".join(lines)
            except Exception as exc:
                return f"Error in cross_situation: {exc}"

        # ------ reified relationship (Nexus) queries ------

        elif mode == "proxy_chains":
            # Find all nexuses with channel='proxy' — actor, via, target chains
            try:
                async with graph._pool.acquire() as conn:
                    await graph._prepare(conn)
                    rows = await graph._cypher(conn, """
                        MATCH (a)-[:PartyTo]->(op:Nexus)-[:ConductedVia]->(via),
                              (op)-[:Targets]->(t)
                        WHERE op.channel = 'proxy'
                        RETURN a.name AS actor, via.name AS via_entity,
                               t.name AS target, op.type AS op_type,
                               op.intent AS intent, op.confidence AS conf
                        LIMIT 50
                    """, cols="actor agtype, via_entity agtype, target agtype, op_type agtype, intent agtype, conf agtype")
                if not rows:
                    return "No proxy chain nexuses found."
                lines = [f"Proxy chain nexuses ({len(rows)} found):"]
                for r in rows:
                    conf_str = f" (conf={r['conf']:.2f})" if r.get('conf') is not None else ""
                    lines.append(
                        f"  {r['actor']} --[{r['op_type']}]--> {r['via_entity']} --> {r['target']}"
                        f"  intent={r.get('intent', '?')}{conf_str}"
                    )
                return "\n".join(lines)
            except Exception as exc:
                return f"Error in proxy_chains: {exc}"

        elif mode == "hostile_nexuses":
            # Find all nexuses with intent='hostile'
            try:
                async with graph._pool.acquire() as conn:
                    await graph._prepare(conn)
                    rows = await graph._cypher(conn, """
                        MATCH (a)-[:PartyTo]->(op:Nexus)-[:Targets]->(t)
                        WHERE op.intent = 'hostile'
                        RETURN a.name AS actor, t.name AS target,
                               op.type AS op_type, op.channel AS channel,
                               op.confidence AS conf
                        LIMIT 50
                    """, cols="actor agtype, target agtype, op_type agtype, channel agtype, conf agtype")
                if not rows:
                    return "No hostile nexuses found."
                lines = [f"Hostile nexuses ({len(rows)} found):"]
                for r in rows:
                    conf_str = f" (conf={r['conf']:.2f})" if r.get('conf') is not None else ""
                    lines.append(
                        f"  {r['actor']} --[{r['op_type']}]--> {r['target']}"
                        f"  channel={r.get('channel', '?')}{conf_str}"
                    )
                return "\n".join(lines)
            except Exception as exc:
                return f"Error in hostile_nexuses: {exc}"

        elif mode == "entity_nexuses":
            # Find all nexuses involving a specific entity (as actor, target, or via)
            if not query:
                return "Error: provide the entity name in 'query'."
            try:
                name_esc = graph._escape(query)
                async with graph._pool.acquire() as conn:
                    await graph._prepare(conn)
                    # Actor role
                    actor_rows = await graph._cypher(conn, f"""
                        MATCH (a {{name: '{name_esc}'}})-[:PartyTo]->(op:Nexus)-[:Targets]->(t)
                        RETURN 'actor' AS role, op.type AS op_type, op.channel AS channel,
                               op.intent AS intent, t.name AS other, op.confidence AS conf
                        LIMIT 25
                    """, cols="role agtype, op_type agtype, channel agtype, intent agtype, other agtype, conf agtype")
                    # Target role
                    target_rows = await graph._cypher(conn, f"""
                        MATCH (a)-[:PartyTo]->(op:Nexus)-[:Targets]->(t {{name: '{name_esc}'}})
                        RETURN 'target' AS role, op.type AS op_type, op.channel AS channel,
                               op.intent AS intent, a.name AS other, op.confidence AS conf
                        LIMIT 25
                    """, cols="role agtype, op_type agtype, channel agtype, intent agtype, other agtype, conf agtype")
                    # Via role
                    via_rows = await graph._cypher(conn, f"""
                        MATCH (a)-[:PartyTo]->(op:Nexus)-[:ConductedVia]->(v {{name: '{name_esc}'}})
                        RETURN 'via' AS role, op.type AS op_type, op.channel AS channel,
                               op.intent AS intent, a.name AS other, op.confidence AS conf
                        LIMIT 25
                    """, cols="role agtype, op_type agtype, channel agtype, intent agtype, other agtype, conf agtype")

                all_rows = actor_rows + target_rows + via_rows
                if not all_rows:
                    return f"No nexuses found involving '{query}'."
                lines = [f"Nexuses involving '{query}' ({len(all_rows)} found):"]
                for r in all_rows:
                    conf_str = f" (conf={r['conf']:.2f})" if r.get('conf') is not None else ""
                    lines.append(
                        f"  [{r['role']}] {r['op_type']} — {r['other']}"
                        f"  channel={r.get('channel', '?')}, intent={r.get('intent', '?')}{conf_str}"
                    )
                return "\n".join(lines)
            except Exception as exc:
                return f"Error in entity_nexuses: {exc}"

        # Reject raw Cypher and unknown modes with helpful guidance
        elif mode == "cypher":
            return (
                "Raw Cypher mode is disabled. Use a named operation instead:\n"
                "  top_connected — most-connected entities by edge count\n"
                "  neighbors — same as 'relationships' mode\n"
                "  shared_connections — entities connected to both A and B (set entity_b)\n"
                "  path — shortest path between two entities (set entity_b)\n"
                "  triangles — A→B→C chain patterns (optionally filter by relation_type)\n"
                "  by_type — all entities of a given type\n"
                "  edge_types — relationship type distribution\n"
                "  isolated — entities with zero connections\n"
                "  recent_edges — edges added since a date\n"
                "  event_actors — entities involved in an event\n"
                "  event_chain — causal chain (CAUSED_BY edges) for an event\n"
                "  event_children — sub-events (PART_OF edges)\n"
                "  event_situation — events tracked by a situation\n"
                "  entity_events — events an entity participates in\n"
                "  cross_situation — events bridging two situations (set entity_b)\n"
                "  proxy_chains — nexuses with proxy intermediaries\n"
                "  hostile_nexuses — all hostile-intent nexuses\n"
                "  entity_nexuses — all nexuses involving a specific entity"
            )

        return (
            f"Unknown mode: '{mode}'. Available modes:\n"
            "  search — find entities by name\n"
            "  relationships — get all connections for an entity\n"
            "  subgraph — explore neighborhood (set depth)\n"
            "  top_connected — most-connected entities\n"
            "  shared_connections — mutual connections between two entities (set entity_b)\n"
            "  path — shortest path between two entities (set entity_b)\n"
            "  triangles — A→B→C relationship chains\n"
            "  by_type — list entities of a type\n"
            "  edge_types — relationship type distribution\n"
            "  isolated — entities with no connections\n"
            "  recent_edges — edges added since a date\n"
            "  event_actors — entities involved in an event\n"
            "  event_chain — causal chain for an event\n"
            "  event_children — sub-events of a parent event\n"
            "  event_situation — events tracked by a situation\n"
            "  entity_events — events an entity participates in\n"
            "  cross_situation — events bridging two situations (set entity_b)\n"
            "  proxy_chains — nexuses with proxy intermediaries\n"
            "  hostile_nexuses — all hostile-intent nexuses\n"
            "  entity_nexuses — all nexuses involving a specific entity"
        )

    registry.register(GRAPH_STORE_DEF, graph_store_handler)
    registry.register(GRAPH_QUERY_DEF, graph_query_handler)
    registry.register(GRAPH_STORE_NEXUS_DEF, graph_store_nexus_handler)


GRAPH_STORE_DEF = ToolDefinition(
    name="graph_store",
    description=(
        "Store an entity, event, or relationship in the knowledge graph. "
        "This is the PRIMARY tool for building relationships (edges) between entities. "
        "Use relate_to + relation_type to connect entities: LeaderOf, AlliedWith, "
        "HostileTo, SuppliesWeaponsTo, TradesWith, MemberOf, LocatedIn, OperatesIn, "
        "SanctionedBy, BordersWith, AffiliatedWith, etc. "
        "For events: set event_type to create an Event vertex instead of an entity. "
        "Event edges: INVOLVED_IN (entity→event), PART_OF (event→event), CAUSED_BY (event→event). "
        "Note: entity_resolve only creates nodes — use graph_store to create the edges "
        "that make the graph useful for intelligence analysis."
    ),
    parameters=[
        ToolParameter(name="entity_name", type="string",
                      description="Name of the entity or event title"),
        ToolParameter(name="entity_type", type="string",
                      description="Type/label: person, country, organization, international_org, "
                                  "political_party, armed_group, location, concept, corporation, "
                                  "military_unit, commodity, infrastructure, media_outlet"),
        ToolParameter(name="event_type", type="string",
                      description="Set this to create an Event vertex instead of an entity. "
                                  "Values: conflict, political, economic, disaster, health, etc. "
                                  "When set, entity_name becomes the event title.",
                      required=False),
        ToolParameter(name="event_id", type="number",
                      description="Numeric event ID from the events table (required with event_type)",
                      required=False),
        ToolParameter(name="lifecycle_status", type="string",
                      description="Event lifecycle: developing, evolving, stable, resolved (used with event_type)",
                      required=False),
        ToolParameter(name="properties", type="string",
                      description="JSON object of key-value properties for the entity", required=False),
        ToolParameter(name="relate_to", type="string",
                      description="Name of another entity or event to create a relationship to", required=False),
        ToolParameter(name="relation_type", type="string",
                      description="Type of relationship. Entity-entity: LeaderOf, AlliedWith, "
                                  "HostileTo, MemberOf, LocatedIn, OperatesIn, PartOf, BordersWith, "
                                  "SuppliesWeaponsTo, SanctionedBy, TradesWith, AffiliatedWith, "
                                  "FundedBy, SignatoryTo, OccupiedBy, ProducesResource, CreatedBy. "
                                  "Event edges: INVOLVED_IN, PART_OF, CAUSED_BY. "
                                  "Aliases are normalized (e.g. 'allies' → AlliedWith).",
                      required=False),
        ToolParameter(name="relation_properties", type="string",
                      description="JSON object of properties for the relationship. "
                                  "For INVOLVED_IN: {\"role\": \"participant\", \"confidence\": 0.9}. "
                                  "For CAUSED_BY: {\"confidence\": 0.8, \"evidence_source\": \"...\"}.",
                      required=False),
        ToolParameter(name="since", type="string",
                      description="When the relationship started (e.g. '2024-01', '2022-02-24'). "
                                  "Stored as edge property for temporal graph queries.",
                      required=False),
        ToolParameter(name="until", type="string",
                      description="When the relationship ended (omit if still active). "
                                  "Stored as edge property for temporal graph queries.",
                      required=False),
    ],
)


GRAPH_QUERY_DEF = ToolDefinition(
    name="graph_query",
    description=(
        "Query the entity knowledge graph using named operations. "
        "Modes: search (find entities by name), relationships (connections for an entity), "
        "subgraph (neighborhood exploration), top_connected (highest edge count), "
        "shared_connections (mutual links between two entities), path (shortest route between two), "
        "triangles (A→B→C chains), by_type (list entities of a type), "
        "edge_types (relationship distribution), isolated (unconnected entities), "
        "recent_edges (edges added since a date), "
        "event_actors (entities involved in an event), event_chain (causal chain for an event), "
        "event_children (sub-events), event_situation (events tracked by a situation), "
        "entity_events (events an entity participates in), "
        "cross_situation (events bridging two situations), "
        "proxy_chains (nexuses with proxy intermediaries), "
        "hostile_nexuses (all hostile-intent nexuses), "
        "entity_nexuses (all nexuses involving a specific entity)."
    ),
    parameters=[
        ToolParameter(name="query", type="string",
                      description="Entity name for most modes. For recent_edges: a date (e.g. '2026-03-01'). "
                                  "For by_type: the entity type if entity_type not set. "
                                  "For event_actors/event_chain/event_children: the event title. "
                                  "For event_situation: the situation name. "
                                  "For entity_events: the entity name. "
                                  "For cross_situation: situation A name (set entity_b for situation B)."),
        ToolParameter(name="mode", type="string",
                      description="Mode: 'search', 'relationships', 'subgraph', 'top_connected', "
                                  "'shared_connections', 'path', 'triangles', 'by_type', "
                                  "'edge_types', 'isolated', 'recent_edges', "
                                  "'event_actors', 'event_chain', 'event_children', "
                                  "'event_situation', 'entity_events', 'cross_situation', "
                                  "'proxy_chains', 'hostile_nexuses', 'entity_nexuses'. Default: search"),
        ToolParameter(name="entity_type", type="string",
                      description="Filter by entity type (for search and by_type modes)", required=False),
        ToolParameter(name="entity_b", type="string",
                      description="Second entity for shared_connections and path modes", required=False),
        ToolParameter(name="relation_type", type="string",
                      description="Filter by relationship type (for relationships and triangles)", required=False),
        ToolParameter(name="depth", type="number",
                      description="Depth for subgraph (default 2) or max hops for path (default 2, max 5)", required=False),
        ToolParameter(name="limit", type="number",
                      description="Max results (default 20)", required=False),
    ],
)


GRAPH_STORE_NEXUS_DEF = ToolDefinition(
    name="graph_store_nexus",
    description=(
        "Store a reified relationship (Nexus) in the knowledge graph. "
        "Use this instead of graph_store when a relationship involves a proxy, "
        "intermediary, covert channel, or non-obvious intent. "
        "Creates a Nexus node connected to actor (PARTY_TO), target (TARGETS), "
        "and optionally an intermediary (CONDUCTED_VIA). "
        "Examples: Iran supplies weapons via Hamas targeting Israel (channel=proxy, intent=hostile). "
        "US funds Kurdish separatists covertly in Iran (channel=covert, intent=hostile). "
        "For simple direct relationships (US AlliedWith UK), use graph_store instead."
    ),
    parameters=[
        ToolParameter(name="nexus_type", type="string",
                      description="The canonical predicate: SuppliesWeaponsTo, AlliedWith, "
                                  "HostileTo, FundedBy, SanctionedBy, TradesWith, etc."),
        ToolParameter(name="channel", type="string",
                      description="How the relationship operates: direct, proxy, covert, institutional"),
        ToolParameter(name="intent", type="string",
                      description="The intent: supportive, hostile, dual-use, neutral"),
        ToolParameter(name="description", type="string",
                      description="Human-readable summary of the nexus"),
        ToolParameter(name="actor", type="string",
                      description="Entity name who initiates/conducts the nexus"),
        ToolParameter(name="target", type="string",
                      description="Entity name who is affected by the nexus"),
        ToolParameter(name="via", type="string",
                      description="Intermediary entity name (proxy, cutout, channel). "
                                  "Required when channel is 'proxy'.",
                      required=False),
        ToolParameter(name="confidence", type="number",
                      description="Confidence 0-1 (default 0.5)",
                      required=False),
        ToolParameter(name="evidence_id", type="string",
                      description="Signal or event UUID for provenance linkage",
                      required=False),
    ],
)


# Stubs — only used if register() is not called
async def graph_store(args: dict) -> str:
    return "Error: Graph store not initialized."


async def graph_query(args: dict) -> str:
    return "Error: Graph query not initialized."
