"""Graph Explorer route -- GET /graph + GET /api/graph + ego/path APIs + edge CRUD."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Form, Query
from fastapi.responses import JSONResponse, HTMLResponse

from ..app import get_stores, templates

logger = logging.getLogger(__name__)

router = APIRouter()

# Color map for entity types (matches Tailwind palette)
NODE_COLORS = {
    "country": "#38bdf8",    # sky-400
    "Country": "#38bdf8",
    "person": "#a78bfa",     # violet-400
    "Person": "#a78bfa",
    "organization": "#fb923c",  # orange-400
    "Organization": "#fb923c",
    "armed_group": "#f97316", # orange-500
    "location": "#4ade80",   # green-400
    "Location": "#4ade80",
    "international_org": "#818cf8", # indigo-400
    "military_unit": "#f43f5e", # rose-500
    "Unknown": "#94a3b8",    # slate-400
    "Entity": "#94a3b8",     # AGE label fallback
}

# Color map for relationship types
EDGE_COLORS = {
    "LeaderOf": "#60a5fa",    # blue-400
    "AlliedWith": "#4ade80",  # green-400
    "HostileTo": "#f87171",   # red-400
    "EconomicTie": "#fbbf24", # amber-400
    "MemberOf": "#818cf8",    # indigo-400
    "LocatedIn": "#2dd4bf",   # teal-400
    "RelatedTo": "#94a3b8",   # slate-400
}

DEFAULT_NODE_COLOR = "#94a3b8"
DEFAULT_EDGE_COLOR = "#64748b"

# Canonical relationship types for the dropdown
RELATIONSHIP_TYPES = [
    "AlliedWith", "HostileTo", "LeaderOf", "MemberOf", "LocatedIn",
    "OperatesIn", "OccupiedBy", "SuppliesWeaponsTo", "SanctionedBy",
    "TradesWith", "EconomicTie", "SignatoryTo", "SubsidiaryOf",
    "ParentOf", "PartOf", "BordersWith", "InfluencedBy",
    "SuccessorTo", "PredecessorTo", "MediatedBy", "NegotiatesWith",
    "CompetitorOf", "ParticipatesIn", "Administers", "FundedBy",
    "HeadquarteredIn", "FoundedBy", "DisputesWith", "RelatedTo",
]


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _build_cytoscape_data(node_rows, edge_rows) -> dict:
    """Convert raw AGE rows into Cytoscape-format {nodes, edges, rel_types, node_types}."""
    nodes = []
    seen_nodes = set()
    node_types = set()
    for row in node_rows:
        v = row["n"]
        props = v.get("properties", {})
        name = props.get("name", "")
        if not name or name in seen_nodes:
            continue
        seen_nodes.add(name)
        ntype = props.get("entity_type", v.get("label", "Unknown"))
        node_types.add(ntype)
        entity_id = props.get("entity_id", "")
        # Lookup entity_type from DB if not in graph properties
        degree = props.get("degree", 0)
        nodes.append({
            "data": {
                "id": name,
                "name": name,
                "type": ntype,
                "color": NODE_COLORS.get(ntype, NODE_COLORS.get(ntype.capitalize(), DEFAULT_NODE_COLOR)),
                "entity_id": entity_id,
                "degree": degree,
            }
        })

    edges = []
    rel_types = set()
    for i, row in enumerate(edge_rows):
        edge = row["r"]
        src = row["src"]
        tgt = row["tgt"]
        # AGE returns quoted strings
        if isinstance(src, str):
            src = src.strip('"')
        if isinstance(tgt, str):
            tgt = tgt.strip('"')
        rel_type = edge.get("label", "RelatedTo") if isinstance(edge, dict) else "RelatedTo"
        rel_types.add(rel_type)
        edge_props = edge.get("properties", {}) if isinstance(edge, dict) else {}
        if src not in seen_nodes or tgt not in seen_nodes:
            continue
        edges.append({
            "data": {
                "id": f"e{i}",
                "source": src,
                "target": tgt,
                "type": rel_type,
                "color": EDGE_COLORS.get(rel_type, DEFAULT_EDGE_COLOR),
                **{k: str(v) for k, v in edge_props.items()
                   if k not in ("id", "source", "target", "type", "color")},
            }
        })

    # Count degree per node from edges
    degree_count: dict[str, int] = {}
    for e in edges:
        s = e["data"]["source"]
        t = e["data"]["target"]
        degree_count[s] = degree_count.get(s, 0) + 1
        degree_count[t] = degree_count.get(t, 0) + 1
    for n in nodes:
        n["data"]["degree"] = degree_count.get(n["data"]["id"], 0)

    return {
        "nodes": nodes,
        "edges": edges,
        "rel_types": sorted(rel_types),
        "node_types": sorted(node_types),
    }


async def _fetch_full_graph(stores) -> dict:
    """Fetch all nodes and edges from the graph."""
    graph = stores.graph
    if not graph.available:
        return {"nodes": [], "edges": [], "rel_types": [], "node_types": []}

    try:
        async with graph._pool.acquire() as conn:
            node_rows = await graph._cypher(conn,
                "MATCH (n) RETURN n",
                cols="n agtype",
            )
            edge_rows = await graph._cypher(conn,
                "MATCH (a)-[r]->(b) RETURN a.name, r, b.name",
                cols="src agtype, r agtype, tgt agtype",
            )
    except Exception as e:
        logger.warning("Full graph fetch failed: %s", e)
        return {"nodes": [], "edges": [], "rel_types": [], "node_types": []}

    return _build_cytoscape_data(node_rows, edge_rows)


async def _fetch_ego_graph(stores, entity_name: str, depth: int = 1) -> dict:
    """Fetch the ego-graph (subgraph) around a named entity within N hops."""
    graph = stores.graph
    if not graph.available:
        return {"nodes": [], "edges": [], "rel_types": [], "node_types": [], "center": entity_name}

    depth = max(1, min(depth, 5))  # clamp 1..5
    name_esc = graph._escape(entity_name)

    try:
        async with graph._pool.acquire() as conn:
            # Get all nodes within N hops (including the start node via *0..N)
            node_rows = await graph._cypher(conn, f"""
                MATCH (start {{name: '{name_esc}'}})-[*0..{depth}]-(n)
                RETURN DISTINCT n
            """, cols="n agtype")

            if not node_rows:
                return {"nodes": [], "edges": [], "rel_types": [], "node_types": [], "center": entity_name}

            # Collect names for edge filtering
            name_set = set()
            for row in node_rows:
                v = row["n"]
                props = v.get("properties", {})
                name = props.get("name", "")
                if name:
                    name_set.add(name)

            # Get all edges between those nodes
            name_list = ", ".join(f"'{graph._escape(n)}'" for n in name_set)
            edge_rows = await graph._cypher(conn, f"""
                MATCH (a)-[r]->(b)
                WHERE a.name IN [{name_list}] AND b.name IN [{name_list}]
                RETURN a.name, r, b.name
            """, cols="src agtype, r agtype, tgt agtype")

    except Exception as exc:
        logger.warning("ego graph query failed: %s", exc)
        return {"nodes": [], "edges": [], "rel_types": [], "node_types": [], "center": entity_name}

    result = _build_cytoscape_data(node_rows, edge_rows)
    result["center"] = entity_name
    return result


async def _fetch_path(stores, from_name: str, to_name: str) -> dict:
    """Find shortest path between two entities, return as Cytoscape data."""
    graph = stores.graph
    if not graph.available:
        return {"nodes": [], "edges": [], "rel_types": [], "node_types": [], "path_found": False}

    from_esc = graph._escape(from_name)
    to_esc = graph._escape(to_name)

    try:
        async with graph._pool.acquire() as conn:
            # Set a statement timeout to prevent runaway queries
            await conn.execute("SET statement_timeout = '5s'")
            # Shortest path — try depth 1, then 2, then 3 (avoid combinatorial explosion)
            path_rows = None
            for max_depth in (1, 2, 3, 4):
                try:
                    path_rows = await graph._cypher(conn, f"""
                        MATCH p = (a {{name: '{from_esc}'}})-[*1..{max_depth}]-(b {{name: '{to_esc}'}})
                        RETURN p
                        LIMIT 1
                    """, cols="p agtype")
                    if path_rows:
                        break
                except Exception as e:
                    logger.debug("Path query at depth %d failed: %s", max_depth, e)
                    continue
            await conn.execute("RESET statement_timeout")

            if not path_rows:
                return {"nodes": [], "edges": [], "rel_types": [], "node_types": [], "path_found": False}

            # Parse the path: [vertex, edge, vertex, edge, vertex, ...]
            path_data = path_rows[0]["p"]
            if not isinstance(path_data, list):
                return {"nodes": [], "edges": [], "rel_types": [], "node_types": [], "path_found": False}

            # Extract node names from path
            path_names = set()
            for item in path_data:
                if isinstance(item, dict) and "properties" in item:
                    name = item["properties"].get("name", "")
                    if name:
                        path_names.add(name)

            if not path_names:
                return {"nodes": [], "edges": [], "rel_types": [], "node_types": [], "path_found": False}

            # Fetch full node data
            name_list = ", ".join(f"'{graph._escape(n)}'" for n in path_names)
            node_rows = await graph._cypher(conn, f"""
                MATCH (n) WHERE n.name IN [{name_list}]
                RETURN DISTINCT n
            """, cols="n agtype")

            # Fetch edges between path nodes
            edge_rows = await graph._cypher(conn, f"""
                MATCH (a)-[r]->(b)
                WHERE a.name IN [{name_list}] AND b.name IN [{name_list}]
                RETURN a.name, r, b.name
            """, cols="src agtype, r agtype, tgt agtype")

    except Exception as exc:
        logger.warning("path query failed: %s", exc)
        return {"nodes": [], "edges": [], "rel_types": [], "node_types": [], "path_found": False}

    result = _build_cytoscape_data(node_rows, edge_rows)
    result["path_found"] = True
    # Mark path node names so the frontend can highlight them
    result["path_nodes"] = list(path_names)
    return result


# ------------------------------------------------------------------
# Page and API routes
# ------------------------------------------------------------------

@router.get("/graph")
async def graph_page(request: Request):
    stores = get_stores(request)
    # Pre-fetch the full graph node list (lightweight -- just names/types for autocomplete)
    # but don't embed the full graph data since we default to ego-graph mode
    graph_data = await _fetch_full_graph(stores)
    return templates.TemplateResponse(
        "graph/explorer.html",
        {
            "request": request,
            "active_page": "graph",
            "graph_data": graph_data,
            "node_count": len(graph_data["nodes"]),
            "edge_count": len(graph_data["edges"]),
            "relationship_types": RELATIONSHIP_TYPES,
            "node_colors": NODE_COLORS,
            "edge_colors": EDGE_COLORS,
        },
    )


@router.get("/api/graph")
async def graph_api(request: Request):
    """JSON endpoint for full graph."""
    stores = get_stores(request)
    graph_data = await _fetch_full_graph(stores)
    return JSONResponse(graph_data)


@router.get("/api/graph/ego")
async def graph_ego_api(
    request: Request,
    entity: str = Query(..., description="Entity name to center on"),
    depth: int = Query(1, ge=1, le=5, description="Number of hops"),
):
    """JSON endpoint for ego-graph around an entity."""
    stores = get_stores(request)
    data = await _fetch_ego_graph(stores, entity, depth)
    return JSONResponse(data)


@router.get("/api/graph/path")
async def graph_path_api(
    request: Request,
    from_entity: str = Query(..., alias="from", description="Source entity name"),
    to_entity: str = Query(..., alias="to", description="Target entity name"),
):
    """JSON endpoint for shortest path between two entities."""
    stores = get_stores(request)
    data = await _fetch_path(stores, from_entity, to_entity)
    return JSONResponse(data)


@router.get("/api/graph/geo")
async def graph_geo_api(request: Request):
    """Return graph nodes with resolved geo coordinates."""
    from ...agent.tools.builtins.geo import resolve_locations

    stores = get_stores(request)
    graph_data = await _fetch_full_graph(stores)

    geo_nodes = []
    for node in graph_data["nodes"]:
        name = node["data"]["name"]
        result = resolve_locations([name])
        if result.get("coordinates"):
            coord = result["coordinates"][0]
            geo_nodes.append({
                **node["data"],
                "lat": coord["lat"],
                "lon": coord["lon"],
            })

    # Only include edges where both endpoints have geo data
    geo_node_ids = {n["id"] for n in geo_nodes}
    geo_edges = [
        e for e in graph_data["edges"]
        if e["data"]["source"] in geo_node_ids and e["data"]["target"] in geo_node_ids
    ]

    return JSONResponse({"nodes": geo_nodes, "edges": [e["data"] for e in geo_edges]})


# ------------------------------------------------------------------
# Write operations (U13: add/remove edges)
# ------------------------------------------------------------------

@router.post("/api/graph/edges")
async def add_edge(
    request: Request,
    from_entity: str = Form(...),
    to_entity: str = Form(...),
    relation_type: str = Form(...),
    since: str = Form(""),
):
    """Add a relationship between two entities."""
    stores = get_stores(request)
    graph = stores.graph
    if not graph.available:
        return HTMLResponse('<div class="text-red-400 text-sm p-2">Graph unavailable.</div>', status_code=503)

    try:
        props = {}
        if since.strip():
            props["since"] = since.strip()
        props["source"] = "operator"

        await graph.add_relationship(from_entity, to_entity, relation_type, props)
        return HTMLResponse(
            f'<div class="text-green-400 text-sm p-2">Edge added: {from_entity} --[{relation_type}]--> {to_entity}</div>'
        )
    except Exception as e:
        return HTMLResponse(f'<div class="text-red-400 text-sm p-2">Error: {e}</div>', status_code=500)


@router.delete("/api/graph/edges")
async def remove_edge(
    request: Request,
    from_entity: str = "",
    to_entity: str = "",
    relation_type: str = "",
):
    """Remove a relationship between two entities."""
    stores = get_stores(request)
    graph = stores.graph
    if not graph.available:
        return HTMLResponse('<div class="text-red-400 text-sm p-2">Graph unavailable.</div>', status_code=503)

    if not (from_entity and to_entity and relation_type):
        return HTMLResponse('<div class="text-red-400 text-sm p-2">Missing parameters.</div>', status_code=400)

    try:
        # AGE Cypher to delete specific edge
        cypher = (
            f"MATCH (a {{name: '{from_entity}'}})-[r:{relation_type}]->(b {{name: '{to_entity}'}}) "
            f"DELETE r"
        )
        await graph.execute_cypher(cypher)
        return HTMLResponse(
            f'<div class="text-green-400 text-sm p-2">Edge removed: {from_entity} --[{relation_type}]--> {to_entity}</div>'
        )
    except Exception as e:
        return HTMLResponse(f'<div class="text-red-400 text-sm p-2">Error: {e}</div>', status_code=500)
