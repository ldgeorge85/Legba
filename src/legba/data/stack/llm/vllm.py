# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""vLLM / gpt-oss-120b LLM provider handler (L-120).

vLLM serves an OpenAI-compatible API; the self-hosted gpt-oss-120b
endpoint at `https://llm.example.internal/v1` is one such deployment
behind the OpenAI Chat Completions wire. This handler reuses 90% of the
OpenAI handler's translation layer and overrides the bits that differ:

  * Self-hosted ⇒ zero-cost; `PRICE_TABLE` is empty by default so cost
    rolls up to $0.00 in the budget UI. Operators can override per
    deployment if they want to apportion compute cost.
  * Token field shape matches OpenAI (prompt_tokens / completion_tokens).
  * GPT-OSS models served by vLLM emit Harmony control tokens in some
    output paths; the existing legba parser handles that in the agent
    layer. The handler here does NOT strip Harmony markers — that's a
    decision the caller (analyst handler) makes based on whether it's
    treating the response as data or as already-parsed text. (We leave
    the `legba.agent.llm.format.strip_harmony_response` path available
    for callers that want it.)
  * Some vLLM builds return multi-message choice lists (reasoning + final);
    we coalesce by joining text in choice order, mirroring the existing
    `legba.agent.llm.provider.VLLMProvider` behavior.
  * Temperature defaults to 1.0 for GPT-OSS workloads per ernie's proven
    pattern (`legba.agent.llm.provider`). Callers can override.

Healthcheck: TCP reachability + vault verification (same lean default as
the base — burning self-hosted compute on every 60s poll still wastes GPU
cycles).
"""

from __future__ import annotations

import json
import logging
import os
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


class VLLMProviderHandler(LLMProviderHandler):
    """Concrete handler for vLLM / gpt-oss-120b / any OpenAI-compatible endpoint."""

    subprovider: ClassVar[str] = "vllm"
    schema_version: ClassVar[str] = "legba/stack.llm_provider/1-0-0"
    handler_version: ClassVar[str] = "0.1.0"
    default_port: ClassVar[int] = 443

    # Self-hosted is zero-list-price by default. Operators can drop in
    # internal cost-per-1M values to apportion compute against budget caps.
    PRICE_TABLE: ClassVar[Mapping[str, ModelPrice]] = {}

    # Default temperature for GPT-OSS-class workloads; callers override.
    _DEFAULT_TEMPERATURE: ClassVar[float] = 1.0

    # ---- Endpoint --------------------------------------------------------

    def _chat_endpoint_path(self) -> str:
        # base.py's `_get_client` strips trailing `/v1` from the operator's
        # `api_endpoint` defensively (so `https://host` and `https://host/v1`
        # both work). The handler must therefore prepend `/v1/...` here —
        # otherwise the final URL becomes `https://host/chat/completions`
        # which the vLLM server doesn't serve (it lives at `/v1/chat/completions`
        # per OpenAI-compat surface). Sibling handlers
        # `openai._chat_endpoint_path` returns `/v1/chat/completions` and
        # `anthropic` returns `/v1/messages` — vLLM was the only one
        # missing the prefix until 2026-05-29.
        return "/v1/chat/completions"

    # ---- Tool translation (OpenAI passthrough) ---------------------------

    def _translate_tools(self, tools: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """vLLM ships OpenAI-compatible tool-call semantics (>=0.6.x). Same
        passthrough as the OpenAI handler."""
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
        system: str | None,
        tools: list[Mapping[str, Any]] | None,
        model: str,
        max_tokens: int,
        temperature: float | None,
        reasoning_effort: str | None,  # vLLM doesn't honor this directly
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "stream": False,
            "temperature": (
                temperature if temperature is not None else self._DEFAULT_TEMPERATURE
            ),
        }
        # `max_tokens` not sent by default — a self-hosted vLLM serves its
        # server-side budget, and an absent cap lets long findings complete.
        # BUT a hosted OpenAI-compatible API with a LOW server default will
        # silently TRUNCATE findings when the field is absent. Opt-in, two
        # ways: per-call (`send_max_tokens=True` kwarg, unchanged) or
        # deployment-wide via LEGBA_LLM_SEND_MAX_TOKENS=1 (for instances
        # pointing this handler at a hosted endpoint). Unset env + no kwarg
        # is byte-identical to the historical behavior.
        if max_tokens and (
            kwargs.pop("send_max_tokens", False)
            or os.getenv("LEGBA_LLM_SEND_MAX_TOKENS", "").strip().lower()
            in ("1", "true", "yes")
        ):
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = list(tools)
        stop = kwargs.pop("stop", None)
        if stop:
            payload["stop"] = list(stop)
        top_p = kwargs.pop("top_p", None)
        if top_p is not None:
            payload["top_p"] = top_p
        # vLLM-specific extras (reasoning effort isn't a wire arg; some
        # GPT-OSS templates expect a "reasoning: high" instruction in the
        # message content. The caller injects that, not the handler.)
        for k, v in kwargs.items():
            payload.setdefault(k, v)
        return payload

    # ---- Response parsing ------------------------------------------------

    def _parse_response(self, data: Mapping[str, Any], *, model: str) -> LLMResponse:
        choices = data.get("choices", []) or []
        if not choices:
            return LLMResponse(
                content="", finish_reason="error",
                raw_response=dict(data),
                usage=LLMUsage(model=model),
            )

        # vLLM reasoning-mode often emits 2+ choices: reasoning then final.
        # Coalesce by joining the text content of every choice in order.
        content_parts: list[str] = []
        tool_calls: list[LLMToolCall] = []
        primary = choices[0]
        for ch in choices:
            message = ch.get("message", {}) or {}
            c = message.get("content")
            if isinstance(c, str) and c:
                content_parts.append(c)
            # Gather tool_calls only from the primary choice (vLLM places
            # them on choices[0] when present; reasoning choices don't carry
            # tool_calls fields).
            if ch is primary:
                for tc in message.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    args_raw = fn.get("arguments", "")
                    try:
                        arguments = (
                            json.loads(args_raw) if isinstance(args_raw, str)
                            else dict(args_raw)
                        )
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

        content = "\n".join(p for p in content_parts if p)
        finish_reason = self._normalize_finish_reason(primary.get("finish_reason"))

        raw_usage = data.get("usage", {}) or {}
        prompt_tokens = int(raw_usage.get("prompt_tokens", 0))
        completion_tokens = int(raw_usage.get("completion_tokens", 0))
        usage = LLMUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=0,
            total_tokens=int(raw_usage.get("total_tokens",
                                            prompt_tokens + completion_tokens)),
            model=model,
        )
        usage.cost_estimate_usd = estimate_cost(model, usage, self.PRICE_TABLE)

        return LLMResponse(
            content=content,
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
        if s == "function_call":
            return "tool_calls"
        return s

    async def _fetch_model_list(self) -> list[str]:
        """vLLM exposes `/v1/models`; base implementation works as-is."""
        # The configured api_endpoint may or may not include `/v1` — try
        # both shapes.
        names = await super()._fetch_model_list()
        if names:
            return names
        client = await self._get_client()
        try:
            resp = await client.get("/models")
        except Exception:
            return []
        if resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        return [str(item.get("id")) for item in data.get("data") or []
                if isinstance(item, dict) and "id" in item]


__all__ = ["VLLMProviderHandler"]
