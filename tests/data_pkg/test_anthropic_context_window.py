# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Anthropic ``max_tokens`` and the context window.

The Messages API requires ``max_tokens`` and rejects a request whose input
plus ``max_tokens`` exceeds the context window. The handler sends the
model's maximum (128,000) on every call, so a large prompt would be rejected
outright. The handler now sends the model's maximum unless the counted input
leaves less room in the window; then the largest value the window allows, and
the call's receipt records that it happened. An API validity constraint, not a
cap: both sides are tested here, over a fake wire (no priced call is made).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from legba.data import run_accounting
from legba.data.registry import consult_turn_stamps
from legba.data.registry.credentials import MissingSecretError  # noqa: F401 — cycle warm-up
from legba.data.schemas import LLMProviderConfig, Property
from legba.data.stack.llm import AnthropicProviderHandler
from legba.data.stack.llm.base import HardLLMFailure

_OK = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-4-8",
    "content": [{"type": "text", "text": "fine"}], "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 2},
}
_WINDOW_MSG = (
    "input length and `max_tokens` exceed context limit: 150000 + 128000 > 200000, "
    "decrease input length or `max_tokens` and try again"
)


class _Wire:
    def __init__(self, responses: list[httpx.Response] | None = None) -> None:
        self.responses = list(responses or [])
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return self.responses.pop(0) if self.responses else httpx.Response(200, json=_OK)


def _handler(wire: _Wire) -> AnthropicProviderHandler:
    h = AnthropicProviderHandler()
    h._cfg = LLMProviderConfig(  # noqa: SLF001
        api_endpoint=Property.Text.of("https://anthropic.example.test"),
        api_key=Property.Secret.of("k"),
        model_name=Property.Text.of("claude-opus-4-8"),
        max_tokens=Property.Number.of(16_384, minimum=1, maximum=200000),
    )
    h._api_key = "k"  # noqa: SLF001
    h._instance_id = "llm.anthropic.test"  # noqa: SLF001
    h._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://anthropic.example.test", transport=httpx.MockTransport(wire),
        timeout=10.0,
    )
    return h


async def _call(h: AnthropicProviderHandler, chars: int) -> list[dict[str, Any]]:
    token = run_accounting.bind_run_accounting()
    try:
        await h.chat_complete([{"role": "user", "content": "x" * chars}], max_tokens=2048)
        return run_accounting.current_llm_calls()
    finally:
        run_accounting.reset_run_accounting(token)


@pytest.mark.asyncio
async def test_a_prompt_that_fits_sends_the_models_maximum_and_records_nothing_extra() -> None:
    wire = _Wire()
    calls = await _call(_handler(wire), 40_000)  # ~10k tokens: 10k + 128k fits in 200k
    assert len(wire.bodies) == 1
    assert wire.bodies[0]["max_tokens"] == 128_000
    assert not any(k.startswith("max_tokens") for k in calls[0])
    receipts = consult_turn_stamps.call_receipts(calls)
    assert receipts and not any(k.startswith("max_tokens") for k in receipts[0])


@pytest.mark.asyncio
async def test_a_large_prompt_sends_the_windows_room_and_the_receipt_says_so() -> None:
    wire = _Wire()
    h = _handler(wire)
    calls = await _call(h, 480_000)  # ~120k tokens: 120k + 128k would be over 200k
    sent = wire.bodies[0]["max_tokens"]
    counted = h._counted_input_tokens(wire.bodies[0])  # noqa: SLF001
    assert 0 < sent < 128_000
    assert counted + sent == h.context_window_tokens() == 200_000  # the largest valid value
    assert calls[0]["max_tokens_sent"] == sent
    assert calls[0]["max_tokens_model_max"] == 128_000
    assert calls[0]["max_tokens_limited_by"] == "context_window"
    receipt = consult_turn_stamps.call_receipts(calls)[0]
    assert receipt["max_tokens_sent"] == sent and receipt["max_tokens_limited_by"] == "context_window"


@pytest.mark.asyncio
async def test_the_apis_own_window_count_resends_once_with_its_room() -> None:
    # The pre-flight estimate let the request through; the API's count says
    # 150,000 + 128,000 > 200,000: resent once with 50,000, recorded.
    wire = _Wire([httpx.Response(400, json={"type": "error", "error": {
        "type": "invalid_request_error", "message": _WINDOW_MSG}})])
    calls = await _call(_handler(wire), 40_000)
    assert [b["max_tokens"] for b in wire.bodies] == [128_000, 50_000]
    assert calls[-1]["status"] == "success"
    assert calls[-1]["max_tokens_sent"] == 50_000


@pytest.mark.asyncio
async def test_other_400s_are_not_retried_and_an_oversized_input_keeps_the_maximum() -> None:
    wire = _Wire([httpx.Response(400, json={"type": "error", "error": {
        "type": "invalid_request_error", "message": "messages: field required"}})])
    with pytest.raises(HardLLMFailure):
        await _call(_handler(wire), 40_000)
    assert len(wire.bodies) == 1
    # input alone over the window: no max_tokens makes it valid, so the model's
    # maximum goes and the API's own error says why (nothing silently shrunk)
    wire2 = _Wire()
    await _call(_handler(wire2), 900_000)
    assert wire2.bodies[0]["max_tokens"] == 128_000
