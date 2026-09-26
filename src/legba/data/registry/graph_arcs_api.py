# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V3/P4b — the cross-layer graph read API over ``graph_arcs``.

``GET /api/v1/v3/graph/arcs`` — a plane-filtered, temporally-aware, bounded
ego/walk over the whole-rebuild projection. Mounted under ``/api/v1/v3``
beside the graph-triggers router (same ``RegistryAPIDeps`` bundle +
``require_bearer`` gate).

The contract (spec §4.2 rules 4–6):

  * **Refusals are distinct states, never ``found=False``.** The endpoint
    refuses with HTTP 409 and a machine-readable ``detail.state`` naming
    exactly one of ``projection_disabled`` (``LEGBA_GRAPH_PROJECTION`` off —
    the table is never built), ``projection_empty`` (built but holding no
    arcs — including a projection whose migration has not yet run), or
    ``projection_stale`` (``projected_at`` older than
    ``LEGBA_GRAPH_PROJECTION_MAX_AGE_SECONDS``). An empty *neighbourhood* on a
    healthy projection is a 200 with ``arcs: []`` — a node that has no arcs
    and a projection that has none are different answers.
  * **Every read publishes the build stamp.** ``projected_at`` +
    ``age_seconds`` ride every 200 response, so a stale projection is loud on
    the payload, not silent.
  * **``plane`` is explicit and ``world`` by default.** 74 % of arcs are
    output lineage; a world-facing read that defaulted to "all planes" would
    report who-cited-whom as world state — the edge_family lesson (Decision
    3) arriving in a second domain.
  * **``as_of`` is a per-arc temporal predicate**, not a projection rewind:
    arcs whose ``valid_from``/``valid_until`` window covers the instant. A
    closed arc drops out of the past read exactly as it does out of the
    present one — the window is what was projected.

Registry-slim: this module imports only the ``legba.data.graph_projection``
leaf (stdlib + the flag/state helpers) — never the analyst runtime — and the
poisoned-import test asserts that stays true.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..graph_projection import (
    PROJECTION_EMPTY,
    PROJECTION_OK,
    PROJECTION_STALE,
    projection_state,
)
from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

#: The node-kind vocabulary the projector writes. Open by contract — a new
#: source adds a kind without a code change — so this is a filter hint, not a
#: gate; unknown kinds are refused LOUD (400) rather than silently empty.
NODE_KINDS: tuple[str, ...] = (
    "signal", "entity", "fact", "event", "situation",
    "finding", "hypothesis", "narrative", "situation_event",
)

#: Deploy-marker convention (judge_reconcile_version etc.): the roll
#: checklist greps for this exact line.
GRAPH_ARCS_API_VERSION = "2026-09/p4b"

#: The plane CHECK vocabulary from migration 0207 — mirrored here (the slim
#: image reads no DDL). Drift-guarded in test_graph_projector.py.
PLANES: tuple[str, ...] = ("world", "evidence", "lineage")

DEFAULT_LIMIT = 200
MAX_LIMIT = 1_000
#: Hop-2 neighbourhood seeds — the fan-out cap that keeps a "walk" bounded.
WALK_MAX_NODES = 250
MAX_DEPTH = 2

#: The open-at-as_of predicate, spelled once. ``as_of`` absent means "open
#: now" — the partial-index predicate ``valid_until IS NULL``.
_OPEN_NOW = "a.valid_until IS NULL"


def _refuse(state_name: str, reason: str) -> HTTPException:
    """The named refusal — 409 with a machine-readable state, never a
    silent empty result."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"state": state_name, "reason": reason},
    )


class NodeRef(BaseModel):
    """One graph node — kind + id, no name resolution (the projection carries
    none; a reader wanting ``canonical_name`` joins entity_profiles itself)."""

    kind: str
    id: str


class ArcOut(BaseModel):
    """One projected arc, verbatim plus the direction the anchor saw it."""

    from_kind: str
    from_id: str
    to_kind: str
    to_id: str
    arc_type: str
    plane: str
    family: str | None = None
    polarity: int = 0
    confidence: float | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    src_table: str
    src_id: str | None = None
    #: ``out`` = arc leaves the anchor; ``in`` = arrives; ``walk`` = hop > 1.
    direction: str = "out"


class GraphArcsResponse(BaseModel):
    """The bounded ego/walk envelope — honest about freshness and truncation."""

    state: Literal["ok"] = PROJECTION_OK  # refusals never reach the envelope
    anchor: NodeRef
    plane: str
    as_of: datetime | None = None
    depth: int
    nodes: list[NodeRef] = Field(default_factory=list)
    arcs: list[ArcOut] = Field(default_factory=list)
    #: How many arcs matched BEFORE the limit — the honest denominator.
    matched: int = 0
    truncated: bool = False
    #: The build stamp every read publishes (spec §4.2 rule 6).
    projected_at: datetime | None = None
    age_seconds: float | None = None


# ---------------------------------------------------------------------------
# SQL — every read is index-driven on (from_kind, from_id) / (to_kind, to_id)
# ---------------------------------------------------------------------------

_BASE_SELECT = """
    SELECT a.from_kind, a.from_id, a.to_kind, a.to_id, a.arc_type, a.plane,
           a.family, a.polarity, a.confidence, a.valid_from, a.valid_until,
           a.src_table, a.src_id
      FROM public.graph_arcs a
"""


def _temporal_pred(as_of: datetime | None, args: list[Any]) -> str:
    """The per-arc temporal predicate: open now, or open at ``as_of``."""
    if as_of is None:
        return _OPEN_NOW
    args.append(as_of)
    p = len(args)
    return (
        f"(a.valid_from IS NULL OR a.valid_from <= ${p}) "
        f"AND (a.valid_until IS NULL OR a.valid_until > ${p})"
    )


def _ego_sql(
    *,
    plane: str,
    as_of: datetime | None,
    direction: str,
    arc_types: list[str],
    kinds: list[str],
    anchor: tuple[str, UUID],
    seeds: list[tuple[str, UUID]] | None,
    args: list[Any],
) -> str:
    """Bounded ego or hop-2 query. ``seeds`` (kind,id pairs) replaces the
    single anchor for the walk's second hop."""
    args.append(plane)
    plane_p = len(args)
    pred = _temporal_pred(as_of, args)
    where = [f"a.plane = ${plane_p}", pred]

    if arc_types:
        args.append(arc_types)
        where.append(f"a.arc_type = ANY(${len(args)}::text[])")
    if kinds:
        # Neighbour-kind filter: the OTHER endpoint's kind.
        args.append(kinds)
        where.append(
            f"(a.from_kind = ANY(${len(args)}::text[]) "
            f"OR a.to_kind = ANY(${len(args)}::text[]))"
        )

    if seeds is not None:
        args.append([k for k, _ in seeds])
        args.append([i for _, i in seeds])
        kp, ip = len(args) - 1, len(args)
        if direction == "out":
            where.append(
                f"(a.from_kind, a.from_id) IN "
                f"(SELECT u.k, u.i FROM unnest(${kp}::text[], ${ip}::uuid[]) u(k,i))"
            )
        elif direction == "in":
            where.append(
                f"(a.to_kind, a.to_id) IN "
                f"(SELECT u.k, u.i FROM unnest(${kp}::text[], ${ip}::uuid[]) u(k,i))"
            )
        else:
            where.append(
                f"((a.from_kind, a.from_id) IN "
                f"(SELECT u.k, u.i FROM unnest(${kp}::text[], ${ip}::uuid[]) u(k,i))"
                f" OR (a.to_kind, a.to_id) IN "
                f"(SELECT u.k, u.i FROM unnest(${kp}::text[], ${ip}::uuid[]) u(k,i)))"
            )
    else:
        args.append(anchor[0])
        args.append(anchor[1])
        kp, ip = len(args) - 1, len(args)
        if direction == "out":
            where.append(f"a.from_kind = ${kp} AND a.from_id = ${ip}")
        elif direction == "in":
            where.append(f"a.to_kind = ${kp} AND a.to_id = ${ip}")
        else:
            where.append(
                f"(a.from_kind = ${kp} AND a.from_id = ${ip}) "
                f"OR (a.to_kind = ${kp} AND a.to_id = ${ip})"
            )
    return _BASE_SELECT + " WHERE " + " AND ".join(where) + f" LIMIT {MAX_LIMIT}"


def _row_to_arc(row: Any, anchor_key: tuple[str, str], seen_dir: str) -> ArcOut:
    """One asyncpg row -> the wire shape."""
    fk, tk = str(row["from_kind"]), str(row["to_kind"])
    fid, tid = str(row["from_id"]), str(row["to_id"])
    direction = seen_dir
    if seen_dir == "both":
        direction = "out" if (fk, fid) == anchor_key else "in"
    return ArcOut(
        from_kind=fk,
        from_id=fid,
        to_kind=tk,
        to_id=tid,
        arc_type=row["arc_type"],
        plane=row["plane"],
        family=row["family"],
        polarity=int(row["polarity"] or 0),
        confidence=row["confidence"],
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
        src_table=row["src_table"],
        src_id=str(row["src_id"]) if row["src_id"] is not None else None,
        direction=direction,
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_graph_arcs_router(deps: RegistryAPIDeps) -> APIRouter:
    """``GET /graph/arcs`` — bounded, plane-filtered, as_of-aware ego/walk."""
    router = APIRouter(tags=["graph-arcs"])

    @router.get("/graph/arcs", response_model=GraphArcsResponse)
    async def graph_arcs(
        node_kind: str = Query(..., description="anchor node kind"),
        node_id: str = Query(..., description="anchor node uuid"),
        plane: str = Query(
            default="world",
            description="arc plane: " + ", ".join(PLANES),
        ),
        as_of: datetime | None = Query(
            default=None,
            description="read the projection as it knew the world at this "
            "instant (arc valid_from/valid_until window); omit for open-now",
        ),
        direction: str = Query(
            default="both", description="both | out | in (relative to the anchor)"
        ),
        depth: int = Query(default=1, ge=1, le=MAX_DEPTH, description="hops"),
        arc_type: list[str] | None = Query(
            default=None, description="arc types to include (repeatable/csv)"
        ),
        to_kind: list[str] | None = Query(
            default=None, description="endpoint-kind filter (repeatable/csv)"
        ),
        limit: int = Query(default=DEFAULT_LIMIT, description="max arcs"),
        principal: str = Depends(require_bearer),
    ) -> GraphArcsResponse:
        if plane not in PLANES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"plane must be one of {PLANES}",
            )
        try:
            anchor_uuid = UUID(node_id)
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="node_id must be a uuid",
            )
        if direction not in ("both", "out", "in"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="direction must be one of both|out|in",
            )
        limit = max(1, min(limit, MAX_LIMIT))
        arc_types = _split_csv(arc_type)
        kinds = _split_csv(to_kind)
        for k in kinds:
            if k not in NODE_KINDS:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"unknown node kind {k!r}; one of {NODE_KINDS}",
                )

        async with deps.descriptor_registry.pg.acquire() as conn:
            state, meta = await projection_state(conn)
            if state != PROJECTION_OK:
                reason = {
                    "projection_disabled": (
                        "LEGBA_GRAPH_PROJECTION is off — the projection is "
                        "never built"
                    ),
                    "projection_empty": (
                        "the projection has never been built or holds zero arcs"
                    ),
                    "projection_stale": (
                        "projected_at is older than "
                        "LEGBA_GRAPH_PROJECTION_MAX_AGE_SECONDS"
                    ),
                }.get(state, "projection unreadable")
                raise _refuse(state, reason)

            args: list[Any] = []
            sql = _ego_sql(
                plane=plane,
                as_of=as_of,
                direction=direction,
                arc_types=arc_types,
                kinds=kinds,
                anchor=(node_kind, anchor_uuid),
                seeds=None,
                args=args,
            )
            rows = await conn.fetch(sql, *args)
            matched = len(rows)
            arcs = [
                _row_to_arc(r, (node_kind, str(anchor_uuid)), direction)
                for r in rows[:limit]
            ]
            truncated = matched > limit

            # Hop 2 (depth=2): the bounded walk — neighbours of the hop-1
            # nodes, same filters, capped at WALK_MAX_NODES seeds.
            if depth > 1 and arcs:
                seen: set[tuple[str, str]] = {(node_kind, str(anchor_uuid))}
                for a in arcs:
                    seen.add((a.from_kind, a.from_id))
                    seen.add((a.to_kind, a.to_id))
                seeds: list[tuple[str, UUID]] = []
                anchor_key = (node_kind, str(anchor_uuid))
                for k, i in seen:
                    if (k, i) != anchor_key and len(seeds) < WALK_MAX_NODES:
                        seeds.append((k, UUID(i)))
                if seeds:
                    args2: list[Any] = []
                    sql2 = _ego_sql(
                        plane=plane,
                        as_of=as_of,
                        direction=direction,
                        arc_types=arc_types,
                        kinds=kinds,
                        anchor=(node_kind, anchor_uuid),
                        seeds=seeds,
                        args=args2,
                    )
                    rows2 = await conn.fetch(sql2, *args2)
                    matched += len(rows2)
                    for r in rows2:
                        if len(arcs) >= limit:
                            break
                        arcs.append(
                            _row_to_arc(r, anchor_key, "walk")
                        )
                        seen.add((str(r["from_kind"]), str(r["from_id"])))
                        seen.add((str(r["to_kind"]), str(r["to_id"])))
                    truncated = truncated or len(rows2) > 0 and len(arcs) >= limit
            else:
                seen = {(node_kind, str(anchor_uuid))}
                for a in arcs:
                    seen.add((a.from_kind, a.from_id))
                    seen.add((a.to_kind, a.to_id))

            projected_at = meta.get("projected_at") if meta else None
            return GraphArcsResponse(
                anchor=NodeRef(kind=node_kind, id=str(anchor_uuid)),
                plane=plane,
                as_of=as_of,
                depth=depth,
                nodes=[NodeRef(kind=k, id=i) for k, i in sorted(seen)],
                arcs=arcs,
                matched=matched,
                truncated=truncated,
                projected_at=projected_at,
                age_seconds=meta.get("age_seconds") if meta else None,
            )

    return router


def _split_csv(values: list[str] | None) -> list[str]:
    """Repeatable-or-comma-separated query params → one flat list."""
    out: list[str] = []
    for v in values or []:
        out.extend(p.strip() for p in v.split(",") if p.strip())
    return out


__all__ = ["NODE_KINDS", "PLANES", "build_graph_arcs_router"]
