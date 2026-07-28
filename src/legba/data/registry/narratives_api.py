# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reified-narratives read surface (P4-1 + P4-2; A11).

Three read-only endpoints over the migration-0102 derived tables the
``narrative_mapper`` deterministic analyst refreshes:

  * ``GET /narratives`` — the reified contested-claim families (carrier sources,
    first-seen / echo lags, propagation ordering), most-recent activity first.
  * ``GET /narratives/echo`` — the directed source-echo graph edges
    (leader -> follower co-carriage + lag), systematic echoes first.
  * ``GET /narratives/{contention_id}`` — one narrative's full detail.

Mounted under ``/api/v1/v3`` beside the telemetry / assurance routers (see
``server.py``); full paths are ``/api/v1/v3/narratives`` etc. Same
``RegistryAPIDeps`` bundle + ``require_bearer`` gate as the rest of the surface.
This is the data surface for a FUTURE UI narratives panel — no UI is built here.

HONESTY (echoed onto every envelope so a client cannot render it as more than it
is): narratives are DETECT-ONLY reifications of contested-claim families and
never mutate facts; echo-lead is DESCRIPTIVE co-carriage timing (who published
first, who followed within the window), computed only from publish-dated
carriage — NOT a causal or coordination claim.

Degrade-not-500 (the source-assurance precedent): every read wraps the fetch in
``try/except asyncpg.UndefinedTableError`` and returns an empty envelope, so a
registry rolled forward ahead of migration 0102 serves an honest empty list
rather than a 500.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

#: The honesty contract carried verbatim on every envelope (machine-checkable).
HONESTY_NOTE = (
    "Narratives are DETECT-ONLY reifications of contested-claim families and "
    "never mutate facts. Echo-lead is DESCRIPTIVE co-carriage timing (who "
    "published first, who followed within the window), computed only from "
    "publish-dated carriage — NOT a causal or coordination claim."
)


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class NarrativeOut(BaseModel):
    """One reified contested-claim family (a ``narratives`` row)."""

    contention_id: str
    subject_key: str
    predicate_key: str
    status: str
    surfaced_value: str | None = None
    variant_count: int
    carrier_source_count: int
    publish_dated_source_count: int
    signal_count: int
    fact_count: int
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    span_hours: float | None = None
    lead_source_id: str | None = None
    lead_first_seen_at: datetime | None = None
    max_echo_lag_hours: float | None = None
    #: Ordered per-source carriage detail (lead first). See narrative_mapper.py.
    carriers: list[dict[str, Any]] = Field(default_factory=list)
    #: Per value-cluster (variant) detail.
    variants: list[dict[str, Any]] = Field(default_factory=list)
    opened_at: datetime | None = None
    contention_surfaced_at: datetime | None = None
    computed_at: datetime | None = None


class NarrativeListOut(BaseModel):
    """``GET /narratives`` envelope."""

    narratives: list[NarrativeOut] = Field(default_factory=list)
    count: int = 0
    honesty_note: str = HONESTY_NOTE


class PropagationEdgeOut(BaseModel):
    """One directed source-echo edge (a ``narrative_echo_edges`` row)."""

    leader_source_id: str
    follower_source_id: str
    co_carried: int
    lead_count: int
    follow_within_count: int
    echo_ratio: float | None = None
    median_lag_hours: float | None = None
    mean_lag_hours: float | None = None
    min_lag_hours: float | None = None
    max_lag_hours: float | None = None
    echo_window_hours: float
    systematic: bool
    computed_at: datetime | None = None


class PropagationGraphOut(BaseModel):
    """``GET /narratives/echo`` envelope."""

    edges: list[PropagationEdgeOut] = Field(default_factory=list)
    count: int = 0
    honesty_note: str = HONESTY_NOTE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_deps(request: Request) -> RegistryAPIDeps:
    deps = getattr(request.app.state, "registry_deps", None)
    if deps is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="narratives api not configured (missing RegistryAPIDeps on app.state)",
        )
    return deps


def _as_list(value: Any) -> list[dict[str, Any]]:
    """jsonb fetch -> Python list (codec-registered pools already decode)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    return value if isinstance(value, list) else []


def _narrative_out(row: Any) -> NarrativeOut:
    return NarrativeOut(
        contention_id=str(row["contention_id"]),
        subject_key=row["subject_key"],
        predicate_key=row["predicate_key"],
        status=row["status"],
        surfaced_value=row["surfaced_value"],
        variant_count=row["variant_count"],
        carrier_source_count=row["carrier_source_count"],
        publish_dated_source_count=row["publish_dated_source_count"],
        signal_count=row["signal_count"],
        fact_count=row["fact_count"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        span_hours=row["span_hours"],
        lead_source_id=row["lead_source_id"],
        lead_first_seen_at=row["lead_first_seen_at"],
        max_echo_lag_hours=row["max_echo_lag_hours"],
        carriers=_as_list(row["carriers"]),
        variants=_as_list(row["variants"]),
        opened_at=row["opened_at"],
        contention_surfaced_at=row["contention_surfaced_at"],
        computed_at=row["computed_at"],
    )


def _edge_out(row: Any) -> PropagationEdgeOut:
    return PropagationEdgeOut(
        leader_source_id=row["leader_source_id"],
        follower_source_id=row["follower_source_id"],
        co_carried=row["co_carried"],
        lead_count=row["lead_count"],
        follow_within_count=row["follow_within_count"],
        echo_ratio=row["echo_ratio"],
        median_lag_hours=row["median_lag_hours"],
        mean_lag_hours=row["mean_lag_hours"],
        min_lag_hours=row["min_lag_hours"],
        max_lag_hours=row["max_lag_hours"],
        echo_window_hours=row["echo_window_hours"],
        systematic=row["systematic"],
        computed_at=row["computed_at"],
    )


_NARRATIVE_COLS = """
    contention_id, subject_key, predicate_key, status, surfaced_value,
    variant_count, carrier_source_count, publish_dated_source_count,
    signal_count, fact_count, first_seen_at, last_seen_at, span_hours,
    lead_source_id, lead_first_seen_at, max_echo_lag_hours, carriers, variants,
    opened_at, contention_surfaced_at, computed_at
"""

_EDGE_COLS = """
    leader_source_id, follower_source_id, co_carried, lead_count,
    follow_within_count, echo_ratio, median_lag_hours, mean_lag_hours,
    min_lag_hours, max_lag_hours, echo_window_hours, systematic, computed_at
"""


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_narratives_router(deps: RegistryAPIDeps) -> APIRouter:
    router = APIRouter(tags=["narratives"])

    @router.get("/narratives", response_model=NarrativeListOut)
    async def list_narratives(
        status_filter: str | None = Query(
            default=None,
            alias="status",
            description="Filter by contention status (contested | surfaced).",
        ),
        min_carriers: int = Query(
            default=1, ge=1, description="Minimum distinct carrier sources."
        ),
        limit: int = Query(default=50, ge=1, le=200),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> NarrativeListOut:
        pg = deps_.descriptor_registry.pg
        sql = (
            f"SELECT {_NARRATIVE_COLS} FROM narratives "
            "WHERE carrier_source_count >= $1 "
            "AND ($2::text IS NULL OR status = $2::text) "
            "ORDER BY last_seen_at DESC NULLS LAST, contention_id "
            "LIMIT $3"
        )
        try:
            async with pg.acquire() as conn:
                rows = await conn.fetch(sql, min_carriers, status_filter, limit)
        except asyncpg.UndefinedTableError:
            logger.warning(
                "narratives unavailable: table missing (migration 0102 not "
                "applied) — serving empty list",
            )
            return NarrativeListOut(narratives=[], count=0)
        out = [_narrative_out(r) for r in rows]
        return NarrativeListOut(narratives=out, count=len(out))

    @router.get("/narratives/echo", response_model=PropagationGraphOut)
    async def echo_graph(
        systematic_only: bool = Query(
            default=False,
            description="Only edges flagged systematic (co-carriage + ratio floor).",
        ),
        leader: str | None = Query(
            default=None, description="Filter to out-edges from this source."
        ),
        follower: str | None = Query(
            default=None, description="Filter to in-edges to this source."
        ),
        limit: int = Query(default=100, ge=1, le=500),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> PropagationGraphOut:
        pg = deps_.descriptor_registry.pg
        sql = (
            f"SELECT {_EDGE_COLS} FROM narrative_echo_edges "
            "WHERE (NOT $1::bool OR systematic) "
            "AND ($2::text IS NULL OR leader_source_id = $2::text) "
            "AND ($3::text IS NULL OR follower_source_id = $3::text) "
            "ORDER BY systematic DESC, echo_ratio DESC NULLS LAST, "
            "co_carried DESC, leader_source_id, follower_source_id "
            "LIMIT $4"
        )
        try:
            async with pg.acquire() as conn:
                rows = await conn.fetch(sql, systematic_only, leader, follower, limit)
        except asyncpg.UndefinedTableError:
            logger.warning(
                "echo graph unavailable: narrative_echo_edges missing "
                "(migration 0102 not applied) — serving empty list",
            )
            return PropagationGraphOut(edges=[], count=0)
        out = [_edge_out(r) for r in rows]
        return PropagationGraphOut(edges=out, count=len(out))

    @router.get("/narratives/{contention_id}", response_model=NarrativeOut)
    async def get_narrative(
        contention_id: str,
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> NarrativeOut:
        pg = deps_.descriptor_registry.pg
        try:
            cid = _uuid(contention_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="contention_id must be a uuid")
        sql = f"SELECT {_NARRATIVE_COLS} FROM narratives WHERE contention_id = $1"
        try:
            async with pg.acquire() as conn:
                row = await conn.fetchrow(sql, cid)
        except asyncpg.UndefinedTableError:
            raise HTTPException(
                status_code=404,
                detail="narratives table not available (migration 0102 not applied)",
            )
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"no narrative for contention {contention_id!r}"
            )
        return _narrative_out(row)

    return router


def _uuid(raw: str) -> Any:
    from uuid import UUID

    return UUID(str(raw))
