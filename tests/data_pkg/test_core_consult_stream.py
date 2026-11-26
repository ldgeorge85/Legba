# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""consult 6cd91190: two attempts, 23 minutes, no answer.

2026-10-29 21:00Z, request 1f2029f7, the CORE plane (``llm.primary.openai_compat``,
gateway alias ``fast``, ``VLLMProviderHandler``). Attempt 1 drilled four rounds,
hit the wall budget, and its forced final sat on ONE non-streaming request for
19 minutes until the connection dropped — zero text delivered; the loop
re-raised and the actor's transient retry re-ran the WHOLE consult. Attempt 2
ran 19 rounds / 162 tool calls with no context ceiling into a provider refusal
at 229,377 prompt tokens.

Four fixes, each tested here:

1. The core plane STREAMS: SSE chunks fold back into the exact non-streaming
   shape (text, a tool-call round, the usage block), text reaches an installed
   sink, and a stream cut after output raises ``StreamInterruptedFailure``
   carrying it — never retried at the HTTP layer.
2. A consult cut by ANY exception after streaming answer text delivers it as a
   partial (drilling round and forced final), on the real core handler.
3. A consult is never transient-retried from scratch: one ``render_prompt``
   per request.
4. With no descriptor ceiling the loop derives one from the route's window.

No live Postgres, no live model: the real handler runs over
``httpx.MockTransport``; the consult loop runs over that handler or doubles.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import yaml

# Warm the registry side of the legba.data.stack.llm <-> provenance cycle first
# (the same workaround tests/data_pkg/test_stack_llm.py documents).
from legba.data.registry.credentials import MissingSecretError  # noqa: F401

from legba.data.analysts import consult_context_ceiling as cc
from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps, run_method
from legba.data.schemas import LLMProviderConfig, Property
from legba.data.schemas.analyst import AnalystDescriptor, TransientRetryPolicy
from legba.data.stack.llm import (
    StreamInterruptedFailure,
    TransientLLMFailure,
    VLLMProviderHandler,
)
from legba.data.stack.llm.stream_observer import (
    TextDeltaSink,
    capture_text_deltas,
    publish_text_delta,
)
from legba.runtime.actor_retry import (
    _effective_transient_attempts,
    _invoke_with_transient_retry,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
BASE_URL = "https://gw.example.test"
MODEL = "fast"


# ---------------------------------------------------------------------------
# Harness — a REAL core-plane handler over a mock transport
# ---------------------------------------------------------------------------


def _handler(*, price: bool = False) -> VLLMProviderHandler:
    kwargs: dict[str, Any] = dict(
        api_endpoint=Property.Text.of(f"{BASE_URL}/v1"),
        api_key=Property.Secret.of("llm.primary.key"),
        model_name=Property.Text.of(MODEL),
        max_tokens=Property.Number.of(32768, minimum=1, maximum=200000),
    )
    if price:
        kwargs["price_input_per_m"] = Property.Number.of(0.5)
        kwargs["price_output_per_m"] = Property.Number.of(2.0)
    h = VLLMProviderHandler()
    h._cfg = LLMProviderConfig(**kwargs)  # noqa: SLF001
    h._api_key = "k"  # noqa: SLF001
    h._instance_id = "llm.primary.openai_compat"  # noqa: SLF001
    return h


class _Wire:
    """Records every request body; answers each with the next scripted reply."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [
                {"id": "fast", "context_tokens": 262144},
                {"id": "deep", "context_tokens": 131072},
            ]})
        self.bodies.append(json.loads(request.content))
        reply = self.replies.pop(0)
        return reply() if callable(reply) else reply


def _install(h: VLLMProviderHandler, wire: _Wire) -> None:
    h._client = httpx.AsyncClient(  # noqa: SLF001
        base_url=BASE_URL,
        headers=h._auth_headers(),  # noqa: SLF001
        transport=httpx.MockTransport(wire),
        timeout=httpx.Timeout(10.0),
    )


def _sse_bytes(chunks: list[dict[str, Any]], *, done: bool = True) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    if done:
        body += "data: [DONE]\n\n"
    return body.encode("utf-8")


def _sse(chunks: list[dict[str, Any]], *, done: bool = True) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=_sse_bytes(chunks, done=done),
    )


class _CutStream(httpx.AsyncByteStream):
    """Delivers ``head`` and then drops the connection."""

    def __init__(self, head: bytes) -> None:
        self._head = head

    async def __aiter__(self):
        yield self._head
        raise httpx.ReadError("connection dropped")

    async def aclose(self) -> None:
        return None


def _cut(chunks: list[dict[str, Any]]) -> Any:
    return lambda: httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=_CutStream(_sse_bytes(chunks, done=False)),
    )


def _chunk(delta: dict[str, Any] | None = None, *, finish: str | None = None,
           usage: dict[str, Any] | None = None, choices: bool = True) -> dict[str, Any]:
    c: dict[str, Any] = {
        "id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1,
        "model": MODEL, "provider": "gateway",
        "choices": (
            [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
            if choices else []
        ),
    }
    if usage is not None:
        c["usage"] = usage
    return c


USAGE = {"prompt_tokens": 1200, "completion_tokens": 340, "total_tokens": 1540}
TEXT_PIECES = ["## Assessment\n\n", "The ladder ", "is intact", "; escalation is unlikely."]

TEXT_STREAM = [
    _chunk({"role": "assistant", "content": ""}),
    *[_chunk({"content": p}) for p in TEXT_PIECES],
    _chunk({}, finish="stop"),
    _chunk(usage=USAGE, choices=False),
]
TEXT_JSON = {
    "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": MODEL,
    "provider": "gateway",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(TEXT_PIECES)},
                 "finish_reason": "stop"}],
    "usage": USAGE,
}

TOOL_STREAM = [
    _chunk({"role": "assistant", "content": None}),
    _chunk({"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                            "function": {"name": "search_signals", "arguments": ""}}]}),
    _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"query": '}}]}),
    _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"iran"}'}}]}),
    _chunk({"tool_calls": [{"index": 1, "id": "call_b", "type": "function",
                            "function": {"name": "query_facts",
                                         "arguments": '{"limit": 3}'}}]}),
    _chunk({}, finish="tool_calls"),
    _chunk(usage=USAGE, choices=False),
]
TOOL_JSON = {
    "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": MODEL,
    "provider": "gateway",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_a", "type": "function",
         "function": {"name": "search_signals", "arguments": '{"query": "iran"}'}},
        {"id": "call_b", "type": "function",
         "function": {"name": "query_facts", "arguments": '{"limit": 3}'}},
    ]}, "finish_reason": "tool_calls"}],
    "usage": USAGE,
}


def _shape(resp: Any) -> dict[str, Any]:
    return {
        "content": resp.content,
        "finish_reason": resp.finish_reason,
        "tool_calls": [(t.id, t.name, t.arguments) for t in resp.tool_calls],
        "usage": resp.usage,
        "served_by": (resp.raw_response or {}).get("provider"),
    }


async def _both(stream: list[dict[str, Any]], body: dict[str, Any], *, price: bool = False,
                tools: Any = None) -> tuple[Any, Any, _Wire]:
    h = _handler(price=price)
    wire = _Wire(_sse(stream), httpx.Response(200, json=body))
    _install(h, wire)
    kwargs = {"tools": tools} if tools else {}
    streamed = await h.chat_complete([{"role": "user", "content": "q"}], **kwargs)
    plain = await h.chat_complete([{"role": "user", "content": "q"}], stream=False, **kwargs)
    return streamed, plain, wire


# ---------------------------------------------------------------------------
# 1. The core plane streams, and a completed stream IS the non-streamed answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_streamed_text_answer_equals_the_non_streamed_one() -> None:
    streamed, plain, wire = await _both(TEXT_STREAM, TEXT_JSON)
    assert _shape(streamed) == _shape(plain)
    assert streamed.content == "".join(TEXT_PIECES)
    assert wire.bodies[0]["stream"] is True
    assert wire.bodies[0]["stream_options"] == {"include_usage": True}
    # The explicit flag is the only way back to the historical wire call.
    assert wire.bodies[1]["stream"] is False
    assert "stream_options" not in wire.bodies[1]


@pytest.mark.asyncio
async def test_a_streamed_tool_call_round_equals_the_non_streamed_one() -> None:
    tools = [{"type": "function", "function": {"name": "search_signals", "parameters": {}}}]
    streamed, plain, _wire = await _both(TOOL_STREAM, TOOL_JSON, tools=tools)
    assert _shape(streamed) == _shape(plain)
    assert [t.name for t in streamed.tool_calls] == ["search_signals", "query_facts"]
    assert streamed.tool_calls[0].arguments == {"query": "iran"}
    assert streamed.finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_a_streamed_usage_block_prices_exactly_like_the_non_streamed_one() -> None:
    streamed, plain, _wire = await _both(TEXT_STREAM, TEXT_JSON, price=True)
    assert streamed.usage == plain.usage
    assert streamed.usage.prompt_tokens == 1200
    assert streamed.usage.completion_tokens == 340
    assert streamed.usage.total_tokens == 1540
    assert streamed.usage.cost_estimate_usd > 0


@pytest.mark.asyncio
async def test_the_stream_publishes_text_and_tool_starts_to_an_installed_sink() -> None:
    h = _handler()
    _install(h, _Wire(_sse(TEXT_STREAM), _sse(TOOL_STREAM)))
    sink = TextDeltaSink()
    with capture_text_deltas(sink):
        await h.chat_complete([{"role": "user", "content": "q"}])
    assert sink.text == "".join(TEXT_PIECES)
    assert sink.tool_use_started is False
    tool_sink = TextDeltaSink()
    with capture_text_deltas(tool_sink):
        await h.chat_complete([{"role": "user", "content": "q"}])
    assert tool_sink.tool_use_started is True


@pytest.mark.asyncio
async def test_a_stream_cut_after_text_raises_with_the_partial_and_is_not_retried() -> None:
    """The 6cd91190 forced final, on a streaming wire: the text survives the drop."""
    h = _handler()
    wire = _Wire(_cut([_chunk({"role": "assistant"}), _chunk({"content": "The ladder "}),
                       _chunk({"content": "is intact"})]))
    _install(h, wire)
    sink = TextDeltaSink()
    with capture_text_deltas(sink), pytest.raises(StreamInterruptedFailure) as info:
        await h.chat_complete([{"role": "user", "content": "q"}])
    exc = info.value
    assert isinstance(exc, TransientLLMFailure)  # every classifier buckets it as before
    assert exc.partial_text == "The ladder is intact"
    assert exc.tool_use_started is False
    assert exc.partial_body["choices"][0]["finish_reason"] == "error"
    assert "ReadError" in exc.partial_body["legba_stream_error"]
    assert sink.text == "The ladder is intact"
    assert len(wire.bodies) == 1, "a stream that delivered output must never be re-requested"


@pytest.mark.asyncio
async def test_a_stream_cut_before_any_output_is_a_plain_transient_failure() -> None:
    h = _handler()
    wire = _Wire(_cut([_chunk({"role": "assistant", "content": ""})]))
    _install(h, wire)
    with pytest.raises(TransientLLMFailure) as info:
        await h.chat_complete([{"role": "user", "content": "q"}])
    assert not isinstance(info.value, StreamInterruptedFailure)
    assert "before any content" in str(info.value)
    assert len(wire.bodies) == 1


@pytest.mark.asyncio
async def test_a_stream_that_closes_without_its_terminator_is_a_cut() -> None:
    h = _handler()
    _install(h, _Wire(_sse([_chunk({"content": "half an ans"})], done=False)))
    with pytest.raises(StreamInterruptedFailure) as info:
        await h.chat_complete([{"role": "user", "content": "q"}])
    assert info.value.partial_text == "half an ans"
    assert "before [DONE]" in str(info.value)


@pytest.mark.asyncio
async def test_a_pre_content_error_event_is_retried_like_the_router_envelope(monkeypatch) -> None:
    """Nothing generated ⇒ the base's router branch: retried, then served."""
    slept: list[float] = []

    async def _no_sleep(seconds: float, *a: Any, **k: Any) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    h = _handler()
    error_event = {"id": "x", "error": {"code": 502, "message": "Upstream overloaded"}}
    wire = _Wire(_sse([error_event], done=False), _sse(TEXT_STREAM))
    _install(h, wire)
    resp = await h.chat_complete([{"role": "user", "content": "q"}])
    assert resp.content == "".join(TEXT_PIECES)
    assert len(wire.bodies) == 2
    assert slept == [1]


@pytest.mark.asyncio
async def test_the_model_listing_carries_the_context_window() -> None:
    h = _handler()
    _install(h, _Wire())
    names = await h._fetch_model_list()  # noqa: SLF001
    assert names == ["fast", "deep"]
    assert h.context_window_tokens() == 262144


# ---------------------------------------------------------------------------
# 2. A consult cut by ANY failure after answer text delivers a partial
# ---------------------------------------------------------------------------


class _Substrate:
    async def search_signals(self, **kwargs: Any) -> dict[str, Any]:
        return {"count": 1, "rows": [{"id": "sig-1", "title": "t"}], "refs": []}


def _tool_round_stream() -> httpx.Response:
    return _sse([
        _chunk({"role": "assistant"}),
        _chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                "function": {"name": "search_signals",
                                             "arguments": '{"query": "iran"}'}}]}),
        _chunk({}, finish="tool_calls"),
        _chunk(usage=USAGE, choices=False),
    ])


ANSWER_HEAD = "## Assessment\n\nThe escalation ladder is intact. " * 20


async def _consult(llm: Any, *, rounds: int, options: dict[str, Any] | None = None,
                   **dep_kwargs: Any) -> tuple[Any, list[dict[str, Any]]]:
    steps: list[dict[str, Any]] = []

    async def _publish(step: dict[str, Any]) -> None:
        steps.append(step)

    deps = ConsultOnDemandDeps(
        llm=llm, substrate=_Substrate(), step_publish=_publish, **dep_kwargs,
    )
    result = await run_method(
        [{"question": "what is happening?", "max_tool_rounds": rounds}], options or {}, deps,
    )
    return result, steps


def _kinds(steps: list[dict[str, Any]]) -> list[str]:
    return [s.get("kind") for s in steps]


def _first(steps: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    return next((s for s in steps if s.get("kind") == kind), None)


@pytest.mark.asyncio
async def test_core_forced_final_cut_mid_stream_delivers_the_partial_and_does_not_raise() -> None:
    """The failure on the real core handler: tool round, then a forced final
    whose stream drops after writing — the text is delivered, once."""
    h = _handler()
    wire = _Wire(
        _tool_round_stream,
        _cut([_chunk({"role": "assistant"}),
              *[_chunk({"content": ANSWER_HEAD[i:i + 64]}) for i in range(0, len(ANSWER_HEAD), 64)]]),
    )
    _install(h, wire)
    result, steps = await _consult(h, rounds=1)

    assert len(wire.bodies) == 2, "no generation may be bought after the cut"
    assert "tools" in wire.bodies[0] and "tools" not in wire.bodies[1]
    assert all(b["stream"] is True for b in wire.bodies)
    pf = _first(steps, "partial_final")
    assert pf is not None and pf["reason"] == "StreamInterruptedFailure"
    assert pf["chars"] == len(ANSWER_HEAD)
    assert "forced_final_error" not in _kinds(steps)
    data = result.consult_response.data
    assert data["synthesis_status"] == "partial"
    assert "The escalation ladder is intact." in result.consult_response.answer
    assert "the synthesis failed (StreamInterruptedFailure)" in result.consult_response.answer
    # The live panel saw the answer form under the final segment.
    deltas = [s for s in steps if s.get("kind") == "answer_delta"]
    assert "".join(d["text"] for d in deltas) == ANSWER_HEAD


@pytest.mark.asyncio
async def test_core_answering_round_cut_mid_stream_delivers_and_skips_the_forced_final() -> None:
    h = _handler()
    wire = _Wire(
        _tool_round_stream,
        _cut([_chunk({"role": "assistant"}), _chunk({"content": ANSWER_HEAD})]),
    )
    _install(h, wire)
    result, steps = await _consult(h, rounds=4)

    assert len(wire.bodies) == 2
    kinds = _kinds(steps)
    assert "forced_final" not in kinds and "llm_error" not in kinds
    pf = _first(steps, "partial_final")
    assert pf is not None and pf["reason"] == "StreamInterruptedFailure" and pf["round"] == 2
    assert result.consult_response.data["synthesis_status"] == "partial"


@pytest.mark.asyncio
async def test_core_round_cut_mid_tool_call_falls_through_to_the_forced_final() -> None:
    """A cut tool call is not an answer: the forced final synthesises instead."""
    h = _handler()
    wire = _Wire(
        _tool_round_stream,
        _cut([_chunk({"role": "assistant"}), _chunk({"content": "Let me check. "}),
              _chunk({"tool_calls": [{"index": 0, "id": "c", "type": "function",
                                      "function": {"name": "search_signals",
                                                   "arguments": '{"qu'}}]})]),
        _sse(TEXT_STREAM),
    )
    _install(h, wire)
    result, steps = await _consult(h, rounds=4)

    kinds = _kinds(steps)
    assert "round_stream_interrupted" in kinds
    assert "partial_final" not in kinds
    assert "forced_final" in kinds
    assert len(wire.bodies) == 3
    assert result.consult_response.data["synthesis_status"] == "complete"


@pytest.mark.asyncio
async def test_a_failure_with_nothing_written_still_propagates() -> None:
    h = _handler()
    wire = _Wire(_tool_round_stream, _cut([_chunk({"role": "assistant", "content": ""})]))
    _install(h, wire)
    steps: list[dict[str, Any]] = []

    async def _publish(step: dict[str, Any]) -> None:
        steps.append(step)

    deps = ConsultOnDemandDeps(llm=h, substrate=_Substrate(), step_publish=_publish)
    with pytest.raises(TransientLLMFailure):
        await run_method([{"question": "q?", "max_tool_rounds": 1}], {}, deps)
    assert "forced_final_error" in _kinds(steps)
    assert "partial_final" not in _kinds(steps)


@dataclass
class _Usage:
    prompt_tokens: int = 100
    completion_tokens: int = 50
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 150
    cost_estimate_usd: float = 0.0
    model: str = "m"


@dataclass
class _ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class _Response:
    content: str = ""
    finish_reason: str = "stop"
    tool_calls: list[_ToolCall] = field(default_factory=list)
    usage: _Usage = field(default_factory=_Usage)
    raw_response: dict[str, Any] | None = None


def _tool_turn() -> _Response:
    return _Response(finish_reason="tool_calls", tool_calls=[
        _ToolCall(id="call-0", name="search_signals", arguments={"query": "iran"}),
    ])


class _FailingFinalLLM:
    """Tool round, then a forced final that streams and raises ``exc``."""

    PRICE_TABLE: dict[str, Any] = {}

    def __init__(self, exc: BaseException, *, subprovider: str = "vllm") -> None:
        self.subprovider = subprovider
        self._exc = exc
        self.calls = 0

    async def chat_complete(self, messages: Any, **kwargs: Any) -> _Response:
        self.calls += 1
        if self.calls == 1:
            return _tool_turn()
        publish_text_delta(ANSWER_HEAD)
        raise self._exc


@pytest.mark.asyncio
async def test_any_exception_class_after_text_delivers_the_partial() -> None:
    """Not only the stream's own failure: whatever ended the call."""
    llm = _FailingFinalLLM(RuntimeError("the gateway closed the socket"))
    result, steps = await _consult(llm, rounds=1)
    pf = _first(steps, "partial_final")
    assert pf is not None and pf["reason"] == "RuntimeError"
    assert llm.calls == 2
    assert result.consult_response.data["synthesis_status"] == "partial"


class _ReturnedCutLLM:
    """Anthropic's contract: the cut comes back as a RESPONSE with an error marker."""

    PRICE_TABLE: dict[str, Any] = {}

    def __init__(self) -> None:
        self.subprovider = "anthropic"
        self.calls = 0

    async def chat_complete(self, messages: Any, **kwargs: Any) -> _Response:
        self.calls += 1
        if self.calls == 1:
            return _tool_turn()
        publish_text_delta(ANSWER_HEAD)
        return _Response(
            content=ANSWER_HEAD, finish_reason="error",
            raw_response={"legba_stream_error": "ReadError: dropped", "stop_reason": "error"},
        )


@pytest.mark.asyncio
async def test_a_returned_stream_cut_is_a_partial_never_a_complete_answer() -> None:
    llm = _ReturnedCutLLM()
    result, steps = await _consult(llm, rounds=4)
    pf = _first(steps, "partial_final")
    assert pf is not None and pf["reason"] == "StreamInterruptedFailure"
    assert llm.calls == 2
    assert result.consult_response.data["synthesis_status"] == "partial"


# ---------------------------------------------------------------------------
# 3. A consult is never re-run from scratch by the actor's transient retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["analyst_consult_default.yaml", "analyst_deep_consult.yaml"])
def test_the_consult_descriptors_run_once(name: str) -> None:
    raw = yaml.safe_load((REPO / "descriptors" / name).read_text(encoding="utf-8"))
    desc = AnalystDescriptor.model_validate(raw)
    assert desc.method.retry.transient.max_attempts == 1


class _NoTextFailureLLM:
    PRICE_TABLE: dict[str, Any] = {}
    subprovider = "vllm"

    async def chat_complete(self, messages: Any, **kwargs: Any) -> _Response:
        raise TransientLLMFailure("network error: ")


async def _actor_style_run(policy: Any, *, with_stream: bool) -> tuple[list[dict[str, Any]], int]:
    """The actor's attempt budget around the REAL consult run_method."""
    steps: list[dict[str, Any]] = []

    async def _publish(step: dict[str, Any]) -> None:
        steps.append(step)

    deps = ConsultOnDemandDeps(
        llm=_NoTextFailureLLM(), substrate=_Substrate(),
        step_publish=_publish if with_stream else None,
    )
    attempts: list[int] = []
    with pytest.raises(TransientLLMFailure):
        await _invoke_with_transient_retry(
            lambda: run_method([{"question": "q?", "request_id": "1f2029f7"}], {}, deps),
            policy=policy,
            max_attempts=_effective_transient_attempts(policy, deps),
            actor_id="consult_default",
            on_attempt=attempts.append,
        )
    return steps, len(attempts)


@pytest.mark.asyncio
async def test_the_consult_descriptor_policy_renders_one_prompt_per_request(monkeypatch) -> None:
    async def _no_sleep(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    raw = yaml.safe_load(
        (REPO / "descriptors" / "analyst_consult_default.yaml").read_text(encoding="utf-8")
    )
    policy = AnalystDescriptor.model_validate(raw).method.retry.transient
    steps, attempts = await _actor_style_run(policy, with_stream=True)
    assert attempts == 1
    assert _kinds(steps).count("render_prompt") == 1


@pytest.mark.asyncio
async def test_a_streaming_run_is_never_retried_even_under_the_default_policy(monkeypatch) -> None:
    """A descriptor that never set ``max_attempts: 1`` is still held to one run."""
    async def _no_sleep(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    steps, attempts = await _actor_style_run(TransientRetryPolicy(), with_stream=True)
    assert attempts == 1
    assert _kinds(steps).count("render_prompt") == 1


@pytest.mark.asyncio
async def test_a_scheduled_analyst_keeps_its_transient_retries(monkeypatch) -> None:
    async def _no_sleep(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    _steps, attempts = await _actor_style_run(TransientRetryPolicy(), with_stream=False)
    assert attempts == 3


# ---------------------------------------------------------------------------
# 4. No descriptor ceiling ⇒ a derived one
# ---------------------------------------------------------------------------


class _Windowed:
    def __init__(self, window: int | None) -> None:
        self._window = window

    def context_window_tokens(self) -> int | None:
        return self._window


class _WindowedWithMaxima(_Windowed):
    def __init__(self, window: int | None, maxima: dict[str, int]) -> None:
        super().__init__(window)
        self._maxima = maxima

    def max_output_tokens_by_alias(self) -> dict[str, int]:
        return dict(self._maxima)


def test_the_ceiling_derives_from_the_routes_window(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_LLM_INPUT_TOKEN_BUDGET", raising=False)
    # No declared maximum output: half the window is kept for the output.
    assert cc.derive_ceiling(_Windowed(262144)) == (
        262144 - 131072 - 4096, cc.SOURCE_MODEL_WINDOW,
    )
    # A thinking alias that may write 131,072 tokens: the same room is kept.
    both = _WindowedWithMaxima(262144, {"fast": 32768, "deep": 131072})
    assert cc.derive_ceiling(both) == (262144 - 131072 - 4096, cc.SOURCE_MODEL_WINDOW)
    # A plain alias whose maximum is smaller leaves the input more of the window.
    plain = _WindowedWithMaxima(262144, {"fast": 32768})
    assert cc.derive_ceiling(plain) == (262144 - 32768 - 4096, cc.SOURCE_MODEL_WINDOW)


def test_without_a_window_the_ceiling_is_the_input_budget(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_LLM_INPUT_TOKEN_BUDGET", "65536")
    assert cc.derive_ceiling(_Windowed(None)) == (65536, cc.SOURCE_INPUT_BUDGET_ENV)
    assert cc.derive_ceiling(object()) == (65536, cc.SOURCE_INPUT_BUDGET_ENV)
    # The explicit override wins over a known window too.
    assert cc.derive_ceiling(_Windowed(262144)) == (65536, cc.SOURCE_INPUT_BUDGET_ENV)
    monkeypatch.delenv("LEGBA_LLM_INPUT_TOKEN_BUDGET")
    ceiling, source = cc.derive_ceiling(object())
    assert source == cc.SOURCE_INPUT_BUDGET_DEFAULT and ceiling > 0


class _BigResults:
    """Every search returns ~24 KB, so two rounds outrun a small window."""

    async def search_signals(self, **kwargs: Any) -> dict[str, Any]:
        rows = [{"id": f"sig-{i}", "title": "x" * 200} for i in range(100)]
        return {"count": len(rows), "rows": rows, "refs": []}


class _WindowedDrillLLM:
    PRICE_TABLE: dict[str, Any] = {}
    subprovider = "vllm"

    def __init__(self, window: int) -> None:
        self._window = window
        self.sent: list[Any] = []

    def context_window_tokens(self) -> int:
        return self._window

    async def chat_complete(self, messages: Any, **kwargs: Any) -> _Response:
        self.sent.append(list(messages))
        if len(self.sent) <= 2:
            return _Response(finish_reason="tool_calls", tool_calls=[
                _ToolCall(id=f"call-{len(self.sent)}", name="search_signals",
                          arguments={"query": f"q{len(self.sent)}"}),
            ])
        return _Response(content="<<<FINAL>>>\nuncertainty: 0.3\n\nthe answer")


async def _ceiling_run(llm: Any, options: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []

    async def _publish(step: dict[str, Any]) -> None:
        steps.append(step)

    deps = ConsultOnDemandDeps(
        llm=llm, substrate=_BigResults(), step_publish=_publish, max_tokens=1000,
    )
    await run_method([{"question": "q?", "max_tool_rounds": 4}], options, deps)
    return steps


@pytest.fixture
def _live_compaction_pins(monkeypatch):
    """The host's operator pins (compaction off) — the 6cd91190 configuration."""
    monkeypatch.setenv("LEGBA_CONSULT_KEEP_FULL_ROUNDS", "1000")
    monkeypatch.setenv("LEGBA_CONSULT_ROUND_RESULT_BYTES", "40000")
    # The input-budget override would win over the window; these runs test the window.
    monkeypatch.delenv("LEGBA_LLM_INPUT_TOKEN_BUDGET", raising=False)


@pytest.mark.asyncio
async def test_a_derived_ceiling_applies_when_the_option_is_absent(_live_compaction_pins) -> None:
    # No declared maximum output: half the window is kept for it, so the
    # derived ceiling is window - window/2 - 4,096 = 8,000.
    llm = _WindowedDrillLLM(window=2 * (8000 + 4096))
    steps = await _ceiling_run(llm, {})

    rp = _first(steps, "render_prompt")
    assert rp is not None
    assert rp["context_ceiling_tokens"] == 8000
    assert rp["context_ceiling_source"] == "model_window"
    stamped = [s for s in steps if s.get("kind") == "llm_call" and s.get(cc.STAMP_KEY)]
    assert stamped, "the derived ceiling never bit on a transcript past it"
    stamp = stamped[-1][cc.STAMP_KEY]
    assert stamp["ceiling"] == 8000
    assert stamp["ceiling_source"] == "model_window"
    assert stamp["results_dropped"] >= 1
    assert "withheld from this call" in json.dumps(llm.sent[-1])


@pytest.mark.asyncio
async def test_a_descriptor_ceiling_still_wins_and_says_so(_live_compaction_pins) -> None:
    llm = _WindowedDrillLLM(window=10**6)
    steps = await _ceiling_run(llm, {cc.OPTION_NAME: 8000})
    rp = _first(steps, "render_prompt")
    assert rp["context_ceiling_tokens"] == 8000
    assert rp["context_ceiling_source"] == "descriptor"
    stamped = [s for s in steps if s.get("kind") == "llm_call" and s.get(cc.STAMP_KEY)]
    assert stamped and stamped[-1][cc.STAMP_KEY]["ceiling_source"] == "descriptor"
