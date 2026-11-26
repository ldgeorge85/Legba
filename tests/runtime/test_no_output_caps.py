# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""No output token caps on any route.

The deep Consult's ``max_analyze_tokens`` (the descriptor's
``method.llm.max_tokens``, 32768) acted as a per-call output cap, with the plan
and candidate-extraction calls clamping it to 2048. On the core route it never
reached the wire; on a priced Anthropic route it was the real cap (that API
requires a ``max_tokens``).

Now: no provider sends a caller's ``max_tokens``. A wire that requires the
field gets the model's maximum from ONE declared place per route
(``LLMProviderHandler.model_max_output_tokens``: Anthropic's documented table,
else the component's declared ``max_output_tokens`` for the alias called); the
"partial" label and the
turn's spend projection read the same limit; and a durable workflow input
written before this change still deserializes and replays.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any
from uuid import uuid4

import httpx
import pytest

from legba.data.registry.credentials import MissingSecretError  # noqa: F401 — cycle warm-up
from legba.data.analysts import consult_round_protocol as cp
from legba.data.analysts.consult_spend_guard import SpendGuard
from legba.data.analysts.consult_turn_cost import (
    PROJECTION_FALLBACK_OUTPUT_TOKENS,
    PROJECTION_HISTORY_DAYS,
    TURN_COST_CEILING_ENV,
    TurnCeilingGuard,
    arm_turn_ceiling,
    arm_turn_ceiling_observed,
)
from legba.data.registry import consult_turn_stamps
from legba.data.schemas import LLMProviderConfig, Property
from legba.data.stack.llm import AnthropicProviderHandler, VLLMProviderHandler
from legba.runtime.dapr_workflow.deep_consult import (
    DeepConsultStageDeps,
    DeepConsultWorkflowInput,
    _run_analyze,
    _run_plan,
)


def _cfg(model: str, max_tokens: int) -> LLMProviderConfig:
    return LLMProviderConfig(
        api_endpoint=Property.Text.of("https://gw.example.test/v1"),
        api_key=Property.Secret.of("k"),
        model_name=Property.Text.of(model),
        max_output_tokens=Property.Number.of(max_tokens, minimum=1),
    )


class _Wire:
    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.bodies: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content or b"{}"))
        content = self.contents.pop(0) if self.contents else json.dumps(
            {"final": True, "answer": "ok", "uncertainty": 0.5, "cited_refs": []},
        )
        return httpx.Response(200, json={
            "choices": [{"index": 0, "message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
        })


def _vllm(wire: _Wire, *, declared: int) -> VLLMProviderHandler:
    h = VLLMProviderHandler()
    h._cfg = _cfg("fast", declared)  # noqa: SLF001
    h._api_key = "k"  # noqa: SLF001
    h._instance_id = "llm.primary.openai_compat"  # noqa: SLF001
    transport = httpx.AsyncBaseTransport()
    transport.handle_async_request = wire.handle_async_request  # type: ignore[method-assign]
    h._client = httpx.AsyncClient(base_url="https://gw.example.test", transport=transport, timeout=10.0)  # noqa: SLF001
    return h


@pytest.mark.asyncio
async def test_the_deep_plan_clamp_never_reaches_a_wire_that_takes_a_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the plan call sends the alias's declared maximum, never
    # min(2048, max_analyze_tokens)
    monkeypatch.delenv("LEGBA_LLM_SEND_MAX_TOKENS", raising=False)
    wire = _Wire([json.dumps({"sub_queries": [], "tool_plan": []})])
    deps = DeepConsultStageDeps(llm=_vllm(wire, declared=65_536), substrate=object())
    plan = await _run_plan(DeepConsultWorkflowInput(question="q", max_analyze_tokens=32_768), deps)
    assert plan["ok"] is True
    assert wire.bodies[0]["max_tokens"] == 65_536
    # and with the deployment-wide off switch, no limit at all
    monkeypatch.setenv("LEGBA_LLM_SEND_MAX_TOKENS", "0")
    wire2 = _Wire([json.dumps({"sub_queries": [], "tool_plan": []})])
    await _run_plan(DeepConsultWorkflowInput(question="q"), DeepConsultStageDeps(llm=_vllm(wire2, declared=65_536), substrate=object()))
    assert "max_tokens" not in wire2.bodies[0]


def _anthropic(model: str = "claude-opus-4-8") -> AnthropicProviderHandler:
    h = AnthropicProviderHandler()
    h._cfg = _cfg(model, 16_384)  # noqa: SLF001
    return h


def test_the_models_maximum_is_declared_in_one_place() -> None:
    from legba.shared import model_output_limits as mol

    assert AnthropicProviderHandler.MAX_OUTPUT_TOKENS_BY_PREFIX is mol.ANTHROPIC_MAX_OUTPUT_TOKENS_BY_PREFIX
    assert _anthropic("claude-sonnet-4-5").model_max_output_tokens() == mol.anthropic_max_output_tokens("claude-sonnet-4-5") == 64_000
    assert _vllm(_Wire([]), declared=65_536).model_max_output_tokens() == 65_536


def test_the_partial_label_reads_the_models_own_limit() -> None:
    h = _anthropic()
    limit = cp.output_limit(h, 32_768)
    assert limit == 128_000  # the descriptor's 32768 is no longer the limit
    assert cp.output_limit(object(), 32_768) == 32_768  # a double that declares none

    class _R:
        def __init__(self, finish: str | None) -> None:
            self.finish_reason = finish

    reply = {"final": True, "answer": "prose", "cited_refs": []}
    # 32,768 tokens with no finish reason: NOT cut any more (the wire allowed 128k)
    _, status, _ = cp.settle_final_reply(
        reply, _R(None), {"completion_tokens": 32_768}, max_tokens=limit,
        rounds_used=1, rounds_available=4, elapsed_s=1.0, last_tool_incorporated=True,
    )
    assert status == "complete"
    # the provider saying "length" is still a partial, at any count
    payload, status, step = cp.settle_final_reply(
        reply, _R("length"), {"completion_tokens": 900}, max_tokens=limit,
        rounds_used=1, rounds_available=4, elapsed_s=1.0, last_tool_incorporated=True,
    )
    assert status == "partial" and step["max_tokens"] == 128_000
    # and the count rule at the real limit
    _, status, _ = cp.settle_final_reply(
        reply, _R(None), {"completion_tokens": 128_000}, max_tokens=limit,
        rounds_used=1, rounds_available=4, elapsed_s=1.0, last_tool_incorporated=True,
    )
    assert status == "partial"


class _Pool:
    """A fake asyncpg pool answering the observed-output read."""

    def __init__(self, row: dict[str, Any] | None = None, *, fail: BaseException | None = None) -> None:
        self.row, self.fail, self.queries = row, fail, []

    def acquire(self) -> "_Pool":
        if self.fail is not None:
            raise self.fail
        return self

    async def __aenter__(self) -> "_Pool":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.queries.append((sql, args))
        return self.row


def _priced(route: str = "llm.anthropic.fable_5_1") -> AnthropicProviderHandler:
    h = _anthropic("claude-opus-4-8")
    h._instance_id = route  # noqa: SLF001
    return h


@pytest.mark.asyncio
async def test_the_spend_projection_is_the_routes_largest_observed_output(monkeypatch: pytest.MonkeyPatch) -> None:
    # The projection is an ESTIMATE from the route's history, not the
    # model's 128,000 maximum (which stopped a priced turn after about one call).
    monkeypatch.setenv(TURN_COST_CEILING_ENV, "5")
    pool = _Pool({"largest": 9_562, "calls": 40})
    guard = await arm_turn_ceiling_observed(SpendGuard(), _priced(), pool=pool)
    assert isinstance(guard, TurnCeilingGuard)
    assert guard.max_output_tokens == 9_562
    sql, args = pool.queries[0]
    assert args == ("llm.anthropic.fable_5_1", PROJECTION_HISTORY_DAYS)
    assert "call_receipts" in sql and "analyst_traces" in sql
    snap = guard.snapshot()
    assert snap["projected_output_tokens"] == 9_562
    assert snap["projection_basis"].startswith("estimate: each call's output taken as 9,562 tokens")
    assert "largest output observed on llm.anthropic.fable_5_1" in snap["projection_basis"]
    # the persisted receipt keeps it, labelled
    receipt = consult_turn_stamps.usage_receipt(guard.receipt(_priced(), elapsed_s=1.0), plane="opus", route="r")
    assert receipt["turn_projection"]["output_tokens"] == 9_562
    assert receipt["turn_projection"]["basis"].startswith("estimate:")
    # A $5 turn after one ~20k-token call has room to drill on; at the model's
    # maximum (the lane's figure) the same turn would already stop.
    guard.last_prompt_tokens, guard.cost_usd = 20_000, 0.5
    assert guard.exhausted_reason() is None
    at_max = arm_turn_ceiling(SpendGuard(), _priced(), max_output_tokens=128_000)
    at_max.last_prompt_tokens, at_max.cost_usd = 20_000, 0.5
    reason = at_max.exhausted_reason()
    assert reason is not None and "estimate: each call's output taken as 128,000 tokens" in reason
    # nothing smaller reaches the wire because of it: the model's maximum is unchanged
    assert _priced().model_max_output_tokens() == 128_000


@pytest.mark.asyncio
async def test_no_history_projects_the_old_figure_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TURN_COST_CEILING_ENV, "5")
    for pool, why in (
        (_Pool({"largest": None, "calls": 0}), "no output observed on llm.anthropic.fable_5_1"),
        (None, "no database handle"),
        (_Pool(fail=RuntimeError("down")), "history read failed (RuntimeError)"),
    ):
        guard = await arm_turn_ceiling_observed(SpendGuard(), _priced(), pool=pool)
        assert guard.max_output_tokens == PROJECTION_FALLBACK_OUTPUT_TOKENS == 32_768
        assert why in guard.projection_basis and "fallback 32,768 tokens" in guard.projection_basis


@pytest.mark.asyncio
async def test_an_unpriced_route_or_no_ceiling_never_reads_history(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _Pool(fail=AssertionError("must not query"))
    monkeypatch.delenv(TURN_COST_CEILING_ENV, raising=False)
    g = SpendGuard()
    assert await arm_turn_ceiling_observed(g, _priced(), pool=pool) is g
    monkeypatch.setenv(TURN_COST_CEILING_ENV, "5")
    core = _vllm(_Wire([]), declared=65_536)  # the self-hosted core plane: no price table
    assert await arm_turn_ceiling_observed(g, core, pool=pool) is g


@pytest.mark.asyncio
async def test_a_workflow_input_from_before_still_deserializes_and_replays() -> None:
    # The exact dict a pre-change submit stamped into the workflow history
    # (every field, max_analyze_tokens included) builds the input unchanged;
    # an acquire output from before (no ``stage_usage``) replays through the
    # new analyze stage, which records only what it can see.
    old = asdict(DeepConsultWorkflowInput(question="q", run_id=str(uuid4()), max_analyze_tokens=32_768))
    old.pop("thinking")  # an even older input: a field added later is absent
    wf = DeepConsultWorkflowInput(**old)
    assert wf.max_analyze_tokens == 32_768
    acquired = {"ok": True, "wf_input": old, "evidence": [], "cited_substrate_refs": [],
                "acquire_seconds": 0.1, "plan_diagnostics": {}}
    wire = _Wire([json.dumps({"final": True, "answer": "fine", "uncertainty": 0.4, "cited_refs": []})])
    out = await _run_analyze(acquired, DeepConsultStageDeps(llm=_vllm(wire, declared=65_536), substrate=object()))
    assert out["ok"] is True and out["answer"]
    assert set(out["stage_usage"]) == {"analyze"}
    assert out["usage"]["calls"] == len(wire.bodies) >= 1
