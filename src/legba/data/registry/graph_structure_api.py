# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Notable-structure read API — ``/api/v1/graph/structure`` (#99).

Surfaces the ranked ``interesting`` shortlist the graph-analysis handlers
(``structural_balance`` + ``graph_mining``) distil every run and persist into
the ``graph_metrics`` table (via ``_graph_metrics_sink``). Each shortlist item
follows the shared #99 contract — ``{kind, label, score, rationale, entities}`` —
so the v3 "Notable structure" overlay can list tense actors, brokers,
new-hostile edges, sign-imbalanced triads and proxy chains with their rationale.

Endpoint (bearer-gated):
  * GET /graph/structure  — latest ``interesting`` items merged across the
    structural_balance + graph_mining metric rows, ranked by score desc.
    Optional ``entity`` query scopes/prioritises items that touch that actor
    (case-insensitive match against an item's ``entities``), mirroring the
    findings-follows-selection keystone.

Built via ``build_graph_structure_router(deps)``; wired in ``server.py``
alongside the entities + substrate-reads routers.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer

# The metric rows whose payloads carry an `interesting` shortlist (#99).
_STRUCTURE_METRIC_KINDS = ("structural_balance", "graph_mining")
DEFAULT_LIMIT = 24
MAX_LIMIT = 100


class StructureItem(BaseModel):
    kind: str
    label: str
    score: float = 0.0
    rationale: str = ""
    entities: list[str] = Field(default_factory=list)
    source: str  # which metric_kind produced it (structural_balance | graph_mining)


class StructurePage(BaseModel):
    data: list[StructureItem] = Field(default_factory=list)
    scoped_entity: str | None = None
    computed_at: Any | None = None


def _coerce_item(raw: Any, *, source: str) -> StructureItem | None:
    if not isinstance(raw, dict):
        return None
    label = raw.get("label")
    if not label:
        return None
    ents = raw.get("entities") or []
    if not isinstance(ents, list):
        ents = []
    try:
        score = float(raw.get("score", 0.0) or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    return StructureItem(
        kind=str(raw.get("kind") or "unknown"),
        label=str(label),
        score=score,
        rationale=str(raw.get("rationale") or ""),
        entities=[str(e) for e in ents],
        source=source,
    )


def build_graph_structure_router(deps: RegistryAPIDeps) -> APIRouter:
    router = APIRouter(tags=["graph-structure"])

    @router.get("/graph/structure", response_model=StructurePage)
    async def graph_structure(
        entity: str | None = Query(
            default=None,
            description="optional actor/country to scope+prioritise items that touch it",
        ),
        limit: int = Query(default=DEFAULT_LIMIT),
        principal: str = Depends(require_bearer),
    ) -> StructurePage:
        limit = DEFAULT_LIMIT if limit <= 0 else min(limit, MAX_LIMIT)
        items: list[StructureItem] = []
        latest_at: Any | None = None
        async with deps.descriptor_registry.pg.acquire() as conn:
            # The latest metric row per kind (graph_metrics_kind_idx covers this).
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (metric_kind)
                       metric_kind, computed_at, payload
                  FROM graph_metrics
                 WHERE metric_kind = ANY($1::text[])
                 ORDER BY metric_kind, computed_at DESC
                """,
                list(_STRUCTURE_METRIC_KINDS),
            )
        for row in rows:
            if latest_at is None or (row["computed_at"] and row["computed_at"] > latest_at):
                latest_at = row["computed_at"]
            payload = row["payload"]
            if isinstance(payload, str):
                import json
                try:
                    payload = json.loads(payload)
                except ValueError:
                    payload = {}
            if not isinstance(payload, dict):
                continue
            for raw in payload.get("interesting") or []:
                item = _coerce_item(raw, source=str(row["metric_kind"]))
                if item is not None:
                    items.append(item)

        scoped = entity.strip() if entity else None
        if scoped:
            needle = scoped.lower()

            def _touches(it: StructureItem) -> bool:
                return any(needle in e.lower() for e in it.entities) or needle in it.label.lower()

            # Selection-aware: items touching the entity first (score desc within),
            # then the rest as context.
            matched = [it for it in items if _touches(it)]
            rest = [it for it in items if not _touches(it)]
            matched.sort(key=lambda it: it.score, reverse=True)
            rest.sort(key=lambda it: it.score, reverse=True)
            items = matched + rest
        else:
            items.sort(key=lambda it: it.score, reverse=True)

        return StructurePage(
            data=items[:limit], scoped_entity=scoped, computed_at=latest_at,
        )

    return router


__all__ = ["build_graph_structure_router"]
