# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/evidence/drift*`` — the source-drift panel's data.

* ``/evidence/drift`` — classifications over a window (``window_hours``,
  default 168) by class and by source, flags by state, and the open total;
* ``/evidence/drift/flags`` — unresolved flags (``state`` = ``open`` |
  ``reviewed`` | ``unresolved``; ``source_id`` optional), newest first, each
  with its dependant, version pair, class, the cited span and whether it
  still appears, and the changed passages of its drift;
* ``/evidence/drift/history?address=`` — every version of one address with
  the classified change between consecutive ones;
* ``/evidence/drift/editors`` — the sources whose pages change in substance
  after we read them most (``window_hours``, default 720), each with its
  EDIT RECORD over 7 and 30 days (D-2: assessments by type, acknowledged vs
  silent, history rewrites, pages re-checked) — a metric, feeding no score;
* ``/evidence/drift/assessments`` (D-2) — the assessments of one change
  (``drift_id``), of the change behind one flag (``flag_id``) or of one
  address, each with its flags' claim effects.

D-2: ``/evidence/drift/flags`` also lists ``resolved`` flags, filters them by
who closed them (``closed_by`` = ``system`` for the drift assessor's tracked
auto-closes, ``other`` for a person or a unit), carries each flag's latest
assessment and claim effect, and sorts history rewrites first, spelling
corrections last; ``would_auto_close=true`` lists the flags the auto-close
rule would close (the shadow count, recorded with the switch off), and the
summary counts them.

Read-only. Defensive: migration 0252/0253 not applied → ``available: false`` at
HTTP 200. No UI in this lane.

Registry-slim: stdlib + fastapi + pydantic + ``.api`` + the stdlib-only
``evidence_drift_reads`` leaf. Nothing from ``legba.data.analysts`` or
``legba.runtime`` (a guard test probes it with the slim image's blocked
modules).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..evidence_drift_reads import (
    MAX_FLAGS,
    MAX_WINDOW_HOURS,
    assessments_for,
    drift_counts,
    editing_sources,
    open_flags,
    version_history,
)
from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

DRIFT_ROUTE_VERSION = "2026-11/d2"


class DriftSummaryOut(BaseModel):
    version: str = DRIFT_ROUTE_VERSION
    available: bool = False
    window_hours: int = 168
    classified: int = 0
    by_class: dict[str, int] = Field(default_factory=dict)
    by_source: dict[str, dict[str, int]] = Field(default_factory=dict)
    flags_by_source: dict[str, int] = Field(default_factory=dict)
    flags: dict[str, int] = Field(default_factory=dict)
    open_flags_total: int = 0
    assessments_by_type: dict[str, int] = Field(default_factory=dict)
    history_rewrites: int = 0
    auto_closed: dict[str, int] = Field(default_factory=dict)
    late_unexplained_changes: int = 0
    would_auto_close: dict[str, Any] = Field(default_factory=dict)


class DriftFlagsOut(BaseModel):
    version: str = DRIFT_ROUTE_VERSION
    available: bool = False
    state: str = "open"
    closed_by: str = "any"
    flags: list[dict[str, Any]] = Field(default_factory=list)


class DriftAssessmentsOut(BaseModel):
    version: str = DRIFT_ROUTE_VERSION
    available: bool = False
    assessments: list[dict[str, Any]] = Field(default_factory=list)


class DriftHistoryOut(BaseModel):
    version: str = DRIFT_ROUTE_VERSION
    available: bool = False
    address: str = ""
    versions: list[dict[str, Any]] = Field(default_factory=list)
    changes: list[dict[str, Any]] = Field(default_factory=list)
    unclassified_drift_rows: int = 0


class DriftEditorsOut(BaseModel):
    version: str = DRIFT_ROUTE_VERSION
    available: bool = False
    window_hours: int = 720
    sources: list[dict[str, Any]] = Field(default_factory=list)


def build_evidence_drift_router(deps: RegistryAPIDeps) -> APIRouter:
    """Read-only router (mounted at ``/api/v1/v3``)."""
    router = APIRouter(tags=["evidence"])

    def _pool(request: Request) -> Any:
        return getattr(request.app.state, "registry_deps", deps).descriptor_registry.pg

    async def _read(request: Request, fn: Any, **kw: Any) -> Any:
        try:
            async with _pool(request).acquire() as conn:
                return await fn(conn, **kw)
        except Exception as exc:  # noqa: BLE001 — degrade, HTTP 200
            logger.info("v3.evidence.drift.unavailable fn=%s err_class=%s",
                        getattr(fn, "__name__", "?"), type(exc).__name__)
            return None

    @router.get("/evidence/drift", response_model=DriftSummaryOut)
    async def evidence_drift_summary(
        request: Request,
        window_hours: int = Query(default=168, ge=1, le=MAX_WINDOW_HOURS),
        principal: str = Depends(require_bearer),
    ) -> DriftSummaryOut:
        counts = await _read(request, drift_counts, window_hours=window_hours)
        if counts is None:
            return DriftSummaryOut(window_hours=window_hours)
        return DriftSummaryOut(available=True, **counts)

    @router.get("/evidence/drift/flags", response_model=DriftFlagsOut)
    async def evidence_drift_flags(
        request: Request,
        state: str = Query(default="open", pattern="^(open|reviewed|unresolved|resolved)$"),
        source_id: Optional[str] = Query(default=None, max_length=256),
        closed_by: str = Query(default="any", pattern="^(any|system|other)$"),
        would_auto_close: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=MAX_FLAGS),
        principal: str = Depends(require_bearer),
    ) -> DriftFlagsOut:
        rows = await _read(request, open_flags, limit=limit, state=state, source_id=source_id,
                           closed_by=closed_by, would_auto_close=would_auto_close)
        if rows is None:
            return DriftFlagsOut(state=state, closed_by=closed_by)
        return DriftFlagsOut(available=True, state=state, closed_by=closed_by, flags=rows)

    @router.get("/evidence/drift/assessments", response_model=DriftAssessmentsOut)
    async def evidence_drift_assessments(
        request: Request,
        drift_id: Optional[int] = Query(default=None, ge=1),
        flag_id: Optional[int] = Query(default=None, ge=1),
        address: Optional[str] = Query(default=None, min_length=1, max_length=4096),
        limit: int = Query(default=20, ge=1, le=200),
        principal: str = Depends(require_bearer),
    ) -> DriftAssessmentsOut:
        given = [x for x in (drift_id, flag_id, address) if x is not None]
        if len(given) != 1:
            raise HTTPException(status_code=422, detail="give exactly one of drift_id, flag_id, address")
        canon = None
        if address is not None:
            from .._url_canon import canonical_url

            canon = canonical_url(address) or address
        body = await _read(request, assessments_for, drift_id=drift_id, flag_id=flag_id,
                           address=canon, limit=limit)
        if body is None:
            return DriftAssessmentsOut()
        return DriftAssessmentsOut(available=True, **body)

    @router.get("/evidence/drift/history", response_model=DriftHistoryOut)
    async def evidence_drift_history(
        request: Request,
        address: str = Query(..., min_length=1, max_length=4096),
        principal: str = Depends(require_bearer),
    ) -> DriftHistoryOut:
        from .._url_canon import canonical_url

        canon = canonical_url(address) or address
        body = await _read(request, version_history, address=canon)
        if body is None:
            return DriftHistoryOut(address=canon)
        return DriftHistoryOut(available=True, **body)

    @router.get("/evidence/drift/editors", response_model=DriftEditorsOut)
    async def evidence_drift_editors(
        request: Request,
        window_hours: int = Query(default=720, ge=1, le=MAX_WINDOW_HOURS),
        limit: int = Query(default=25, ge=1, le=200),
        principal: str = Depends(require_bearer),
    ) -> DriftEditorsOut:
        rows = await _read(request, editing_sources, window_hours=window_hours, limit=limit)
        if rows is None:
            return DriftEditorsOut(window_hours=window_hours)
        return DriftEditorsOut(available=True, window_hours=window_hours, sources=rows)

    return router
