# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""On-demand consult invocation endpoint — Pass 3.5.

Mounts under ``/api/v1/consult``. Built via ``build_consult_router(deps)``;
``server.py`` wires it alongside the v3 / runtime_telemetry / budget / etc.
routers.

Why this exists
===============

The existing A2A skill router (``src/legba/data/outputs/a2a_skill.py``)
returns the *latest* outputs an analyst has emitted — that's the read
surface used by federation peers and the legacy ConsultPanel skim. For
the L-204 daily-driver consult panel, the operator wants to ASK A QUESTION
and get an answer synthesised on demand. That requires invoking the
``consult_default`` analyst actor through Dapr.

This module is the thin server-side proxy that:

  1. Looks up the active head version of ``consult_default`` from
     the descriptor registry.
  2. Builds the actor_id per the canonical grammar
     (``analyst::consult_default::<version[:16]>`` — matches
     ``runtime/reconcile._default_actor_id`` so daprd routes to the
     existing actor instance instead of materialising a phantom one).
  3. PUTs to the Dapr sidecar's actor-invoke endpoint with the
     question + scope_predicate payload.
  4. Parses the actor's success envelope, then reads back the produced
     ``analyst_outputs`` row to extract the structured answer + tool
     trace + cited refs.
  5. Returns the combined response shape to the SPA.

The proxy lives in the registry process — not the runtime — because the
registry already owns the Postgres pool, the descriptor lookup, and the
HTTP bearer-token surface the UI authenticates against. The runtime
sidecar is reached via the docker-network DNS name
``legba-dapr-sidecar:3500`` (default, overridable via
``LEGBA_DAPR_SIDECAR_URL``).

Scope
=====

Intentionally narrow:

  * Only ``consult_default`` is supported. Other on-demand
    analysts will get their own endpoints (or a future generic
    ``/api/v1/analysts/{id}/invoke`` once the use-case surfaces).
  * No envelope signing — this is a same-origin operator UI invocation,
    not an A2A federation hop.
  * Dapr call timeout is 300s (DAPR_INVOKE_TIMEOUT_SECONDS); the runtime's own
    retry policy handles transient failures inside the actor.
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
from .api import RegistryAPIDeps, require_bearer
from .descriptor import Family
from .errors import DescriptorNotFound
from .rate_limit import CONSULT_RATE_LIMIT
from .rate_limit import limiter as _limiter

logger = logging.getLogger(__name__)


# The on-demand consult analyst the front door invokes. Default matches the
# canonical p17 workingset seed (``analyst_consult_default.yaml`` →
# ``consult_default``); env-overridable for non-default deployments. The legacy
# ``legba_consult_default`` descriptor was the pre-pivot ``tools_whitelist``
# build — removed (it was registered by no current bringup, only the legacy
# week3 script), since two consult descriptors invited exactly the drift that
# 404'd this endpoint against every live seed.
CONSULT_ANALYST_ID = os.getenv("LEGBA_CONSULT_ANALYST_ID", "consult_default")
ACTOR_TYPE = "AnalystActor"
DESCRIPTOR_KIND = "analyst"
DAPR_SIDECAR_URL_ENV = "LEGBA_DAPR_SIDECAR_URL"
DAPR_SIDECAR_URL_DEFAULT = "http://dapr-sidecar:3500"
# The synchronous consult endpoint blocks for the whole ReAct loop
# (MAX_TOOL_ROUNDS=6 + a forced-final turn = up to 7 LLM calls). With the
# finished-intelligence tool palette the planner runs more tool rounds and a
# larger system prompt, so a complex question's loop can exceed the prior 180s
# and return a 504 even though the actor completes + writes its row in the
# background. 300s gives the worst-case loop headroom. The streaming endpoint
# (consult_stream_api, 25s keepalives) is the better path for long consults.
DAPR_INVOKE_TIMEOUT_SECONDS = 300.0


# F1 model picker — the SMALL server-side allowlist mapping the operator's
# FRIENDLY choice to a sanctioned LLM stack-component id. The client NEVER passes
# a raw component id; it sends ``model`` = "opus" | "core" (or nothing → the
# default), and we map here. "opus" = the billed Anthropic Opus plane
# (``llm.anthropic.opus_4_7``) — TODAY'S default, so no selection preserves
# current behavior; "core" = the free self-hosted core (openai_compat) plane.
# Any other value is rejected by the pydantic ``Literal`` (422) before it reaches
# this map. The two ids MUST stay in sync with the runtime allowlist
# (:data:`legba.data.analysts.consult_on_demand.LLM_OVERRIDE_ALLOWLIST`).
CONSULT_MODEL_ALLOWLIST: dict[str, str] = {
    "opus": "llm.anthropic.opus_4_7",
    "core": "llm.primary.openai_compat",
}
#: The default plane when the request omits ``model`` (or sends null) — Opus, so
#: the picker is default-preserving. When the chosen plane is the default we do
#: NOT thread an override (the cached ACTIVATE-time primary handler is used
#: unchanged); the override key is threaded ONLY for a non-default choice.
DEFAULT_CONSULT_MODEL = "opus"


def resolve_consult_model_override(model: str | None) -> tuple[str, str | None]:
    """``(friendly, component_id_override_or_None)`` for a request's ``model``.

    Returns the normalized friendly value (``model`` or the default) plus the
    stack-component id to thread as ``llm_component_override`` — ``None`` when the
    choice IS the default plane (so the run keeps the cached primary handler
    unchanged, the default-preserving contract). Shared by the chat + deep front
    doors so both map identically off the ONE allowlist.
    """
    friendly = model or DEFAULT_CONSULT_MODEL
    if friendly == DEFAULT_CONSULT_MODEL:
        return friendly, None
    return friendly, CONSULT_MODEL_ALLOWLIST[friendly]


# H4(a) — provider/plane error surfacing. When the actor surfaces a plane outage
# (the Anthropic credit-balance / auth / rate-limit error on Opus, or a core /
# F-A fail-closed "llm plane ... unavailable"), the front door returns a graceful
# 503 with an ACTIONABLE message naming the OTHER plane so the operator can switch
# + retry — instead of a bare 502. A case-insensitive substring match keeps this
# robust to provider-specific wording.

#: Markers that mean "this is a provider / plane error at all" (else: keep 502).
_PROVIDER_ERROR_MARKERS = (
    "credit balance", "unavailable", "llm plane", "authentication",
    "unauthorized", "401", "402", "429",
)
#: Markers that pin the outage to the Anthropic (Opus) plane → suggest core. The
#: Opus component id ("anthropic"/"opus") appearing in a fail-closed message also
#: routes here. Everything else that matched routes to the core plane.
_OPUS_PLANE_MARKERS = ("anthropic", "opus", "credit balance", "claude")


def _classify_provider_error(text: str | None) -> str | None:
    """Return an actionable 503 message when ``text`` names a provider/plane
    outage, else ``None`` (the caller keeps the existing 502).

    ``text`` is the actor's surfaced error/reason/detail (any casing). We first
    confirm it LOOKS like a provider/plane error, then name the OTHER plane so
    the operator can retry on it: Anthropic/Opus markers → the Opus plane is
    down → suggest core; everything else that matched (core / vllm /
    openai_compat / a fail-closed "llm plane ...") → the core plane is down →
    suggest Opus.
    """
    t = (text or "").lower()
    if not any(m in t for m in _PROVIDER_ERROR_MARKERS):
        return None
    reason = (text or "").strip() or "provider error"
    if any(m in t for m in _OPUS_PLANE_MARKERS):
        return (
            f"The Anthropic (Opus) plane is unavailable: {reason}. "
            f"Retry, or select the Core model."
        )
    return (
        f"The Core plane is unavailable: {reason}. "
        f"Retry, or select the Opus model."
    )


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


class ConsultMessage(BaseModel):
    """One prior turn of a client-held consult transcript (multi-turn, D6)."""

    role: Literal["user", "assistant"]
    content: str = Field(max_length=16384)


class ConsultRequest(BaseModel):
    """Inbound shape from the SPA consult panel."""

    question: str = Field(min_length=1, max_length=8192)
    scope_predicate: str | None = Field(default=None, max_length=2048)
    # Chat default 10, ceiling 30 (Piece 1, D1). The kind clamps at 30 too.
    max_tool_rounds: int = Field(default=10, ge=1, le=30)
    # Chat = no finding, response in the envelope; deep = persist a finding (D3/D4).
    mode: Literal["chat", "deep"] = "chat"
    # F1 model picker — which registered LLM plane answers this request. None /
    # absent ⇒ "opus" (the billed Anthropic Opus plane, TODAY'S default). "core"
    # routes to the free self-hosted core plane. Any other value 422s (the
    # Literal). Mapped friendly→component id server-side (never a raw id).
    model: Literal["opus", "core"] | None = None
    # Prior turns the client holds + resends (the client also re-seeds these
    # when continuing a persisted session).
    messages: list[ConsultMessage] = Field(default_factory=list)
    # Optional client-supplied request id (for the SSE subscribe-before-POST
    # race); the server mints one when absent.
    request_id: str | None = None
    # Optional session id to CONTINUE a persisted conversation (0038 audit
    # trail). Absent on the first turn — the server opens a session and returns
    # its id; the client passes it back on each subsequent turn so the audit
    # log threads the whole conversation under one session.
    session_id: str | None = None


class ConsultToolCall(BaseModel):
    """One entry from the ReAct tool trace (best-effort projection)."""

    tool: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None


class ConsultCitedRef(BaseModel):
    """One cited substrate row — kind + id + optional description."""

    kind: str = "signal"
    id: str
    description: str | None = None


class ConsultResponse(BaseModel):
    """Outbound shape returned to the SPA."""

    answer: str
    # None for chat-mode runs (no durable row); set for deep-mode findings.
    finding_id: str | None = None
    derived_from: list[str] = Field(default_factory=list)
    tool_calls: list[ConsultToolCall] = Field(default_factory=list)
    cited_refs: list[ConsultCitedRef] = Field(default_factory=list)
    receipt_hash: str | None = None
    uncertainty: float | None = None
    unanswered_aspects: list[str] = Field(default_factory=list)
    # The persisted audit-trail session id (0038). Echoed back so the client
    # can thread the next turn under the same conversation (continue / history).
    session_id: str | None = None
    # F1 model picker — the FRIENDLY plane that answered ("opus"/"core"), echoed
    # so the UI can surface which model produced the answer. Mirrors the request's
    # chosen ``model`` (default "opus"), independent of chat/deep transport.
    model: str | None = None


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
    return f"{DESCRIPTOR_KIND}::{CONSULT_ANALYST_ID}::{short}"


def _project_tool_calls(raw: Any) -> list[ConsultToolCall]:
    """Coerce the consult run's intermediate_steps / tool_trace into the
    SPA's expected shape.

    The ``consult_on_demand`` kind currently stashes loop trace under
    ``data["consult_response"]["data"]`` and may also surface
    ``intermediate_steps`` on the typed result; the latter doesn't survive
    the substrate write so we look in the persisted ``data`` JSONB. Be
    defensive: missing fields produce an empty list, never a 500.
    """
    if not raw:
        return []
    if not isinstance(raw, list):
        return []
    out: list[ConsultToolCall] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(ConsultToolCall(
                tool=str(entry.get("tool", "")),
                args=entry.get("args") if isinstance(entry.get("args"), dict) else {},
                result=entry.get("result"),
            ))
        except Exception:
            # Defensive — bad tool-call entry shouldn't fail the request.
            continue
    return out


def _project_cited_refs(refs: list[str]) -> list[ConsultCitedRef]:
    """The kind's ``cited_substrate_refs`` is a flat list of UUIDs. We
    don't know the kind per-ref at projection time (the lineage walk would
    be a separate query); label them all ``signal`` since the consult tool
    whitelist only reads from signal-like surfaces. The SPA can render
    by-id without the kind label being load-bearing.
    """
    out: list[ConsultCitedRef] = []
    for ref in refs:
        if not ref:
            continue
        out.append(ConsultCitedRef(kind="signal", id=str(ref)))
    return out


def _project_consult_response(
    consult_payload: dict[str, Any],
    *,
    finding_id: str | None,
    derived_from: list[str],
    receipt_hash: str | None = None,
    session_id: str | None = None,
    model: str | None = None,
) -> ConsultResponse:
    """Project a ConsultResponsePayload dict into the SPA's ConsultResponse.

    This is the SHARED shape both transports project (Piece 1, §7 discipline):
    the chat branch reads the payload straight from the actor envelope, the
    deep branch reads the same payload back from the persisted row's
    ``data["consult_response"]``. One assembly, two sources.
    """
    answer = consult_payload.get("answer") or "(no answer produced)"

    uncertainty = consult_payload.get("uncertainty")
    if uncertainty is not None:
        try:
            uncertainty = float(uncertainty)
        except (TypeError, ValueError):
            uncertainty = None

    unanswered_raw = consult_payload.get("unanswered_aspects") or []
    unanswered: list[str] = []
    if isinstance(unanswered_raw, list):
        unanswered = [str(u) for u in unanswered_raw if u is not None]

    cited_substrate_refs = consult_payload.get("cited_substrate_refs") or []
    if not isinstance(cited_substrate_refs, list):
        cited_substrate_refs = []
    cited_refs = _project_cited_refs(
        [str(r) for r in cited_substrate_refs if r is not None],
    )

    inner_data = consult_payload.get("data")
    if isinstance(inner_data, dict):
        tool_calls_raw = (
            inner_data.get("tool_calls")
            or inner_data.get("intermediate_steps")
            or []
        )
    else:
        tool_calls_raw = []
    tool_calls = _project_tool_calls(tool_calls_raw)

    return ConsultResponse(
        answer=str(answer),
        finding_id=finding_id,
        derived_from=derived_from,
        tool_calls=tool_calls,
        cited_refs=cited_refs,
        receipt_hash=receipt_hash,
        uncertainty=uncertainty,
        unanswered_aspects=unanswered,
        session_id=session_id,
        model=model,
    )


def _steps_from_payload(consult_payload: dict[str, Any]) -> list[Any]:
    """Lift the FULL ReAct step trace off a ConsultResponsePayload dict.

    The ``consult_on_demand`` kind stashes its per-round trace under
    ``data["steps"]`` (see ``run_method``); this is the per-turn tool-call trace
    the audit row records in ``consult_turns.steps``. Defensive: a missing /
    malformed ``data`` yields an empty list, never a 500.
    """
    data = consult_payload.get("data") if isinstance(consult_payload, dict) else None
    if isinstance(data, dict):
        steps = data.get("steps")
        if isinstance(steps, list):
            return steps
    return []


async def _persist_assistant_turn(
    pg: Any,
    session_id: str | None,
    response: ConsultResponse,
    *,
    steps: Any = None,
) -> None:
    """Append the assistant turn to the audit trail (0038), best-effort.

    Projects the response's typed tool_calls / cited_refs back to plain dicts
    for the jsonb columns and threads the FULL ReAct ``steps`` trace into the
    ``consult_turns.steps`` column so a turn is inspectable after the fact
    (previously that column was never populated). A no-op when there's no
    session (the open failed) — the consult answer is unaffected either way.
    """
    if not session_id:
        return
    await consult_persistence.append_turn(
        pg,
        session_id=session_id,
        role="assistant",
        content=response.answer,
        steps=steps if steps is not None else [],
        tool_calls=[tc.model_dump() for tc in response.tool_calls],
        cited_refs=[cr.model_dump() for cr in response.cited_refs],
        finding_id=response.finding_id,
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_consult_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the on-demand consult router bound to the registry deps.

    Mount on a FastAPI app via::

        app.include_router(build_consult_router(deps), prefix="/api/v1")
    """
    router = APIRouter(tags=["consult"])

    pg = deps.descriptor_registry.pg

    @router.post(
        "/consult",
        response_model=ConsultResponse,
        status_code=status.HTTP_200_OK,
    )
    @_limiter.limit(CONSULT_RATE_LIMIT)
    async def invoke_consult(
        request: Request,
        response: Response,
        body: ConsultRequest,
        _principal: str = Depends(require_bearer),
    ) -> ConsultResponse:
        """Invoke ``consult_default`` on-demand via Dapr.

        Returns the structured answer + finding row id once the actor
        completes. The dapr sidecar holds the connection open until the
        actor finishes; the 180s timeout caps the wait so a stuck actor
        can't pin the registry worker forever.
        """
        # 1. Resolve head version. Use the descriptor registry's typed
        #    get — same path the v3 promote endpoint uses — so we share
        #    the registry's caching + auto-upgrade behaviour.
        try:
            row = await deps.descriptor_registry.get(
                CONSULT_ANALYST_ID, family=Family.ANALYST,
            )
        except DescriptorNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"consult analyst {CONSULT_ANALYST_ID!r} is not "
                    f"registered; activate it first via bringup."
                ),
            ) from exc

        actor_id = _build_actor_id(row.version)
        sidecar_url = _dapr_sidecar_url()
        invoke_url = (
            f"{sidecar_url}/v1.0/actors/{ACTOR_TYPE}/{actor_id}/method/run"
        )
        # Request-scoped id for the SSE step relay (Piece 1, D5). Accept a
        # client-supplied id so the browser can subscribe BEFORE it POSTs
        # (subscribe-before-publish), else mint one.
        request_id = body.request_id or str(uuid4())
        # F1 model picker: normalize the friendly choice + resolve the plane
        # override. ``override`` is None when the choice is the default (Opus) —
        # in that case we DO NOT thread the key, so the run keeps the cached
        # ACTIVATE-time primary handler unchanged (default-preserving). For "core"
        # the sanctioned component id is threaded and the kind resolves it fresh.
        chosen_model, llm_component_override = resolve_consult_model_override(
            body.model,
        )
        first_input: dict[str, Any] = {
            "question": body.question,
            "scope_predicate": body.scope_predicate,
            "max_tool_rounds": body.max_tool_rounds,
            "mode": body.mode,
            "request_id": request_id,
            "messages": [m.model_dump() for m in body.messages],
        }
        if llm_component_override is not None:
            first_input["llm_component_override"] = llm_component_override
        invoke_body = {
            "trigger_kind": "method",
            "inputs": [first_input],
        }

        # Audit trail (0038): open a session on the first turn (or reuse the
        # client-supplied one when continuing), then log the user turn BEFORE
        # the actor runs so a failed/slow run still leaves the question on
        # record. Persistence is best-effort — a write failure must NOT block
        # the consult, so the helpers swallow + log their own errors.
        session_id = body.session_id
        if not session_id:
            session_id = await consult_persistence.create_session(
                pg,
                mode=body.mode,
                question=body.question,
                principal=_principal,
            )
        if session_id:
            await consult_persistence.append_turn(
                pg,
                session_id=session_id,
                role="user",
                content=body.question,
            )

        logger.info(
            "consult.invoke actor_id=%s descriptor_version=%s url=%s "
            "question_len=%d",
            actor_id, row.version[:16], invoke_url, len(body.question),
        )

        # 2. PUT to the dapr sidecar. The actor's run method blocks until
        #    the ReAct loop terminates; the sidecar holds the HTTP
        #    connection open over that span.
        try:
            async with httpx.AsyncClient(
                timeout=DAPR_INVOKE_TIMEOUT_SECONDS,
            ) as client:
                dapr_response = await client.put(
                    invoke_url,
                    json=invoke_body,
                    headers={"Content-Type": "application/json"},
                )
        except httpx.TimeoutException as exc:
            logger.warning(
                "consult.invoke.timeout actor_id=%s err=%s", actor_id, exc,
            )
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=(
                    f"consult invocation timed out after "
                    f"{DAPR_INVOKE_TIMEOUT_SECONDS:.0f}s; the actor may "
                    f"still complete in the background and write its row "
                    f"to analyst_outputs."
                ),
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning(
                "consult.invoke.transport actor_id=%s err=%s", actor_id, exc,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"dapr sidecar unreachable at {sidecar_url}: {exc}",
            ) from exc

        if dapr_response.status_code >= 400:
            logger.warning(
                "consult.invoke.bad_status actor_id=%s status=%d body=%s",
                actor_id, dapr_response.status_code, dapr_response.text[:512],
            )
            # H4(a): a plane outage bubbled through the sidecar body → graceful
            # 503 naming the other plane; else the existing 502.
            provider_msg = _classify_provider_error(dapr_response.text)
            if provider_msg is not None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=provider_msg,
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
            # The actor's own outcome reporting carries the error /
            # noop reason; surface it so the SPA can show a real message
            # (e.g. budget throttled, cooldown).
            detail = {
                "outcome": outcome,
                "error": actor_result.get("error"),
                "reason": actor_result.get("reason"),
                "detail": actor_result.get("detail"),
            }
            # H4(a): classify a plane outage (Anthropic credit / auth / rate
            # limit, or a core / F-A fail-closed "llm plane unavailable") across
            # the actor's error/reason/detail text and return a graceful 503
            # naming the OTHER plane so the operator can switch + retry.
            provider_msg = _classify_provider_error(
                " ".join(
                    str(v)
                    for v in (
                        actor_result.get("error"),
                        actor_result.get("reason"),
                        actor_result.get("detail"),
                    )
                    if v
                )
            )
            if provider_msg is not None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=provider_msg,
                )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=detail,
            )

        # 3a. Chat-mode branch (Piece 1, D4): the actor returns the typed
        #     ConsultResponsePayload IN the envelope — no row was written, so
        #     SKIP the DB read-back entirely and project the same payload the
        #     deep branch reloads. ``finding_id`` is None (chat is ephemeral).
        if actor_result.get("mode") == "chat":
            derived_from = [
                str(d) for d in (actor_result.get("derived_from") or [])
            ]
            consult_payload = actor_result.get("consult_response")
            if not isinstance(consult_payload, dict):
                consult_payload = {}
            projected = _project_consult_response(
                consult_payload,
                finding_id=None,
                derived_from=derived_from,
                session_id=session_id,
                model=chosen_model,
            )
            await _persist_assistant_turn(
                pg, session_id, projected,
                steps=_steps_from_payload(consult_payload),
            )
            return projected

        # 3b. Deep-mode (or absent mode) — the existing persist + read-back path.
        finding_id = actor_result.get("finding_id") or actor_result.get("output_id")
        if not finding_id:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"dapr actor success envelope missing finding_id / "
                    f"output_id: {actor_result!r}"
                ),
            )

        derived_from = [str(d) for d in (actor_result.get("derived_from") or [])]
        receipt_hash = actor_result.get("receipt_hash")

        # 3. Read the produced row back from analyst_outputs so we can
        #    surface the structured ConsultResponsePayload (answer text,
        #    cited refs, tool trace) to the SPA. The actor envelope only
        #    carries identifiers; the body lives in the substrate row.
        async with deps.descriptor_registry.pg.acquire() as conn:
            output_row = await conn.fetchrow(
                """
                SELECT id, kind, title, body, data
                  FROM analyst_outputs
                 WHERE id = $1
                """,
                finding_id,
            )

        if output_row is None:
            # Race: the actor reported success but the row isn't queryable
            # yet (extremely unlikely — the row is committed before the
            # actor returns). Surface a clear 502 rather than synth data.
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"consult actor returned finding_id={finding_id!r} but "
                    f"no matching analyst_outputs row was found."
                ),
            )

        # The ConsultResponsePayload is nested under data["consult_response"]
        # per ``_wrap_as_finding`` in
        # ``src/legba/data/analysts/consult_on_demand.py``.
        data_blob = output_row["data"]
        if isinstance(data_blob, str):
            try:
                data_blob = json.loads(data_blob)
            except (ValueError, TypeError):
                data_blob = {}
        if not isinstance(data_blob, dict):
            data_blob = {}

        consult_payload = data_blob.get("consult_response")
        if not isinstance(consult_payload, dict):
            consult_payload = {}

        # The synthesised answer lives in the consult payload; the row's
        # ``body`` column carries the same answer but capped. Prefer the
        # payload's answer when present, fall back to the row body so the
        # shared projection always has something to render.
        if not consult_payload.get("answer") and output_row["body"]:
            consult_payload = {**consult_payload, "answer": output_row["body"]}

        # Project the same payload the chat branch projects (one assembly).
        projected = _project_consult_response(
            consult_payload,
            finding_id=str(finding_id),
            derived_from=derived_from,
            receipt_hash=receipt_hash,
            session_id=session_id,
            model=chosen_model,
        )
        await _persist_assistant_turn(
            pg, session_id, projected,
            steps=_steps_from_payload(consult_payload),
        )
        return projected

    return router


__all__ = [
    "CONSULT_ANALYST_ID",
    "CONSULT_MODEL_ALLOWLIST",
    "DEFAULT_CONSULT_MODEL",
    "ConsultCitedRef",
    "ConsultMessage",
    "ConsultRequest",
    "ConsultResponse",
    "ConsultToolCall",
    "build_consult_router",
    "resolve_consult_model_override",
]
