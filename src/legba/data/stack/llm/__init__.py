# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.stack.llm — LLM provider stack-component handlers (L-120).

Each handler is a registered stack-component conforming to the L-102 §1
`KindHandler` Protocol, extended with the LLM-specific surface
(`chat_complete`, `stream_complete`, native tool-calling per M-061/062,
budget reporting hooks, cost-aware healthcheck).

Three concrete subproviders:

  * `AnthropicProviderHandler` — Anthropic Messages API (Claude family).
  * `VLLMProviderHandler`      — OpenAI-compatible vLLM endpoint. Used for
                                 gpt-oss-120b at `llm.example.internal`.
  * `OpenAIProviderHandler`    — OpenAI Chat Completions API (GPT-5, o3, …).

All three share `LLMProviderHandler` (base) and the unified
`LLMResponse` / `LLMUsage` / `LLMToolCall` / `LLMChunk` shapes — analyst
handlers in Phase 5/6 (L-150..L-159) author tool specs once in OpenAI shape
and dispatch across providers without re-translation.

Per L-102 the runtime owns lifecycle; in-tree first-party registration is
exposed via `LLM_HANDLERS` (a per-subprovider lookup) so the bootstrap step
in `legba.runtime.bootstrap` (Phase 5) can drop them into the handler
registry without import-side-effects.
"""

from __future__ import annotations

from .anthropic import AnthropicProviderHandler
from .base import (
    BudgetExhausted,
    BudgetReporter,
    HandlerContext,
    HardLLMFailure,
    LLMChunk,
    LLMProviderHandler,
    LLMResponse,
    LLMTool,
    LLMToolCall,
    LLMUsage,
    ModelPrice,
    RuntimeContextLike,
    TelemetryHandle,
    TransientLLMFailure,
    estimate_cost,
)
from .openai import OpenAIProviderHandler
from .vllm import VLLMProviderHandler

LLM_HANDLERS: dict[str, type[LLMProviderHandler]] = {
    AnthropicProviderHandler.subprovider: AnthropicProviderHandler,
    VLLMProviderHandler.subprovider: VLLMProviderHandler,
    OpenAIProviderHandler.subprovider: OpenAIProviderHandler,
}


def resolve_handler(subprovider: str) -> type[LLMProviderHandler]:
    """Look up a handler class by subprovider id. Raises `KeyError` if
    unknown. Used by the runtime to construct a handler when binding an
    `LLMProvider` stack-component descriptor to a live instance."""
    if subprovider not in LLM_HANDLERS:
        raise KeyError(
            f"unknown LLM subprovider {subprovider!r}; "
            f"known: {sorted(LLM_HANDLERS)}"
        )
    return LLM_HANDLERS[subprovider]


__all__ = [
    # Base + protocols
    "LLMProviderHandler",
    "LLMResponse",
    "LLMChunk",
    "LLMTool",
    "LLMToolCall",
    "LLMUsage",
    "ModelPrice",
    "estimate_cost",
    "BudgetExhausted",
    "BudgetReporter",
    "HandlerContext",
    "HardLLMFailure",
    "RuntimeContextLike",
    "TelemetryHandle",
    "TransientLLMFailure",
    # Subproviders
    "AnthropicProviderHandler",
    "VLLMProviderHandler",
    "OpenAIProviderHandler",
    # Registry
    "LLM_HANDLERS",
    "resolve_handler",
]
