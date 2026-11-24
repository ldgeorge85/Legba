# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/eval/runs`` — the published evaluation record (E-3; SEAMS #89 resolved).

Every version of every evaluation run the owner published with
``scripts/publish_eval_run.py`` (migration 0260), newest as-of first: the
arms in plain words, each arm's score on the run's primary measure with its
interval and n and its per-class scores, the caveats and the departures from
the pre-registered design, the grader, the temperature and the gold
version(s). A corrected run is listed in all its versions — the older one
``status: "superseded"`` with ``superseded_by`` — and a withdrawn version is
listed ``status: "withdrawn"`` with its reason, never dropped. No question,
gold answer or model answer exists to serve: the tables hold aggregates only.

THE EMPTY STATE. Nothing published is HTTP 200 with ``runs: []`` and
``available: true`` — the console's designed "No evaluation published yet".
The tables unreadable (0260 not applied, or any read failure) is HTTP 200
with ``available: false`` — "could not be read", never "none published".

Read-only, bearer-gated like the other ledger routes; the default ``read``
role (an unlisted GET in ``route_roles.ROUTE_ROLES`` is ``read`` — the table
lists only writes and non-read GETs), so every signed-in reader of this
instance may read it and no unauthenticated caller can (it is not in
``PUBLIC_ROUTES``). Bounded: the ``idx_eval_runs_as_of`` index for the
runs (at most ``limit``), then the arms by the ``(run_id, position)``
unique index.

Registry-slim: stdlib + fastapi + pydantic + ``.api`` + the LEAF
``legba.data.eval_runs``.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..eval_runs import fetch_runs
from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

EVAL_RUNS_ROUTE_VERSION = "2026-11/e3"
_ROUTE = "/eval/runs"


class EvalDesign(BaseModel):
    preregistered_at: Optional[str] = None
    prereg_sha256: Optional[str] = None
    version: Optional[str] = None


class EvalArm(BaseModel):
    id: str
    label: str
    description: Optional[str] = None
    sample_of: Optional[str] = None
    sample_index: Optional[int] = None


class EvalClassScore(BaseModel):
    model_config = {"populate_by_name": True}
    klass: str = Field(alias="class")
    score: Optional[float] = None
    n: int


class EvalResult(BaseModel):
    arm: str
    metric: str
    metric_label: Optional[str] = None
    score: Optional[float] = None
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None
    ci_level: Optional[float] = None
    n: int
    higher_is_better: Optional[bool] = None
    by_class: list[EvalClassScore] = Field(default_factory=list)


class EvalQuestionClass(BaseModel):
    model_config = {"populate_by_name": True}
    klass: str = Field(alias="class")
    n: int


class EvalRun(BaseModel):
    id: str
    run_key: str
    version: int
    #: current | superseded | withdrawn
    status: str
    supersedes: Optional[str] = None
    superseded_by: Optional[str] = None
    correction_reason: Optional[str] = None
    withdrawn_at: Optional[str] = None
    withdrawn_reason: Optional[str] = None
    title: str
    question: Optional[str] = None
    as_of: Optional[str] = None
    published_at: Optional[str] = None
    design: EvalDesign
    question_count: int
    question_classes: list[EvalQuestionClass] = Field(default_factory=list)
    grader: str
    temperature: float
    gold_versions: list[str] = Field(default_factory=list)
    arms: list[EvalArm] = Field(default_factory=list)
    results: list[EvalResult] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    deviations: list[str] = Field(default_factory=list)
    content_sha256: str


class EvalRunsOut(BaseModel):
    version: str = EVAL_RUNS_ROUTE_VERSION
    #: False when the record could not be read (an empty list then means
    #: "not read", not "none published").
    available: bool = True
    runs: list[EvalRun] = Field(default_factory=list)


def build_eval_runs_router(deps: RegistryAPIDeps) -> APIRouter:
    """Build the read-only router (mounted at ``/api/v1/v3``)."""
    router = APIRouter(tags=["eval"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(_ROUTE, response_model=EvalRunsOut, response_model_by_alias=True)
    async def eval_runs(
        request: Request,
        limit: int = Query(default=200, ge=1, le=500),
        principal: str = Depends(require_bearer),
    ) -> Any:
        """Every published evaluation run version, newest as-of first."""
        d = _get_deps(request)
        try:
            async with d.descriptor_registry.pg.acquire() as conn:
                runs = await fetch_runs(conn, limit=limit)
        except Exception as exc:  # noqa: BLE001 — degrade to available:false, HTTP 200
            logger.info("v3.eval.runs.unavailable err_class=%s", type(exc).__name__)
            return EvalRunsOut(available=False)
        return EvalRunsOut(runs=[EvalRun.model_validate(r) for r in runs])

    return router
