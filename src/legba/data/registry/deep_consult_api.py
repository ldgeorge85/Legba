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

Owner check (X4 GATE, W11-F76)
==============================

A signed-in user reads only the status of a task THEY submitted
(:func:`require_deep_task_owner`): the task's ``deep`` session — matched on the
EXACT ``task_id`` — must carry their principal. The status read itself matches
the finding on the task id's 8-hex run prefix, so a looser match here would let
a variant spelling of someone else's task id through; no session for the exact
id is a refusal, not a pass. The operator, the fleet and dev mode pass.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, model_validator

from ..consult_on_screen import OnScreen
from ..pinned_context import (
    MAX_PINNED_RECORDS,
    MAX_PINNED_TOTAL_CHARS,
    pinned_context_chars,
)
from . import consult_persistence
from . import consult_turn_stamps as _turn_stamps
from . import session_auth as _session_auth
from .consult_api import CONSULT_MODEL_ALLOWLIST, PinnedRef, resolve_consult_model_override
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
    # plan/analyze stages. None / absent ⇒ "core" (the self-hosted core
    # component; plan and analyze think, extraction does not — T-1). "opus" and
    # "fable" route to the billed Anthropic planes, ONLY when picked.
    # Mapped friendly→component id server-side off the SAME allowlist as chat
    # (``resolve_consult_model_override`` / ``CONSULT_MODEL_ALLOWLIST`` in
    # ``consult_api.py``) — this Literal must stay a superset-free mirror of
    # that allowlist's keys, since the Deep Consult UI shares the SAME
    # `CONSULT_MODEL_OPTIONS` dropdown as chat (`lib/api.ts`): an option this
    # Literal doesn't accept would 422 the moment it's picked here.
    model: Literal["opus", "fable", "core"] | None = None
    # Records the operator pinned to the conversation — the SAME field name,
    # caps and entry shape the chat front door takes (``consult_api.PinnedRef``)
    # so a Chat→Deep handoff carries the operator's context instead of
    # dropping it on the floor. Absent / [] ⇒ no ``pinned_context`` key on the
    # actor's input row, keeping a pre-pin client's submit byte-identical.
    pinned_context: list[PinnedRef] = Field(
        default_factory=list, max_length=MAX_PINNED_RECORDS,
    )
    # Q-1 — the turn contract's inputs, the chat front door's own fields.
    window: dict[str, Any] | None = Field(default=None, max_length=4)
    grounding_mode: Literal["record_only", "default"] | None = None
    on_screen: OnScreen | None = None

    @model_validator(mode="after")
    def _bounded_pinned_context(self) -> DeepConsultRequest:
        """Cap the pin set's TOTAL size — the twin of ``ConsultRequest``'s."""
        used = pinned_context_chars(
            [p.model_dump() for p in self.pinned_context],
        )
        if used > MAX_PINNED_TOTAL_CHARS:
            raise ValueError(
                f"pinned_context is {used} chars; the cap is "
                f"{MAX_PINNED_TOTAL_CHARS}"
            )
        return self


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
    # Q-2 — the deep turn's contract (``consult_contract/1``) and its
    # settle-point verdict, as the analyze stage settled them; ``None`` for a
    # finding written before Q-2.
    contract: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None


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


#: The owner of a deep-consult task: its ``deep`` session, on the exact task id
#: (``idx_consult_sessions_task``, a partial btree on ``task_id``).
_TASK_OWNER_SQL = (
    "SELECT principal FROM consult_sessions "
    "WHERE task_id = $1 AND mode = 'deep' LIMIT 1"
)


async def require_deep_task_owner(pg: Any, request: Request, task_id: str) -> None:
    """X4 GATE (W11-F76): refuse (403 ``not_row_owner``) a signed-in user on a
    deep-consult task they did not submit (module docstring, "Owner check").

    Unconfined callers (``session_auth.consult_owner_filter`` is ``None``: the
    operator, the fleet, dev mode) pass without a read. A task with no session
    for its exact id is refused for a confined caller — the owner cannot be
    established — and a store error is a 503, never a pass.
    """
    owner = _session_auth.consult_owner_filter(request)
    if owner is None:
        return
    try:
        async with pg.acquire() as conn:
            principal = await conn.fetchval(_TASK_OWNER_SQL, task_id)
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning("deep_consult.owner_check_failed err=%s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not check who submitted this deep-consult task",
        ) from exc
    if principal != owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "not_row_owner",
                "message": f"deep-consult task {task_id} belongs to another user",
            },
        )


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
        # F1 model picker: thread the plane. The deep_consult kind validates it
        # against the same allowlist and stamps it into the workflow input, so
        # the workflow's stage deps + budget key off the plane. Roll 84 (the
        # owner: "there is no paid deep... it's just the same model"): the
        # DEFAULT is threaded too — the core component, explicitly — so a deep
        # run that names no model runs on core whatever the descriptor head's
        # primary says; a paid plane runs only when the request picks it.
        _chosen_model, llm_component_override = resolve_consult_model_override(
            body.model,
        )
        if llm_component_override is None:
            llm_component_override = CONSULT_MODEL_ALLOWLIST[_chosen_model]
        first_input: dict[str, Any] = {
            "question": body.question,
            "scope_predicate": body.scope_predicate,
        }
        if llm_component_override is not None:
            first_input["llm_component_override"] = llm_component_override
        pinned_context = [p.model_dump() for p in body.pinned_context]
        if pinned_context:
            first_input["pinned_context"] = pinned_context
        for _k, _v in (("window", body.window), ("grounding_mode", body.grounding_mode),
                       ("on_screen", body.on_screen.wire() if body.on_screen else None)):
            if _v is not None:
                first_input[_k] = _v  # Q-1: set only when sent
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
            "question_len=%d pinned_context=%d pinned_chars=%d",
            actor_id, row.version[:16], run_id, len(body.question),
            len(pinned_context), pinned_context_chars(pinned_context),
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
        request: Request,
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
        await require_deep_task_owner(pg, request, task_id)
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
        # The row's ``data`` column holds the whole Finding (body, data, tags,
        # ...): the synthesize stage's blob sits at data.data.deep_consult.
        # The flat data.deep_consult shape is read too (older callers, tests).
        deep_blob = data_blob.get("deep_consult")
        if not isinstance(deep_blob, dict):
            inner = data_blob.get("data")
            deep_blob = inner.get("deep_consult") if isinstance(inner, dict) else None
        if not isinstance(deep_blob, dict):
            deep_blob = {}
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
        contract = deep_blob.get("contract") if isinstance(deep_blob.get("contract"), dict) else None
        verification = (
            deep_blob.get("verification") if isinstance(deep_blob.get("verification"), dict) else None
        )
        judged_refs = deep_blob.get("judged_refs")
        usage = deep_blob.get("usage") if isinstance(deep_blob.get("usage"), dict) else None
        plane, route = _deep_plane_route(usage)
        await consult_persistence.record_deep_completion(
            pg,
            task_id=task_id,
            answer=output_row["body"] or "",
            cited_refs=cited_refs,
            finding_id=str(output_row["id"]),
            contract=contract,
            verification=verification,
            judged_refs=judged_refs,
            usage=usage,
            plane=plane,
            route=route,
        )
        # The verdict as the turn stores it: checked against the persisted
        # answer (a stamp over other text is withdrawn), or the label.
        verification = _turn_stamps.verification_stamp(
            verification, reason_if_absent="deep_consult_not_judged",
            answer=(output_row["body"] or "") if verification is not None else None,
            cited_refs=[str(r) for r in judged_refs] if isinstance(judged_refs, list) else None,
        )

        return DeepConsultStatusResponse(
            task_id=task_id,
            status="completed",
            finding_id=str(output_row["id"]),
            answer=output_row["body"],
            uncertainty=uncertainty,
            cited_refs=cited_refs,
            contract=contract,
            verification=verification,
        )

    return router


def _deep_plane_route(usage: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """The plane and route a deep run's calls ran on, read off its per-call
    receipts (``component_id``): the route when every call names the same
    component, its friendly plane when the allowlist names it. Calls on
    several components, or none recorded: ``(None, None)`` — not a guess."""
    receipts = usage.get("call_receipts") if isinstance(usage, dict) else None
    ids = {r.get("component_id") for r in receipts or [] if isinstance(r, dict) and r.get("component_id")}
    if len(ids) != 1:
        return None, None
    route = str(next(iter(ids)))
    plane = next((k for k, v in CONSULT_MODEL_ALLOWLIST.items() if v == route), None)
    return plane, route


__all__ = [
    "DEEP_CONSULT_ANALYST_ID",
    "DeepConsultRequest",
    "DeepConsultStatusResponse",
    "DeepConsultSubmitResponse",
    "build_deep_consult_router",
]
