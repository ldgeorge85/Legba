"""
Entity graph tools: graph_store, graph_query

Let the agent explicitly manage entities and their relationships in the
knowledge graph (Apache AGE / Cypher). Used for tracking connections between
people, organizations, events, locations, concepts, etc.

The graph_query tool includes a 'cypher' mode for raw Cypher queries,
enabling complex pattern matching, path analysis, and graph algorithms.
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
    # RelatedTo (catch-all synonyms)
    "RELATED_TO": "RelatedTo", "Related": "RelatedTo", "LinksTo": "RelatedTo",
    "Has": "RelatedTo", "Knows": "RelatedTo", "ReportsTo": "RelatedTo",
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
    "AffiliatedWith", "PartOf", "FundedBy", "RelatedTo",
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

    # 4. Unrecognized — default to RelatedTo
    return "RelatedTo", f"Note: '{rel_type}' not recognized. Defaulted to 'RelatedTo'."


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

    async def graph_store_handler(args: dict) -> str:
        import json as _json
        from ....shared.schemas.memory import Entity

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
            rel_type, rel_note = normalize_relationship_type(rel_type)
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

    async def graph_query_handler(args: dict) -> str:
        import json as _json
        import re as _re

        query = args.get("query", "")
        mode = args.get("mode", "search")
        etype = args.get("entity_type")
        rel_type = args.get("relation_type")
        depth = int(args.get("depth", 2))
        limit = int(args.get("limit", 20))

        # Auto-detect Cypher: if mode is default "search" but query looks like
        # Cypher syntax, upgrade to cypher mode. Safe because entity names never
        # start with "MATCH (" etc.
        if mode == "search" and query and _re.match(
            r'\s*(MATCH|CREATE|MERGE|RETURN|WITH|OPTIONAL|UNWIND|CALL)\s*[\(\[]',
            query, _re.IGNORECASE,
        ):
            mode = "cypher"

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
                # Include temporal info if present on edge
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
            entities = sg.get("entities", [])
            rels = sg.get("relationships", [])
            if not entities:
                return f"No subgraph found around '{query}'."
            lines = [f"Subgraph around '{query}' (depth={depth}):"]
            lines.append(f"Entities ({len(entities)}):")
            for e in entities:
                lines.append(f"  - {e['name']} ({e['type']})")
            lines.append(f"Relationships ({len(rels)}):")
            for r in rels:
                lines.append(f"  - {r['source']} --[{r['relation']}]--> {r['target']}")
            return "\n".join(lines)

        elif mode == "cypher":
            results = await graph.execute_cypher(query)
            if not results:
                return "Cypher query returned no results."
            if len(results) == 1 and "error" in results[0]:
                return f"Cypher error: {results[0]['error']}"
            return _json.dumps(results, indent=2, default=str)

        return f"Unknown query mode: {mode}. Use 'search', 'relationships', 'subgraph', or 'cypher'."

    registry.register(GRAPH_STORE_DEF, graph_store_handler)
    registry.register(GRAPH_QUERY_DEF, graph_query_handler)


GRAPH_STORE_DEF = ToolDefinition(
    name="graph_store",
    description=(
        "Store an entity and/or relationship in the knowledge graph. "
        "This is the PRIMARY tool for building relationships (edges) between entities. "
        "Use relate_to + relation_type to connect entities: LeaderOf, AlliedWith, "
        "HostileTo, SuppliesWeaponsTo, TradesWith, MemberOf, LocatedIn, OperatesIn, "
        "SanctionedBy, BordersWith, AffiliatedWith, etc. "
        "Note: entity_resolve only creates nodes — use graph_store to create the edges "
        "that make the graph useful for intelligence analysis."
    ),
    parameters=[
        ToolParameter(name="entity_name", type="string",
                      description="Name of the entity (e.g. 'Russia', 'OPEC', 'CVE-2024-1234')"),
        ToolParameter(name="entity_type", type="string",
                      description="Type/label: person, country, organization, international_org, "
                                  "political_party, armed_group, location, concept, corporation, "
                                  "military_unit, commodity, infrastructure, media_outlet"),
        ToolParameter(name="properties", type="string",
                      description="JSON object of key-value properties for the entity", required=False),
        ToolParameter(name="relate_to", type="string",
                      description="Name of another entity to create a relationship to", required=False),
        ToolParameter(name="relation_type", type="string",
                      description="Type of relationship. Use specific types: LeaderOf, AlliedWith, "
                                  "HostileTo, MemberOf, LocatedIn, OperatesIn, PartOf, BordersWith, "
                                  "SuppliesWeaponsTo, SanctionedBy, TradesWith, AffiliatedWith, "
                                  "FundedBy, SignatoryTo, OccupiedBy, ProducesResource, CreatedBy. "
                                  "Aliases are normalized (e.g. 'allies' → AlliedWith).",
                      required=False),
        ToolParameter(name="relation_properties", type="string",
                      description="JSON object of properties for the relationship", required=False),
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
        "Query the entity knowledge graph. Search for entities, get relationships, "
        "explore subgraphs, or execute raw Cypher for complex pattern matching."
    ),
    parameters=[
        ToolParameter(name="query", type="string",
                      description="Entity name/search term, OR a raw Cypher query when mode='cypher'. "
                                  "NOTE: This graph uses Apache AGE (not Neo4j). Key Cypher differences: "
                                  "(1) No NOT-pattern in WHERE — instead of WHERE NOT (n)-[]-(), use "
                                  "OPTIONAL MATCH (n)-[r]-() ... WHERE r IS NULL. "
                                  "(2) Use label(n) not n.type to get vertex labels. "
                                  "(3) No EXISTS subqueries — use OPTIONAL MATCH + IS NULL pattern. "
                                  "(4) RETURN columns need aliases (AS name) for clean output."),
        ToolParameter(name="entity_type", type="string",
                      description="Filter by entity type (for search mode)", required=False),
        ToolParameter(name="mode", type="string",
                      description="Query mode: 'search' (find entities), 'relationships' (get connections), "
                                  "'subgraph' (explore neighborhood), 'cypher' (raw Cypher query). Default: search",
                      required=False),
        ToolParameter(name="relation_type", type="string",
                      description="Filter relationships by type", required=False),
        ToolParameter(name="depth", type="number",
                      description="Depth for subgraph/path exploration (default 2)", required=False),
        ToolParameter(name="limit", type="number",
                      description="Max results (default 20)", required=False),
    ],
)


# Stubs — only used if register() is not called
async def graph_store(args: dict) -> str:
    return "Error: Graph store not initialized."


async def graph_query(args: dict) -> str:
    return "Error: Graph query not initialized."
