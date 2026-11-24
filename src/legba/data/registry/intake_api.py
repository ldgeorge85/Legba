# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/ingestion/intake`` — what the ingestion intake is doing.

Queue depth by state and by reason, refusals by reason, requests served from
a stored copy, the busiest hosts (requests, pending, claims in the last hour,
last claim) and the oldest queued request's age, over a window of request
creation (``window_hours``, default 24, at most 168; pending requests are
counted whatever their age). The counting is
:func:`legba.data.intake_visibility.intake_counts`, shared with the
``get_source_health`` read.

Defensive: migration 0249 not applied yet → ``available: false`` at HTTP
200, never zeros. No UI in this lane.

Registry-slim: stdlib + fastapi + pydantic + ``.api`` + the stdlib-only
``intake_visibility`` leaf. Nothing from ``legba.data.analysts`` or
``legba.runtime`` (a guard test probes it with the slim image's blocked
modules).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..intake_visibility import MAX_WINDOW_HOURS, intake_counts
from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

INTAKE_ROUTE_VERSION = "2026-11/i1"
_ROUTE = "/ingestion/intake"


class IntakeStatusOut(BaseModel):
    version: str = INTAKE_ROUTE_VERSION
    available: bool = False
    window_hours: int = 24
    queue: dict[str, int] = Field(default_factory=dict)
    by_reason: dict[str, dict[str, int]] = Field(default_factory=dict)
    refusals: dict[str, int] = Field(default_factory=dict)
    satisfied: dict[str, int] = Field(default_factory=dict)
    hosts: list[dict[str, Any]] = Field(default_factory=list)
    oldest_queued_age_s: Optional[float] = None


def build_intake_router(deps: RegistryAPIDeps) -> APIRouter:
    """Read-only router (mounted at ``/api/v1/v3``)."""
    router = APIRouter(tags=["ingestion"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(_ROUTE, response_model=IntakeStatusOut)
    async def ingestion_intake_status(
        request: Request,
        window_hours: int = Query(default=24, ge=1, le=MAX_WINDOW_HOURS),
        principal: str = Depends(require_bearer),
    ) -> IntakeStatusOut:
        d = _get_deps(request)
        try:
            async with d.descriptor_registry.pg.acquire() as conn:
                counts = await intake_counts(conn, window_hours=window_hours)
        except Exception as exc:  # noqa: BLE001 — degrade, HTTP 200
            logger.info("v3.ingestion.intake.unavailable err_class=%s", type(exc).__name__)
            return IntakeStatusOut(window_hours=window_hours)
        if counts is None:
            return IntakeStatusOut(window_hours=window_hours)
        return IntakeStatusOut(available=True, **counts)

    return router
