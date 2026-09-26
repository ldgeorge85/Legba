# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult loop on the native tool-call route, and its wall-clock budget.

Two D-7 changes to ``consult_on_demand.run_method``, both traceable to run
``3ae77c64`` (2026-09-16), and both tested here through the real loop rather
than against the helpers in isolation.

The round protocol
==================

That run burned a round on an ``unparseable`` reply — prose the text protocol
could parse as neither a tool call nor a final — and each burned round on a
pinned-context Opus consult costs 40 to 100 seconds of a wall clock that later
ran out. On a plane with a structured tool channel that class of failure need
not exist: tool calls arrive in their own field, so a reply either has them (a
tool round) or does not (the answer). :func:`test_a_prose_plus_json_reply_is_
an_answer_not_an_unparseable_round` is the direct proof — the exact reply shape
that burned the round now ANSWERS.

Native is the default on both planes we run (Anthropic; the core plane, whose
vLLM is served with ``--enable-auto-tool-choice``). A handler we don't
recognise keeps the text protocol, which is why every pre-existing test in
``test_analyst_consult_on_demand`` still exercises that route: its double
reports ``subprovider = "vllm-test"``.

The budget
==========

The loop had a drilling budget that stopped it asking for MORE tools, and
nothing at all bounding the synthesis that followed. So the wall-clock check
passed at round 6, the final synthesis began at 269s, and the front door's
300s clock ran out mid-generation — the loop never got to choose a shorter
answer, because nothing asked it to. Now there is a total budget, a per-call
deadline, and a degraded-but-honest FINAL when either is hit.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from legba.data.analysts.consult_on_demand import (
    ConsultOnDemandDeps,
    run_method,
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    prompt_tokens: int = 100
    completion_tokens: int = 50
    reasoning_tokens: int = 0


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


class _NativeLLM:
    """A handler double that speaks the NATIVE channel.

    ``subprovider`` decides the wire shape the loop builds, exactly as the real
    handlers' class attribute does. ``turns`` is the scripted sequence of
    replies; every call's kwargs are recorded so a test can assert what went
    over the wire.
    """

    def __init__(self, subprovider: str, turns: list[_Response]) -> None:
        self.subprovider = subprovider
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages, **kwargs: Any) -> _Response:
        self.calls.append({"messages": list(messages), **kwargs})
        if self._turns:
            return self._turns.pop(0)
        return _Response(content="<<<FINAL>>>\nuncertainty: 0.4\n\nscript exhausted")


class _SlowLLM(_NativeLLM):
    """Like :class:`_NativeLLM` but each turn takes ``delay`` seconds."""

    def __init__(self, subprovider: str, turns: list[_Response], delay: float) -> None:
        super().__init__(subprovider, turns)
        self._delay = delay

    async def chat_complete(self, messages, **kwargs: Any) -> _Response:
        await asyncio.sleep(self._delay)
        return await super().chat_complete(messages, **kwargs)


class _Substrate:
    """Minimal substrate port: ``search_signals`` works, ``query_facts`` fails."""

    def __init__(self) -> None:
        self.searched: list[dict[str, Any]] = []

    async def search_signals(self, **kwargs: Any) -> dict[str, Any]:
        self.searched.append(kwargs)
        return {"count": 1, "rows": [{"id": "s1", "title": "a signal"}], "refs": []}

    async def query_facts(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("the substrate said no")


def _tool_turn(*calls: dict[str, Any], text: str = "") -> _Response:
    """A reply carrying one native call per entry.

    Each tool is offered under its OWN name on the native route (the shared
    catalogue in ``stack/llm/tool_rounds``), so a call names the substrate tool
    directly rather than wrapping it.
    """
    return _Response(
        content=text,
        finish_reason="tool_calls",
        tool_calls=[
            _ToolCall(id=f"call-{i}", name=c["tool"], arguments=dict(c.get("args") or {}))
            for i, c in enumerate(calls)
        ],
    )


def _final_turn(text: str) -> _Response:
    """A reply with NO tool calls — on the native route, that IS the answer."""
    return _Response(content=text, finish_reason="stop")


SEARCH = {"tool": "search_signals", "args": {"query": "iran"}}


async def _run(llm: Any, **dep_kwargs: Any) -> Any:
    steps: list[dict[str, Any]] = []

    async def _publish(step: dict[str, Any]) -> None:
        steps.append(step)

    deps = ConsultOnDemandDeps(
        llm=llm, substrate=_Substrate(), step_publish=_publish, **dep_kwargs,
    )
    result = await run_method([{"question": "what is happening?"}], {}, deps)
    return result, steps


def _kinds(steps: list[dict[str, Any]]) -> list[str]:
    return [s.get("kind") for s in steps]


# ---------------------------------------------------------------------------
# The native round protocol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_route_round_trips_tool_use_and_tool_result_blocks() -> None:
    """One native tool round, then a text answer, on the Anthropic wire."""
    llm = _NativeLLM(
        "anthropic", [_tool_turn(SEARCH, text="checking"), _final_turn("the answer")],
    )
    result, steps = await _run(llm)

    # The tool roster went over the wire on the tool round...
    assert llm.calls[0]["tools"], "no tools were offered — the route did not engage"
    # ...and the transcript we sent back carried tool_use + tool_result blocks
    # with MATCHING ids, which is what Anthropic requires.
    second_request = llm.calls[1]["messages"]
    assistant = second_request[-2]
    tool_reply = second_request[-1]
    assert assistant["role"] == "assistant"
    use_block = [b for b in assistant["content"] if b["type"] == "tool_use"][0]
    assert use_block["id"] == "call-0"
    assert tool_reply["role"] == "user"
    assert tool_reply["content"][0]["tool_use_id"] == "call-0"

    assert result.consult_response.answer == "the answer"
    assert "unparseable" not in _kinds(steps)


@pytest.mark.asyncio
async def test_core_plane_route_uses_the_openai_tool_message_shape() -> None:
    """The same protocol, the other wire: ``role='tool'`` + ``tool_call_id``."""
    llm = _NativeLLM(
        "vllm", [_tool_turn(SEARCH), _final_turn("core plane answer")],
    )
    result, _steps = await _run(llm)

    second_request = llm.calls[1]["messages"]
    assistant = second_request[-2]
    tool_reply = second_request[-1]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["id"] == "call-0"
    # OpenAI wants the arguments as a JSON string.
    assert isinstance(assistant["tool_calls"][0]["function"]["arguments"], str)
    assert tool_reply["role"] == "tool"
    assert tool_reply["tool_call_id"] == "call-0"
    assert result.consult_response.answer == "core plane answer"


@pytest.mark.asyncio
async def test_a_batch_of_calls_in_one_turn_is_ONE_round() -> None:
    """N calls, N results, one round — the latency lever, kept honest.

    Each issued call must get exactly one result back or the provider rejects
    the next turn, so the count is asserted rather than assumed.
    """
    llm = _NativeLLM(
        "anthropic",
        [
            _tool_turn(
                {"tool": "search_signals", "args": {"query": "a"}},
                {"tool": "search_signals", "args": {"query": "b"}},
            ),
            _final_turn("batched"),
        ],
    )
    result, steps = await _run(llm)

    tool_reply = llm.calls[1]["messages"][-1]
    assert [b["tool_use_id"] for b in tool_reply["content"]] == ["call-0", "call-1"]
    # Both calls executed, and they cost ONE round.
    assert _kinds(steps).count("tool_call") == 2
    assert {s["round"] for s in steps if s.get("kind") == "tool_call"} == {1}
    assert result.consult_response.answer == "batched"


@pytest.mark.asyncio
async def test_a_failing_tool_comes_back_as_an_error_result_and_the_loop_continues() -> None:
    """A tool error is information for the planner, not a crash.

    The batch's result is marked ``is_error`` so the model reacts to it, and
    the run still produces an answer.
    """
    llm = _NativeLLM(
        "anthropic",
        [
            _tool_turn({"tool": "query_facts", "args": {}}),
            _final_turn("recovered from the tool error"),
        ],
    )
    result, steps = await _run(llm)

    tool_reply = llm.calls[1]["messages"][-1]
    # The failure rides IN the result body — the shared builder does not carry a
    # separate error flag, and the model reads the body either way.
    assert "the substrate said no" in tool_reply["content"][0]["content"]
    assert '"error"' in tool_reply["content"][0]["content"]
    failed = [s for s in steps if s.get("kind") == "tool_call"][0]
    assert failed["ok"] is False
    assert result.consult_response.answer == "recovered from the tool error"


@pytest.mark.asyncio
async def test_a_prose_plus_json_reply_is_an_answer_not_an_unparseable_round() -> None:
    """The failure class this route exists to delete.

    On the text protocol, prose wrapped around a JSON-ish fragment matched
    neither reply shape: ``unparseable``, a re-prompt, and a burned round. With
    tool calls in their own field, a reply with none of them is the answer —
    there is no branch left that can classify it as anything else.
    """
    burned_the_round = (
        "Looking at this, I think the right move is to check signals first.\n"
        '{"tool": "search_signals", "args": {"query": "iran"}}\n'
        "But actually the evidence above already answers it."
    )
    llm = _NativeLLM("anthropic", [_final_turn(burned_the_round)])
    result, steps = await _run(llm)

    assert "unparseable" not in _kinds(steps)
    assert result.consult_response.answer.startswith("Looking at this")
    assert len(llm.calls) == 1, "the round was re-prompted — it should have ANSWERED"


@pytest.mark.asyncio
async def test_a_handler_that_ignored_tools_is_caught_by_the_fallback() -> None:
    """The paranoid guard: bare JSON on the native route is a TOOL ROUND.

    If a provider accepted ``tools`` and ignored them, a model still emitting
    the old protocol would have its JSON persisted AS the answer — worse than
    the unparseable round we removed. So a text reply that is nothing but a
    tool-request object is executed as one.
    """
    llm = _NativeLLM(
        "anthropic",
        [
            _final_turn('{"tool": "search_signals", "args": {"query": "iran"}}'),
            _final_turn("answered after the fallback ran the tool"),
        ],
    )
    result, steps = await _run(llm)

    assert "native_text_tool_fallback" in _kinds(steps)
    assert _kinds(steps).count("tool_call") == 1
    assert result.consult_response.answer == "answered after the fallback ran the tool"


@pytest.mark.asyncio
async def test_an_unrecognised_handler_sends_NO_tools_key_at_all() -> None:
    """Byte-identity for every caller that is not on the native route.

    ``tools`` is threaded only when non-empty, so a text-protocol run produces
    the request it produced before the parameter existed. This is the property
    the frozen analysts' shared GATHER loop depends on: it calls the same
    handlers, and nothing about its requests may move.
    """
    llm = _NativeLLM("some-unknown-plane", [_final_turn("<<<FINAL>>>\n\nplain")])
    await _run(llm)

    assert "tools" not in llm.calls[0], (
        "a tools key reached a handler on the text-protocol route — that is a "
        "wire-shape change for every caller that never asked for one"
    )
    # And the system prompt carries no native-protocol override either.
    assert "Round protocol override" not in (llm.calls[0]["system"] or "")


# ---------------------------------------------------------------------------
# The budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stuck_call_hits_the_round_deadline_and_the_run_still_answers() -> None:
    """One slow call must not consume the run.

    Before D-7 an LLM call had no deadline at all: a round that hung burned the
    whole budget and the caller's clock decided the outcome.
    """
    # An EMPTY script, so every call falls through to the double's text final.
    # The scripted-tool-turn version of this test could not tell the new
    # behaviour from a bug: the forced-final call would pop a TOOL turn, whose
    # text is empty, and the run would answer nothing for a reason that has
    # nothing to do with the budget.
    llm = _SlowLLM("anthropic", [], delay=1.4)
    # The deadline floors at 1s (a nearly-spent budget must not hand a round a
    # 10ms slice), so 1.4s of model time is what overruns it.
    result, steps = await _run(
        llm, round_deadline_seconds=0.05, total_budget_seconds=30.0,
    )

    # The DRILLING round is still cut by its deadline — that guard is unchanged
    # and is what stops one stuck call eating the run.
    assert "round_deadline_exceeded" in _kinds(steps)
    # But the run now SYNTHESISES rather than degrading. Since the c8a0105c
    # train the final turn gets the remaining budget under a floor, so a
    # drilling round losing its slice no longer costs the answer as well:
    # ``round_deadline_seconds`` bounds drilling rounds, never the synthesis.
    assert "degraded_final" not in _kinds(steps)
    assert "forced_final" in _kinds(steps)
    answer = result.consult_response.answer
    assert "ran out of its time budget" not in answer
    assert answer.strip(), "a deadline on ONE round must still produce an answer"


@pytest.mark.asyncio
async def test_budget_exhaustion_produces_an_HONEST_final_not_a_death(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The degraded FINAL says what it got and what it never folded in.

    This is the shape of answer run 3ae77c64 should have produced instead of
    vanishing: a real turn, persisted, that an operator can read and act on.

    Reaching it now takes a pinned floor. Since the c8a0105c train the
    synthesis gets ``max(remaining, LEGBA_CONSULT_FINAL_FLOOR_SECONDS)`` — 240s
    by default — so a spent TOTAL budget no longer denies the run its answer,
    which is the entire point of that change. Pinning the floor to 0.01s is how
    a test still reaches the branch that handles "there is genuinely no room to
    synthesise", and that branch must stay honest.
    """
    monkeypatch.setenv("LEGBA_CONSULT_FINAL_FLOOR_SECONDS", "0.01")
    llm = _SlowLLM(
        "anthropic",
        [_tool_turn(SEARCH), _tool_turn(SEARCH), _tool_turn(SEARCH)],
        delay=0.2,
    )
    result, steps = await _run(
        llm,
        total_budget_seconds=0.45,
        wall_budget_seconds=30.0,  # the DRILLING budget is untouched...
        round_deadline_seconds=10.0,
    )

    # ...and the TOTAL budget is what stopped it, which is the new guard.
    assert "degraded_final" in _kinds(steps)
    answer = result.consult_response.answer
    assert "ran out of its time budget" in answer
    assert "of 6 available rounds" in answer
    assert "The last tool result was not incorporated." in answer, (
        "the degraded final must say when it never got to read its last tool "
        "result — a thin answer presented as a complete one is the dishonest "
        "failure mode"
    )
    assert result.consult_response.uncertainty == 0.85


@pytest.mark.asyncio
async def test_the_degraded_final_still_carries_the_tool_trace() -> None:
    """A budget-stopped run is not an empty turn.

    Whatever it gathered is real evidence and rides on the payload, so the
    panel (and the persisted turn) show the work rather than a bare apology.
    """
    llm = _SlowLLM("anthropic", [_tool_turn(SEARCH), _tool_turn(SEARCH)], delay=0.2)
    result, _steps = await _run(
        llm, total_budget_seconds=0.45, round_deadline_seconds=10.0,
    )

    trace = result.consult_response.data.get("tool_calls") or []
    assert trace, "the degraded final dropped the evidence the run did gather"
    assert trace[0]["tool"] == "search_signals"


@pytest.mark.asyncio
async def test_env_budget_overrides_are_read_and_a_malformed_one_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``LEGBA_CONSULT_BUDGET_SECONDS`` tunes it; garbage must not zero it.

    A zeroed budget would degrade every consult instantly, which is a worse
    outage than the one being fixed — so a malformed pin falls back rather than
    being honoured.
    """
    from legba.data.analysts import consult_round_protocol as cp

    monkeypatch.setenv("LEGBA_CONSULT_BUDGET_SECONDS", "900")
    assert cp.default_total_budget_seconds() == 900.0

    for bad in ("0", "-5", "soon", ""):
        monkeypatch.setenv("LEGBA_CONSULT_BUDGET_SECONDS", bad)
        assert cp.default_total_budget_seconds() == cp.DEFAULT_TOTAL_BUDGET_SECONDS


@pytest.mark.asyncio
async def test_the_step_trace_names_which_protocol_the_run_used() -> None:
    """An operator reading a trace must be able to tell which route ran.

    Two protocols with different failure modes are otherwise indistinguishable
    after the fact, which makes every future incident harder to read.
    """
    native, native_steps = await _run(
        _NativeLLM("anthropic", [_final_turn("a")]),
    )
    text, text_steps = await _run(
        _NativeLLM("unknown", [_final_turn("<<<FINAL>>>\n\nb")]),
    )
    assert native_steps[0]["round_protocol"] == "native_tools"
    assert text_steps[0]["round_protocol"] == "json_text"
    assert native.consult_response.answer and text.consult_response.answer
