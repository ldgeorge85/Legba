# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE declared maximum output of the Claude models (roll-86 follow-up).

Standing rule: no output token caps anywhere on this platform. No provider
handler sends a caller's ``max_tokens`` (a descriptor's ``method.llm.max_tokens``,
a stage's clamp); where an API REQUIRES the field — Anthropic's Messages API —
the value sent is the model's maximum output, read from here and nowhere else:
``AnthropicProviderHandler`` sends it, and the registry's Flow explainer
(``registry/flow_params``) reports it. Other routes declare their model's
maximum on the stack component (``max_tokens``), read through
``LLMProviderHandler.model_max_output_tokens``.

Anthropic's DOCUMENTED per-request output ceilings by model-family prefix,
verified against the published model catalog 2026-08-15 and probed on the
5-series 2026-09-16 (the 400 message names the maximum). Streaming is required
for outputs this large, which the handler always does.

A leaf: standard library only, so the registry's slim image imports it.
"""
from __future__ import annotations

from typing import Mapping

__all__ = [
    "ANTHROPIC_MAX_OUTPUT_TOKENS_BY_PREFIX",
    "ANTHROPIC_MAX_OUTPUT_TOKENS_DEFAULT",
    "anthropic_max_output_tokens",
]

ANTHROPIC_MAX_OUTPUT_TOKENS_BY_PREFIX: Mapping[str, int] = {
    "claude-opus-5": 128_000,  # probed 2026-09-16: "130000 > 128000 … maximum … for claude-opus-5"
    "claude-fable-5": 128_000,  # probed 2026-09-16: same ceiling message for claude-fable-5-1
    "claude-opus-4-8": 128_000,
    "claude-opus-4-7": 128_000,
    "claude-sonnet-4-6": 128_000,
    "claude-sonnet-4-5": 64_000,
    "claude-haiku-3-5": 8_192,
}
#: For a model with no prefix match above: the current-generation maximum.
ANTHROPIC_MAX_OUTPUT_TOKENS_DEFAULT: int = 128_000


def anthropic_max_output_tokens(model: str) -> int:
    """The documented maximum output of ``model`` (longest matching prefix)."""
    best = ""
    for prefix in ANTHROPIC_MAX_OUTPUT_TOKENS_BY_PREFIX:
        if model.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return ANTHROPIC_MAX_OUTPUT_TOKENS_BY_PREFIX[best] if best else ANTHROPIC_MAX_OUTPUT_TOKENS_DEFAULT
