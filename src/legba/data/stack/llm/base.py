# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""LLM provider stack-component handler base (L-120).

This module defines:

  * `LLMResponse` / `LLMChunk` / `LLMUsage` — unified response shapes.
  * `LLMTool` / `LLMToolCall` — provider-neutral tool-call shape (OpenAI
    Chat Completions JSON Schema spec on the wire; translated per provider).
  * `LLMProviderHandler` — base class conforming to L-102 §1 `KindHandler`
    plus the LLM-specific `chat_complete` / `stream_complete` /
    `count_tokens` / `health_check` surface used by L-103 runtime.

Concrete providers (Anthropic, vLLM, OpenAI) subclass this and
override the network-shaped methods (`_translate_messages`,
`_translate_tools`, `_call_chat`, `_parse_response`). Common bits
(retry/backoff, telemetry hooks, vault-keyed credential resolution,
usage/cost normalization, healthcheck) live here.

Per L-102 the runtime is responsible for instantiating handlers; the
handler does NOT own connection pools across instances. Each handler
instance manages exactly one provider configuration (one StackComponent
descriptor); `on_configure` loads the live config, `on_activate` opens the
HTTP client, `on_pause`/`on_retire` close it. Handlers are idempotent on
repeated lifecycle calls.

Healthcheck policy (per L-111 lean): TCP reachability of endpoint host +
vault verification that the api_key secret resolves. NO real model call —
each healthcheck would otherwise burn paid tokens against the provider
every poll cycle (default 60s). When the dispatcher caches a status as
HEALTHY, the next chat_complete call is the real liveness probe.

Cost calculation:
  per-1M-token pricing constants keyed by model family. Subproviders may
  extend or override `PRICE_TABLE` for their concrete models. Anthropic
  exposes input/output/cache-read/cache-write tokens distinctly; OpenAI
  exposes reasoning tokens for o-series + GPT-5. vLLM is treated
  as zero-cost (self-hosted) but still counts tokens for budget hooks.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any,
    AsyncIterator,
    ClassVar,
    Literal,
    Mapping,
    Protocol,
    runtime_checkable,
)

import httpx

from ...registry.credentials import CredentialResolverProtocol, MissingSecretError
from ...registry.health import HealthState, StackComponentHealth
# R11 per-run receipt accounting. ``legba.data.run_accounting`` is stdlib-only
# and its package ``__init__`` imports nothing, so this costs no import weight
# and cannot cycle back into the stack plane. No account bound → no-op.
from ...run_accounting import prompt_digest, record_llm_call, record_prompt_rendered
from ...schemas.stack import LLMProviderConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Unified response / chunk / tool-call shapes
# ---------------------------------------------------------------------------


@dataclass
class LLMUsage:
    """Per-call usage and cost.

    All token counts in raw integers (the provider's billing unit, i.e. BPE
    tokens for OpenAI/Anthropic). `cost_estimate_usd` is derived from
    `PRICE_TABLE` at parse time; the handler stamps it onto the response
    so downstream budget accounting (L-163) doesn't need the price table.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0  # o-series / Claude extended thinking
    cache_read_tokens: int = 0  # Anthropic prompt caching
    cache_write_tokens: int = 0
    total_tokens: int = 0
    cost_estimate_usd: float = 0.0
    model: str = ""


@dataclass
class LLMToolCall:
    """One tool invocation the model emitted, normalized across providers."""

    id: str                       # provider-supplied or hash-generated
    name: str                     # the tool name
    arguments: dict[str, Any]     # parsed JSON arguments


@dataclass
class LLMResponse:
    """Unified response from a single completion call."""

    content: str                  # final text output (excluding tool_use blocks)
    finish_reason: str            # normalized: stop | length | tool_calls | content_filter | error
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)
    raw_response: dict[str, Any] | None = None


@dataclass
class LLMChunk:
    """One streamed chunk from `stream_complete`."""

    delta_content: str = ""
    delta_tool_calls: list[LLMToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: LLMUsage | None = None  # populated on the terminal chunk
    raw_chunk: dict[str, Any] | None = None


# A `tools` argument follows the OpenAI Chat Completions JSON Schema spec
# on the wire (each tool is `{type: "function", function: {name, description,
# parameters: <JSONSchema>}}`); each subprovider translates to its native
# wire shape inside `_translate_tools`. This single canonical input shape
# means callers (e.g. analyst handlers in Phase 5/6) author tool specs once
# and reuse them across providers.
LLMTool = dict[str, Any]


# ---------------------------------------------------------------------------
# Lightweight context Protocols
#
# L-103 (runtime) owns the canonical `ConfigureContext` / `RuntimeContext`
# pydantic dataclasses. Those don't exist yet at L-120-land time; we define
# Protocols here that match the relevant slice from L-102 §1 so handlers
# can be constructed against test doubles today and against the real
# runtime context once L-160/L-161 land.
# ---------------------------------------------------------------------------


@runtime_checkable
class BudgetReporter(Protocol):
    """Per L-102 §7. Handlers report consumed tokens / cost; the runtime
    enforces envelope out-of-band. Phase-2 hook surface only."""

    async def record(
        self,
        *,
        kind: Literal["tokens", "api_call", "proxy_minute"],
        amount: int,
        dimension: str | None = None,
    ) -> None: ...

    async def check_envelope(self) -> Literal["ok", "throttle", "exhausted"]: ...


@runtime_checkable
class TelemetryHandle(Protocol):
    """Per L-102 §1. Bound to the instance by the runtime."""

    def log(self, level: int, msg: str, /, **fields: Any) -> None: ...

    def event(self, name: str, payload: Mapping[str, Any] | None = None) -> None: ...

    def span(self, name: str, /, **attrs: Any) -> Any: ...  # context-manager-ish


@runtime_checkable
class HandlerContext(Protocol):
    """Common context slice used by both Configure and Runtime phases.

    Subset of L-102 §1 `ConfigureContext` / `RuntimeContext`. Concrete L-103
    pydantic objects are duck-compatible: they expose these attrs.
    """

    instance_id: str
    instance_version: str
    secrets: CredentialResolverProtocol

    def telemetry(self) -> TelemetryHandle: ...


@runtime_checkable
class RuntimeContextLike(HandlerContext, Protocol):
    """Adds the budget reporter and a logger for run-time call sites."""

    budget: BudgetReporter | None


# ---------------------------------------------------------------------------
# Typed exceptions per L-102 §7
# ---------------------------------------------------------------------------


class TransientLLMFailure(Exception):
    """5xx / network / 429. Runtime retries per descriptor.method.retries."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class BudgetExhausted(Exception):
    """The budget envelope said `exhausted` — paused until next window."""


class HardLLMFailure(Exception):
    """4xx (auth / validation) or unrecoverable provider error."""

    def __init__(self, message: str, *, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


#: R11 — how a raised call classifies in the ``analyst_traces.llm_calls``
#: receipt. Keyed by exception CLASS NAME (not the class) so a subclass or a
#: re-declared double still buckets sensibly; anything unrecognized is "error".
#: The receipt never carries an exception MESSAGE — provider bodies can echo
#: request content and 4xx bodies can name credential ids.
_CALL_STATUS_BY_EXC: Mapping[str, str] = {
    "TransientLLMFailure": "transient_fail",
    "HardLLMFailure": "hard_fail",
    "BudgetExhausted": "budget_exhausted",
    "CancelledError": "cancelled",
    "TimeoutError": "timeout",
}


# ---------------------------------------------------------------------------
# Pricing tables (USD per 1M tokens; current as of mid-2026)
#
# The numbers below are the public list-price for each model at the time of
# writing. Operators can override `PRICE_TABLE` on a per-subprovider basis
# (subclass attribute) or stamp a custom price into a single LLMUsage by
# constructing it directly. Internal/self-hosted providers (vLLM)
# carry zero list price.
# ---------------------------------------------------------------------------


# `ModelPrice` lives in the dep-free `pricing` module so consumers that
# only need the static schema (e.g. `provenance.budget`) can import it
# without pulling `httpx` through this module. Re-exported here so the
# provider handlers keep their existing `from .base import ModelPrice`
# imports working.
from .pricing import ModelPrice  # noqa: F401  (re-export for back-compat)


def estimate_cost(model: str, usage: LLMUsage, price_table: Mapping[str, ModelPrice]) -> float:
    """Compute cost in USD from token counts + a price table. Returns 0.0 if
    the model isn't in the table (e.g. self-hosted vLLM)."""
    price = price_table.get(model)
    if price is None:
        # Try a prefix match — providers ship many minor revisions on a base
        # model name (e.g. claude-opus-4-7-20260301 vs claude-opus-4-7).
        for key, entry in price_table.items():
            if model.startswith(key):
                price = entry
                break
    if price is None:
        return 0.0
    cost = (
        (usage.prompt_tokens / 1_000_000) * price.input_per_m
        + (usage.completion_tokens / 1_000_000) * price.output_per_m
        + (usage.cache_read_tokens / 1_000_000) * price.cache_read_per_m
        + (usage.cache_write_tokens / 1_000_000) * price.cache_write_per_m
        + (usage.reasoning_tokens / 1_000_000) * price.reasoning_per_m
    )
    return round(cost, 6)


# ---------------------------------------------------------------------------
# Base handler
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _split_endpoint(endpoint: str, default_port: int) -> tuple[str | None, int, str]:
    """Parse `host:port` or `scheme://host:port[/path]` -> `(host, port, scheme)`."""
    if not endpoint:
        return None, default_port, "https"
    scheme = "https"
    rest = endpoint
    if "://" in endpoint:
        scheme, rest = endpoint.split("://", 1)
    rest = rest.split("/", 1)[0].split("?", 1)[0]
    if ":" in rest:
        host, _, port_str = rest.partition(":")
        try:
            port = int(port_str)
        except ValueError:
            port = default_port
    else:
        host = rest
        port = 80 if scheme == "http" else 443 if scheme == "https" else default_port
    return (host or None), port, scheme


class LLMProviderHandler:
    """Base class for LLM-provider stack-component handlers.

    Conforms to L-102 §1 `KindHandler` Protocol (kind, family, schema_version,
    config_schema, handler_version + lifecycle hooks + health_check +
    telemetry). Subclasses set the kind / endpoint defaults / pricing and
    implement the wire-shape methods.
    """

    # ---- KindHandler classvars (L-102 §1) --------------------------------
    kind: ClassVar[str] = "llm_provider"
    family: ClassVar[Literal["source", "filter", "output", "analyst", "discovery", "stack"]] = "stack"  # type: ignore[assignment]
    schema_version: ClassVar[str] = "legba/stack.llm_provider/1-0-0"
    config_schema: ClassVar[type] = LLMProviderConfig
    handler_version: ClassVar[str] = "0.1.0"

    # ---- Subprovider classvars -------------------------------------------
    #: Subprovider identifier (e.g. "anthropic", "vllm", "openai"). Used as
    #: the dispatcher-key when more than one LLM subhandler is registered.
    subprovider: ClassVar[str] = "base"
    #: Default port for endpoint TCP probes when the endpoint has no port.
    default_port: ClassVar[int] = 443
    #: Per-subprovider pricing table; subclasses override.
    PRICE_TABLE: ClassVar[Mapping[str, ModelPrice]] = {}

    # ---- Instance state --------------------------------------------------
    def __init__(self) -> None:
        self._cfg: LLMProviderConfig | None = None
        self._api_key: str | None = None
        # HTTP Basic credentials (alternative to the bearer key); resolved
        # from the vault in on_configure when api_user/api_pass are set.
        self._api_user: str | None = None
        self._api_pass: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._tel: TelemetryHandle | None = None
        self._instance_id: str = ""
        self._instance_version: str = ""
        self._model_list: list[str] = []  # populated on configure when supported

    # ---- KindHandler-facing surface --------------------------------------

    def telemetry(self) -> TelemetryHandle:
        """Return the bound telemetry handle. Subclasses can override to use
        a richer per-call span emitter once L-107 lands."""
        if self._tel is None:
            return _NoopTelemetry()
        return self._tel

    async def on_configure(self, ctx: HandlerContext) -> None:
        """Bind to the configured `LLMProviderConfig` instance.

        Per L-102 §1, `ctx.config` is the pydantic model parsed against
        `self.config_schema`. Idempotent: repeated calls re-resolve the
        credential and reset the HTTP client if the endpoint changed.
        """
        cfg = self._extract_config(ctx)
        self._instance_id = ctx.instance_id
        self._instance_version = ctx.instance_version
        self._tel = ctx.telemetry()

        # Resolve credentials to plaintext; never log the plaintext. Auth is
        # a switch: a bearer api_key OR an HTTP Basic api_user/api_pass pair.
        # Each is optional in the schema; resolve whichever is configured and
        # fail loud only when NEITHER resolves (see the guard below).
        self._api_key = None
        self._api_user = None
        self._api_pass = None

        if cfg.api_key is not None:
            secret_id = cfg.api_key.raw
            try:
                secret_bytes = await ctx.secrets.resolve(secret_id)
            except MissingSecretError as exc:
                raise HardLLMFailure(
                    f"vault missing api_key for {ctx.instance_id!r}: {secret_id!r}",
                ) from exc
            self._api_key = secret_bytes.decode("utf-8")

        if cfg.api_user is not None and cfg.api_pass is not None:
            user_id = cfg.api_user.raw
            pass_id = cfg.api_pass.raw
            try:
                user_bytes = await ctx.secrets.resolve(user_id)
                pass_bytes = await ctx.secrets.resolve(pass_id)
            except MissingSecretError as exc:
                raise HardLLMFailure(
                    f"vault missing basic-auth credential for "
                    f"{ctx.instance_id!r}: {user_id!r}/{pass_id!r}",
                ) from exc
            self._api_user = user_bytes.decode("utf-8")
            self._api_pass = pass_bytes.decode("utf-8")

        # Require at least one usable auth mode actually resolved.
        has_basic = self._api_user is not None and self._api_pass is not None
        if not self._api_key and not has_basic:
            raise HardLLMFailure(
                f"no auth credential resolved for {ctx.instance_id!r}: "
                f"set api_key (Bearer) or api_user+api_pass (HTTP Basic)",
            )

        # Reset the client iff the underlying endpoint shape changed.
        if self._cfg is None or self._cfg.api_endpoint.raw != cfg.api_endpoint.raw:
            if self._client is not None:
                await self._client.aclose()
                self._client = None

        self._cfg = cfg
        # Populate model list (best-effort; failure leaves list empty).
        try:
            self._model_list = await self._fetch_model_list()
        except Exception as exc:  # pragma: no cover — depends on provider
            logger.debug("on_configure(%s) model-list fetch failed: %s",
                         ctx.instance_id, exc)
            self._model_list = []

    async def on_activate(self, ctx: HandlerContext) -> None:
        """Open the HTTP client if it isn't already open."""
        _ = await self._get_client()

    async def on_pause(self, ctx: HandlerContext) -> None:
        """Close any open connection; preserve the parsed config so a
        subsequent `on_resume` doesn't re-resolve the vault key."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def on_resume(self, ctx: HandlerContext) -> None:
        """Re-open the HTTP client. Equivalent to `on_activate` if config
        didn't change."""
        _ = await self._get_client()

    async def on_retire(self, ctx: HandlerContext) -> None:
        """Final shutdown — close client, clear credential, forget config.
        Idempotent: subsequent calls are no-ops."""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # pragma: no cover
                pass
            self._client = None
        self._api_key = None
        self._api_user = None
        self._api_pass = None
        self._cfg = None

    async def health_check(
        self,
        ctx: HandlerContext | None = None,
    ) -> StackComponentHealth:
        """L-111-aligned probe: TCP reachability + vault key resolution.

        DOES NOT call the model — see module docstring for rationale.
        """
        if self._cfg is None:
            return StackComponentHealth(
                component_id=self._instance_id or "<unconfigured>",
                kind=self.kind,
                state=HealthState.UNHEALTHY,
                checked_at=_now(),
                detail="handler not configured (call on_configure first)",
            )

        endpoint = self._cfg.api_endpoint.raw
        host, port, scheme = _split_endpoint(endpoint, default_port=self.default_port)
        if not host:
            return StackComponentHealth(
                component_id=self._instance_id, kind=self.kind,
                state=HealthState.UNHEALTHY, checked_at=_now(),
                detail=f"unparseable endpoint {endpoint!r}",
            )

        has_basic = self._api_user is not None and self._api_pass is not None
        if not self._api_key and not has_basic:
            return StackComponentHealth(
                component_id=self._instance_id, kind=self.kind,
                state=HealthState.UNHEALTHY, checked_at=_now(),
                detail="auth credential not resolved (on_configure failed?)",
            )

        # Loop runs synchronously in a thread to avoid blocking the event
        # loop on a stuck connection.
        reachable = await asyncio.to_thread(_tcp_reachable, host, port)
        return StackComponentHealth(
            component_id=self._instance_id, kind=self.kind,
            state=HealthState.HEALTHY if reachable else HealthState.UNHEALTHY,
            checked_at=_now(),
            detail=f"{scheme}://{host}:{port} reachable={reachable}",
            last_success_at=_now() if reachable else None,
            extra={
                "subprovider": self.subprovider,
                "endpoint": endpoint,
                "model": self._cfg.model_name.raw,
                "model_list_size": len(self._model_list),
            },
        )

    # ---- LLM-specific surface --------------------------------------------

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        tools: list[LLMTool] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        system: str | None = None,
        temperature: float | None = None,
        ctx: RuntimeContextLike | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Single-turn chat completion.

        Args:
            messages: list of `{role, content}` dicts (OpenAI shape). The
                base translates to provider-native shape via
                `_translate_messages`.
            tools: optional OpenAI-tool-spec list; translated per provider.
            model: optional model override; defaults to the configured one.
            max_tokens: optional max output tokens; defaults to configured.
            reasoning_effort: o-series / GPT-5 / Claude extended thinking.
            system: top-level system prompt for providers that take it
                separately (Anthropic). For OpenAI-style providers the base
                prepends it as the first system message.
            ctx: optional runtime context for budget reporting hooks.
        """
        self._require_configured()
        cfg = self._cfg  # type: ignore[assignment]
        chosen_model = model or cfg.model_name.raw  # type: ignore[union-attr]
        chosen_max_tokens = max_tokens or int(cfg.max_tokens.raw)  # type: ignore[union-attr]

        # RUST-5 — ``analyst_traces.prompt_rendered``. Captured from the
        # ORIGINAL (pre-translation) ``messages``/``system`` — the same args
        # every caller passes regardless of provider — so this is one
        # provider-agnostic line instead of per-subprovider wire-shape
        # bookkeeping. Overwrites the run account's single slot; a no-op when
        # no account is bound (registry process, filter plane, ad-hoc
        # scripts, most tests). See run_accounting.py's module docstring.
        record_prompt_rendered(system, messages)

        # Budget gate (best-effort; runtime enforces out-of-band per KC-5).
        if ctx is not None and ctx.budget is not None:
            envelope = await ctx.budget.check_envelope()
            if envelope == "exhausted":
                raise BudgetExhausted(
                    f"budget envelope exhausted for {self._instance_id}",
                )

        wire_messages, wire_system = self._translate_messages(messages, system=system)
        wire_tools = self._translate_tools(tools) if tools else None

        payload = self._build_chat_payload(
            messages=wire_messages,
            system=wire_system,
            tools=wire_tools,
            model=chosen_model,
            max_tokens=chosen_max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            **kwargs,
        )

        # R11 — the single chokepoint every provider-plane LLM call passes
        # through (no subclass overrides ``chat_complete``, and the base
        # ``stream_complete`` delegates here), so this is where a run's
        # ``analyst_traces.llm_calls`` receipt is accumulated. Records BOTH
        # outcomes: a failed call is exactly the evidence the receipt was
        # missing. Entirely defensive — the accounting is wrapped so it can
        # never fail a call, and it is a no-op when no run account is bound.
        _acct_started = time.monotonic()
        _acct_response: LLMResponse | None = None
        _acct_exc: BaseException | None = None
        try:
            data = await self._call_chat(payload)
            response = self._parse_response(data, model=chosen_model)
            _acct_response = response
        except BaseException as exc:
            _acct_exc = exc
            raise
        finally:
            self._account_call(
                model=chosen_model,
                messages=wire_messages,
                system=wire_system,
                started_monotonic=_acct_started,
                response=_acct_response,
                exc=_acct_exc,
            )

        # Report tokens for budget tracking.
        if ctx is not None and ctx.budget is not None:
            total = (
                response.usage.prompt_tokens
                + response.usage.completion_tokens
                + response.usage.reasoning_tokens
            )
            if total > 0:
                await ctx.budget.record(
                    kind="tokens",
                    amount=total,
                    dimension=f"{self.subprovider}:{chosen_model}",
                )

        return response

    async def stream_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        tools: list[LLMTool] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        system: str | None = None,
        temperature: float | None = None,
        ctx: RuntimeContextLike | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMChunk]:
        """Streaming chat completion. Default base implementation falls back
        to a single `chat_complete` call wrapped as one chunk. Subproviders
        with native streaming override this."""
        response = await self.chat_complete(
            messages,
            tools=tools, model=model, max_tokens=max_tokens,
            reasoning_effort=reasoning_effort, system=system,
            temperature=temperature, ctx=ctx, **kwargs,
        )
        yield LLMChunk(
            delta_content=response.content,
            delta_tool_calls=list(response.tool_calls),
            finish_reason=response.finish_reason,
            usage=response.usage,
            raw_chunk=response.raw_response,
        )

    # ---- R11 receipt accounting ------------------------------------------

    def _account_call(
        self,
        *,
        model: str,
        messages: list[Mapping[str, Any]],
        system: str | None,
        started_monotonic: float,
        response: "LLMResponse | None",
        exc: BaseException | None,
    ) -> None:
        """Record one completed/failed call into the bound run account.

        Pure instrumentation: swallows everything. A raise here would either
        fail a run that succeeded or mask the provider exception propagating
        out of the ``finally`` that calls it — both unacceptable for a receipt
        field. When no run account is bound (the registry process, the filter
        plane, ad-hoc scripts, most tests) ``record_llm_call`` is a no-op and
        this costs one dict build.
        """
        try:
            prompt_sha, prompt_chars = prompt_digest(messages, system)
            fields: dict[str, Any] = {
                "component_id": self._instance_id or None,
                "subprovider": self.subprovider,
                "model": model,
                "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
                "prompt_sha256": prompt_sha,
                "prompt_chars": prompt_chars,
            }
            if exc is not None:
                fields["status"] = _CALL_STATUS_BY_EXC.get(
                    type(exc).__name__, "error",
                )
                fields["error"] = type(exc).__name__
                http_status = getattr(exc, "status", None)
                if isinstance(http_status, int):
                    fields["http_status"] = http_status
            else:
                fields["status"] = "success"
                if response is not None:
                    usage = response.usage
                    fields.update(
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        reasoning_tokens=usage.reasoning_tokens,
                        total_tokens=usage.total_tokens,
                        cost_estimate_usd=usage.cost_estimate_usd,
                        finish_reason=response.finish_reason,
                        tool_call_count=len(response.tool_calls),
                    )
                    # UPSTREAM SERVING PROVIDER (2026-08-16). `model` and
                    # `subprovider` name what we ASKED for and which handler
                    # class asked — neither names who actually served it. A
                    # router (OpenRouter) picks a provider per request, and
                    # measurement showed that choice is NOT cosmetic: the same
                    # model id, same prompt and same 94 critiques flipped 13.6%
                    # of pass/fail verdicts between two providers of the same
                    # weights (Nvidia vs DeepInfra) — including a pass-stratum
                    # claim, on a plane whose stated invariant is zero false
                    # passes. That drift was the same magnitude as an entire
                    # doctrine prompt rewrite, and it was structurally
                    # invisible: no receipt field could carry it, so no gauge
                    # could page on it. Recorded only when the response names
                    # one, so every non-routed provider's receipt stays
                    # byte-identical and the field's PRESENCE is itself the
                    # evidence that a router chose on our behalf.
                    served_by = (response.raw_response or {}).get("provider")
                    if isinstance(served_by, str) and served_by:
                        fields["served_by"] = served_by
                    # Prompt-caching receipt (Anthropic). Recorded only when
                    # NON-ZERO so every uncached provider's receipt stays
                    # byte-identical, and so `cache_read_tokens` appearing in
                    # a receipt is itself the evidence that a cached prefix
                    # was actually hit — the field you read to confirm the
                    # breakpoints are working on a live consult.
                    if usage.cache_read_tokens:
                        fields["cache_read_tokens"] = usage.cache_read_tokens
                    if usage.cache_write_tokens:
                        fields["cache_write_tokens"] = usage.cache_write_tokens
            record_llm_call(**fields)
        except Exception:  # pragma: no cover — instrumentation must never bite
            logger.debug("llm.account_call failed", exc_info=True)

    # ---- Provider hooks (subclass overrides) -----------------------------

    def _translate_messages(
        self,
        messages: list[Mapping[str, Any]],
        *,
        system: str | None,
    ) -> tuple[list[Mapping[str, Any]], str | None]:
        """Translate a unified message list into the provider's wire shape.

        Default implementation = OpenAI-style: if `system` is provided and
        the first message isn't already a system message, prepend it. The
        return is `(wire_messages, wire_system)`; `wire_system` is None for
        OpenAI-style providers.
        """
        wire = list(messages)
        if system and not (wire and wire[0].get("role") == "system"):
            wire = [{"role": "system", "content": system}, *wire]
        return wire, None

    def _translate_tools(self, tools: list[LLMTool]) -> list[Mapping[str, Any]]:
        """Translate OpenAI-spec tools to provider-native wire shape.
        Default = OpenAI passthrough (the input shape is OpenAI-native)."""
        return list(tools)

    def _build_chat_payload(
        self,
        *,
        messages: list[Mapping[str, Any]],
        system: str | None,
        tools: list[Mapping[str, Any]] | None,
        model: str,
        max_tokens: int,
        temperature: float | None,
        reasoning_effort: str | None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Assemble the JSON body for the chat endpoint. Subclass-specific."""
        raise NotImplementedError

    async def _call_chat(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """POST the payload to the chat endpoint and return parsed JSON.
        Default implementation: POST to `<api_endpoint>/<chat_path>` with
        retry/backoff on retryable HTTP codes. Subclass picks the path
        and auth header shape."""
        client = await self._get_client()
        path = self._chat_endpoint_path()
        retryable = {429, 500, 502, 503, 529}
        max_retries = 3
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                response = await client.post(path, json=dict(payload))
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise TransientLLMFailure(f"network error: {exc}") from exc

            if response.status_code in retryable and attempt < max_retries:
                # Honor `retry-after` if set.
                retry_after = response.headers.get("retry-after")
                wait_s = int(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                await asyncio.sleep(wait_s)
                continue

            if response.status_code >= 400:
                body = self._safe_body(response)
                # 4xx → HardLLMFailure (auth, bad request); 5xx → TransientLLMFailure
                if response.status_code in retryable:
                    raise TransientLLMFailure(
                        f"{self.subprovider} {response.status_code}: {body[:300]}",
                        status=response.status_code,
                    )
                raise HardLLMFailure(
                    f"{self.subprovider} {response.status_code}: {body[:300]}",
                    status=response.status_code, body=body[:1000],
                )

            try:
                return response.json()
            except ValueError as exc:
                raise HardLLMFailure(
                    f"{self.subprovider} returned non-JSON body: {response.text[:200]}",
                ) from exc

        raise last_exc or TransientLLMFailure("call failed after retries")

    def _chat_endpoint_path(self) -> str:
        """Provider-specific path under the configured api_endpoint."""
        raise NotImplementedError

    def _parse_response(self, data: Mapping[str, Any], *, model: str) -> LLMResponse:
        """Parse the provider's JSON body into a unified `LLMResponse`."""
        raise NotImplementedError

    async def _fetch_model_list(self) -> list[str]:
        """Best-effort `GET /v1/models` (OpenAI compatible) — providers
        without a discovery endpoint return [] (their default override)."""
        client = await self._get_client()
        try:
            resp = await client.get("/v1/models")
        except Exception:
            return []
        if resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        items = data.get("data") or []
        names: list[str] = []
        for item in items:
            if isinstance(item, dict) and "id" in item:
                names.append(str(item["id"]))
        return names

    def _auth_headers(self) -> dict[str, str]:
        """Return the auth headers for HTTP calls.

        Auth is a switch. PRECEDENCE: if HTTP Basic credentials (both
        api_user AND api_pass) are present, send
        ``Authorization: Basic <base64(user:pass)>`` (used by the verify
        judge / Caddy-fronted slm today). Otherwise fall back to the
        historical OpenAI-style ``Authorization: Bearer <api_key>`` —
        unchanged, so every existing api_key-only component is byte-for-byte
        backward-compatible.
        """
        if self._api_user is not None and self._api_pass is not None:
            token = base64.b64encode(
                f"{self._api_user}:{self._api_pass}".encode("utf-8")
            ).decode("ascii")
            authorization = f"Basic {token}"
        else:
            authorization = f"Bearer {self._api_key}"
        return {
            "Authorization": authorization,
            "Content-Type": "application/json",
        }

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        cfg = self._require_configured()
        # Subclasses' `_chat_endpoint_path()` prepends `/v1/...` (OpenAI
        # convention, mirrored by vLLM / gpt-oss-120b / Anthropic). If the
        # operator-supplied `api_endpoint` already ends in `/v1` the two
        # would double up (e.g. `/v1/v1/chat/completions` → 404). Strip
        # trailing `/v1` segments defensively so both
        # `https://host` and `https://host/v1` configs work.
        base_url = cfg.api_endpoint.raw.rstrip("/")
        while base_url.endswith("/v1"):
            base_url = base_url[:-3]
        timeout = float(cfg.timeout_seconds.raw)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=self._auth_headers(),
            timeout=httpx.Timeout(timeout),
        )
        return self._client

    # ---- helpers ---------------------------------------------------------

    def _require_configured(self) -> LLMProviderConfig:
        if self._cfg is None:
            raise HardLLMFailure(
                f"{self.subprovider} handler not configured; "
                "call on_configure() before chat_complete()",
            )
        return self._cfg

    def _extract_config(self, ctx: HandlerContext) -> LLMProviderConfig:
        """The runtime guarantees `ctx.config` is parsed against
        `self.config_schema`. For Phase-2 tests we accept the config either
        on `ctx.config` or on `ctx.cfg` (test doubles use either)."""
        cfg = getattr(ctx, "config", None)
        if cfg is None:
            cfg = getattr(ctx, "cfg", None)
        if cfg is None:
            raise HardLLMFailure(
                "HandlerContext missing `config` attribute (LLMProviderConfig)",
            )
        if not isinstance(cfg, LLMProviderConfig):
            # Test doubles may pass a plain dict — parse it.
            if isinstance(cfg, Mapping):
                cfg = LLMProviderConfig.model_validate(dict(cfg))
            else:
                raise HardLLMFailure(
                    f"unexpected config type {type(cfg).__name__}; "
                    "expected LLMProviderConfig",
                )
        return cfg

    @staticmethod
    def _safe_body(response: httpx.Response) -> str:
        try:
            return response.text
        except Exception:  # pragma: no cover
            return "<non-text body>"

    @property
    def model_list(self) -> list[str]:
        """The provider's catalog as fetched by `on_configure` (best-effort).
        Empty for providers without a discovery endpoint."""
        return list(self._model_list)


# ---------------------------------------------------------------------------
# No-op telemetry stand-in for tests / construction-without-runtime
# ---------------------------------------------------------------------------


class _NoopTelemetry:
    def log(self, level: int, msg: str, /, **fields: Any) -> None:
        pass

    def event(self, name: str, payload: Mapping[str, Any] | None = None) -> None:
        pass

    def span(self, name: str, /, **attrs: Any):  # pragma: no cover
        return _NoopSpan()


class _NoopSpan:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
