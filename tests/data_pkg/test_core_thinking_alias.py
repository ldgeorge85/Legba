# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""T-1 — thinking as a declared, per-caller choice on the core plane.

The gateway serves one model as ``fast`` (thinking off) and ``deep`` (thinking
on). A call asks with ``thinking=True``; the handler names the component's
``thinking_model_name`` — resolved in ONE place (``stack/llm/thinking.py``).
Pinned on the REAL vLLM handler over ``httpx.MockTransport`` (no model call):

* the alias on the wire, and nothing else changes (temperature 1.0, no
  ``max_tokens``, no reasoning budget while it is unset);
* a component with no alias, or an explicit conflicting model, refuses loud;
* the declared reasoning-budget pair rides thinking calls only;
* streamed reasoning is not answer text, not published to a live sink, not
  answer length, and is counted only as reasoning tokens;
* both token tallies count reasoning ONCE on this wire and separately on the
  Anthropic shape;
* the schema refuses a half-declared budget or an empty alias.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from legba.data.registry.credentials import MissingSecretError  # noqa: F401 — cycle warm-up

from legba.data import run_accounting
from legba.data.schemas import LLMProviderConfig, Property
from legba.data.stack.llm import AnthropicProviderHandler, HardLLMFailure, VLLMProviderHandler
from legba.data.stack.llm.pricing import call_token_total, reasoning_in_completion
from legba.data.stack.llm.stream_observer import TextDeltaSink, capture_text_deltas
from legba.data.stack.llm.thinking import (
    ThinkingBoundHandler,
    ThinkingUnavailable,
    bind_thinking,
    parse_thinking,
    thinking_available,
)

BASE_URL = "https://gw.example.test"
CORE_ID = "llm.primary.openai_compat"
REASONING = "The user wants a short answer; check the ladder first."
DEEP_USAGE = {
    "prompt_tokens": 40, "completion_tokens": 17, "total_tokens": 57,
    "completion_tokens_details": {"reasoning_tokens": 13},
}


def _cfg(**extra: Any) -> LLMProviderConfig:
    return LLMProviderConfig(
        api_endpoint=Property.Text.of(f"{BASE_URL}/v1"),
        api_key=Property.Secret.of("llm.primary.key"),
        model_name=Property.Text.of("fast"),
        max_tokens=Property.Number.of(16384, minimum=1, maximum=200000),
        **extra,
    )


def _handler(*, alias: str | None = "deep", budget: tuple[str, int] | None = None,
             instance_id: str = CORE_ID) -> VLLMProviderHandler:
    extra: dict[str, Any] = {}
    if alias is not None:
        extra["thinking_model_name"] = Property.Text.of(alias)
    if budget is not None:
        extra["reasoning_budget_param"] = Property.Text.of(budget[0])
        extra["reasoning_budget_tokens"] = Property.Number.of(budget[1], minimum=1)
    h = VLLMProviderHandler()
    h._cfg = _cfg(**extra)  # noqa: SLF001
    h._api_key = "k"  # noqa: SLF001
    h._instance_id = instance_id  # noqa: SLF001
    return h


class _Wire:
    def __init__(self, *replies: httpx.Response) -> None:
        self.replies = list(replies)
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return self.replies.pop(0)


def _install(h: VLLMProviderHandler, wire: _Wire) -> None:
    h._client = httpx.AsyncClient(  # noqa: SLF001
        base_url=BASE_URL, headers=h._auth_headers(),  # noqa: SLF001
        transport=httpx.MockTransport(wire), timeout=httpx.Timeout(10.0),
    )


def _chunk(delta: dict[str, Any] | None = None, *, finish: str | None = None,
           usage: dict[str, Any] | None = None, choices: bool = True,
           model: str = "deep") -> dict[str, Any]:
    c: dict[str, Any] = {
        "id": "c1", "object": "chat.completion.chunk", "created": 1, "model": model,
        "choices": ([{"index": 0, "delta": delta or {}, "finish_reason": finish}]
                    if choices else []),
    }
    if usage is not None:
        c["usage"] = usage
    return c


def _thinking_sse(answer: str = "OK", *, rkey: str = "reasoning_content") -> httpx.Response:
    """The documented shape: reasoning deltas first, then content, then usage."""
    chunks = [
        _chunk({"role": "assistant", "content": ""}),
        *[_chunk({rkey: REASONING[i:i + 12]}) for i in range(0, len(REASONING), 12)],
        _chunk({"content": answer}),
        _chunk({}, finish="stop"),
        _chunk(usage=DEEP_USAGE, choices=False),
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content=body.encode())


# ---------------------------------------------------------------------------
# The one place: the alias on the wire, nothing else changes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_thinking_call_names_the_declared_alias_and_changes_nothing_else() -> None:
    h = _handler()
    wire = _Wire(_thinking_sse(), _thinking_sse())
    _install(h, wire)
    await h.chat_complete([{"role": "user", "content": "q"}], thinking=True)
    await h.chat_complete([{"role": "user", "content": "q"}])
    on, off = wire.bodies
    assert on["model"] == "deep" and off["model"] == "fast"
    assert on["temperature"] == 1.0 == off["temperature"]
    for body in (on, off):
        for key in ("max_tokens", "max_completion_tokens", "thinking", "reasoning_effort",
                    "chat_template_kwargs", "thinking_token_budget"):
            assert key not in body, key
    # Byte-identical apart from the alias.
    assert {k: v for k, v in on.items() if k != "model"} == {
        k: v for k, v in off.items() if k != "model"}


@pytest.mark.asyncio
async def test_thinking_false_and_none_are_the_call_as_it_always_was() -> None:
    h = _handler()
    wire = _Wire(_thinking_sse(), _thinking_sse())
    _install(h, wire)
    await h.chat_complete([{"role": "user", "content": "q"}], thinking=False)
    await h.chat_complete([{"role": "user", "content": "q"}], model="other-model")
    assert [b["model"] for b in wire.bodies] == ["fast", "other-model"]


@pytest.mark.asyncio
async def test_a_component_with_no_alias_refuses_a_thinking_call_before_the_wire() -> None:
    h = _handler(alias=None)
    wire = _Wire()
    _install(h, wire)
    assert h.thinking_available() is False
    with pytest.raises(HardLLMFailure, match="no thinking_model_name"):
        await h.chat_complete([{"role": "user", "content": "q"}], thinking=True)
    assert wire.bodies == []


@pytest.mark.asyncio
async def test_thinking_with_a_conflicting_explicit_model_is_refused() -> None:
    h = _handler()
    _install(h, _Wire())
    with pytest.raises(HardLLMFailure, match="explicit model"):
        await h.chat_complete([{"role": "user", "content": "q"}], thinking=True, model="fast")


@pytest.mark.asyncio
async def test_the_declared_reasoning_budget_rides_thinking_calls_only() -> None:
    h = _handler(budget=("thinking_token_budget", 4096))
    wire = _Wire(_thinking_sse(), _thinking_sse())
    _install(h, wire)
    await h.chat_complete([{"role": "user", "content": "q"}], thinking=True)
    await h.chat_complete([{"role": "user", "content": "q"}])
    assert wire.bodies[0]["thinking_token_budget"] == 4096
    assert "thinking_token_budget" not in wire.bodies[1]
    assert "max_tokens" not in wire.bodies[0]


def test_the_schema_refuses_a_half_declared_budget_and_an_empty_alias() -> None:
    with pytest.raises(ValidationError, match="set together"):
        _cfg(reasoning_budget_param=Property.Text.of("thinking_token_budget"))
    with pytest.raises(ValidationError, match="set together"):
        _cfg(reasoning_budget_tokens=Property.Number.of(100, minimum=1))
    with pytest.raises(ValidationError, match="must name an alias"):
        _cfg(thinking_model_name=Property.Text.of("  "))
    # The shipped state: no alias, no budget — valid, and cannot think.
    assert _cfg().thinking_model_name is None


# ---------------------------------------------------------------------------
# Streamed reasoning: not answer text, not shown live, not answer length
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("rkey", ["reasoning_content", "reasoning"])
async def test_streamed_reasoning_is_not_answer_text_and_is_counted_as_reasoning(rkey: str) -> None:
    h = _handler()
    _install(h, _Wire(_thinking_sse("The ladder is intact.", rkey=rkey)))
    sink = TextDeltaSink()
    token = run_accounting.bind_run_accounting()
    try:
        with capture_text_deltas(sink):
            resp = await h.chat_complete([{"role": "user", "content": "q"}], thinking=True)
        calls = run_accounting.current_llm_calls()
    finally:
        run_accounting.reset_run_accounting(token)
    assert resp.content == "The ladder is intact."
    assert REASONING not in resp.content and len(resp.content) == len("The ladder is intact.")
    # The live sink (what a person watching a Consult sees) got the answer only.
    assert sink.text == "The ladder is intact."
    # Kept on the raw message for whoever needs it, never in content.
    assert resp.raw_response["choices"][0]["message"][rkey] == REASONING
    assert resp.usage.reasoning_tokens == 13 and resp.usage.completion_tokens == 17
    (entry,) = calls
    assert entry["model"] == "deep" and entry["thinking"] is True
    assert entry["reasoning_tokens"] == 13
    assert REASONING not in entry.get("completion_text", "")


@pytest.mark.asyncio
async def test_a_thinking_off_receipt_is_byte_identical_in_shape() -> None:
    h = _handler(instance_id="llm.some.other_component")
    _install(h, _Wire(_thinking_sse()))
    token = run_accounting.bind_run_accounting()
    try:
        await h.chat_complete([{"role": "user", "content": "q"}])
        (entry,) = run_accounting.current_llm_calls()
    finally:
        run_accounting.reset_run_accounting(token)
    assert "thinking" not in entry and "gate_wait_ms" not in entry


# ---------------------------------------------------------------------------
# The double count: reasoning once on this wire, separately on Anthropic's
# ---------------------------------------------------------------------------


class _Budget:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, int, str | None]] = []

    async def record(self, *, kind: str, amount: int, dimension: str | None = None) -> None:
        self.recorded.append((kind, amount, dimension))

    async def check_envelope(self) -> str:
        return "ok"


class _Ctx:
    def __init__(self) -> None:
        self.budget = _Budget()


@pytest.mark.asyncio
async def test_the_per_call_budget_report_counts_reasoning_once_on_the_gateway() -> None:
    h = _handler()
    _install(h, _Wire(_thinking_sse()))
    ctx = _Ctx()
    await h.chat_complete([{"role": "user", "content": "q"}], thinking=True, ctx=ctx)
    # 40 prompt + 17 completion (13 of which are reasoning) — not 70.
    assert ctx.budget.recorded == [("tokens", 57, "vllm:deep")]


def test_the_anthropic_shape_counts_thinking_separately() -> None:
    h = AnthropicProviderHandler()
    resp = h._parse_response({  # noqa: SLF001
        "content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "A"}],
        "usage": {"input_tokens": 40, "output_tokens": 4, "thinking_tokens": 13},
        "stop_reason": "end_turn",
    }, model="claude-x")
    u = resp.usage
    assert (u.prompt_tokens, u.completion_tokens, u.reasoning_tokens) == (40, 4, 13)
    assert not reasoning_in_completion("anthropic")
    assert call_token_total("anthropic", prompt_tokens=40, completion_tokens=4,
                            reasoning_tokens=13) == u.total_tokens == 57


@pytest.mark.parametrize("provider", ["vllm", "openai", ""])
def test_the_openai_compatible_shape_counts_reasoning_inside_completion(provider: str) -> None:
    assert reasoning_in_completion(provider)
    assert call_token_total(provider, prompt_tokens=40, completion_tokens=17,
                            reasoning_tokens=13) == 57


class _Conn:
    def __init__(self) -> None:
        self.args: tuple[Any, ...] = ()

    async def fetchrow(self, _sql: str, *args: Any) -> dict[str, Any]:
        self.args = args
        return {
            "analyst_id": args[0], "analyst_version": args[1], "bucket": args[2],
            "tokens_used": args[3], "runs": args[4], "cost_usd": 0,
            "cost_estimate_usd": args[5], "last_updated": None,
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "expected"), [("vllm", 57), ("anthropic", 70)])
async def test_the_ledger_increment_uses_the_same_rule(provider: str, expected: int) -> None:
    from legba.data.provenance.budget import record_budget

    conn = _Conn()
    await record_budget(
        conn, analyst_id="consult_default", analyst_version="v1", provider=provider,
        model="deep", prompt_tokens=40, completion_tokens=17, reasoning_tokens=13,
    )
    assert conn.args[3] == expected


# ---------------------------------------------------------------------------
# The descriptor choice and the handler binding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "expected"), [
    (None, None), ("on", True), ("off", False), (True, True), (False, False),
    ({"raw": "on"}, True), (" OFF ", False),
])
def test_parse_thinking_reads_the_documented_spellings(value: Any, expected: Any) -> None:
    assert parse_thinking(value) is expected


@pytest.mark.parametrize("value", ["maybe", 2, ["on"]])
def test_parse_thinking_refuses_anything_else(value: Any) -> None:
    with pytest.raises(ValueError, match="not a thinking choice"):
        parse_thinking(value)


@pytest.mark.asyncio
async def test_a_bound_handler_thinks_by_default_and_a_call_can_say_no() -> None:
    h = _handler()
    wire = _Wire(_thinking_sse(), _thinking_sse())
    _install(h, wire)
    bound = bind_thinking(h, True, analyst_id="x")
    assert isinstance(bound, ThinkingBoundHandler) and bound.wrapped is h
    assert bound.subprovider == "vllm"  # every other attribute is the handler's
    await bound.chat_complete([{"role": "user", "content": "q"}])
    await bound.chat_complete([{"role": "user", "content": "q"}], thinking=False)
    assert [b["model"] for b in wire.bodies] == ["deep", "fast"]
    assert bind_thinking(h, False) is h and bind_thinking(h, None) is h


def test_binding_thinking_on_a_plane_that_cannot_think_is_refused() -> None:
    with pytest.raises(ThinkingUnavailable, match="declares no"):
        bind_thinking(_handler(alias=None), True, analyst_id="desk")


def test_availability_for_handlers_and_doubles() -> None:
    class _Double:
        async def chat_complete(self, *a: Any, **k: Any) -> Any: ...

    assert thinking_available(_handler()) and not thinking_available(_handler(alias=None))
    assert not thinking_available(_Double())
    unconfigured = VLLMProviderHandler()
    assert not thinking_available(unconfigured)
