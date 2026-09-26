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

The run is DETACHED (D-7)
=========================

Step 3 above used to happen *on the request*: the handler held the invoke open
for the whole ReAct loop under a 300s client timeout. On 2026-09-16 a real
consult ran past it, the handler's httpx client closed the connection, and the
answer — which for a chat consult exists nowhere but that envelope — was lost
with a 504 whose text promised a background completion that could not happen.

The invoke now belongs to a registry-owned task (``consult_runs``) that carries
its own timeout, sized above the analyst's wall-clock budget, and persists the
turn under ``asyncio.shield`` whether or not anyone is still listening. The
POST waits only briefly for a fast answer (200, unchanged contract) and
otherwise returns 202 + the ids — invisible to the operator, whose panel is
already streaming rounds off the SSE relay and takes the answer from its
terminal frame.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from ..pinned_context import (
    MAX_PIN_ID_CHARS,
    MAX_PIN_TEXT_CHARS,
    MAX_PIN_TITLE_CHARS,
    MAX_PINNED_RECORDS,
    MAX_PINNED_TOTAL_CHARS,
    PINNED_KINDS,
    pinned_context_chars,
)
from . import consult_persistence, consult_synthesis_api
from .api import RegistryAPIDeps, require_bearer
from .consult_runs import (
    ConsultRun,
    ConsultRunError,
    ConsultRunManager,
    invoke_timeout_seconds,
    publish_terminal_frame,
    sync_wait_seconds,
)
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
# HISTORICAL. This was the request handler's own read timeout while the POST
# *was* the run: 300s, after which httpx closed the connection to the sidecar,
# daprd cancelled the in-flight actor method, and the answer died mid-synthesis
# (2026-09-16, run 3ae77c64). The endpoint no longer blocks on the invoke at
# all — the run is detached (``consult_runs``) and carries its own, far longer
# timeout from :func:`consult_runs.invoke_timeout_seconds`, sized ABOVE the
# analyst's wall-clock budget so the LOOP's graceful degradation ends a long
# run rather than the caller's clock.
#
# Kept as the documented default for that historical wait and as the legacy
# name external callers may still import. It no longer bounds anything.
DAPR_INVOKE_TIMEOUT_SECONDS = 300.0

#: How long ``POST /consult/runs/{id}/stop`` waits for the cancelled run to
#: finish persisting its partial turn. The caller's very next move is
#: ``/synthesize`` over that turn, so returning before the write lands would
#: race the recovery against its own evidence. Cancellation unwinds a couple of
#: awaits and one insert; 10s is generous, and exceeding it is not fatal — the
#: stop still happened, and the recovery's own lookup is what would 404.
_STOP_PERSIST_WAIT_SECONDS = 10.0


# F1 model picker — the SMALL server-side allowlist mapping the operator's
# FRIENDLY choice to a sanctioned LLM stack-component id. The client NEVER passes
# a raw component id; it sends ``model`` = "opus" | "fable" | "core" (or nothing
# → the default), and we map here. "opus" = the billed Anthropic Opus plane
# (``llm.anthropic.opus_4_7``) — TODAY'S default, so no selection preserves
# current behavior; "fable" = the billed Anthropic Claude Fable 5.1 plane
# (``llm.anthropic.fable_5_1``), selectable but never default (see
# ``descriptors/stack_component_llm_anthropic_fable_5_1.yaml`` for the
# registration); "core" = the free self-hosted core (openai_compat) plane.
# Any other value is rejected by the pydantic ``Literal`` (422) before it reaches
# this map. The three ids MUST stay in sync with the runtime allowlist
# (:data:`legba.data.analysts.consult_on_demand.LLM_OVERRIDE_ALLOWLIST`).
CONSULT_MODEL_ALLOWLIST: dict[str, str] = {
    "opus": "llm.anthropic.opus_4_7",
    "fable": "llm.anthropic.fable_5_1",
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


class PinnedRef(BaseModel):
    """One record the operator pinned to the conversation.

    ``title`` accepts the SPA's ``label`` key as a synonym (that is what a
    ``Selection`` row carries) so the client that was ALREADY sending pins
    keeps working unchanged. ``text`` is the optional hydrated body — a
    caller that already holds the record's prose can hand it over rather than
    make the planner spend a tool round re-reading it.

    See ``legba.data.pinned_context`` for the caps and the render contract.
    """

    kind: str
    id: str = Field(min_length=1, max_length=MAX_PIN_ID_CHARS)
    title: str | None = Field(
        default=None,
        max_length=MAX_PIN_TITLE_CHARS,
        validation_alias=AliasChoices("title", "label"),
    )
    text: str | None = Field(default=None, max_length=MAX_PIN_TEXT_CHARS)

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        """Reject a kind nothing can resolve — see ``PINNED_KINDS``."""
        if v not in PINNED_KINDS:
            raise ValueError(
                f"unknown pinned_context kind {v!r}; expected one of "
                f"{', '.join(sorted(PINNED_KINDS))}"
            )
        return v


class ConsultRequest(BaseModel):
    """Inbound shape from the SPA consult panel."""

    question: str = Field(min_length=1, max_length=8192)
    scope_predicate: str | None = Field(default=None, max_length=2048)
    # Chat default 10, ceiling 30 (Piece 1, D1). The kind clamps at 30 too.
    max_tool_rounds: int = Field(default=10, ge=1, le=30)
    # Chat = no finding, response in the envelope; deep = persist a finding (D3/D4).
    mode: Literal["chat", "deep"] = "chat"
    # F1 model picker — which registered LLM plane answers this request. None /
    # absent ⇒ "opus" (the billed Anthropic Opus plane, TODAY'S default).
    # "fable" routes to the billed Anthropic Claude Fable 5.1 plane (selectable,
    # never default). "core" routes to the free self-hosted core plane. Any
    # other value 422s (the Literal). Mapped friendly→component id
    # server-side (never a raw id).
    model: Literal["opus", "fable", "core"] | None = None
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
    # Records the operator pinned to the conversation. Absent / [] ⇒ the
    # actor's first input row carries NO ``pinned_context`` key at all, which
    # is what keeps a pre-pin client's invoke body byte-identical to today's.
    #
    # This field existed on the wire for the whole of the panel's life and was
    # DROPPED here every time: the model declared no such field and set no
    # ``model_config``, so pydantic v2's default ``extra='ignore'`` swallowed
    # it. Only the client-side ``[Pinned …]`` text prefix ever reached the
    # planner. Declaring it is the whole fix.
    pinned_context: list[PinnedRef] = Field(
        default_factory=list, max_length=MAX_PINNED_RECORDS,
    )

    @model_validator(mode="after")
    def _bounded_pinned_context(self) -> ConsultRequest:
        """Cap the pin set's TOTAL size, not just its per-entry sizes.

        Twenty maxed-out bodies would be 160k chars — far past the consult
        input budget — so the sum is gated too, and past it the request is a
        422 the client can act on rather than a silent truncation it cannot
        see.
        """
        used = pinned_context_chars(
            [p.model_dump() for p in self.pinned_context],
        )
        if used > MAX_PINNED_TOTAL_CHARS:
            raise ValueError(
                f"pinned_context is {used} chars; the cap is "
                f"{MAX_PINNED_TOTAL_CHARS}"
            )
        return self


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
    # Detached-run fields (D-7). ``status`` is "complete" when this body carries
    # a finished answer (HTTP 200 — the fast path, byte-identical to the old
    # contract for every field above) and "accepted" when the run outlived the
    # sync wait (HTTP 202): ``answer`` is then empty and the client reads the
    # answer off the SSE stream it already has open, or from
    # ``GET /consult/runs/{request_id}``. ``request_id`` is always echoed so a
    # client that let the server mint one can still subscribe / recover.
    status: Literal["complete", "accepted"] = "complete"
    request_id: str | None = None
    # Recovery fields (the c8a0105c train). ``synthesis_status`` says whether
    # the answer above is a finished synthesis ("complete"), the prefix of one
    # that was cut ("partial"), or an honest apology because none was produced
    # ("none"); ``resynthesizable`` is the panel's cue to offer "Synthesize
    # from evidence". Defaulted to the happy case so every existing producer of
    # this model — the deep branch, the tests' goldens — is unchanged.
    synthesis_status: Literal["complete", "partial", "none"] = "complete"
    resynthesizable: bool = False
    # What the run cost: calls, input/output tokens, estimated USD, and the
    # ceilings they ran against. Empty for a run that predates the spend guard.
    usage: dict[str, Any] = Field(default_factory=dict)
    # Set only on a re-synthesis: how faithfully the replayed prompt matched
    # the one the original run sent, and the sentence saying what was lost.
    replay_fidelity: str | None = None
    replay_note: str | None = None
    # 7g-2 — the PROVENANCE CENSUS: what this answer rests on, counted rather
    # than asserted. Cited refs by origin class (live / web_retrieval /
    # history / seed) plus the number of sentences carrying no citation at
    # all. Composed SERVER-side by
    # ``analysts.consult_provenance_census.build_provenance_census`` — a share
    # the client derived could disagree with the answer it is printed beside.
    # ``None`` for a run that produced none (every pre-7g-2 turn), and the
    # per-class counts inside it are ``None`` rather than 0 when the classes
    # could not be measured: a zero would read as "cites no live reporting",
    # which is a claim.
    provenance_census: dict[str, Any] | None = None


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
        inner_data = {}
        tool_calls_raw = []
    tool_calls = _project_tool_calls(tool_calls_raw)

    # The run's own account of whether it finished and what it cost. These used
    # to die here: ``data`` carried ``forced_final`` all along and the
    # projection dropped it, so the panel could not tell a complete answer from
    # a truncated one and the c8a0105c turn looked, in the UI, like any other.
    synthesis_status = str(inner_data.get("synthesis_status") or "complete")
    if synthesis_status not in ("complete", "partial", "none"):
        synthesis_status = "complete"
    usage = inner_data.get("usage")

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
        synthesis_status=synthesis_status,  # type: ignore[arg-type]
        resynthesizable=bool(inner_data.get("resynthesizable")),
        usage=dict(usage) if isinstance(usage, dict) else {},
        replay_fidelity=inner_data.get("replay_fidelity"),
        replay_note=inner_data.get("replay_note"),
        # 7g-2 — lifted here or it dies at this boundary, which is exactly
        # what happened to ``forced_final`` (see the note above).
        provenance_census=(
            dict(census)
            if isinstance(census := inner_data.get("provenance_census"), dict)
            else None
        ),
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
# The detached run body (D-7)
# ---------------------------------------------------------------------------


async def _invoke_and_project(
    deps: RegistryAPIDeps,
    *,
    invoke_url: str,
    sidecar_url: str,
    actor_id: str,
    invoke_body: dict[str, Any],
    session_id: str | None,
    chosen_model: str,
    request_id: str,
) -> dict[str, Any]:
    """PUT the actor method, classify the envelope, project the response.

    This is the whole of what the request handler used to do inline, lifted so
    a :class:`consult_runs.ConsultRunManager` task can own it and the HTTP
    request can return without it. Behaviour is unchanged except for two
    things, both deliberate:

    * the read timeout is :func:`consult_runs.invoke_timeout_seconds` (600s by
      default) rather than the handler's old 300s, because nothing is waiting
      on this connection any more and the analyst's own budget is what should
      end a long run;
    * every failure raises :class:`consult_runs.ConsultRunError` carrying the
      SAME ``(status_code, detail)`` the handler used to raise as an
      ``HTTPException``, so the errors a client sees are identical whether they
      arrive on the POST (fast path) or over the stream (detached path).

    Returns the projected :class:`ConsultResponse` as a dict.
    """
    logger.info(
        "consult.run.invoke actor_id=%s request_id=%s url=%s",
        actor_id, request_id, invoke_url,
    )
    try:
        async with httpx.AsyncClient(
            timeout=invoke_timeout_seconds(),
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
        raise ConsultRunError(
            status.HTTP_504_GATEWAY_TIMEOUT,
            (
                f"the consult actor did not return within "
                f"{invoke_timeout_seconds():.0f}s — past even its own "
                f"wall-clock budget. Whatever it had gathered is on this turn."
            ),
        ) from exc
    except httpx.HTTPError as exc:
        logger.warning(
            "consult.invoke.transport actor_id=%s err=%s", actor_id, exc,
        )
        raise ConsultRunError(
            status.HTTP_502_BAD_GATEWAY,
            f"dapr sidecar unreachable at {sidecar_url}: {exc}",
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
            raise ConsultRunError(
                status.HTTP_503_SERVICE_UNAVAILABLE, provider_msg,
            )
        raise ConsultRunError(
            status.HTTP_502_BAD_GATEWAY,
            (
                f"dapr actor invoke returned {dapr_response.status_code}: "
                f"{dapr_response.text[:512]}"
            ),
        )

    try:
        actor_result = dapr_response.json()
    except ValueError as exc:
        raise ConsultRunError(
            status.HTTP_502_BAD_GATEWAY,
            f"dapr actor returned non-JSON body: {exc}",
        ) from exc

    if not isinstance(actor_result, dict):
        raise ConsultRunError(
            status.HTTP_502_BAD_GATEWAY,
            f"dapr actor returned unexpected shape: {actor_result!r}",
        )

    outcome = actor_result.get("outcome")
    if outcome != "success":
        # The actor's own outcome reporting carries the error / noop reason;
        # surface it so the SPA can show a real message (e.g. budget
        # throttled, cooldown).
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
            raise ConsultRunError(
                status.HTTP_503_SERVICE_UNAVAILABLE, provider_msg,
            )
        raise ConsultRunError(status.HTTP_502_BAD_GATEWAY, detail)

    # Chat-mode branch (Piece 1, D4): the actor returns the typed
    # ConsultResponsePayload IN the envelope — no row was written, so SKIP the
    # DB read-back entirely and project the same payload the deep branch
    # reloads. ``finding_id`` is None (chat is ephemeral).
    if actor_result.get("mode") == "chat":
        derived_from = [str(d) for d in (actor_result.get("derived_from") or [])]
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
        projected.request_id = request_id
        return {
            "response": projected,
            "steps": _steps_from_payload(consult_payload),
        }

    # Deep-mode (or absent mode) — the existing persist + read-back path.
    finding_id = actor_result.get("finding_id") or actor_result.get("output_id")
    if not finding_id:
        raise ConsultRunError(
            status.HTTP_502_BAD_GATEWAY,
            (
                f"dapr actor success envelope missing finding_id / "
                f"output_id: {actor_result!r}"
            ),
        )

    derived_from = [str(d) for d in (actor_result.get("derived_from") or [])]
    receipt_hash = actor_result.get("receipt_hash")

    # Read the produced row back from analyst_outputs so we can surface the
    # structured ConsultResponsePayload (answer text, cited refs, tool trace)
    # to the SPA. The actor envelope only carries identifiers; the body lives
    # in the substrate row.
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
        # Race: the actor reported success but the row isn't queryable yet
        # (extremely unlikely — the row is committed before the actor
        # returns). Surface a clear 502 rather than synth data.
        raise ConsultRunError(
            status.HTTP_502_BAD_GATEWAY,
            (
                f"consult actor returned finding_id={finding_id!r} but "
                f"no matching analyst_outputs row was found."
            ),
        )

    # The ConsultResponsePayload is nested under data["consult_response"] per
    # ``_wrap_as_finding`` in ``legba.data.analysts.consult_on_demand``.
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

    # The synthesised answer lives in the consult payload; the row's ``body``
    # column carries the same answer but capped. Prefer the payload's answer
    # when present, fall back to the row body so the shared projection always
    # has something to render.
    if not consult_payload.get("answer") and output_row["body"]:
        consult_payload = {**consult_payload, "answer": output_row["body"]}

    projected = _project_consult_response(
        consult_payload,
        finding_id=str(finding_id),
        derived_from=derived_from,
        receipt_hash=receipt_hash,
        session_id=session_id,
        model=chosen_model,
    )
    projected.request_id = request_id
    return {
        "response": projected,
        "steps": _steps_from_payload(consult_payload),
    }


async def _deliver_run(
    deps: RegistryAPIDeps,
    run: ConsultRun,
    *,
    request_id: str,
) -> None:
    """Persist the run's outcome, then publish the terminal frame.

    Called by the run manager under ``asyncio.shield`` — so this is the code
    that has to hold when a client vanishes, the actor is cancelled, or the
    loop runs out of budget. Persist FIRST: the turn is the durable record and
    the frame is best-effort telemetry on top of it, never the other way round.

    On failure the persisted turn is the PARTIAL — the steps the run watched go
    by plus a plain-language account of where it stopped — because a transcript
    that silently skips a turn is worse than one that says what broke.
    """
    pg = deps.descriptor_registry.pg
    if run.status == "complete" and isinstance(run.result, dict):
        response = run.result.get("response")
        steps = run.result.get("steps")
        if isinstance(response, ConsultResponse):
            # Swap in the JSON-safe body FIRST: the status endpoint and the
            # terminal frame must carry the answer even if the audit write
            # below trips, and a half-converted result would strand both.
            run.result = response.model_dump(mode="json")
            await _persist_assistant_turn(
                pg, run.session_id, response, steps=steps,
            )
    else:
        await _persist_partial_turn(pg, run)
    await publish_terminal_frame(
        deps.nats_store, request_id=request_id, run=run,
    )


async def _persist_partial_turn(pg: Any, run: ConsultRun) -> None:
    """Record a failed / cancelled run as an assistant turn carrying its partial.

    Best-effort like every other audit write (``append_turn`` swallows its own
    errors). The content is :meth:`ConsultRun.partial_summary` — prose, not a
    status code — and ``steps`` carries whatever the run watched go by, so a
    reload renders the same partial the live panel showed.
    """
    if not run.session_id:
        return
    await consult_persistence.append_turn(
        pg,
        session_id=run.session_id,
        role="assistant",
        content=run.partial_summary(),
        steps=list(run.steps),
    )


def _response_from_run_result(
    run: ConsultRun,
    session_id: str | None,
    chosen_model: str,
    request_id: str,
) -> ConsultResponse:
    """Re-hydrate the completed run's projected response for the POST body.

    ``_deliver_run`` leaves ``run.result`` as a JSON-safe dict (that is what
    the status endpoint and the terminal frame need). The fast path re-validates
    it back into the typed model so the 200 response is exactly the shape it
    always was. A result that somehow isn't dict-shaped degrades to an empty
    ``complete`` answer rather than a 500 — the turn is already persisted, and
    the client can read it back.
    """
    raw = run.result if isinstance(run.result, dict) else {}
    try:
        return ConsultResponse.model_validate(raw)
    except Exception:  # noqa: BLE001 — never 500 on a run that succeeded
        logger.warning(
            "consult.run.reproject_failed request_id=%s", request_id,
        )
        return ConsultResponse(
            answer=str(raw.get("answer") or ""),
            status="complete",
            request_id=request_id,
            session_id=session_id,
            model=chosen_model,
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
    # One manager per app. It watches each run's step subject so a run that
    # dies still has a trace to persist — see ``consult_runs``.
    runs = ConsultRunManager(nats_store=deps.nats_store)

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
        """Start a ``consult_default`` run and return as soon as it is safe to.

        **200** with the full answer when the run finishes inside
        :func:`consult_runs.sync_wait_seconds` — the common short consult, and
        the pre-detachment contract unchanged.

        **202** with ``status="accepted"`` + ``request_id`` / ``session_id``
        when it doesn't. The run continues in a registry-owned task; the
        client's already-open EventSource keeps receiving every round and ends
        on the registry's terminal frame. Nothing about the panel's behaviour
        changes at this boundary — it was streaming before the POST returned
        and it keeps streaming after.

        Errors inside the sync window still raise on this response, exactly as
        before. Errors after it arrive on the stream, and the partial is
        persisted either way.
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
        # FAIL CLOSED (F1): a non-default plane must actually be registered in
        # the stack before we ever build an invoke body around it. Without
        # this check an allowlisted-but-unregistered choice (e.g. "fable"
        # before the operator runs bringup_register_stack.py) would only fail
        # deep inside the actor's ReAct loop, minutes later, as a generic
        # provider-error 503 — or, on an actor built without the by-id
        # resolver wired at all, would silently run on the cached Opus
        # primary. Checking here means the SAME clear, model-named message
        # comes back immediately, and Opus is never silently billed for a
        # request the operator asked to route elsewhere.
        if llm_component_override is not None:
            try:
                await deps.stack_registry.get(llm_component_override)
            except DescriptorNotFound as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        f"the {chosen_model!r} model "
                        f"({llm_component_override}) is not registered yet — "
                        f"ask the operator to register it (bringup), or "
                        f"select a different model."
                    ),
                ) from exc
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
        # Only set the key when there ARE pins: a client that sends none must
        # produce the exact invoke body it produced before this field existed
        # (the golden the chat tests hold). ``by_alias=False`` so the actor
        # always sees the canonical ``title``, whichever key came in.
        pinned_context = [p.model_dump() for p in body.pinned_context]
        if pinned_context:
            first_input["pinned_context"] = pinned_context
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
            "question_len=%d pinned_context=%d pinned_chars=%d",
            actor_id, row.version[:16], invoke_url, len(body.question),
            len(pinned_context), pinned_context_chars(pinned_context),
        )

        # 2. DETACH the run (D-7). The invoke is owned by a registry task with
        #    its own timeout, so the answer no longer depends on this request
        #    surviving. A browser that navigates away, or a wait that runs long,
        #    can no longer cancel the actor mid-synthesis — which is exactly
        #    what destroyed run 3ae77c64 on 2026-09-16.
        run = runs.start(
            request_id=request_id,
            session_id=session_id,
            execute=lambda: _invoke_and_project(
                deps,
                invoke_url=invoke_url,
                sidecar_url=sidecar_url,
                actor_id=actor_id,
                invoke_body=invoke_body,
                session_id=session_id,
                chosen_model=chosen_model,
                request_id=request_id,
            ),
            on_complete=lambda r: _deliver_run(deps, r, request_id=request_id),
        )

        # 3. Wait BRIEFLY for a fast answer. A short consult lands inside this
        #    window and returns 200 with the full body — byte-identical to the
        #    pre-detachment contract for every field that existed before. Past
        #    it we hand back 202 + the ids. The client's EventSource is already
        #    open and keeps rendering rounds either way, so the 202 is invisible
        #    to the operator: the panel streams from the first second exactly as
        #    it did, and simply learns the answer from the stream's terminal
        #    frame instead of from this response.
        if run.task is not None:
            await asyncio.wait([run.task], timeout=sync_wait_seconds())

        if run.status == "running":
            logger.info(
                "consult.invoke.detached request_id=%s session_id=%s "
                "after_s=%.0f",
                request_id, session_id, sync_wait_seconds(),
            )
            response.status_code = status.HTTP_202_ACCEPTED
            return ConsultResponse(
                answer="",
                status="accepted",
                request_id=request_id,
                session_id=session_id,
                model=chosen_model,
            )

        if run.status == "error" and run.error is not None:
            # A failure INSIDE the sync window is raised on the POST exactly as
            # it always was — the error contract is unchanged for the fast path.
            # (A failure after it reaches the client on the stream instead, and
            # the partial is persisted either way.)
            raise HTTPException(
                status_code=run.error[0], detail=run.error[1],
            )

        return _response_from_run_result(run, session_id, chosen_model, request_id)

    @router.get(
        "/consult/runs/{request_id}",
        status_code=status.HTTP_200_OK,
    )
    async def get_consult_run(
        request_id: str,
        _principal: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        """Status + result of a detached run — the reconnect path.

        A client that had a 202 and then lost its stream (refresh, sleep, flaky
        network) asks here: ``running`` means keep waiting, ``complete`` carries
        the answer, ``error`` carries the reason plus the steps the run saw.
        Unknown ids 404 — the run either never existed or aged out of
        ``RUN_RETENTION_SECONDS``, in which case the durable record is the
        persisted turn under ``GET /consult/sessions/{id}``.
        """
        run = runs.get(request_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"no live consult run for request_id {request_id!r}; if it "
                    f"completed earlier, read the session's turns instead."
                ),
            )
        return run.as_status_payload()

    @router.post(
        "/consult/runs/{request_id}/stop",
        status_code=status.HTTP_200_OK,
    )
    async def stop_consult_run(
        request_id: str,
        _principal: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        """Stop a run that is still drilling.

        Until this existed there was no way to stop one. The panel's "Dismiss"
        detached the browser and left the run drilling and billing — which on a
        Fable-priced route is the expensive half of the c8a0105c incident still
        running with nobody watching.

        Cancelling the run task cancels the registry→sidecar invoke, which
        cancels the actor method; the manager's ``CancelledError`` branch then
        persists whatever the run had gathered as a partial turn. We WAIT for
        that persist before returning, because the caller's next move is
        ``/synthesize`` over exactly that turn, and returning early would race
        it against its own evidence.
        """
        run = runs.get(request_id)
        if run is None or run.status != "running":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"no live consult run for request_id {request_id!r} — it "
                    f"already finished, or it aged out."
                ),
            )
        run.task.cancel()
        await asyncio.wait([run.task], timeout=_STOP_PERSIST_WAIT_SECONDS)
        logger.info("consult.run.stopped request_id=%s", request_id)
        return {
            "request_id": request_id,
            "status": "stopped",
            "steps": len(run.steps),
        }

    @router.post(
        "/consult/runs/{request_id}/synthesize",
        response_model=consult_synthesis_api.SynthesizeResponse,
        status_code=status.HTTP_200_OK,
    )
    @_limiter.limit(CONSULT_RATE_LIMIT)
    async def synthesize_from_evidence(
        request: Request,
        request_id: str,
        body: consult_synthesis_api.SynthesizeRequest,
        _principal: str = Depends(require_bearer),
    ) -> consult_synthesis_api.SynthesizeResponse:
        """Write the answer a cut run never got to write.

        ONE model call over the evidence that run already gathered and already
        paid for — no new drilling. The run being recovered spent its money on
        50 tool calls; asking the question again would spend it twice.

        Resolves the turn by ``request_id``, or by ``turn_id`` in the body for
        any turn written before migration 0195 (which is every turn that
        existed when this was built, the c8a0105c turn included).
        """
        evidence = await consult_persistence.load_turn_for_recovery(
            pg, request_id=request_id, turn_id=body.turn_id,
        )
        ok, why = consult_synthesis_api.recovery_precheck(evidence)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=why,
            )
        assert evidence is not None  # recovery_precheck rejects None

        try:
            row = await deps.descriptor_registry.get(CONSULT_ANALYST_ID)
        except DescriptorNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"consult analyst {CONSULT_ANALYST_ID!r} is not "
                    f"registered; activate it first via bringup."
                ),
            ) from exc

        chosen_model, llm_component_override = resolve_consult_model_override(
            body.model,
        )
        if llm_component_override is not None:
            try:
                await deps.stack_registry.get(llm_component_override)
            except DescriptorNotFound as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        f"the {chosen_model!r} model "
                        f"({llm_component_override}) is not registered yet."
                    ),
                ) from exc

        recovery_request_id = str(uuid4())
        actor_id = _build_actor_id(row.version)
        sidecar_url = _dapr_sidecar_url()
        try:
            result = await _invoke_and_project(
                deps,
                invoke_url=(
                    f"{sidecar_url}/v1.0/actors/{ACTOR_TYPE}/{actor_id}/method/run"
                ),
                sidecar_url=sidecar_url,
                actor_id=actor_id,
                invoke_body={
                    "trigger_kind": "method",
                    "inputs": [
                        consult_synthesis_api.build_recovery_input(
                            evidence,
                            llm_component_override=llm_component_override,
                            request_id=recovery_request_id,
                        )
                    ],
                },
                session_id=evidence.get("session_id"),
                chosen_model=chosen_model,
                request_id=recovery_request_id,
            )
        except ConsultRunError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail=exc.detail,
            ) from exc

        projected = result["response"]
        # The recovered answer is its OWN turn, linked to the one it re-read.
        # Never an overwrite: the cut answer and what it cost stay on the
        # record, which is the only way the next post-mortem can see that this
        # happened at all.
        turn_id = await consult_persistence.append_turn(
            pg,
            session_id=evidence.get("session_id") or "",
            role="assistant",
            content=projected.answer,
            steps=result.get("steps") or [],
            tool_calls=[tc.model_dump() for tc in projected.tool_calls],
            cited_refs=[cr.model_dump() for cr in projected.cited_refs],
            request_id=recovery_request_id,
            parent_turn_id=evidence.get("turn_id"),
            synthesis_status=projected.synthesis_status,
            usage=projected.usage,
        )
        return consult_synthesis_api.project_recovery(
            projected,
            evidence=evidence,
            request_id=recovery_request_id,
            chosen_model=chosen_model,
            turn_id=turn_id,
        )

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
