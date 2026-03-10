"""Graph Explorer route — GET /graph + GET /api/graph + edge CRUD."""

from __future__ import annotations

from fastapi import APIRouter, Request, Form
from fastapi.responses import JSONResponse, HTMLResponse

from ..app import get_stores, templates

router = APIRouter()

# Color map for entity types (matches Tailwind palette)
NODE_COLORS = {
    "Country": "#38bdf8",    # sky-400
    "Person": "#a78bfa",     # violet-400
    "Organization": "#fb923c",  # orange-400
    "Location": "#4ade80",   # green-400
    "Unknown": "#94a3b8",    # slate-400
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


async def _fetch_full_graph(stores) -> dict:
    """Fetch all nodes and edges from the graph."""
    graph = stores.graph
    if not graph.available:
        return {"nodes": [], "edges": [], "rel_types": [], "node_types": []}

    try:
        async with graph._pool.acquire() as conn:
            # Fetch all nodes
            node_rows = await graph._cypher(conn,
                "MATCH (n) RETURN n",
                cols="n agtype",
            )

            # Fetch all edges
            edge_rows = await graph._cypher(conn,
                "MATCH (a)-[r]->(b) RETURN a.name, r, b.name",
                cols="src agtype, r agtype, tgt agtype",
            )
    except Exception:
        return {"nodes": [], "edges": [], "rel_types": [], "node_types": []}

    # Build Cytoscape-format elements
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
        ntype = v.get("label", "Unknown")
        node_types.add(ntype)
        nodes.append({
            "data": {
                "id": name,
                "name": name,
                "type": ntype,
                "color": NODE_COLORS.get(ntype, DEFAULT_NODE_COLOR),
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


@router.get("/graph")
async def graph_page(request: Request):
    stores = get_stores(request)
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
        },
    )


@router.get("/api/graph")
async def graph_api(request: Request):
    """JSON endpoint for dynamic graph reloads."""
    stores = get_stores(request)
    graph_data = await _fetch_full_graph(stores)
    return JSONResponse(graph_data)


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
