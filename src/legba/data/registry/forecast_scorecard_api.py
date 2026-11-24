# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/ledger/scorecard`` — the forecast ledger's scorecard (ST1-A/B).

Every sealed question with its status, due date, each arm's number, the
spread between arms, the outcome and its evidence once resolved; from
``SCORES_MIN_RESOLVED`` (5) resolved questions Brier per number and the arm
table with n (below it every score is null and ``scores_withheld: true``), what is due next, and a plain header
(n resolved; no skill claim below the minimum sample). Before any question
resolves it still shows the sealed questions, the numbers, the spread and
"0 resolved". Payload: :func:`legba.data.forecast_scoring.build_scorecard`.

It reads only the sealed ledger and the 0244 resolution records — never the
legacy calibration figures (``separation_note`` says so on every response).

Read-only; bearer-gated like its siblings (``require_bearer``). Defensive:
the ledger unreadable ⇒ ``available: false`` at HTTP 200; the 0244 records
unreadable ⇒ the scorecard without proposals and ``records_available: false``.

Registry-slim: stdlib + fastapi + ``.api`` + LEAF data modules only.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

from ..forecast_ledger import LEDGER_VERSION, fetch_ledger
from ..forecast_resolution import latest_records
from ..forecast_scoring import SCORES_MIN_RESOLVED, SEPARATION_NOTE, build_scorecard
from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

FORECAST_SCORECARD_ROUTE_VERSION = "2026-11/st1"
_ROUTE = "/ledger/scorecard"


def build_forecast_scorecard_router(deps: RegistryAPIDeps) -> APIRouter:
    """Build the read-only router (mounted at ``/api/v1/v3``)."""
    router = APIRouter(tags=["ledger"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(_ROUTE)
    async def ledger_scorecard(
        request: Request,
        principal: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        d = _get_deps(request)
        base = {"version": FORECAST_SCORECARD_ROUTE_VERSION, "ledger_version": LEDGER_VERSION}
        try:
            async with d.descriptor_registry.pg.acquire() as conn:
                rows = await fetch_ledger(conn, limit=1000)
                try:
                    recs = await latest_records(conn, [r["id"] for r in rows])
                    recs_ok = True
                except Exception as exc:  # noqa: BLE001 — 0244 not applied yet
                    logger.info("v3.ledger.scorecard.records_unavailable err_class=%s", type(exc).__name__)
                    recs, recs_ok = {}, False
        except Exception as exc:  # noqa: BLE001 — degrade, HTTP 200
            logger.info("v3.ledger.scorecard.unavailable err_class=%s", type(exc).__name__)
            return {**base, "available": False, "separation_note": SEPARATION_NOTE, "questions": [],
                    "arms": [], "single_arm": [], "due_next": [], "void_questions": [],
                    "header": None, "calibration": None, "scores_withheld": True,
                    "scores_min_resolved": SCORES_MIN_RESOLVED}
        return {**base, "available": True, "records_available": recs_ok, **build_scorecard(rows, recs)}

    return router
