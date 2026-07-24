# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deep-consult submit + status endpoint — anchor §5 PIECE 4.

Mounts under ``/api/v1/deep_consult``. Built via
``build_deep_consult_router(deps)``; ``server.py`` wires it alongside the
consult router.

Why this exists
===============

The on-demand consult endpoint (``consult_api.py``) is the BLOCKING path — the
dapr sidecar holds the HTTP connection open until the analyst actor finishes
(180s cap). Deep consult is the DETACHED variant: submit returns a task id
IMMEDIATELY, the staged Dapr Workflow (plan → acquire → analyze → synthesize)
runs on the WorkflowRuntime worker (minutes → hours), and status is polled.

How submit stays detached
=========================

Like ``consult_api`` the registry has NO Dapr sidecar of its own, so it cannot
open the workflow engine's gRPC channel directly. It HTTP-invokes the
``deep_consult`` analyst actor over the RUNTIME's dapr sidecar (the same
``http://dapr-sidecar:3500`` consult uses). The actor's ``run_method`` SCHEDULES
the workflow and returns the task id WITHOUT awaiting completion (the actor's
``deep_consult`` short-circuit in ``dapr_actors.py``), so the invoke returns in
<1s — the budget envelope is preserved INSIDE the analyze stage, not by pinning
the HTTP connection.

Status polling reads the produced FINDING row back from Postgres (the registry
owns the pool) keyed by the workflow's ``run_id``: a row present → ``completed``
with the finding id; absent → ``running``. The finding's ``derived_from`` makes
it lineage-walkable via the existing lineage route.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from . import consult_persistence
from .consult_api import resolve_consult_model_override
from .api import RegistryAPIDeps, require_bearer
from .descriptor import Family
from .errors import DescriptorNotFound
from .rate_limit import DEEP_CONSULT_RATE_LIMIT
from .rate_limit import limiter as _limiter

logger = logging.getLogger(__name__)


DEEP_CONSULT_ANALYST_ID = os.getenv("LEGBA_DEEP_CONSULT_ANALYST_ID", "deep_consult")
ACTOR_TYPE = "AnalystActor"
DESCRIPTOR_KIND = "analyst"
DAPR_SIDECAR_URL_ENV = "LEGBA_DAPR_SIDECAR_URL"
DAPR_SIDECAR_URL_DEFAULT = "http://dapr-sidecar:3500"
# The submit invoke is detached — the actor schedules + returns immediately, so
# a short timeout suffices (it is NOT the 180s blocking consult path).
DAPR_SUBMIT_TIMEOUT_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


class DeepConsultRequest(BaseModel):
    """Inbound submit shape."""

    question: str = Field(min_length=1, max_length=8192)
    scope_predicate: str | None = Field(default=None, max_length=2048)
    emit_facts: bool = True
    emit_hypotheses: bool = True
    # F1 model picker — which registered LLM plane runs the deep workflow's
    # plan/analyze stages. None / absent ⇒ "opus" (the billed Anthropic Opus
    # plane, the default). "core" routes to the free self-hosted core plane.
    # Mapped friendly→component id server-side off the SAME allowlist as chat.
    model: Literal["opus", "core"] | None = None


class DeepConsultSubmitResponse(BaseModel):
    """202 submit response — the task id the client polls."""

    task_id: str
    status: str = "running"
    run_id: str | None = None


class DeepConsultStatusResponse(BaseModel):
    """Status-poll response."""

    task_id: str
    status: str  # running | completed | failed | unknown
    finding_id: str | None = None
    answer: str | None = None
    uncertainty: float | None = None
    cited_refs: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)
    hypothesis_ids: list[str] = Field(default_factory=list)
    detail: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dapr_sidecar_url() -> str:
    return os.getenv(DAPR_SIDECAR_URL_ENV, DAPR_SIDECAR_URL_DEFAULT).strip().rstrip(
        "/"
    ) or DAPR_SIDECAR_URL_DEFAULT


def _build_actor_id(version: str) -> str:
    """Match ``runtime.reconcile._default_actor_id``: ``kind::id::ver[:16]``."""
    short = (version or "")[:16] or "0" * 16
    return f"{DESCRIPTOR_KIND}::{DEEP_CONSULT_ANALYST_ID}::{short}"


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_deep_consult_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the deep-consult router bound to the registry deps.

    Mount on a FastAPI app via::

        app.include_router(build_deep_consult_router(deps), prefix="/api/v1")
    """
    router = APIRouter(tags=["deep_consult"])
    pg = deps.descriptor_registry.pg

    @router.post(
        "/deep_consult",
        response_model=DeepConsultSubmitResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    @_limiter.limit(DEEP_CONSULT_RATE_LIMIT)
    async def submit_deep_consult(
        request: Request,
        response: Response,
        body: DeepConsultRequest,
        _principal: str = Depends(require_bearer),
    ) -> DeepConsultSubmitResponse:
        """Submit a deep consult — schedules the workflow, returns the task id.

        Detached: the actor schedules the durable workflow and returns the task
        id immediately (the actor's ``deep_consult`` short-circuit), so this
        POST returns 202 in <1s — NOT the 180s blocking consult path.
        """
        # 1. Resolve the deep_consult analyst head version (provenance identity).
        try:
            row = await deps.descriptor_registry.get(
                DEEP_CONSULT_ANALYST_ID, family=Family.ANALYST,
            )
        except DescriptorNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"deep_consult analyst {DEEP_CONSULT_ANALYST_ID!r} is not "
                    f"registered; activate it first via bringup."
                ),
            ) from exc

        actor_id = _build_actor_id(row.version)
        sidecar_url = _dapr_sidecar_url()
        invoke_url = (
            f"{sidecar_url}/v1.0/actors/{ACTOR_TYPE}/{actor_id}/method/run"
        )
        run_id = str(uuid4())
        # F1 model picker: thread the plane override (None for the Opus default,
        # so the deep run keeps the descriptor primary unchanged). The deep_consult
        # kind validates it against the same allowlist and stamps it into the
        # workflow input, so the workflow's stage deps + budget key off the plane.
        _chosen_model, llm_component_override = resolve_consult_model_override(
            body.model,
        )
        first_input: dict[str, Any] = {
            "question": body.question,
            "scope_predicate": body.scope_predicate,
        }
        if llm_component_override is not None:
            first_input["llm_component_override"] = llm_component_override
        invoke_body = {
            "trigger_kind": "method",
            "inputs": [first_input],
            "options": {
                "run_id": run_id,
                "submitted_by": _principal,
            },
        }

        logger.info(
            "deep_consult.submit actor_id=%s descriptor_version=%s run_id=%s "
            "question_len=%d",
            actor_id, row.version[:16], run_id, len(body.question),
        )

        # 2. PUT to the dapr sidecar. The actor schedules the workflow and
        #    returns immediately (detached) — short timeout suffices.
        try:
            async with httpx.AsyncClient(
                timeout=DAPR_SUBMIT_TIMEOUT_SECONDS,
            ) as client:
                dapr_response = await client.put(
                    invoke_url,
                    json=invoke_body,
                    headers={"Content-Type": "application/json"},
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "deep_consult.submit.transport actor_id=%s err=%s", actor_id, exc,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"dapr sidecar unreachable at {sidecar_url}: {exc}",
            ) from exc

        if dapr_response.status_code >= 400:
            logger.warning(
                "deep_consult.submit.bad_status actor_id=%s status=%d body=%s",
                actor_id, dapr_response.status_code, dapr_response.text[:512],
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"dapr actor invoke returned {dapr_response.status_code}: "
                    f"{dapr_response.text[:512]}"
                ),
            )

        try:
            actor_result = dapr_response.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"dapr actor returned non-JSON body: {exc}",
            ) from exc

        if not isinstance(actor_result, dict):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"dapr actor returned unexpected shape: {actor_result!r}",
            )

        outcome = actor_result.get("outcome")
        if outcome != "success":
            detail = {
                "outcome": outcome,
                "error": actor_result.get("error"),
                "reason": actor_result.get("reason"),
            }
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=detail,
            )

        task_id = actor_result.get("task_id")
        if not task_id:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"deep_consult actor success envelope missing task_id: "
                    f"{actor_result!r}"
                ),
            )

        resolved_run_id = str(actor_result.get("run_id") or run_id)

        # Audit trail (0038): record the deep-consult task as a 'deep' session
        # correlated by task_id + run_id, and log the asked question as the user
        # turn. The synthesised answer is appended when the status poll first
        # observes completion. Best-effort — never blocks the submit.
        session_id = await consult_persistence.create_session(
            pg,
            mode="deep",
            question=body.question,
            principal=_principal,
            task_id=str(task_id),
            run_id=resolved_run_id,
        )
        if session_id:
            await consult_persistence.append_turn(
                pg,
                session_id=session_id,
                role="user",
                content=body.question,
            )

        return DeepConsultSubmitResponse(
            task_id=str(task_id),
            status=str(actor_result.get("status") or "running"),
            run_id=resolved_run_id,
        )

    @router.get(
        "/deep_consult/{task_id}",
        response_model=DeepConsultStatusResponse,
        status_code=status.HTTP_200_OK,
    )
    async def deep_consult_status(
        task_id: str,
        _principal: str = Depends(require_bearer),
    ) -> DeepConsultStatusResponse:
        """Poll deep-consult status.

        The task id encodes the workflow instance id; the workflow stamps its
        ``run_id`` (the trailing 8 hex chars of the run_id) into the instance
        id, and the synthesize stage writes the finding under that run_id. We
        read the produced FINDING row from Postgres keyed by the run_id prefix:
        a row present → ``completed`` with the finding + lineage; absent →
        ``running``. (The registry has no workflow-engine gRPC channel; the
        finding row is the authoritative completion signal.)
        """
        # The instance id grammar is ``deep_consult.<scope>.<run8>`` where
        # ``run8`` is the run_id's first 8 hex chars (no dashes). Match the
        # produced finding row by that prefix on its run_id.
        run8 = task_id.rsplit(".", 1)[-1] if "." in task_id else task_id
        run8 = "".join(c for c in run8 if c in "0123456789abcdefABCDEF")[:8]
        if not run8:
            return DeepConsultStatusResponse(
                task_id=task_id, status="unknown", detail="unparseable_task_id",
            )

        async with deps.descriptor_registry.pg.acquire() as conn:
            output_row = await conn.fetchrow(
                """
                SELECT id, body, data, derived_from
                  FROM analyst_outputs
                 WHERE kind = 'finding'
                   AND analyst_id = $1
                   AND replace(run_id::text, '-', '') LIKE $2
                 ORDER BY produced_at DESC
                 LIMIT 1
                """,
                DEEP_CONSULT_ANALYST_ID,
                run8 + "%",
            )

        if output_row is None:
            return DeepConsultStatusResponse(task_id=task_id, status="running")

        data_blob = output_row["data"]
        if isinstance(data_blob, str):
            try:
                data_blob = json.loads(data_blob)
            except (ValueError, TypeError):
                data_blob = {}
        if not isinstance(data_blob, dict):
            data_blob = {}
        deep_blob = data_blob.get("deep_consult") or {}
        uncertainty = deep_blob.get("uncertainty")
        try:
            uncertainty = float(uncertainty) if uncertainty is not None else None
        except (TypeError, ValueError):
            uncertainty = None

        derived = output_row["derived_from"] or []
        cited_refs = [str(d) for d in derived]

        # Audit trail (0038): on the FIRST poll that observes completion, append
        # the synthesised answer as the assistant turn of the deep session.
        # Idempotent (at-most-once per session) so repeated polls don't dup it;
        # best-effort so the poll never fails on an audit write.
        await consult_persistence.record_deep_completion(
            pg,
            task_id=task_id,
            answer=output_row["body"] or "",
            cited_refs=cited_refs,
            finding_id=str(output_row["id"]),
        )

        return DeepConsultStatusResponse(
            task_id=task_id,
            status="completed",
            finding_id=str(output_row["id"]),
            answer=output_row["body"],
            uncertainty=uncertainty,
            cited_refs=cited_refs,
        )

    return router


__all__ = [
    "DEEP_CONSULT_ANALYST_ID",
    "DeepConsultRequest",
    "DeepConsultStatusResponse",
    "DeepConsultSubmitResponse",
    "build_deep_consult_router",
]
