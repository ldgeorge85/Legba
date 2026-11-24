# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A deep consult turn records per-call receipts and totals, as a chat turn
does (roll-86 follow-up).

Roll 86's live deep turn carried ``call_receipts: null`` and no route or
token counts: the worker's stages bound no run account, so the provider
handler's per-call accounting recorded into nothing. These drive the stages
through the REAL provider handler (``VLLMProviderHandler`` over a fake wire)
so the receipts come from the same chokepoint a chat turn's do, then through
the synthesize write and the same ``usage_receipt`` the turn row is stamped
with. No prompt or completion text reaches a receipt.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from legba.data.registry.credentials import MissingSecretError  # noqa: F401 — cycle warm-up
from legba.data.registry import consult_turn_stamps as st
from legba.data.registry.deep_consult_api import _deep_plane_route
from legba.data.schemas import LLMProviderConfig, Property
from legba.data.stack.llm import VLLMProviderHandler
from legba.runtime.dapr_workflow.deep_consult import (
    DeepConsultStageDeps,
    DeepConsultWorkflowInput,
    _run_acquire,
    _run_analyze,
    _run_plan,
    _run_synthesize,
)
from legba.runtime.dapr_workflow.deep_consult_usage import deep_usage

CORE_ID = "llm.primary.openai_compat"
SECRET_WORDS = "the-model-said-this-and-it-must-not-leave-the-trace"


class _Wire:
    """Answers each request with the next scripted content and its usage."""

    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.requests: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append(body)
        n = len(self.requests)
        content = self.contents.pop(0) if self.contents else json.dumps(
            {"final": True, "answer": "(default)", "uncertainty": 1.0, "cited_refs": []},
        )
        return httpx.Response(200, json={
            "choices": [{"index": 0, "message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100 * n, "completion_tokens": 10 * n, "total_tokens": 110 * n},
        })


def _handler(wire: _Wire) -> VLLMProviderHandler:
    h = VLLMProviderHandler()
    h._cfg = LLMProviderConfig(  # noqa: SLF001
        api_endpoint=Property.Text.of("https://gw.example.test/v1"),
        api_key=Property.Secret.of("k"),
        model_name=Property.Text.of("fast"),
        max_tokens=Property.Number.of(16384, minimum=1, maximum=200000),
    )
    h._api_key = "k"  # noqa: SLF001
    h._instance_id = CORE_ID  # noqa: SLF001
    transport = httpx.AsyncBaseTransport()
    transport.handle_async_request = wire.handle_async_request  # type: ignore[method-assign]
    h._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://gw.example.test", transport=transport, timeout=10.0,
    )
    return h


class _Substrate:
    def __init__(self, ref: str) -> None:
        self.ref = ref

    async def search_signals(self, *, query: str, limit: int = 20, scope_predicate: str | None = None) -> dict[str, Any]:
        return {"rows": [{"id": self.ref, "title": "row"}], "refs": [self.ref]}


class _Conn:
    def __init__(self, sink: list[tuple[str, tuple[Any, ...]]]) -> None:
        self.sink = sink

    async def execute(self, sql: str, *args: Any) -> str:
        self.sink.append((sql, args))
        return "INSERT 0 1"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return None

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return None

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return []

    async def executemany(self, sql: str, args: Any) -> None:
        for a in args:
            self.sink.append((sql, tuple(a)))

    def transaction(self) -> Any:
        # A finding and its citation rows (lane DM-A) commit in one
        # transaction; this fake records statements and has none to open.
        class _Tx:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *exc: Any) -> None:
                return None

        return _Tx()


class _Pool:
    def __init__(self) -> None:
        self.sink: list[tuple[str, tuple[Any, ...]]] = []

    def acquire(self) -> Any:
        conn = _Conn(self.sink)

        class _Ctx:
            async def __aenter__(self) -> _Conn:
                return conn

            async def __aexit__(self, *exc: Any) -> None:
                return None

        return _Ctx()


@pytest.mark.asyncio
async def test_a_deep_turn_records_the_receipts_and_totals_a_chat_turn_does() -> None:
    ref = str(uuid4())
    wire = _Wire([
        json.dumps({"sub_queries": ["a"], "tool_plan": [{"tool": "search_signals", "args": {"query": "q"}}]}),
        json.dumps({"final": True, "answer": f"Grid strained. {SECRET_WORDS}", "uncertainty": 0.3,
                    "cited_refs": [ref], "unanswered_aspects": []}),
        json.dumps({"candidate_facts": [], "candidate_hypotheses": []}),
    ])
    llm = _handler(wire)
    pool = _Pool()
    deps = DeepConsultStageDeps(llm=llm, substrate=_Substrate(ref), pg_pool=pool)
    wf_input = DeepConsultWorkflowInput(
        question="How is the grid?", run_id=str(uuid4()), llm_component_id=CORE_ID,
    )

    plan = await _run_plan(wf_input, deps)
    acquired = await _run_acquire(plan, deps)
    analyzed = await _run_analyze(acquired, deps)
    usage = analyzed["usage"]

    # one receipt per call that went over the wire, in call order
    n = len(wire.requests)
    assert n >= 3 and usage["calls"] == n == len(usage["call_receipts"])
    assert [r["prompt_tokens"] for r in usage["call_receipts"]] == [100 * i for i in range(1, n + 1)]
    # totals are the receipts' own sums
    assert usage["input_tokens"] == sum(100 * i for i in range(1, n + 1))
    assert usage["output_tokens"] == sum(10 * i for i in range(1, n + 1))
    assert usage["priced"] is False and usage["subprovider"] == "vllm"
    assert all(r["component_id"] == CORE_ID and r["status"] == "success" for r in usage["call_receipts"])
    # no prompt or completion text anywhere in what the workflow carries on
    assert SECRET_WORDS not in json.dumps(analyzed["stage_usage"]) + json.dumps(usage)
    assert "completion_text" not in json.dumps(usage)

    # synthesize stores it on the finding; the status poll stamps it on the turn
    await _run_synthesize(analyzed, deps)
    finding_args = [a for sql, args in pool.sink if "analyst_outputs" in sql for a in args]
    assert any(isinstance(a, str) and '"call_receipts"' in a for a in finding_args)
    plane, route = _deep_plane_route(usage)
    assert (plane, route) == ("core", CORE_ID)
    turn = st.usage_receipt(usage, plane=plane, route=route, reason_if_absent="deep_consult_usage_not_carried")
    assert turn["calls"] == n and turn["input_tokens"] == usage["input_tokens"]
    assert turn["route"] == CORE_ID and turn["plane"] == "core"
    assert turn["cost_usd"] is None and turn["cost_reason"] == "route_unpriced"
    assert turn["model"] == "fast" and turn["models"] == {"fast": n}
    assert turn["call_receipts"] == usage["call_receipts"] and turn["reason"] is None


def test_a_run_from_before_carries_no_stage_usage_and_keeps_its_label() -> None:
    # an in-flight workflow replayed across the deploy has no ``stage_usage``
    assert deep_usage(None, object()) is None
    assert deep_usage({}, object()) is None
    turn = st.usage_receipt(None, plane=None, route=None, reason_if_absent="deep_consult_usage_not_carried")
    assert turn["call_receipts"] is None and turn["reason"] == "deep_consult_usage_not_carried"
    assert _deep_plane_route(None) == (None, None)
    two = {"call_receipts": [{"component_id": "a"}, {"component_id": "b"}]}
    assert _deep_plane_route(two) == (None, None)
