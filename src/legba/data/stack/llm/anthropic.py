# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Anthropic LLM provider handler (L-120).

Wraps the Anthropic Messages API (`POST /v1/messages`) per M-061/062 native
tool-calling. Tool-arg translation: the unified OpenAI tool-spec shape
`{type: "function", function: {name, description, parameters}}` is converted
to Anthropic's `{name, description, input_schema}` shape on the wire.

Auth: `x-api-key` header (not Bearer).
System prompt: top-level `system` field, not a message role.
Max tokens: required by Anthropic; defaults from descriptor config.
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

import json
import logging
import uuid
from typing import Any, ClassVar, Mapping

from .base import (
    HardLLMFailure,
    LLMProviderHandler,
    LLMResponse,
    LLMToolCall,
    LLMUsage,
    ModelPrice,
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
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "max_tokens": max_tokens,
            "stream": False,
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
        """POST the payload, with a one-shot fallback if caching is rejected.

        Prompt caching is an optimization; a consult must never fail because a
        cache hint was unwelcome. If the endpoint 400s on a `cache_control`
        marker, we disable caching for this handler instance (logged ONCE),
        strip the markers back to the historical wire shape, and retry the same
        call. Every subsequent call on this instance is assembled uncached.
        """
        try:
            return await super()._call_chat(payload)
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
            return await super()._call_chat(_strip_cache_control(payload))

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
