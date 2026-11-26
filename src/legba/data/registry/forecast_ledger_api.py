# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/ledger/forecasts`` — the forecast ledger read.

The Ledger surface's Open tab: every exogenous (and stored endogenous)
forecast question, sorted by what resolves next — open questions first by
due date, then closed ones — each with every issuer's number (the operator,
the tower, the consult, the frontier and decision-model arms; an issuer that had no
number appears as an ABSENCE with its reason, never as a blank), the sealed
state, the due date and the active resolution. The Brier column is
computed from the resolution through the ``forecast_scores`` view.

Read-only; the default ``read`` role (no ``ROUTE_ROLES`` row). Fully
defensive (the receipt-anchors precedent): any query failure — including
migration 0242 not yet applied — degrades to an empty list at HTTP 200 with
``available: false``, so "no forecasts yet" and "the ledger cannot be read"
stay two different answers.

Registry-slim: stdlib + fastapi + pydantic + ``.api`` + the LEAF
``legba.data.forecast_ledger``. Nothing from ``legba.data.analysts`` or
``legba.runtime``.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..forecast_ledger import (
    ISSUER_KINDS,
    LEDGER_VERSION,
    RESOLVER_KINDS,
    fetch_ledger,
    fetch_ledger_as_of,
)
from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

FORECAST_LEDGER_ROUTE_VERSION = "2026-11/st0"

_ROUTE = "/ledger/forecasts"


class LedgerNumber(BaseModel):
    issuer_kind: str
    issuer_ref: str
    p: Optional[float] = None
    absent_reason: Optional[str] = None
    as_of: Optional[str] = None
    issued_at: Optional[str] = None
    rationale: Optional[str] = None
    rationale_ref: Optional[str] = None
    sealed: bool = False
    content_sha256: Optional[str] = None


class LedgerResolution(BaseModel):
    id: str
    outcome: str
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    evidence_refs: list[str] = Field(default_factory=list)
    early: bool = False
    note: Optional[str] = None


class LedgerForecast(BaseModel):
    id: str
    slug: str
    question: str
    question_class: Optional[str] = None
    resolver_kind: str
    resolver_spec: Any = None
    machine_resolvable: bool = False
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    due_at: str
    grace_hours: int = 0
    condition: Optional[str] = None
    void_rules: Optional[str] = None
    exogenous: bool = True
    subject_keys: list[str] = Field(default_factory=list)
    status: str = "open"
    sealed_at: Optional[str] = None
    content_sha256: Optional[str] = None
    overdue: bool = False
    numbers: list[LedgerNumber] = Field(default_factory=list)
    resolution: Optional[LedgerResolution] = None


class LedgerForecastsOut(BaseModel):
    version: str = FORECAST_LEDGER_ROUTE_VERSION
    ledger_version: str = LEDGER_VERSION
    #: False when the ledger could not be read (an empty list then means
    #: "not read", not "no forecasts").
    available: bool = True
    issuer_kinds: list[str] = Field(default_factory=lambda: list(ISSUER_KINDS))
    resolver_kinds: list[str] = Field(default_factory=lambda: list(RESOLVER_KINDS))
    forecasts: list[LedgerForecast] = Field(default_factory=list)
    #: Set on a read of the ledger as it stood (``?at=``): the instant read.
    at: Optional[str] = None
    #: On such a read, questions that existed then but were not yet sealed;
    #: their wording then is not kept, so they are counted, not shown.
    unsealed_then: int = 0


def build_forecast_ledger_router(deps: RegistryAPIDeps) -> APIRouter:
    """Build the read-only router (mounted at ``/api/v1/v3``)."""
    router = APIRouter(tags=["ledger"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(_ROUTE, response_model=LedgerForecastsOut)
    async def ledger_forecasts(
        request: Request,
        status: str = Query(default="all", pattern="^(all|open|resolved|void)$"),
        limit: int = Query(default=200, ge=1, le=1000),
        # The ledger as it stood at this instant (``forecast_ledger.fetch_ledger_as_of``).
        at: Optional[datetime] = Query(default=None),
        principal: str = Depends(require_bearer),
    ) -> LedgerForecastsOut:
        """The ledger, open first by due date. ``status`` filters."""
        d = _get_deps(request)
        unsealed = 0
        try:
            async with d.descriptor_registry.pg.acquire() as conn:
                if at is None:
                    rows = await fetch_ledger(conn, limit=limit)
                else:
                    rows, unsealed = await fetch_ledger_as_of(conn, at, limit=limit)
        except Exception as exc:  # noqa: BLE001 — degrade to [], HTTP 200
            logger.info("v3.ledger.forecasts.unavailable err_class=%s", type(exc).__name__)
            return LedgerForecastsOut(available=False)
        if status != "all":
            rows = [r for r in rows if r["status"] == status]
        return LedgerForecastsOut(
            forecasts=[LedgerForecast(**r) for r in rows],
            at=None if at is None else at.isoformat(),
            unsealed_then=unsealed,
        )

    return router
