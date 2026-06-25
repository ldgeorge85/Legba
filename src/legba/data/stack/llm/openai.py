# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OpenAI LLM provider handler (L-120).

Wraps the OpenAI `POST /v1/chat/completions` endpoint with native tool-calling
per M-061/062. The unified tool-spec is already OpenAI-native, so
`_translate_tools` is a passthrough.

Reasoning-effort support: GPT-5 and o-series accept `reasoning_effort`
("low" | "medium" | "high"); other models silently ignore it (we strip
it before sending to non-reasoning models).

Pricing (USD per 1M tokens, mid-2026 list price):

  * gpt-5            : input 5    / output 40   / cached 0.5
  * gpt-5-mini       : input 1    / output 8    / cached 0.10
  * gpt-4.1          : input 2    / output 8    / cached 0.20
  * gpt-4.1-mini     : input 0.40 / output 1.60 / cached 0.04
  * o3               : input 30   / output 120  / reasoning 120
  * o4-mini          : input 1.10 / output 4.40 / reasoning 4.40

Operators are expected to refresh these via subclass when OpenAI publishes
new prices; the L-163 budget UI surfaces a per-call sanity check vs the
provider's billing-reported numbers.
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


# Models that honor `reasoning_effort`. Other models receive the kwarg
# stripped to avoid 400-level "unknown parameter" errors.
_REASONING_MODELS = (
    "gpt-5",
    "o1",
    "o3",
    "o4",
)


class OpenAIProviderHandler(LLMProviderHandler):
    """Concrete handler for OpenAI Chat Completions API."""

    subprovider: ClassVar[str] = "openai"
    schema_version: ClassVar[str] = "legba/stack.llm_provider/1-0-0"
    handler_version: ClassVar[str] = "0.1.0"
    default_port: ClassVar[int] = 443

    PRICE_TABLE: ClassVar[Mapping[str, ModelPrice]] = {
        "gpt-5":         ModelPrice(input_per_m=5.0,  output_per_m=40.0,  cache_read_per_m=0.50),
        "gpt-5-mini":    ModelPrice(input_per_m=1.0,  output_per_m=8.0,   cache_read_per_m=0.10),
        "gpt-4.1":       ModelPrice(input_per_m=2.0,  output_per_m=8.0,   cache_read_per_m=0.20),
        "gpt-4.1-mini":  ModelPrice(input_per_m=0.40, output_per_m=1.60,  cache_read_per_m=0.04),
        "gpt-4o":        ModelPrice(input_per_m=2.50, output_per_m=10.0,  cache_read_per_m=1.25),
        "gpt-4o-mini":   ModelPrice(input_per_m=0.15, output_per_m=0.60,  cache_read_per_m=0.075),
        "o3":            ModelPrice(input_per_m=30.0, output_per_m=120.0, reasoning_per_m=120.0),
        "o4-mini":       ModelPrice(input_per_m=1.10, output_per_m=4.40,  reasoning_per_m=4.40),
    }

    # ---- Endpoint --------------------------------------------------------

    def _chat_endpoint_path(self) -> str:
        return "/v1/chat/completions"

    # ---- Tool translation (passthrough) ----------------------------------

    def _translate_tools(self, tools: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """OpenAI is the source-shape — passthrough. Accept either the
        Chat-Completions wire shape (`{type: "function", function: {...}}`)
        or a bare `{name, description, parameters}` for operator
        convenience."""
        result: list[Mapping[str, Any]] = []
        for spec in tools:
            if "function" in spec:
                result.append(dict(spec))
            elif "name" in spec:
                result.append({"type": "function", "function": dict(spec)})
            else:
                result.append(dict(spec))
        return result

    # ---- Payload assembly ------------------------------------------------

    def _build_chat_payload(
        self,
        *,
        messages: list[Mapping[str, Any]],
        system: str | None,  # already merged into messages by _translate_messages
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
            "stream": False,
        }
        # Note: reasoning models use `max_completion_tokens`, not `max_tokens`.
        if self._is_reasoning_model(model):
            payload["max_completion_tokens"] = max_tokens
        else:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = list(tools)
        if temperature is not None and not self._is_reasoning_model(model):
            # Reasoning models reject `temperature` ≠ 1.0; let the server
            # apply its default rather than risking a 400.
            payload["temperature"] = temperature
        if reasoning_effort and self._is_reasoning_model(model):
            payload["reasoning_effort"] = reasoning_effort
        stop = kwargs.pop("stop", None)
        if stop:
            payload["stop"] = list(stop)
        top_p = kwargs.pop("top_p", None)
        if top_p is not None and not self._is_reasoning_model(model):
            payload["top_p"] = top_p
        for k, v in kwargs.items():
            payload.setdefault(k, v)
        return payload

    @staticmethod
    def _is_reasoning_model(model: str) -> bool:
        return any(model.startswith(prefix) for prefix in _REASONING_MODELS)

    # ---- Response parsing ------------------------------------------------

    def _parse_response(self, data: Mapping[str, Any], *, model: str) -> LLMResponse:
        choices = data.get("choices", []) or []
        if not choices:
            return LLMResponse(
                content="", finish_reason="error",
                raw_response=dict(data),
                usage=LLMUsage(model=model),
            )
        choice = choices[0]
        message = choice.get("message", {}) or {}
        content = message.get("content") or ""
        finish_reason = self._normalize_finish_reason(choice.get("finish_reason"))

        tool_calls: list[LLMToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args_raw = fn.get("arguments", "")
            try:
                arguments = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                arguments = {"_raw": args_raw}
            if not isinstance(arguments, dict):
                arguments = {"_value": arguments}
            tool_calls.append(
                LLMToolCall(
                    id=str(tc.get("id") or uuid.uuid4().hex),
                    name=str(fn.get("name") or ""),
                    arguments=arguments,
                )
            )

        raw_usage = data.get("usage", {}) or {}
        prompt_tokens = int(raw_usage.get("prompt_tokens", 0))
        completion_tokens = int(raw_usage.get("completion_tokens", 0))
        # Reasoning models surface `completion_tokens_details.reasoning_tokens`.
        reasoning_tokens = 0
        details = raw_usage.get("completion_tokens_details") or {}
        if isinstance(details, dict):
            reasoning_tokens = int(details.get("reasoning_tokens", 0))
        cache_read = int(
            (raw_usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
            if isinstance(raw_usage.get("prompt_tokens_details"), dict) else 0
        )
        usage = LLMUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=0,
            total_tokens=int(raw_usage.get("total_tokens", prompt_tokens + completion_tokens)),
            model=model,
        )
        usage.cost_estimate_usd = estimate_cost(model, usage, self.PRICE_TABLE)

        return LLMResponse(
            content=str(content),
            finish_reason=finish_reason,
            tool_calls=tool_calls,
            usage=usage,
            raw_response=dict(data),
        )

    @staticmethod
    def _normalize_finish_reason(raw: Any) -> str:
        if raw is None:
            return "unknown"
        s = str(raw)
        # OpenAI returns `tool_calls`, `stop`, `length`, `content_filter`.
        if s == "function_call":
            return "tool_calls"
        return s


__all__ = ["OpenAIProviderHandler"]
