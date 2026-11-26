# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Consult thinks on the core plane.

The consult descriptors declare ``method.llm.thinking: "on"``; which calls
think is ``consult_thinking``'s table (rounds, forced final, recovery, deep
plan: on; repair, deep extraction: off). Pinned here on the REAL core handler
over ``httpx.MockTransport`` with the documented thinking stream (reasoning
deltas, then content, usage with a reasoning breakout) — no model call:

* a turn's rounds and its forced final name the thinking alias; the answer and
  the live answer frames carry no reasoning; reasoning tokens are counted;
* every consult call is INTERACTIVE on the core-plane gate;
* the descriptor default is off for hand-built deps; a request row can turn a
  turn off; a plane with no alias runs as before and the trace says why; a
  bake-off ARM never thinks and its descriptors declare nothing;
* the real descriptors resolve through the runtime deps builder;
* deep consult's plan thinks and its candidate extraction does not.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import httpx
import pytest
import yaml

from legba.data.registry.credentials import MissingSecretError  # noqa: F401 — cycle warm-up

from legba.data.analysts import consult_thinking as T
from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps, run_method
from legba.data.schemas import LLMProviderConfig, Property
from legba.data.schemas.analyst import AnalystDescriptor
from legba.data.stack.llm import VLLMProviderHandler
from legba.data.stack.llm import call_gate as cg

REPO = pathlib.Path(__file__).resolve().parents[2]
BASE_URL = "https://gw.example.test"
CORE_ID = "llm.primary.openai_compat"
REASONING = "Plan: read the ladder signals first, then weigh them."
USAGE = {"prompt_tokens": 900, "completion_tokens": 60, "total_tokens": 960,
         "completion_tokens_details": {"reasoning_tokens": 41}}
ANSWER = "## Assessment\n\nThe escalation ladder is intact; escalation is unlikely."


@pytest.fixture(autouse=True)
def _fresh_gates(monkeypatch: pytest.MonkeyPatch):
    for env in (cg.COMPONENT_ENV, cg.LIMIT_ENV, cg.RESERVED_ENV):
        monkeypatch.delenv(env, raising=False)
    cg.clear_gates()
    yield
    cg.clear_gates()


def _handler(*, alias: str | None = "deep") -> VLLMProviderHandler:
    extra = {"thinking_model_name": Property.Text.of(alias)} if alias else {}
    h = VLLMProviderHandler()
    h._cfg = LLMProviderConfig(  # noqa: SLF001
        api_endpoint=Property.Text.of(f"{BASE_URL}/v1"),
        api_key=Property.Secret.of("llm.primary.key"),
        model_name=Property.Text.of("fast"),
        max_tokens=Property.Number.of(32768, minimum=1, maximum=200000),
        **extra,
    )
    h._api_key = "k"  # noqa: SLF001
    h._instance_id = CORE_ID  # noqa: SLF001
    return h


class _Wire:
    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return self.replies.pop(0)()


def _install(h: VLLMProviderHandler, wire: _Wire) -> None:
    h._client = httpx.AsyncClient(  # noqa: SLF001
        base_url=BASE_URL, headers=h._auth_headers(),  # noqa: SLF001
        transport=httpx.MockTransport(wire), timeout=httpx.Timeout(10.0),
    )


def _chunk(delta: dict[str, Any] | None = None, *, finish: str | None = None,
           usage: dict[str, Any] | None = None, choices: bool = True) -> dict[str, Any]:
    c: dict[str, Any] = {
        "id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "deep",
        "choices": ([{"index": 0, "delta": delta or {}, "finish_reason": finish}]
                    if choices else []),
    }
    if usage is not None:
        c["usage"] = usage
    return c


def _sse(chunks: list[dict[str, Any]]) -> Any:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return lambda: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=body.encode())


def _reasoning() -> list[dict[str, Any]]:
    return [_chunk({"reasoning_content": REASONING[i:i + 10]})
            for i in range(0, len(REASONING), 10)]


def _tool_round() -> Any:
    return _sse([
        _chunk({"role": "assistant"}),
        *_reasoning(),
        _chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                "function": {"name": "search_signals",
                                             "arguments": '{"query": "ladder"}'}}]}),
        _chunk({}, finish="tool_calls"),
        _chunk(usage=USAGE, choices=False),
    ])


def _answer() -> Any:
    return _sse([
        _chunk({"role": "assistant", "content": ""}),
        *_reasoning(),
        *[_chunk({"content": ANSWER[i:i + 16]}) for i in range(0, len(ANSWER), 16)],
        _chunk({}, finish="stop"),
        _chunk(usage=USAGE, choices=False),
    ])


class _Substrate:
    async def search_signals(self, **kwargs: Any) -> dict[str, Any]:
        return {"count": 1, "rows": [{"id": "sig-1", "title": "t"}], "refs": []}


async def _consult(llm: Any, *, first_extra: dict[str, Any] | None = None,
                   **dep_kwargs: Any) -> tuple[Any, list[dict[str, Any]]]:
    steps: list[dict[str, Any]] = []

    async def _publish(step: dict[str, Any]) -> None:
        steps.append(step)

    deps = ConsultOnDemandDeps(llm=llm, substrate=_Substrate(), step_publish=_publish,
                               **dep_kwargs)
    result = await run_method(
        [{"question": "is the ladder intact?", "max_tool_rounds": 1, **(first_extra or {})}],
        {}, deps,
    )
    return result, steps


def _thinking_step(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((s for s in steps if s.get("kind") == "thinking"), None)


# ---------------------------------------------------------------------------
# A Consult turn on the core plane
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_consult_turn_thinks_on_its_rounds_and_its_final() -> None:
    h = _handler()
    wire = _Wire(_tool_round(), _answer())
    _install(h, wire)
    result, steps = await _consult(h, thinking=True)

    assert [b["model"] for b in wire.bodies] == ["deep", "deep"]
    for body in wire.bodies:
        assert body["temperature"] == 1.0
        assert "max_tokens" not in body and "thinking" not in body
    assert "tools" in wire.bodies[0] and "tools" not in wire.bodies[1]

    step = _thinking_step(steps)
    assert step == {"phase": "plan", "kind": "thinking", "requested": True, "active": True,
                    "reason": T.REASON_DECLARED, "roles": sorted(T.THINKING_ROLES)}
    answer = result.consult_response.answer
    assert "escalation ladder is intact" in answer and REASONING not in answer
    # The live frames a person watches carry answer text only.
    live = "".join(str(s.get("text") or s.get("delta") or "") for s in steps
                   if s.get("kind") == "answer_delta")
    assert "ladder is intact" in live and REASONING[:10] not in live
    # Reasoning is counted, once, as reasoning: 41 per call.
    assert result.usage["reasoning_tokens"] == 82
    assert result.usage["completion_tokens"] == 120
    # Both calls were interactive on the core-plane gate.
    (snap,) = cg.gate_snapshots()
    assert snap["granted_by_priority"] == {"interactive": 2, "scheduled": 0}


@pytest.mark.asyncio
async def test_hand_built_deps_do_not_think_and_record_nothing() -> None:
    h = _handler()
    wire = _Wire(_tool_round(), _answer())
    _install(h, wire)
    _result, steps = await _consult(h)
    assert [b["model"] for b in wire.bodies] == ["fast", "fast"]
    assert _thinking_step(steps) is None


@pytest.mark.asyncio
async def test_a_request_row_turns_one_turn_off() -> None:
    h = _handler()
    wire = _Wire(_tool_round(), _answer())
    _install(h, wire)
    _result, steps = await _consult(h, thinking=True, first_extra={"thinking": "off"})
    assert [b["model"] for b in wire.bodies] == ["fast", "fast"]
    assert _thinking_step(steps) is None


@pytest.mark.asyncio
async def test_a_plane_with_no_thinking_alias_runs_as_before_and_says_why() -> None:
    h = _handler(alias=None)
    wire = _Wire(_tool_round(), _answer())
    _install(h, wire)
    _result, steps = await _consult(h, thinking=True)
    assert [b["model"] for b in wire.bodies] == ["fast", "fast"]
    step = _thinking_step(steps)
    assert step["requested"] is True and step["active"] is False
    assert step["reason"] == T.REASON_NO_ALIAS and step["roles"] == []


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


class _Plane:
    def thinking_available(self) -> bool:
        return True


@pytest.mark.parametrize(("role", "thinks"), [
    (T.ROLE_ROUND, True), (T.ROLE_FINAL, True), (T.ROLE_RECOVERY, True),
    (T.ROLE_DEEP_PLAN, True), (T.ROLE_REPAIR, False), (T.ROLE_DEEP_EXTRACT, False),
])
def test_which_roles_think(role: str, thinks: bool) -> None:
    on = T.ConsultThinking.decide(True, first={}, arm=False, llm=_Plane())
    assert on.for_role(role) is thinks
    off = T.ConsultThinking.decide(False, first={}, arm=False, llm=_Plane())
    assert off.for_role(role) is False


def test_a_bakeoff_arm_never_thinks_whatever_the_row_says() -> None:
    d = T.ConsultThinking.decide(True, first={"thinking": "on"}, arm=True, llm=_Plane())
    assert (d.requested, d.active, d.reason) == (False, False, T.REASON_ARM)


def test_a_request_row_can_also_turn_a_turn_on() -> None:
    d = T.ConsultThinking.decide(False, first={"thinking": "on"}, arm=False, llm=_Plane())
    assert d.active and d.reason == T.REASON_ROW


# ---------------------------------------------------------------------------
# The real descriptors, through the runtime deps builder
# ---------------------------------------------------------------------------


def _descriptor(name: str) -> AnalystDescriptor:
    body = yaml.safe_load((REPO / "descriptors" / f"analyst_{name}.yaml").read_text())
    return AnalystDescriptor.model_validate(body)


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "thinks"), [
    ("consult_default", True),
    ("consult_bakeoff_raw", False),
    ("consult_bakeoff_verified", False),
])
async def test_the_consult_descriptors_resolve_their_thinking(name: str, thinks: bool) -> None:
    from legba.data.analysts import discover_analyst_kinds
    from legba.runtime.analyst_deps_builder import _build_consult_on_demand

    handler = discover_analyst_kinds()["consult_on_demand"]

    async def _resolve() -> Any:
        return _handler()

    _run, deps, _kind = await _build_consult_on_demand(
        _descriptor(name), handler, _resolve, substrate_query_port=_Substrate(),
    )
    assert deps.thinking is thinks
    # The consult kind applies thinking per role; its primary is never wrapped.
    assert isinstance(deps.llm, VLLMProviderHandler)


def test_the_deep_consult_descriptor_resolves_its_thinking() -> None:
    from legba.data.analysts import discover_analyst_kinds
    from legba.runtime.analyst_deps_builder import _build_deep_consult

    handler = discover_analyst_kinds()["deep_consult"]
    _run, deps, _kind = _build_deep_consult(
        _descriptor("deep_consult"), handler, deep_consult_client=object(),
    )
    assert deps.thinking is True


def test_the_frozen_bakeoff_descriptors_declare_no_thinking() -> None:
    for name in ("consult_bakeoff_raw", "consult_bakeoff_verified"):
        assert "thinking" not in (_descriptor(name).method.llm or {})


def test_a_nested_or_misspelt_thinking_choice_is_refused_at_registration() -> None:
    body = yaml.safe_load((REPO / "descriptors" / "analyst_consult_default.yaml").read_text())
    body["method"]["llm"]["thinking"] = "sometimes"
    with pytest.raises(ValueError, match="not a thinking choice"):
        AnalystDescriptor.model_validate(body)
    body["method"]["llm"]["thinking"] = "on"
    body["method"]["llm"]["verify"] = {"thinking": "on"}
    with pytest.raises(ValueError, match="not a thinking block"):
        AnalystDescriptor.model_validate(body)
    # Narrate IS a thinking block — but consult has no narrate calls.
    del body["method"]["llm"]["verify"]
    body["method"]["llm"]["narrate"] = {"thinking": "on"}
    with pytest.raises(ValueError, match="read only by the kinds"):
        AnalystDescriptor.model_validate(body)


# ---------------------------------------------------------------------------
# Deep consult: the plan thinks, the extraction does not
# ---------------------------------------------------------------------------


class _Scripted:
    subprovider = "vllm"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def thinking_available(self) -> bool:
        return True

    async def chat_complete(self, messages: Any, **kwargs: Any) -> Any:
        self.calls.append({**kwargs, "priority": cg.current_priority()})

        class _U:
            prompt_tokens, completion_tokens, reasoning_tokens = 10, 5, 3

        class _R:
            content = self.reply
            usage = _U()
            tool_calls: list[Any] = []
            raw_response: dict[str, Any] = {}

        return _R()


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [True, False])
async def test_deep_consult_plan_thinks_when_declared(declared: bool) -> None:
    from legba.runtime.dapr_workflow.deep_consult import (
        DeepConsultStageDeps,
        DeepConsultWorkflowInput,
        _run_plan,
    )

    llm = _Scripted('{"sub_queries": ["a"], "tool_plan": []}')
    out = await _run_plan(
        DeepConsultWorkflowInput(question="q", thinking=declared),
        DeepConsultStageDeps(llm=llm, substrate=None),
    )
    assert out["ok"] is True
    (call,) = llm.calls
    assert call.get("thinking", False) is declared
    assert call["priority"] == cg.INTERACTIVE


@pytest.mark.asyncio
async def test_deep_consult_candidate_extraction_never_thinks() -> None:
    from legba.runtime.dapr_workflow.deep_consult import _extract_candidates_via_llm

    llm = _Scripted('{"candidate_facts": [], "candidate_hypotheses": []}')
    await _extract_candidates_via_llm(
        llm, answer="a", evidence_brief="e", max_tokens=100,
        want_facts=True, want_hypotheses=True,
    )
    (call,) = llm.calls
    assert "thinking" not in call


@pytest.mark.asyncio
@pytest.mark.parametrize(("declared", "row", "expected"), [
    (True, None, True), (True, "off", False), (False, "on", True), (False, None, False),
])
async def test_deep_consult_submit_stamps_the_thinking_choice(
    declared: bool, row: str | None, expected: bool,
) -> None:
    from legba.data.analysts.deep_consult import DeepConsultKindDeps
    from legba.data.analysts.deep_consult import run_method as submit

    class _Client:
        wf_input: Any = None

        async def start_deep_consult_workflow(self, wf_input: Any, *, workflow_id: str) -> str:
            _Client.wf_input = wf_input
            return "task-1"

    first: dict[str, Any] = {"question": "q"}
    if row is not None:
        first["thinking"] = row
    await submit([first], {"run_id": "r"},
                 DeepConsultKindDeps(workflow_client=_Client(), thinking=declared))
    assert _Client.wf_input.thinking is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("override", "expected"), [
    # The front door threads core for a request naming no model
    ("llm.primary.openai_compat", "llm.primary.openai_compat"),
    # a paid plane only when the request picked it
    ("llm.anthropic.opus_4_7", "llm.anthropic.opus_4_7"),
])
async def test_deep_consult_runs_on_the_threaded_plane_not_a_paid_primary(
    override: str, expected: str,
) -> None:
    """Deep consult's default is the core component even when the descriptor
    head's primary is a paid plane; plan + analyze think (declared on)."""
    from legba.data.analysts.deep_consult import DeepConsultKindDeps
    from legba.data.analysts.deep_consult import run_method as submit

    class _Client:
        wf_input: Any = None

        async def start_deep_consult_workflow(self, wf_input: Any, *, workflow_id: str) -> str:
            _Client.wf_input = wf_input
            return "task-1"

    deps = DeepConsultKindDeps(workflow_client=_Client(), thinking=True,
                               llm_component_id="llm.anthropic.opus_4_7")
    await submit([{"question": "q", "llm_component_override": override}], {"run_id": "r"}, deps)
    assert _Client.wf_input.llm_component_id == expected
    assert _Client.wf_input.thinking is True
