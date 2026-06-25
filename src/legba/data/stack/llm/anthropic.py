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
    TEMPERATURE_DEPRECATED_PREFIXES: ClassVar[tuple[str, ...]] = (
        "claude-opus-4-8",
    )

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
        if temperature is not None and not any(
            model.startswith(p) for p in self.TEMPERATURE_DEPRECATED_PREFIXES
        ):
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
        return payload

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
