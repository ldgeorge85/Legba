# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entity knowledge-graph read API — ``/api/v1/entities``.

Surfaces the entity substrate the backfill/resolution pipeline populates
(``entity_profiles`` nodes, ``signal_entity_links`` provenance, ``proposed_edges``
relationships) for the v3 Entities / Entity-Graph / Entity-Detail panels — the
source-first analogue of v2's entity knowledge-graph (which ran on the legacy
``/api/graph`` surface).

Endpoints (all bearer-gated):
  * GET /entities                      — list/search nodes (+ mention counts, geo)
  * GET /entities/{id}                 — one entity: profile + linked signals + relationships
  * GET /entities/graph               — nodes + edges for the graph viz (ego or top-N)

Built via ``build_entities_router(deps)``; wired in ``server.py`` alongside the
substrate-reads + lineage routers.
"""
from __future__ import annotations

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


class EntityDetail(BaseModel):
    node: EntityNode
    signals: list[EntitySignalRef] = Field(default_factory=list)
    relationships: list[EntityRelationship] = Field(default_factory=list)


class GraphEdge(BaseModel):
    source: str
    target: str
    relationship_type: str
    confidence: float


class EntityGraph(BaseModel):
    nodes: list[EntityNode]
    edges: list[GraphEdge]


def _validate_limit(limit: int) -> int:
    if limit <= 0:
        return DEFAULT_LIMIT
    return min(limit, MAX_LIMIT)


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
        where: list[str] = []
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
            total = await conn.fetchval("SELECT count(*) FROM entity_profiles")
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
                # Ego graph: edges touching the center, then its neighbours' nodes.
                edge_rows = await conn.fetch(
                    """
                    SELECT source_entity, target_entity, relationship_type, confidence
                      FROM proposed_edges
                     WHERE lower(source_entity) = lower($1) OR lower(target_entity) = lower($1)
                     ORDER BY confidence DESC
                     LIMIT $2
                    """,
                    center, limit,
                )
            else:
                # Top edges by confidence (densest part of the graph).
                edge_rows = await conn.fetch(
                    """
                    SELECT source_entity, target_entity, relationship_type, confidence
                      FROM proposed_edges
                     ORDER BY confidence DESC
                     LIMIT $1
                    """,
                    limit,
                )
            edges = [
                GraphEdge(
                    source=r["source_entity"],
                    target=r["target_entity"],
                    relationship_type=r["relationship_type"],
                    confidence=float(r["confidence"]),
                )
                for r in edge_rows
            ]
            names: set[str] = set()
            for e in edges:
                names.add(e.source)
                names.add(e.target)
            if center:
                names.add(center)
            nodes: list[EntityNode] = []
            if names:
                node_rows = await conn.fetch(
                    """
                    SELECT ep.id, ep.canonical_name, ep.entity_class, ep.entity_type,
                           ep.geo_lat, ep.geo_lon, ep.geo_country, ep.completeness_score,
                           count(sel.signal_id) AS mentions
                      FROM entity_profiles ep
                      LEFT JOIN signal_entity_links sel ON sel.entity_id = ep.id
                     WHERE lower(ep.canonical_name) = ANY($1::text[])
                     GROUP BY ep.id
                    """,
                    [n.lower() for n in names],
                )
                nodes = [_node(r) for r in node_rows]
        return EntityGraph(nodes=nodes, edges=edges)

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
             WHERE {pred}
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
            rel_rows = await conn.fetch(
                """
                SELECT source_entity, target_entity, relationship_type, confidence, evidence_text
                  FROM proposed_edges
                 WHERE lower(source_entity) = lower($1) OR lower(target_entity) = lower($1)
                 ORDER BY confidence DESC
                 LIMIT 50
                """,
                node.canonical_name,
            )
            relationships: list[EntityRelationship] = []
            for r in rel_rows:
                out = r["source_entity"].lower() == node.canonical_name.lower()
                relationships.append(
                    EntityRelationship(
                        other=r["target_entity"] if out else r["source_entity"],
                        relationship_type=r["relationship_type"],
                        confidence=float(r["confidence"]),
                        direction="out" if out else "in",
                        evidence_text=r["evidence_text"] or "",
                    )
                )
        return EntityDetail(node=node, signals=signals, relationships=relationships)

    return router
