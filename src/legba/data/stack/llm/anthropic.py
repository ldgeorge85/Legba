# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Anthropic LLM provider handler (L-120).

Wraps the Anthropic Messages API (`POST /v1/messages`) per M-061/062 native
tool-calling. Tool-arg translation: the unified OpenAI tool-spec shape
`{type: "function", function: {name, description, parameters}}` is converted
to Anthropic's `{name, description, input_schema}` shape on the wire.

Auth: `x-api-key` header (not Bearer).
System prompt: top-level `system` field, not a message role.
Max tokens: required by Anthropic; defaults from descriptor config, clamped
to the model line's documented per-request output ceiling (see
`MAX_OUTPUT_TOKENS_BY_PREFIX`).
Streaming: EVERY generation goes over the wire with `stream: true` and the
SSE events are accumulated back into the same JSON shape the non-streaming
endpoint returns — transparent to `_parse_response` and every caller. The
enabler for large output budgets: a non-streaming 4k+ generation used to
outrun the provider HTTP window (network error → actor retry storm → 504),
while a live stream resets the read timeout with every delta. See
`_call_chat_streaming` for the retry + mid-stream-failure contract.
Stop reasons: `end_turn` / `max_tokens` / `tool_use` / `stop_sequence`
normalized to `stop` / `length` / `tool_calls`.
Prompt caching: `cache_control` breakpoints on the stable prefix — see
`_apply_cache_control` and the `CACHE_*` classvars.

Pricing (USD per 1M tokens, mid-2026 list price; override via subclass or
env if Anthropic publishes a change before the registry's pricing-update
task lands):

  * claude-opus-4-8      : input 15 / output 75 / cache_read 1.50 / cache_write 18.75
  * claude-opus-4-7      : input 15 / output 75 / cache_read 1.50 / cache_write 18.75
  * claude-sonnet-4-6    : input 3  / output 15  / cache_read 0.30 / cache_write 3.75
  * claude-sonnet-4-5    : input 3  / output 15  / cache_read 0.30 / cache_write 3.75
  * claude-haiku-3-5     : input 0.80 / output 4 / cache_read 0.08 / cache_write 1.00
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncIterator, ClassVar, Mapping

import httpx

from .base import (
    HardLLMFailure,
    LLMProviderHandler,
    LLMResponse,
    LLMToolCall,
    LLMUsage,
    ModelPrice,
    TransientLLMFailure,
    estimate_cost,
)

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"

_STOP_REASON_MAP = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
}

#: The marker Anthropic reads as "cache the prefix ending here". `ephemeral` is
#: the 5-minute TTL — the right one for a tool loop whose rounds are seconds
#: apart. A 1h TTL doubles the write premium and buys nothing within one run.
_CACHE_CONTROL = {"type": "ephemeral"}


def _strip_cache_control(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return `payload` with every cache breakpoint removed.

    Restores the HISTORICAL uncached wire shape exactly — a one-block `system`
    list collapses back to the bare string, a one-block message content
    collapses back to its text — so the retry after a rejected marker is
    byte-identical to what this handler sent before caching existed.
    """
    out = dict(payload)

    def _uncache(block: Any) -> Any:
        if isinstance(block, Mapping):
            return {k: v for k, v in block.items() if k != "cache_control"}
        return block

    def _collapse(blocks: Any) -> Any:
        """A one-text-block list → its bare string; otherwise strip in place."""
        if not isinstance(blocks, list):
            return _uncache(blocks)
        cleaned = [_uncache(b) for b in blocks]
        if (
            len(cleaned) == 1
            and isinstance(cleaned[0], Mapping)
            and set(cleaned[0]) == {"type", "text"}
            and cleaned[0].get("type") == "text"
        ):
            return cleaned[0]["text"]
        return cleaned

    if "system" in out:
        out["system"] = _collapse(out["system"])
    if isinstance(out.get("tools"), list):
        out["tools"] = [_uncache(t) for t in out["tools"]]
    if isinstance(out.get("messages"), list):
        msgs: list[Any] = []
        for msg in out["messages"]:
            if isinstance(msg, Mapping) and "content" in msg:
                msg = {**msg, "content": _collapse(msg["content"])}
            msgs.append(msg)
        out["messages"] = msgs
    return out


def _has_cache_control(payload: Mapping[str, Any]) -> bool:
    """True when the assembled payload actually carries a breakpoint."""
    def _marked(blocks: Any) -> bool:
        if isinstance(blocks, Mapping):
            return "cache_control" in blocks
        if isinstance(blocks, list):
            return any(_marked(b) for b in blocks)
        return False

    if _marked(payload.get("system")) or _marked(payload.get("tools")):
        return True
    messages = payload.get("messages")
    if isinstance(messages, list):
        return any(
            _marked(m.get("content")) for m in messages if isinstance(m, Mapping)
        )
    return False


class AnthropicProviderHandler(LLMProviderHandler):
    """Concrete handler for Anthropic Messages API (Claude family)."""

    subprovider: ClassVar[str] = "anthropic"
    schema_version: ClassVar[str] = "legba/stack.llm_provider/1-0-0"
    handler_version: ClassVar[str] = "0.1.0"
    default_port: ClassVar[int] = 443

    #: Model-name prefixes that REJECT the `temperature` param — Anthropic
    #: deprecated it on the Opus 4.8+ line (sending it 400s with
    #: "`temperature` is deprecated for this model."). Older models (Sonnet 4.x,
    #: Haiku) still accept it, so this is prefix-scoped, not a blanket drop.
    #:
    #: The drop is LOGGED (once per handler per model, see
    #: `_log_dropped_temperature`). It used to be silent, which made every
    #: caller's `temperature` setting on this plane a dead knob nobody could
    #: see: the consult kind has been passing temperature=0.2 to a deployed
    #: claude-opus-4-8 for as long as it has been pointed there, tuning nothing.
    TEMPERATURE_DEPRECATED_PREFIXES: ClassVar[tuple[str, ...]] = (
        "claude-opus-4-8",
    )

    #: Anthropic's DOCUMENTED per-request output ceilings (`max_tokens` upper
    #: bound) by model-family prefix, verified against the published model
    #: catalog 2026-08-15: the Opus 4.7/4.8 and Sonnet 4.6 lines take up to
    #: 128K output tokens per request (streaming required for large outputs —
    #: which this handler now always does); Sonnet 4.5 caps at 64K and the
    #: legacy Haiku 3.5 at 8192. A caller's `max_tokens` above the ceiling is
    #: CLAMPED with a loud log (see `_log_clamped_max_tokens`) rather than
    #: allowed to 400 at the provider. The deployed consult budget (32768)
    #: sits well under the Opus ceiling, so the clamp is a guard rail, not an
    #: expected path.
    MAX_OUTPUT_TOKENS_BY_PREFIX: ClassVar[Mapping[str, int]] = {
        "claude-opus-4-8": 128_000,
        "claude-opus-4-7": 128_000,
        "claude-sonnet-4-6": 128_000,
        "claude-sonnet-4-5": 64_000,
        "claude-haiku-3-5": 8_192,
    }
    #: Ceiling for models with no prefix match above — the current-generation
    #: documented maximum.
    MAX_OUTPUT_TOKENS_DEFAULT: ClassVar[int] = 128_000

    # ---- Prompt caching --------------------------------------------------
    #
    # A QUALITY enabler on the operator-paid plane, not a cost trim: Anthropic
    # bills a cached-prefix read at ~0.1x input and a cache write at ~1.25x, so
    # a loop that re-sends a byte-identical prefix every round pays several
    # times over for tokens the provider already holds. The consult ReAct loop
    # re-sends a ~10.4KB system prompt AND a monotonically growing transcript
    # on each of 7-10 rounds; caching that prefix is what buys LONGER and
    # RICHER consults at the same spend, not the same consult for less.
    #
    # Two breakpoints, both on prefix content that is stable by construction
    # (max 4 per request, so this leaves headroom):
    #
    #   1. `system` — byte-identical on every round of a run. Anthropic renders
    #      `tools` BEFORE `system`, so this one breakpoint caches the tool
    #      roster with it; the roster is only marked separately when there is
    #      no cacheable system block for it to ride (see `_apply_cache_control`).
    #   2. the LAST message — a tool loop only ever APPENDS to its transcript,
    #      so round N+1's prompt has round N's as a byte-identical prefix: the
    #      moving breakpoint reads what the previous round wrote and writes
    #      only the delta.
    #
    #: Master switch — flip off to restore the pre-caching wire shape exactly.
    CACHE_CONTROL_ENABLED: ClassVar[bool] = True
    #: Below Anthropic's minimum cacheable prefix (~1k tokens on the deployed
    #: Opus tier) a breakpoint is silently ignored — no error, and no write
    #: premium either. These char floors therefore only avoid pointless payload
    #: churn on short prompts; they are not a correctness guard.
    CACHE_MIN_SYSTEM_CHARS: ClassVar[int] = 4000
    CACHE_MIN_TRANSCRIPT_CHARS: ClassVar[int] = 4000

    def __init__(self) -> None:
        super().__init__()
        #: Flipped False — once, loudly — if the endpoint ever rejects a
        #: `cache_control` marker (an older API version, a cache-unaware
        #: Anthropic-compatible proxy). Caching is an optimization, never a
        #: correctness requirement: see `_call_chat`, which strips the markers
        #: and retries rather than failing the caller's turn.
        self._cache_control_ok: bool = True
        #: Models already logged for a dropped dead knob — see
        #: `_log_dropped_temperature`. Once per handler per model, NOT per call:
        #: a 10-round consult must surface the dead knob without emitting ten
        #: identical warnings.
        self._temperature_drop_logged: set[str] = set()
        #: Models already logged for a clamped over-ceiling `max_tokens` — see
        #: `_log_clamped_max_tokens`. Same once-per-model discipline.
        self._max_tokens_clamp_logged: set[str] = set()

    PRICE_TABLE: ClassVar[Mapping[str, ModelPrice]] = {
        # Use family-prefix keys so any minor revision rolls up under the same
        # price tier (e.g. claude-opus-4-7-20260301).
        "claude-opus-4-8": ModelPrice(
            # Opus pricing tier (same as 4-7); update if Anthropic revises it.
            input_per_m=15.0, output_per_m=75.0,
            cache_read_per_m=1.50, cache_write_per_m=18.75,
        ),
        "claude-opus-4-7": ModelPrice(
            input_per_m=15.0, output_per_m=75.0,
            cache_read_per_m=1.50, cache_write_per_m=18.75,
        ),
        "claude-sonnet-4-6": ModelPrice(
            input_per_m=3.0, output_per_m=15.0,
            cache_read_per_m=0.30, cache_write_per_m=3.75,
        ),
        "claude-sonnet-4-5": ModelPrice(
            input_per_m=3.0, output_per_m=15.0,
            cache_read_per_m=0.30, cache_write_per_m=3.75,
        ),
        "claude-haiku-3-5": ModelPrice(
            input_per_m=0.80, output_per_m=4.0,
            cache_read_per_m=0.08, cache_write_per_m=1.00,
        ),
    }

    # ---- Auth ------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key or "",
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    # ---- Endpoint --------------------------------------------------------

    def _chat_endpoint_path(self) -> str:
        return "/v1/messages"

    # ---- Message + tool translation --------------------------------------

    def _translate_messages(
        self,
        messages: list[Mapping[str, Any]],
        *,
        system: str | None,
    ) -> tuple[list[Mapping[str, Any]], str | None]:
        """Anthropic does NOT take a system role inside the messages list.

        If the caller put a system message as the first entry, hoist it out;
        otherwise honor the `system` kwarg as-is.
        """
        wire: list[Mapping[str, Any]] = []
        wire_system: str | None = system
        for msg in messages:
            role = msg.get("role")
            if role == "system":
                content = msg.get("content") or ""
                wire_system = (
                    f"{wire_system}\n{content}" if wire_system else content
                )
                continue
            if role == "tool":
                # Anthropic has no "tool" role (it is an OpenAI convention). The
                # text-protocol tool loops (consult / deep_consult) append the
                # tool result as a {"role": "tool", "name", "content"} message;
                # fold it into a user message so the model reads it and Anthropic
                # does not 400 on the unknown role.
                name = msg.get("name")
                content = msg.get("content") or ""
                label = f"Tool result ({name}):" if name else "Tool result:"
                wire.append({"role": "user", "content": f"{label}\n{content}"})
                continue
            wire.append(msg)
        return wire, wire_system

    def _translate_tools(self, tools: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """Convert OpenAI tool-spec → Anthropic tool-spec.

        OpenAI shape:
            {type: "function", function: {name, description, parameters: <JSONSchema>}}
        Anthropic shape:
            {name, description, input_schema: <JSONSchema>}
        """
        result: list[Mapping[str, Any]] = []
        for spec in tools:
            if "function" in spec:
                fn = spec.get("function", {})
                result.append({
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {}),
                })
            elif "name" in spec and "input_schema" in spec:
                # Already Anthropic-shaped — passthrough (operator
                # convenience).
                result.append(dict(spec))
            else:
                # Best effort: pass it through; let Anthropic validate.
                result.append(dict(spec))
        return result

    # ---- Payload assembly ------------------------------------------------

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
        ceiling = self._max_output_ceiling(model)
        if max_tokens > ceiling:
            self._log_clamped_max_tokens(model, max_tokens, ceiling)
            max_tokens = ceiling
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "max_tokens": max_tokens,
            # ALWAYS stream on the wire (accumulated back to one response in
            # `_call_chat_streaming`): a live stream keeps the HTTP connection
            # alive for the whole generation, so a large output budget can no
            # longer outrun the provider window the way non-streaming 4k+
            # generations did (network error → actor retry storm → 504).
            "stream": True,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = list(tools)
        if temperature is not None:
            if any(
                model.startswith(p) for p in self.TEMPERATURE_DEPRECATED_PREFIXES
            ):
                self._log_dropped_temperature(model, temperature)
            else:
                payload["temperature"] = temperature
        if reasoning_effort:
            # Extended thinking — only honored on opus / sonnet 4.x.
            # Anthropic shape: `{type: "enabled", budget_tokens: N}`.
            budget = {
                "low": 4_000,
                "medium": 16_000,
                "high": 32_000,
            }.get(reasoning_effort, 16_000)
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        stop = kwargs.pop("stop", None)
        if stop:
            payload["stop_sequences"] = list(stop)
        # Carry through any caller-supplied extras (e.g. metadata).
        for k, v in kwargs.items():
            payload.setdefault(k, v)
        self._apply_cache_control(payload)
        return payload

    # ---- Dead-knob hygiene -----------------------------------------------

    def _log_dropped_temperature(self, model: str, temperature: float) -> None:
        """WARN once per handler per model that a caller's `temperature` is a
        DEAD KNOB on this plane.

        Anthropic 400s on `temperature` for the opus-4-8+ line, so the payload
        builder drops it — silently, until now. A silent drop is the worst of
        both worlds: the caller believes it is tuning determinism and the
        operator has no way to find out it isn't. WARNING (not DEBUG) because
        the whole point is that this is visible in the runtime log without
        anyone going looking; once per model (not per call) because a 10-round
        consult would otherwise emit ten identical lines.
        """
        if model in self._temperature_drop_logged:
            return
        self._temperature_drop_logged.add(model)
        logger.warning(
            "anthropic.temperature.DEAD_KNOB component=%s model=%s requested=%s "
            "— this model deprecated `temperature`; the parameter is NOT sent "
            "and has no effect. Tune behaviour via the prompt, or point the "
            "caller at a plane that honors it. (logged once per model)",
            self._instance_id or "<unconfigured>", model, temperature,
        )

    def _max_output_ceiling(self, model: str) -> int:
        """The documented per-request output ceiling for `model`."""
        for prefix, ceiling in self.MAX_OUTPUT_TOKENS_BY_PREFIX.items():
            if model.startswith(prefix):
                return ceiling
        return self.MAX_OUTPUT_TOKENS_DEFAULT

    def _log_clamped_max_tokens(
        self, model: str, requested: int, ceiling: int,
    ) -> None:
        """WARN once per handler per model that an over-ceiling `max_tokens`
        was CLAMPED to Anthropic's documented per-request output limit.

        Loud on purpose: a descriptor asking for more than the model line can
        serve is a configuration error the operator should see and fix (lower
        the descriptor's ``method.llm.max_tokens``), not a silent adjustment.
        Clamping — instead of letting the provider 400 the call — keeps the
        consult answering while the config is wrong.
        """
        if model in self._max_tokens_clamp_logged:
            return
        self._max_tokens_clamp_logged.add(model)
        logger.warning(
            "anthropic.max_tokens.CLAMPED component=%s model=%s requested=%d "
            "ceiling=%d — Anthropic's documented per-request output limit for "
            "this model line is %d tokens; sending the ceiling instead of "
            "letting the provider 400. Lower the caller's max_tokens "
            "(descriptor method.llm.max_tokens) to silence this. "
            "(logged once per model)",
            self._instance_id or "<unconfigured>", model, requested, ceiling,
            ceiling,
        )

    # ---- Prompt caching --------------------------------------------------

    def _apply_cache_control(self, payload: dict[str, Any]) -> None:
        """Mark the stable prefix of an assembled payload for caching.

        Mutates `payload` in place. SHAPE-GUARDED throughout: every branch
        recognizes the exact shape it rewrites and leaves anything else
        untouched, so an unusual content shape degrades to the historical
        uncached payload rather than a malformed one.
        """
        if not (self.CACHE_CONTROL_ENABLED and self._cache_control_ok):
            return

        # 1. System — the byte-identical per-run prefix. `tools` renders ahead
        #    of it, so this breakpoint caches the tool roster too.
        system = payload.get("system")
        system_cached = False
        if isinstance(system, str) and len(system) >= self.CACHE_MIN_SYSTEM_CHARS:
            payload["system"] = [
                {"type": "text", "text": system, "cache_control": dict(_CACHE_CONTROL)},
            ]
            system_cached = True

        # 2. Tool roster — only when it has no system breakpoint to ride. The
        #    consult/deep planes send no `tools` param at all (their roster is
        #    prose inside the system prompt), so this covers native-tool callers.
        tools = payload.get("tools")
        if (
            not system_cached
            and isinstance(tools, list)
            and tools
            and isinstance(tools[-1], Mapping)
        ):
            payload["tools"] = [
                *tools[:-1],
                {**dict(tools[-1]), "cache_control": dict(_CACHE_CONTROL)},
            ]

        # 3. Transcript — the moving breakpoint on the newest turn. Skipped for
        #    a one-shot call (a single message), where the write could never be
        #    read back and would only cost the 1.25x premium.
        messages = payload.get("messages")
        if not isinstance(messages, list) or len(messages) < 2:
            return
        transcript_chars = sum(
            len(m.get("content") or "")
            for m in messages
            if isinstance(m, Mapping) and isinstance(m.get("content"), str)
        )
        if transcript_chars < self.CACHE_MIN_TRANSCRIPT_CHARS:
            return
        last = messages[-1]
        if not isinstance(last, Mapping):
            return
        content = last.get("content")
        if isinstance(content, str) and content:
            marked: Any = [
                {"type": "text", "text": content, "cache_control": dict(_CACHE_CONTROL)},
            ]
        elif (
            isinstance(content, list)
            and content
            and isinstance(content[-1], Mapping)
        ):
            marked = [
                *content[:-1],
                {**dict(content[-1]), "cache_control": dict(_CACHE_CONTROL)},
            ]
        else:
            return  # unrecognized content shape — leave the payload alone
        payload["messages"] = [*messages[:-1], {**dict(last), "content": marked}]

    async def _call_chat(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Wire the payload, with a one-shot fallback if caching is rejected.

        Prompt caching is an optimization; a consult must never fail because a
        cache hint was unwelcome. If the endpoint 400s on a `cache_control`
        marker, we disable caching for this handler instance (logged ONCE),
        strip the markers back to the historical wire shape, and retry the same
        call. Every subsequent call on this instance is assembled uncached.
        A cache rejection is always PRE-STREAM (the 400 arrives instead of the
        200 that opens the stream), so the retry never duplicates generation.
        """
        try:
            return await self._wire_call(payload)
        except HardLLMFailure as exc:
            if not self._cache_control_ok or not _has_cache_control(payload):
                raise
            detail = f"{exc} {getattr(exc, 'body', '') or ''}"
            if "cache_control" not in detail and "cache-control" not in detail.lower():
                raise
            self._cache_control_ok = False
            logger.warning(
                "anthropic.cache_control.rejected component=%s status=%s — prompt "
                "caching DISABLED for this handler instance; retrying uncached "
                "(cost/latency regress, correctness unaffected)",
                self._instance_id or "<unconfigured>",
                getattr(exc, "status", None),
            )
            return await self._wire_call(_strip_cache_control(payload))

    async def _wire_call(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Route one assembled payload to the right wire shape.

        `stream: true` payloads (the default — see `_build_chat_payload`) go
        through the SSE accumulator; anything else keeps the base handler's
        plain JSON POST, so an operator flipping streaming off in a payload
        override degrades to the historical wire call exactly.
        """
        if payload.get("stream"):
            return await self._call_chat_streaming(payload)
        return await super()._call_chat(payload)

    async def _call_chat_streaming(
        self, payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """POST with `stream: true`; accumulate SSE back to one response dict.

        The return value has the SAME shape as the non-streaming Messages API
        body (`content` block list, `usage`, `stop_reason`), so
        `_parse_response` and every call site are unchanged.

        RETRY CONTRACT. Pre-stream failures — connect errors and HTTP status
        codes, which arrive before any token is generated — follow the base
        handler's retry/backoff exactly (same retryable set, same attempt
        count, `retry-after` honored). Once the stream is OPEN, a failure is
        handled by `_accumulate_stream` and is NEVER retried here: the
        provider has already generated (and billed) the streamed prefix, and
        a retry would generate a second, divergent copy of it.

        TIMEOUT SEMANTICS. httpx applies the client's read timeout PER READ —
        on a stream that means per chunk, not per body. A live generation
        resets the clock with every delta, so wall time is unbounded by the
        read timeout and only a genuine stall (including a long prefill gap
        before `message_start`) can trip it. That is exactly the semantics a
        32k output budget needs; the component's `timeout_seconds` is the
        stall ceiling, not a generation ceiling.
        """
        client = await self._get_client()
        path = self._chat_endpoint_path()
        retryable = {429, 500, 502, 503, 529}
        max_retries = 3
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            stream_open = False
            try:
                async with client.stream(
                    "POST", path, json=dict(payload),
                ) as response:
                    status = response.status_code
                    if status in retryable and attempt < max_retries:
                        retry_after = response.headers.get("retry-after")
                        wait_s = (
                            int(retry_after)
                            if retry_after and retry_after.isdigit()
                            else 2 ** attempt
                        )
                        await asyncio.sleep(wait_s)
                        continue
                    if status >= 400:
                        raw = await response.aread()
                        body = raw.decode("utf-8", errors="replace")
                        if status in retryable:
                            raise TransientLLMFailure(
                                f"{self.subprovider} {status}: {body[:300]}",
                                status=status,
                            )
                        raise HardLLMFailure(
                            f"{self.subprovider} {status}: {body[:300]}",
                            status=status, body=body[:1000],
                        )
                    content_type = response.headers.get("content-type", "")
                    if "text/event-stream" not in content_type:
                        # An Anthropic-compatible proxy that ignored
                        # `stream: true` and returned the complete JSON body.
                        # Accept it — streaming is a transport optimization,
                        # never a correctness requirement.
                        raw = await response.aread()
                        try:
                            return json.loads(raw.decode("utf-8", errors="replace"))
                        except (json.JSONDecodeError, ValueError) as exc:
                            raise HardLLMFailure(
                                f"{self.subprovider} returned non-JSON non-SSE "
                                f"body: {raw[:200]!r}",
                            ) from exc
                    stream_open = True
                    return await self._accumulate_stream(response)
            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
            ) as exc:
                if stream_open:
                    # Defensive: `_accumulate_stream` catches transport errors
                    # itself, so this arm should be unreachable — but a stream
                    # that already produced billable output must NEVER be
                    # retried into a duplicate generation.
                    raise TransientLLMFailure(
                        f"mid-stream network error: {exc}",
                    ) from exc
                last_exc = exc
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise TransientLLMFailure(f"network error: {exc}") from exc
        raise last_exc or TransientLLMFailure("call failed after retries")

    async def _accumulate_stream(
        self, response: httpx.Response,
    ) -> dict[str, Any]:
        """Fold the SSE event stream into one non-streaming-shaped body.

        Event handling per the Anthropic streaming contract: `message_start`
        seeds the message envelope (id/model/role + input-side usage);
        `content_block_start` / `content_block_delta` / `content_block_stop`
        build the content blocks (`text_delta` text, `input_json_delta` tool
        args, `thinking_delta` thinking); `message_delta` carries the final
        `stop_reason` + output-side usage; `message_stop` ends the message.
        `ping` and unknown forward-compat events are ignored.

        MID-STREAM FAILURES ARE HANDLED HONESTLY, never as silent truncation:

          * failure BEFORE any content accumulated → raise
            :class:`TransientLLMFailure` (nothing was generated, so the
            runtime's retry classification cannot duplicate a generation);
          * failure AFTER partial content → return the partial body with
            ``stop_reason: "error"`` (normalized to ``finish_reason="error"``
            — an EXPLICIT error state every caller can see) plus a
            ``legba_stream_error`` marker in the raw body, logged loudly.
            The call is NOT retried: the streamed prefix is already billed.
        """
        message: dict[str, Any] = {}
        blocks: dict[int, dict[str, Any]] = {}
        text_parts: dict[int, list[str]] = {}
        thinking_parts: dict[int, list[str]] = {}
        tool_json_parts: dict[int, list[str]] = {}
        final_usage: dict[str, Any] = {}
        stop_reason: str | None = None
        stop_sequence: Any = None
        saw_message_stop = False
        stream_error: str | None = None
        try:
            async for event in self._iter_sse_events(response):
                etype = event.get("type")
                if etype == "message_start":
                    msg = event.get("message")
                    if isinstance(msg, Mapping):
                        message = dict(msg)
                        usage = message.get("usage")
                        if isinstance(usage, Mapping):
                            final_usage.update(usage)
                elif etype == "content_block_start":
                    idx = int(event.get("index") or 0)
                    block = event.get("content_block")
                    blocks[idx] = dict(block) if isinstance(block, Mapping) else {}
                    btype = blocks[idx].get("type")
                    if btype == "text":
                        text_parts[idx] = [str(blocks[idx].get("text") or "")]
                    elif btype == "thinking":
                        thinking_parts[idx] = [
                            str(blocks[idx].get("thinking") or ""),
                        ]
                    elif btype == "tool_use":
                        tool_json_parts[idx] = []
                elif etype == "content_block_delta":
                    idx = int(event.get("index") or 0)
                    delta = event.get("delta")
                    if not isinstance(delta, Mapping):
                        continue
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        blocks.setdefault(idx, {"type": "text"})
                        text_parts.setdefault(idx, []).append(
                            str(delta.get("text") or ""),
                        )
                    elif dtype == "input_json_delta":
                        blocks.setdefault(idx, {"type": "tool_use"})
                        tool_json_parts.setdefault(idx, []).append(
                            str(delta.get("partial_json") or ""),
                        )
                    elif dtype == "thinking_delta":
                        blocks.setdefault(idx, {"type": "thinking"})
                        thinking_parts.setdefault(idx, []).append(
                            str(delta.get("thinking") or ""),
                        )
                elif etype == "message_delta":
                    delta = event.get("delta")
                    if isinstance(delta, Mapping):
                        if delta.get("stop_reason"):
                            stop_reason = str(delta["stop_reason"])
                        if delta.get("stop_sequence") is not None:
                            stop_sequence = delta.get("stop_sequence")
                    usage = event.get("usage")
                    if isinstance(usage, Mapping):
                        final_usage.update(usage)
                elif etype == "message_stop":
                    saw_message_stop = True
                    break
                elif etype == "error":
                    err = event.get("error")
                    stream_error = (
                        "provider error event: "
                        + json.dumps(err, default=str)[:300]
                    )
                    break
                # "ping" / unknown event types: intentionally ignored.
        except httpx.HTTPError as exc:
            stream_error = f"{type(exc).__name__}: {exc}"

        if stream_error is None and not saw_message_stop:
            stream_error = "stream closed before message_stop"

        content: list[dict[str, Any]] = []
        for idx in sorted(blocks):
            block = dict(blocks[idx])
            btype = block.get("type")
            if btype == "text":
                block["text"] = "".join(text_parts.get(idx, []))
            elif btype == "thinking":
                block["thinking"] = "".join(thinking_parts.get(idx, []))
            elif btype == "tool_use":
                raw_json = "".join(tool_json_parts.get(idx, []))
                if raw_json.strip():
                    try:
                        block["input"] = json.loads(raw_json)
                    except (json.JSONDecodeError, ValueError):
                        # A cut stream can truncate the args JSON mid-string.
                        # Surface the raw text — never invent arguments.
                        block["input"] = {"_raw": raw_json}
                else:
                    block.setdefault("input", {})
            content.append(block)

        data: dict[str, Any] = dict(message)
        data["content"] = content
        data["usage"] = final_usage
        if stop_reason is not None:
            data["stop_reason"] = stop_reason
        if stop_sequence is not None:
            data["stop_sequence"] = stop_sequence

        if stream_error is not None:
            has_content = any(
                (b.get("type") == "text" and b.get("text"))
                or b.get("type") == "tool_use"
                for b in content
            )
            if not has_content:
                raise TransientLLMFailure(
                    "anthropic stream failed before any content arrived: "
                    + stream_error,
                )
            logger.warning(
                "anthropic.stream.MID_STREAM_FAILURE component=%s model=%s "
                "err=%s — returning the PARTIAL generation with "
                "finish_reason='error' (explicit error state, not a silent "
                "truncation). NOT retried: the streamed prefix is already "
                "generated and billed; a retry would produce a duplicate "
                "generation.",
                self._instance_id or "<unconfigured>",
                data.get("model") or "<unknown>",
                stream_error,
            )
            data["stop_reason"] = "error"
            data["legba_stream_error"] = stream_error

        return data

    @staticmethod
    async def _iter_sse_events(
        response: httpx.Response,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed `data:` payloads from an SSE body.

        Anthropic stamps the event type INSIDE each data payload
        (``{"type": ...}``), so ``event:`` lines are redundant and skipped.
        Multi-line data segments are joined per the SSE spec; a payload that
        fails to parse is skipped rather than fatal — the accumulator's
        missing-`message_stop` check catches a stream that carried nothing
        usable.
        """
        data_lines: list[str] = []

        def _flush() -> dict[str, Any] | None:
            if not data_lines:
                return None
            raw = "\n".join(data_lines)
            data_lines.clear()
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return None
            return parsed if isinstance(parsed, dict) else None

        async for line in response.aiter_lines():
            if line == "":
                event = _flush()
                if event is not None:
                    yield event
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
        # Trailing event without a terminating blank line (seen on abrupt
        # stream closure) — flush it too.
        event = _flush()
        if event is not None:
            yield event

    # ---- Response parsing ------------------------------------------------

    def _parse_response(self, data: Mapping[str, Any], *, model: str) -> LLMResponse:
        """Walk the content-block list. `text` blocks → content; `tool_use`
        blocks → tool_calls; `thinking` blocks → ignored (tokens still
        billed; we count them in `reasoning_tokens`)."""
        content_parts: list[str] = []
        tool_calls: list[LLMToolCall] = []
        for block in data.get("content", []) or []:
            btype = block.get("type")
            if btype == "text":
                content_parts.append(block.get("text", "") or "")
            elif btype == "tool_use":
                arguments = block.get("input", {}) or {}
                if not isinstance(arguments, dict):
                    arguments = {"_value": arguments}
                tool_calls.append(
                    LLMToolCall(
                        id=str(block.get("id") or uuid.uuid4().hex),
                        name=str(block.get("name") or ""),
                        arguments=arguments,
                    )
                )
            # `thinking` blocks intentionally skipped from content.

        raw_usage = data.get("usage", {}) or {}
        prompt_tokens = int(raw_usage.get("input_tokens", 0))
        completion_tokens = int(raw_usage.get("output_tokens", 0))
        # Anthropic surfaces cache_read_input_tokens / cache_creation_input_tokens
        cache_read = int(raw_usage.get("cache_read_input_tokens", 0))
        cache_write = int(raw_usage.get("cache_creation_input_tokens", 0))
        # Reasoning / thinking tokens (when extended thinking enabled) are
        # already counted in output_tokens by Anthropic; we surface them
        # separately if the response carries the breakdown.
        reasoning_tokens = int(raw_usage.get("thinking_tokens", 0))
        usage = LLMUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            total_tokens=prompt_tokens + completion_tokens + reasoning_tokens,
            model=model,
        )
        usage.cost_estimate_usd = estimate_cost(model, usage, self.PRICE_TABLE)

        stop_reason = data.get("stop_reason", "unknown") or "unknown"
        finish_reason = _STOP_REASON_MAP.get(stop_reason, str(stop_reason))

        return LLMResponse(
            content="\n".join(p for p in content_parts if p),
            finish_reason=finish_reason,
            tool_calls=tool_calls,
            usage=usage,
            raw_response=dict(data),
        )

    async def _fetch_model_list(self) -> list[str]:
        """Anthropic doesn't expose a public `/v1/models` discovery endpoint
        for non-admin keys. Return the static catalog matching `PRICE_TABLE`."""
        return sorted(self.PRICE_TABLE.keys())


__all__ = ["AnthropicProviderHandler"]
