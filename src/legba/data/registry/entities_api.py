# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entity knowledge-graph read API — ``/api/v1/entities``.

Surfaces the entity substrate the backfill/resolution pipeline populates
(``entity_profiles`` nodes, ``signal_entity_links`` provenance, ``entity_edges``
relationships) for the v3 Entities / Entity-Graph / Entity-Detail panels — the
source-first analogue of v2's entity knowledge-graph (which ran on the legacy
``/api/graph`` surface).

W3-A — CUT OVER TO ``entity_edges`` (migration 0143). Both edge readers here
used to query ``proposed_edges WHERE status = 'promoted'``, keyed by entity
NAME. Three things were wrong with that and all three are fixed by the id-keyed
store rather than by patching the query:

  * **A name is not a key.** The node hydration matched
    ``lower(canonical_name)`` with no class disambiguation, but the entity
    uniqueness index is ``(lower(canonical_name), entity_class)`` — so one name
    could pull several profile rows and the panel drew one actor as several
    nodes. The endpoints are foreign keys now, so the join is exact.
  * **A merge stranded the edge.** A promoted candidate naming an entity the GC
    later merged away kept naming a tombstone, and the node join (which hides
    tombstones) dropped it — the edge rendered with a missing endpoint, or not
    at all. ``entity_edges`` repoints inside the merge transaction.
  * **The candidate queue is not the graph.** ``status='promoted'`` was the
    guard keeping 25,004 rejected and 26,373 orphaned proposals out of the
    panel (graph-debate JUDGE_SYNTHESIS P0). That guard is now structural:
    ``entity_edges`` only ever contained promoted rows.

The cutover also STRICTLY WIDENS what the panel can show. The old surface was
promoted co-occurrence only; ``entity_edges`` additionally carries the derived
typed relations (0144) and the relational facts population (0180). Every family
is returned — this is a VIEWER, and the "cooccurrence is off by default" rule in
0143 governs signed/balance ANALYTICS, not visualisation — but ``edge_family``
now travels on every edge so the UI can render a co-mention differently from an
asserted relation instead of conflating them.

Endpoints (all bearer-gated):
  * GET /entities                      — list/search nodes (+ mention counts, geo)
  * GET /entities/{id}                 — one entity: profile + linked signals + relationships
  * GET /entities/graph               — nodes + edges for the graph viz (ego or top-N)

Built via ``build_entities_router(deps)``; wired in ``server.py`` alongside the
substrate-reads + lineage routers.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


class EntityNode(BaseModel):
    id: str
    canonical_name: str
    entity_class: str
    entity_type: str
    mentions: int = 0
    geo_lat: float | None = None
    geo_lon: float | None = None
    geo_country: str | None = None
    completeness_score: float = 0.0


class EntitiesPage(BaseModel):
    data: list[EntityNode]
    total: int


class EntitySignalRef(BaseModel):
    id: str
    title: str | None
    source_id: str | None
    produced_at: Any | None
    role: str


class EntityRelationship(BaseModel):
    other: str
    relationship_type: str
    confidence: float
    direction: str  # "out" (this→other) | "in" (other→this)
    evidence_text: str = ""
    edge_family: str = ""
    polarity: int = 0
    observed_count: int = 1


class EntityDetail(BaseModel):
    node: EntityNode
    signals: list[EntitySignalRef] = Field(default_factory=list)
    relationships: list[EntityRelationship] = Field(default_factory=list)


class GraphEdge(BaseModel):
    """One edge of the viz graph.

    ``source``/``target`` stay canonical NAMES because the cytoscape panel keys
    its nodes by name and a node-click selects by name. ``src_id``/``dst_id``
    carry the real identity alongside them, so the UI can migrate to ids
    without a second breaking change, and ``edge_family`` is exposed because a
    co-mention and an asserted relation must never render as the same claim.
    """

    source: str
    target: str
    relationship_type: str
    confidence: float
    src_id: str = ""
    dst_id: str = ""
    edge_family: str = ""
    polarity: int = 0
    observed_count: int = 1


class EntityGraph(BaseModel):
    nodes: list[EntityNode]
    edges: list[GraphEdge]


#: Open-edge predicate, the same one `entity_edges` indexes on (migration 0143).
_OPEN = "e.valid_until IS NULL AND e.superseded_by IS NULL"

#: Both endpoints hydrated in the edge query itself. The endpoints are real
#: foreign keys now, so the node join is exact — where the name-keyed version
#: matched `lower(canonical_name)` and could pull SEVERAL profiles for one name
#: (the entity uniqueness index is (lower(canonical_name), entity_class), so a
#: name is not a key), this cannot.
#:
#: THE `merged_into IS NULL` TOMBSTONE GUARD IS GONE ON PURPOSE. The old node
#: query carried it, and re-adding it here would be a regression, not a
#: safeguard: because the node join is now the EDGE's join, filtering a
#: tombstone endpoint would drop the whole EDGE — reintroducing exactly the
#: silent disappearance this cutover fixes. The guarantee moved upstream where
#: it belongs: `fold_entity_edges` (0143 §4) repoints an edge onto the keeper
#: INSIDE the merge transaction, so an open edge cannot name a tombstone.
#: Verified live read-only 2026-08-03: 0 open edges with a tombstone endpoint.
#: `_CENTER_NODE_SQL` keeps the guard, because there the filter drops a NODE and
#: dropping a merged-away centre is correct.
_EDGE_NODE_COLS = """
       s.id AS src_node_id, s.canonical_name AS src_canonical_name,
       s.entity_class AS src_entity_class, s.entity_type AS src_entity_type,
       s.geo_lat AS src_geo_lat, s.geo_lon AS src_geo_lon,
       s.geo_country AS src_geo_country,
       s.completeness_score AS src_completeness_score,
       d.id AS dst_node_id, d.canonical_name AS dst_canonical_name,
       d.entity_class AS dst_entity_class, d.entity_type AS dst_entity_type,
       d.geo_lat AS dst_geo_lat, d.geo_lon AS dst_geo_lon,
       d.geo_country AS dst_geo_country,
       d.completeness_score AS dst_completeness_score
"""

_EDGE_COLS = """
       e.src_id, e.dst_id, e.edge_type, e.edge_family, e.polarity,
       e.confidence, e.observed_count
"""

# Mention counts are a per-node aggregate over `signal_entity_links`; computing
# them inside the edge join would multiply by edge degree, so they are a lateral
# scalar per endpoint.
_MENTIONS = """
       (SELECT count(*) FROM signal_entity_links sl
         WHERE sl.entity_id = s.id) AS src_mentions,
       (SELECT count(*) FROM signal_entity_links sl
         WHERE sl.entity_id = d.id) AS dst_mentions
"""

_EGO_GRAPH_SQL = f"""
SELECT {_EDGE_COLS}, {_EDGE_NODE_COLS}, {_MENTIONS}
  FROM entity_edges e
  JOIN entity_profiles s ON s.id = e.src_id
  JOIN entity_profiles d ON d.id = e.dst_id
 WHERE {_OPEN}
   AND (lower(s.canonical_name) = lower($1)
        OR lower(d.canonical_name) = lower($1))
 ORDER BY e.confidence DESC, e.observed_count DESC
 LIMIT $2
"""

_TOP_GRAPH_SQL = f"""
SELECT {_EDGE_COLS}, {_EDGE_NODE_COLS}, {_MENTIONS}
  FROM entity_edges e
  JOIN entity_profiles s ON s.id = e.src_id
  JOIN entity_profiles d ON d.id = e.dst_id
 WHERE {_OPEN}
 ORDER BY e.confidence DESC, e.observed_count DESC
 LIMIT $1
"""

_CENTER_NODE_SQL = """
SELECT ep.id, ep.canonical_name, ep.entity_class, ep.entity_type,
       ep.geo_lat, ep.geo_lon, ep.geo_country, ep.completeness_score,
       count(sel.signal_id) AS mentions
  FROM entity_profiles ep
  LEFT JOIN signal_entity_links sel ON sel.entity_id = ep.id
 WHERE lower(ep.canonical_name) = lower($1)
   AND ep.merged_into IS NULL
 GROUP BY ep.id
"""

#: One entity's edges, both directions, with the OTHER endpoint hydrated.
_RELATIONSHIPS_SQL = f"""
SELECT e.edge_type, e.edge_family, e.polarity, e.confidence, e.observed_count,
       e.evidence_set,
       CASE WHEN e.src_id = $1 THEN 'out' ELSE 'in' END AS direction,
       o.canonical_name AS other
  FROM entity_edges e
  JOIN entity_profiles o
    ON o.id = CASE WHEN e.src_id = $1 THEN e.dst_id ELSE e.src_id END
 WHERE {_OPEN}
   AND (e.src_id = $1 OR e.dst_id = $1)
 ORDER BY e.confidence DESC, e.observed_count DESC
 LIMIT 50
"""


def _validate_limit(limit: int) -> int:
    if limit <= 0:
        return DEFAULT_LIMIT
    return min(limit, MAX_LIMIT)


def _node_row(row: Any, side: str) -> dict[str, Any]:
    """Project one endpoint's prefixed columns back onto the `_node` contract."""
    return {
        "id": row[f"{side}_node_id"],
        "canonical_name": row[f"{side}_canonical_name"],
        "entity_class": row[f"{side}_entity_class"],
        "entity_type": row[f"{side}_entity_type"],
        "geo_lat": row[f"{side}_geo_lat"],
        "geo_lon": row[f"{side}_geo_lon"],
        "geo_country": row[f"{side}_geo_country"],
        "completeness_score": row[f"{side}_completeness_score"],
        "mentions": row[f"{side}_mentions"],
    }


def _evidence_text(evidence_set: Any) -> str:
    """Lift the citable free text off an edge's ``evidence_set``.

    The blob is the promoted candidate's ``evidence_text`` where it had one
    (0145) and a projection marker otherwise (0180); asyncpg hands jsonb back
    as text unless a codec is registered, so both shapes are tolerated.
    """
    if not evidence_set:
        return ""
    if isinstance(evidence_set, str):
        try:
            evidence_set = json.loads(evidence_set)
        except (ValueError, TypeError):
            return ""
    if not isinstance(evidence_set, dict):
        return ""
    return str(evidence_set.get("evidence_text") or "")


def _graph_edge(row: Any) -> GraphEdge:
    return GraphEdge(
        source=row["src_canonical_name"],
        target=row["dst_canonical_name"],
        relationship_type=row["edge_type"],
        confidence=float(row["confidence"]),
        src_id=str(row["src_id"]),
        dst_id=str(row["dst_id"]),
        edge_family=row["edge_family"],
        polarity=int(row["polarity"] or 0),
        observed_count=int(row["observed_count"] or 1),
    )


def _node(row: Any) -> EntityNode:
    return EntityNode(
        id=str(row["id"]),
        canonical_name=row["canonical_name"],
        entity_class=row["entity_class"],
        entity_type=row["entity_type"],
        mentions=int(row["mentions"]) if row.get("mentions") is not None else 0,
        geo_lat=row.get("geo_lat"),
        geo_lon=row.get("geo_lon"),
        geo_country=row.get("geo_country"),
        completeness_score=float(row["completeness_score"]) if row.get("completeness_score") is not None else 0.0,
    )


def build_entities_router(deps: RegistryAPIDeps) -> APIRouter:
    router = APIRouter(tags=["entities"])

    @router.get("/entities", response_model=EntitiesPage)
    async def list_entities(
        q: str | None = Query(default=None),
        entity_class: str | None = Query(default=None),
        limit: int = Query(default=DEFAULT_LIMIT),
        principal: str = Depends(require_bearer),
    ) -> EntitiesPage:
        limit = _validate_limit(limit)
        # E5: exclude merged-loser tombstones (merged_into set) so a folded
        # fragment never surfaces as a separate entity or inflates the count.
        where: list[str] = ["ep.merged_into IS NULL"]
        args: list[Any] = []
        if q:
            args.append(f"%{q}%")
            where.append(f"ep.canonical_name ILIKE ${len(args)}")
        if entity_class:
            args.append(entity_class)
            where.append(f"ep.entity_class = ${len(args)}")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        args.append(limit)
        sql = f"""
            SELECT ep.id, ep.canonical_name, ep.entity_class, ep.entity_type,
                   ep.geo_lat, ep.geo_lon, ep.geo_country, ep.completeness_score,
                   count(sel.signal_id) AS mentions
              FROM entity_profiles ep
              LEFT JOIN signal_entity_links sel ON sel.entity_id = ep.id
              {where_sql}
             GROUP BY ep.id
             ORDER BY mentions DESC, ep.canonical_name ASC
             LIMIT ${len(args)}
        """
        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(sql, *args)
            total = await conn.fetchval(
                "SELECT count(*) FROM entity_profiles WHERE merged_into IS NULL")
        return EntitiesPage(data=[_node(r) for r in rows], total=int(total or 0))

    @router.get("/entities/graph", response_model=EntityGraph)
    async def entity_graph(
        center: str | None = Query(default=None, description="canonical_name to ego-center on"),
        limit: int = Query(default=60),
        principal: str = Depends(require_bearer),
    ) -> EntityGraph:
        limit = min(max(limit, 1), 300)
        async with deps.descriptor_registry.pg.acquire() as conn:
            if center:
                edge_rows = await conn.fetch(
                    _EGO_GRAPH_SQL, center, limit)
            else:
                edge_rows = await conn.fetch(_TOP_GRAPH_SQL, limit)
            edges = [_graph_edge(r) for r in edge_rows]
            # Nodes come back with the edge rows now (the endpoints are FKs, so
            # one join replaces the second name-keyed round trip), but the
            # CENTER still has to be fetched when its ego set is empty — an
            # isolated entity must render as a lone node, not as nothing.
            nodes: dict[str, EntityNode] = {}
            for r in edge_rows:
                for side in ("src", "dst"):
                    nodes.setdefault(str(r[f"{side}_id"]),
                                     _node(_node_row(r, side)))
            if center and not any(
                n.canonical_name.lower() == center.lower()
                for n in nodes.values()
            ):
                center_row = await conn.fetchrow(_CENTER_NODE_SQL, center)
                if center_row is not None:
                    nodes[str(center_row["id"])] = _node(center_row)
        return EntityGraph(nodes=list(nodes.values()), edges=edges)

    @router.get("/entities/{entity_id}", response_model=EntityDetail)
    async def entity_detail(
        entity_id: str,
        signal_limit: int = Query(default=25),
        principal: str = Depends(require_bearer),
    ) -> EntityDetail:
        signal_limit = min(max(signal_limit, 1), 200)
        # Accept EITHER the entity UUID or its canonical_name. The knowledge-graph
        # keys cytoscape nodes by canonical_name (edges reference names), so a
        # node-click selects by name, not id — the old bare-UUID route 422'd on it.
        try:
            as_uuid: UUID | None = UUID(entity_id)
        except ValueError:
            as_uuid = None
        node_sql = """
            SELECT ep.id, ep.canonical_name, ep.entity_class, ep.entity_type,
                   ep.geo_lat, ep.geo_lon, ep.geo_country, ep.completeness_score,
                   count(sel.signal_id) AS mentions
              FROM entity_profiles ep
              LEFT JOIN signal_entity_links sel ON sel.entity_id = ep.id
             WHERE ({pred}) AND ep.merged_into IS NULL   -- E5: hide tombstones
             GROUP BY ep.id
        """
        async with deps.descriptor_registry.pg.acquire() as conn:
            if as_uuid is not None:
                node_row = await conn.fetchrow(node_sql.format(pred="ep.id = $1"), as_uuid)
            else:
                node_row = await conn.fetchrow(
                    node_sql.format(pred="lower(ep.canonical_name) = lower($1)"), entity_id,
                )
            if node_row is None:
                from fastapi import HTTPException, status
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="entity not found")
            node = _node(node_row)
            resolved_id = node_row["id"]  # the actual UUID, for the signals join below
            sig_rows = await conn.fetch(
                """
                SELECT s.id, s.payload->>'title' AS title, s.source_id,
                       s.fetched_at AS produced_at, sel.role
                  FROM signal_entity_links sel
                  JOIN signals s ON s.id = sel.signal_id
                 WHERE sel.entity_id = $1
                 ORDER BY s.fetched_at DESC
                 LIMIT $2
                """,
                resolved_id, signal_limit,
            )
            signals = [
                EntitySignalRef(
                    id=str(r["id"]),
                    title=r["title"],
                    source_id=r["source_id"],
                    produced_at=r["produced_at"],
                    role=r["role"],
                )
                for r in sig_rows
            ]
            # Keyed on the RESOLVED id, not the name: direction is now a
            # property of the row rather than a string comparison that a
            # case- or alias-variant endpoint could get backwards.
            rel_rows = await conn.fetch(_RELATIONSHIPS_SQL, resolved_id)
            relationships = [
                EntityRelationship(
                    other=r["other"],
                    relationship_type=r["edge_type"],
                    confidence=float(r["confidence"]),
                    direction=r["direction"],
                    evidence_text=_evidence_text(r["evidence_set"]),
                    edge_family=r["edge_family"],
                    polarity=int(r["polarity"] or 0),
                    observed_count=int(r["observed_count"] or 1),
                )
                for r in rel_rows
            ]
        return EntityDetail(node=node, signals=signals, relationships=relationships)

    return router
