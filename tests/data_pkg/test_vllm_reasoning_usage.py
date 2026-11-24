# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R-0 — the core plane's reasoning tokens reach the receipt.

``VLLMProviderHandler._parse_response`` hard-coded ``reasoning_tokens=0``, so
the usage block's ``completion_tokens_details.reasoning_tokens`` (the
OpenAI-compatible breakout the gateway documents; the 11-13 probes read 13
reasoning of 17 completion on ``deep``) was discarded and every receipt said 0
even on a thinking alias.

Pinned here, on the REAL handler over ``httpx.MockTransport`` (no model call):
the breakout is read on both the streamed and the JSON path; it is a SUBSET of
``completion_tokens`` (output and total are the server's numbers, unchanged);
it lands on the run-accounting receipt beside input and output; an absent
breakout records 0 and is COUNTED, not estimated; and nothing about the
request changes — no output cap, no reasoning budget, temperature 1.0.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

# Warm the registry side of the legba.data.stack.llm <-> provenance cycle first
# (the same workaround tests/data_pkg/test_stack_llm.py documents).
from legba.data.registry.credentials import MissingSecretError  # noqa: F401

from legba.data import run_accounting
from legba.data.schemas import LLMProviderConfig, Property
from legba.data.stack.llm import VLLMProviderHandler

BASE_URL = "https://gw.example.test"
MODEL = "deep"

DEEP_USAGE = {
    "prompt_tokens": 40, "completion_tokens": 17, "total_tokens": 57,
    "completion_tokens_details": {"reasoning_tokens": 13},
}
FAST_USAGE_NO_BREAKOUT = {
    "prompt_tokens": 40, "completion_tokens": 2, "total_tokens": 42,
}


def _handler(*, price: bool = False) -> VLLMProviderHandler:
    kwargs: dict[str, Any] = dict(
        api_endpoint=Property.Text.of(f"{BASE_URL}/v1"),
        api_key=Property.Secret.of("llm.primary.key"),
        model_name=Property.Text.of(MODEL),
        max_tokens=Property.Number.of(32768, minimum=1, maximum=200000),
    )
    if price:
        kwargs["price_input_per_m"] = Property.Number.of(1.0)
        kwargs["price_output_per_m"] = Property.Number.of(2.0)
    h = VLLMProviderHandler()
    h._cfg = LLMProviderConfig(**kwargs)  # noqa: SLF001
    h._api_key = "k"  # noqa: SLF001
    h._instance_id = "llm.primary.openai_compat"  # noqa: SLF001
    return h


class _Wire:
    def __init__(self, *replies: httpx.Response) -> None:
        self.replies = list(replies)
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": MODEL}]})
        self.bodies.append(json.loads(request.content))
        return self.replies.pop(0)


def _install(h: VLLMProviderHandler, wire: _Wire) -> None:
    h._client = httpx.AsyncClient(  # noqa: SLF001
        base_url=BASE_URL,
        headers=h._auth_headers(),  # noqa: SLF001
        transport=httpx.MockTransport(wire),
        timeout=httpx.Timeout(10.0),
    )


def _chunk(delta: dict[str, Any] | None = None, *, finish: str | None = None,
           usage: dict[str, Any] | None = None, choices: bool = True) -> dict[str, Any]:
    c: dict[str, Any] = {
        "id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1,
        "model": MODEL,
        "choices": (
            [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
            if choices else []
        ),
    }
    if usage is not None:
        c["usage"] = usage
    return c


def _sse(usage: dict[str, Any]) -> httpx.Response:
    chunks = [
        _chunk({"role": "assistant", "content": ""}),
        _chunk({"reasoning_content": "The user wants OK."}),
        _chunk({"content": "OK"}),
        _chunk({}, finish="stop"),
        _chunk(usage=usage, choices=False),
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=body.encode(),
    )


def _json(usage: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                     "finish_reason": "stop"}],
        "usage": usage,
    })


async def _call(h: VLLMProviderHandler, reply: httpx.Response, *, stream: bool) -> Any:
    wire = _Wire(reply)
    _install(h, wire)
    kwargs = {} if stream else {"stream": False}
    resp = await h.chat_complete([{"role": "user", "content": "q"}], **kwargs)
    return resp, wire


# ---------------------------------------------------------------------------
# The breakout is read, on both wire shapes, as a subset of output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False], ids=["streamed", "json"])
async def test_reasoning_breakout_is_recorded_as_a_subset_of_completion(stream: bool) -> None:
    h = _handler()
    reply = _sse(DEEP_USAGE) if stream else _json(DEEP_USAGE)
    resp, _ = await _call(h, reply, stream=stream)
    assert resp.content == "OK"
    assert resp.usage.reasoning_tokens == 13
    # Reasoning is INCLUDED in completion_tokens: never added on top.
    assert resp.usage.completion_tokens == 17
    assert resp.usage.prompt_tokens == 40
    assert resp.usage.total_tokens == 57
    assert h.reasoning_usage_absent == 0


@pytest.mark.asyncio
async def test_the_receipt_carries_reasoning_beside_input_and_output() -> None:
    """The run-accounting receipt (``analyst_traces.llm_calls``) is where input
    and output tokens already go; reasoning goes in the same entry."""
    h = _handler()
    token = run_accounting.bind_run_accounting()
    try:
        await _call(h, _sse(DEEP_USAGE), stream=True)
        calls = run_accounting.current_llm_calls()
    finally:
        run_accounting.reset_run_accounting(token)
    assert len(calls) == 1, calls
    entry = calls[0]
    assert entry["status"] == "success"
    assert entry["prompt_tokens"] == 40
    assert entry["completion_tokens"] == 17
    assert entry["reasoning_tokens"] == 13
    assert entry["total_tokens"] == 57


@pytest.mark.asyncio
async def test_a_priced_lane_does_not_charge_reasoning_twice() -> None:
    """Reasoning is already priced inside completion_tokens at the output
    rate; the component price carries no separate reasoning rate, so the cost
    is exactly input + output."""
    h = _handler(price=True)
    resp, _ = await _call(h, _json(DEEP_USAGE), stream=False)
    assert resp.usage.cost_estimate_usd == pytest.approx(
        (40 * 1.0 + 17 * 2.0) / 1_000_000, abs=1e-6,
    )


# ---------------------------------------------------------------------------
# Absent or malformed: 0, counted, never estimated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_absent_breakout_records_zero_and_is_counted_once_logged(caplog) -> None:
    h = _handler()
    with caplog.at_level(logging.INFO, logger="legba.data.stack.llm.vllm"):
        r1, _ = await _call(h, _json(FAST_USAGE_NO_BREAKOUT), stream=False)
        r2, _ = await _call(h, _sse(FAST_USAGE_NO_BREAKOUT), stream=True)
    assert r1.usage.reasoning_tokens == 0
    assert r2.usage.reasoning_tokens == 0
    assert r1.usage.completion_tokens == 2
    assert h.reasoning_usage_absent == 2
    logged = [r for r in caplog.records if "reasoning_absent" in r.getMessage()]
    assert len(logged) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("details", [
    {"reasoning_tokens": None}, {"reasoning_tokens": "many"}, "not-a-mapping", {},
])
async def test_a_malformed_breakout_records_zero(details: Any) -> None:
    h = _handler()
    usage = {**FAST_USAGE_NO_BREAKOUT, "completion_tokens_details": details}
    resp, _ = await _call(h, _json(usage), stream=False)
    assert resp.usage.reasoning_tokens == 0
    assert h.reasoning_usage_absent == 1


@pytest.mark.asyncio
async def test_a_measured_zero_is_not_counted_as_absent() -> None:
    h = _handler()
    usage = {**FAST_USAGE_NO_BREAKOUT,
             "completion_tokens_details": {"reasoning_tokens": 0}}
    resp, _ = await _call(h, _json(usage), stream=False)
    assert resp.usage.reasoning_tokens == 0
    assert h.reasoning_usage_absent == 0


@pytest.mark.asyncio
async def test_no_usage_block_is_not_a_reasoning_absence() -> None:
    """A body with no usage at all is the stream's own ``usage_absent`` case
    (logged there); it is not double-counted as a missing breakout."""
    h = _handler()
    resp, _ = await _call(
        h,
        httpx.Response(200, json={
            "choices": [{"index": 0, "message": {"content": "OK"},
                         "finish_reason": "stop"}],
        }),
        stream=False,
    )
    assert resp.usage.reasoning_tokens == 0
    assert h.reasoning_usage_absent == 0


# ---------------------------------------------------------------------------
# Recording only: the request is unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recording_adds_no_cap_and_no_reasoning_budget_to_the_request() -> None:
    h = _handler()
    _, wire = await _call(h, _sse(DEEP_USAGE), stream=True)
    body = wire.bodies[0]
    for key in ("thinking_token_budget", "reasoning_effort", "max_completion_tokens",
                "thinking", "chat_template_kwargs"):
        assert key not in body, key
    assert body.get("temperature") == 1.0
