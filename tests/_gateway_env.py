# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Neutral values for the variables the model-gateway stack components name.

The gateway descriptors write their endpoint and model names as
``${env:NAME}``; tests that build or probe those components set these.
"""

from __future__ import annotations

from typing import Any

GATEWAY_ENV: dict[str, str] = {
    "LEGBA_GATEWAY_API_ENDPOINT": "https://gateway.example.test/v1",
    "LEGBA_GATEWAY_JUDGE_MODEL": "judge-model",
    "LEGBA_GATEWAY_JUDGE_THINK_MODEL": "judge-think-model",
    "LEGBA_GATEWAY_GOLD_DRAFTER_MODEL": "drafter-think-model",
    "LEGBA_GATEWAY_GRADER_MODEL": "grader-model",
    "LEGBA_GATEWAY_DECIDE_MODEL": "decide-model",
    "LEGBA_GATEWAY_TRANSLATE_MODEL": "chat-model",
    "LEGBA_GATEWAY_JUDGE_MAX_OUTPUT_TOKENS": "32768",
    "LEGBA_GATEWAY_JUDGE_THINK_MAX_OUTPUT_TOKENS": "32768",
    "LEGBA_GATEWAY_GOLD_DRAFTER_MAX_OUTPUT_TOKENS": "32768",
    "LEGBA_GATEWAY_GRADER_MAX_OUTPUT_TOKENS": "32768",
    "LEGBA_GATEWAY_CONTEXT_WINDOW_TOKENS": "262144",
}


def set_gateway_env(monkeypatch: Any, **overrides: str) -> dict[str, str]:
    values = {**GATEWAY_ENV, **overrides}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values
