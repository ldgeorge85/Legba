# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W11-F132 — run 9b902fec: two paid generations, nothing delivered.

2026-10-29 18:31Z, consult session 963b7b08, route ``llm.anthropic.fable_5_1``.
Twelve drilling rounds took two minutes; in round 13 the model began ANSWERING
(native route, no tool call) and the round deadline cut it at 480s with no
text sink installed — the answer was discarded. The loop then bought a second
long generation in the forced final; that one streamed 26 KB of answer, and
the registry's invoke timeout (900s, equal to the operator's 900s budget) cut
the run one second before the loop's own slice would have delivered it. The
persisted turn said "did not finish… What it had gathered: <tool list>" while
the answer sat in the run's own step frames.

Four fixes, each tested here:

1. A drilling round cut WHILE ANSWERING delivers what it wrote and makes no
   second LLM call; a round cut mid-tool-call (or silent) keeps today's path.
2. ``ConsultRun.partial_summary`` leads with the streamed answer.
3. The registry's invoke timeout is floored at the loop's own clocks + 300s.
4. A receipt with an estimate but no pricing flag stamps the estimate.

No live Postgres, no live model: the loop runs over doubles that publish
through the SAME context-local seam the Anthropic accumulator publishes from.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from typing import Any

import pytest

from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps, run_method
from legba.data.registry import consult_runs, consult_turn_stamps
from legba.data.registry.consult_runs import ConsultRun, ConsultRunManager
from legba.data.stack.llm.stream_observer import (
    TextDeltaSink,
    capture_text_deltas,
    publish_text_delta,
    publish_tool_use_start,
)

# ---------------------------------------------------------------------------
# Doubles — the native (Anthropic-wire) route
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    prompt_tokens: int = 100
    completion_tokens: int = 50
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 150
    cost_estimate_usd: float = 0.0
    model: str = "claude-fable-5-1"


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
    return _Response(
        content="",
        finish_reason="tool_calls",
        tool_calls=[_ToolCall(id="call-0", name="search_signals", arguments={"query": "iran"})],
    )


class _Substrate:
    async def search_signals(self, **kwargs: Any) -> dict[str, Any]:
        return {"count": 1, "rows": [{"id": "sig-1", "title": "t"}], "refs": []}


class _RoundCutLLM:
    """Round 1 calls a tool; round 2 STREAMS and never returns.

    ``tool_use`` marks the cut round as a tool call in progress (the preamble
    case); ``partial`` empty is the silent stall. Every request is counted, so
    "no second generation" is an assertion on the wire, not on a step kind.
    """

    PRICE_TABLE: dict[str, Any] = {}

    def __init__(self, partial: str, *, tool_use: bool = False) -> None:
        self.subprovider = "anthropic"
        self._partial = partial
        self._tool_use = tool_use
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages: Any, **kwargs: Any) -> _Response:
        self.calls.append({"tools": bool(kwargs.get("tools"))})
        if len(self.calls) == 1:
            return _tool_turn()
        if not kwargs.get("tools"):
            # The forced final — tools withheld. Answer at once.
            return _Response(content="<<<FINAL>>>\nuncertainty: 0.4\n\nforced answer")
        for i in range(0, len(self._partial), 50):
            publish_text_delta(self._partial[i : i + 50])
            await asyncio.sleep(0)
        if self._tool_use:
            publish_tool_use_start()
        await asyncio.sleep(30)  # the round deadline fires here
        raise AssertionError("unreachable — the round must be cut")


class _AnsweringLLM:
    """Round 1 tool call; round 2 streams and RETURNS the answer (no cut)."""

    PRICE_TABLE: dict[str, Any] = {}

    def __init__(self, answer: str) -> None:
        self.subprovider = "anthropic"
        self._answer = answer
        self.calls = 0

    async def chat_complete(self, messages: Any, **kwargs: Any) -> _Response:
        self.calls += 1
        if self.calls == 1:
            return _tool_turn()
        for i in range(0, len(self._answer), 50):
            publish_text_delta(self._answer[i : i + 50])
            await asyncio.sleep(0)
        return _Response(content=self._answer)


async def _run(llm: Any, **dep_kwargs: Any) -> tuple[Any, list[dict[str, Any]]]:
    steps: list[dict[str, Any]] = []

    async def _publish(step: dict[str, Any]) -> None:
        steps.append(step)

    deps = ConsultOnDemandDeps(
        llm=llm, substrate=_Substrate(), step_publish=_publish, **dep_kwargs,
    )
    result = await run_method(
        [{"question": "what is happening?", "max_tool_rounds": 4}], {}, deps,
    )
    return result, steps


def _kinds(steps: list[dict[str, Any]]) -> list[str]:
    return [s.get("kind") for s in steps]


def _first(steps: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    return next((s for s in steps if s.get("kind") == kind), None)


ANSWER = "## Assessment\n\n" + ("The escalation ladder is intact. " * 120)

# ---------------------------------------------------------------------------
# 1. A drilling round cut while answering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_round_cut_while_answering_delivers_the_partial_and_spends_nothing_more() -> None:
    """The 9b902fec round 13, reproduced: the answer is DELIVERED, once.

    ``round_deadline_seconds`` is floored at 1s by the loop, so this costs one
    real second. The forced final must not run: two requests on the wire.
    """
    llm = _RoundCutLLM(ANSWER)
    result, steps = await _run(llm, round_deadline_seconds=0.2)

    assert len(llm.calls) == 2, (
        f"a second generation was bought for an answer already paid for: {llm.calls}"
    )
    kinds = _kinds(steps)
    assert "forced_final" not in kinds
    assert "degraded_final" not in kinds
    pf = _first(steps, "partial_final")
    assert pf is not None
    assert pf["reason"] == "round_deadline_exceeded_while_answering"
    assert pf["round"] == 2
    assert pf["chars"] == len(ANSWER)

    answer = result.consult_response.answer
    assert "The escalation ladder is intact." in answer
    assert "while writing the answer" in answer
    data = result.consult_response.data
    assert data["synthesis_status"] == "partial"
    assert data["resynthesizable"] is True


@pytest.mark.asyncio
async def test_the_drilling_round_relays_its_answer_live_under_its_segment() -> None:
    """The panel watches the answer form in a drilling round, not a spinner.

    Every ``answer_delta`` frame names its segment, and the round's frames
    concatenate to exactly what the model streamed — that is what the registry
    rebuilds a cut run's partial from.
    """
    llm = _RoundCutLLM(ANSWER)
    _result, steps = await _run(llm, round_deadline_seconds=0.2)

    deltas = [s for s in steps if s.get("kind") == "answer_delta"]
    assert deltas, "the drilling round streamed and relayed nothing"
    assert {d["segment"] for d in deltas} == {"round-2"}
    assert "".join(d["text"] for d in deltas) == ANSWER


@pytest.mark.asyncio
async def test_an_answering_round_that_finishes_also_relays() -> None:
    """The relay is not a timeout-only path: a finished answer streamed too."""
    llm = _AnsweringLLM(ANSWER)
    result, steps = await _run(llm)

    assert llm.calls == 2
    deltas = [s for s in steps if s.get("kind") == "answer_delta"]
    assert "".join(d["text"] for d in deltas) == ANSWER
    assert result.consult_response.data["synthesis_status"] == "complete"


@pytest.mark.asyncio
async def test_a_round_cut_mid_tool_call_still_falls_through_to_the_forced_final() -> None:
    """Text plus a ``tool_use`` block in flight is a PREAMBLE, not an answer."""
    llm = _RoundCutLLM("Let me check the latest reporting.", tool_use=True)
    result, steps = await _run(llm, round_deadline_seconds=0.2)

    kinds = _kinds(steps)
    assert "round_deadline_exceeded" in kinds
    assert "partial_final" not in kinds
    assert "forced_final" in kinds
    assert len(llm.calls) == 3 and llm.calls[-1]["tools"] is False
    assert "forced answer" in result.consult_response.answer


@pytest.mark.asyncio
async def test_a_round_cut_while_writing_text_json_tool_call_is_not_an_answer() -> None:
    """The legacy JSON tool-call shape in flight is not prose to deliver."""
    llm = _RoundCutLLM('{"tool": "search_signals", "args": {"query": "ir')
    _result, steps = await _run(llm, round_deadline_seconds=0.2)

    assert "partial_final" not in _kinds(steps)
    assert "forced_final" in _kinds(steps)


@pytest.mark.asyncio
async def test_a_silent_stalled_round_keeps_todays_path() -> None:
    """Nothing streamed ⇒ nothing to deliver ⇒ the forced final, as before."""
    llm = _RoundCutLLM("")
    result, steps = await _run(llm, round_deadline_seconds=0.2)

    kinds = _kinds(steps)
    assert "round_deadline_exceeded" in kinds
    assert "partial_final" not in kinds
    assert "forced_final" in kinds
    assert len(llm.calls) == 3
    assert "forced answer" in result.consult_response.answer


def test_the_sink_records_a_tool_use_start_only_when_installed() -> None:
    publish_tool_use_start()  # no sink: a no-op, never a raise
    sink = TextDeltaSink()
    assert sink.tool_use_started is False
    with capture_text_deltas(sink):
        publish_tool_use_start()
    assert sink.tool_use_started is True


# ---------------------------------------------------------------------------
# 2. The registry's partial leads with the streamed answer
# ---------------------------------------------------------------------------


def _cut_run(steps: list[dict[str, Any]]) -> ConsultRun:
    run = ConsultRun(request_id="r-9b9", session_id="s-963", status="error")
    for step in steps:
        run.accumulate(step)
    run.error = (504, "the consult actor did not return within 900s")
    return run


def test_partial_summary_puts_the_streamed_answer_first() -> None:
    run = _cut_run([
        {"phase": "reason", "kind": "llm_call", "round": 12},
        {"phase": "act", "kind": "tool_call", "round": 12, "tool": "search_signals", "ok": True},
        {"phase": "narrate", "kind": "answer_delta", "segment": "final", "text": "## Findings\n\nThe "},
        {"phase": "narrate", "kind": "answer_delta", "segment": "final", "text": "ladder holds."},
    ])
    summary = run.partial_summary()
    lines = summary.splitlines()
    assert lines[0].startswith("_This consult was cut during its final synthesis")
    assert lines[0].endswith("unfinished._")
    assert "## Findings\n\nThe ladder holds." in summary
    # The answer comes BEFORE the account of the stop, which is still there.
    assert summary.index("The ladder holds.") < summary.index("_This consult did not finish._")
    assert "Reason: the consult actor did not return within 900s" in summary
    assert "`search_signals` — ok" in summary


def test_partial_summary_keeps_only_the_last_segment() -> None:
    """An earlier round's preamble is not part of the answer in flight."""
    run = _cut_run([
        {"kind": "answer_delta", "segment": "round-3", "text": "Let me look that up."},
        {"kind": "tool_call", "round": 3, "tool": "query_facts", "ok": True},
        {"kind": "answer_delta", "segment": "round-4", "text": "The answer, "},
        {"kind": "answer_delta", "segment": "round-4", "text": "in progress"},
    ])
    summary = run.partial_summary()
    assert "Let me look that up." not in summary
    assert "The answer, in progress" in summary
    assert summary.splitlines()[0].startswith("_This consult was cut while answering in round 4")


def test_partial_summary_concatenates_segmentless_frames_from_an_older_runtime() -> None:
    run = _cut_run([
        {"kind": "answer_delta", "text": "older "},
        {"kind": "answer_delta", "text": "runtime"},
    ])
    assert "older runtime" in run.partial_summary()


def test_partial_summary_without_a_streamed_answer_is_unchanged() -> None:
    run = _cut_run([{"kind": "tool_call", "round": 1, "tool": "search_signals", "ok": True}])
    assert run.partial_summary().splitlines()[0] == "_This consult did not finish._"


def test_answer_frames_never_count_against_the_step_cap() -> None:
    """A tool-heavy run must not lose its answer to the trace before it."""
    steps: list[dict[str, Any]] = [
        {"kind": "tool_call", "round": 1, "tool": "search_signals", "ok": True}
        for _ in range(consult_runs.MAX_ACCUMULATED_STEPS + 5)
    ]
    steps += [{"kind": "answer_delta", "segment": "final", "text": "kept"}]
    run = _cut_run(steps)
    assert run.answer_frames == 1
    assert len(run.steps) == consult_runs.MAX_ACCUMULATED_STEPS + 1
    assert run.streamed_answer() == "kept"


@pytest.mark.asyncio
async def test_the_manager_feeds_answer_frames_through_the_same_caps() -> None:
    """The NATS watcher accumulates via ``ConsultRun.accumulate``."""
    import json as _json

    class _Msg:
        def __init__(self, frame: dict[str, Any]) -> None:
            self.data = _json.dumps(frame).encode()

    class _NC:
        def __init__(self) -> None:
            self.cb: Any = None

        async def subscribe(self, subject: str, cb: Any) -> Any:
            self.cb = cb

            class _Sub:
                async def unsubscribe(self) -> None:
                    return None

            return _Sub()

    class _Store:
        def __init__(self) -> None:
            self.nc = _NC()

    store = _Store()
    manager = ConsultRunManager(nats_store=store)
    run = ConsultRun(request_id="r", session_id=None)
    await manager._watch_steps(run)
    await store.nc.cb(_Msg({"type": "step", "kind": "answer_delta", "segment": "final", "text": "abc"}))
    await store.nc.cb(_Msg({"type": "step", "kind": "answer_delta", "segment": "final", "text": ""}))
    assert run.answer_chars == 3 and run.answer_frames == 1


def test_the_run_status_carries_the_partial_for_a_reconnecting_client() -> None:
    run = _cut_run([{"kind": "answer_delta", "segment": "final", "text": "the answer"}])
    payload = run.as_status_payload()
    assert "the answer" in payload["partial_answer"]
    assert "partial_answer" not in ConsultRun(request_id="ok", session_id=None).as_status_payload()


# ---------------------------------------------------------------------------
# 3. The invoke timeout is floored at the loop's own clocks
# ---------------------------------------------------------------------------


_CLOCK_ENVS = (
    "LEGBA_CONSULT_BUDGET_SECONDS",
    "LEGBA_CONSULT_FINAL_FLOOR_SECONDS",
    "LEGBA_CONSULT_INVOKE_TIMEOUT_SECONDS",
)


@pytest.fixture
def _clean_clocks(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in _CLOCK_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(consult_runs, "_warned_floor_pins", set())
    return monkeypatch


def test_the_default_invoke_timeout_is_the_derived_floor(_clean_clocks, caplog) -> None:
    """480 + 240 + 300 = 1020 beats the 900 default — silently: no pin was set."""
    with caplog.at_level(logging.WARNING, logger=consult_runs.logger.name):
        assert consult_runs.invoke_timeout_floor_seconds() == pytest.approx(1020.0)
        assert consult_runs.invoke_timeout_seconds() == pytest.approx(1020.0)
    assert not [r for r in caplog.records if "below_loop_floor" in r.getMessage()]


def test_a_pin_equal_to_the_budget_is_raised_and_said_once(_clean_clocks, caplog) -> None:
    """The 9b902fec configuration: budget 900 beside an invoke timeout of 900."""
    _clean_clocks.setenv("LEGBA_CONSULT_BUDGET_SECONDS", "900")
    _clean_clocks.setenv("LEGBA_CONSULT_INVOKE_TIMEOUT_SECONDS", "900")
    with caplog.at_level(logging.WARNING, logger=consult_runs.logger.name):
        first = consult_runs.invoke_timeout_seconds()
        second = consult_runs.invoke_timeout_seconds()
    assert first == second == pytest.approx(900 + 240 + 300)
    warnings = [r.getMessage() for r in caplog.records if "below_loop_floor" in r.getMessage()]
    assert len(warnings) == 1, warnings
    assert "=900s" in warnings[0] and "1440s" in warnings[0]


def test_a_pin_above_the_floor_wins(_clean_clocks) -> None:
    _clean_clocks.setenv("LEGBA_CONSULT_BUDGET_SECONDS", "3600")
    _clean_clocks.setenv("LEGBA_CONSULT_FINAL_FLOOR_SECONDS", "900")
    _clean_clocks.setenv("LEGBA_CONSULT_INVOKE_TIMEOUT_SECONDS", "6000")
    assert consult_runs.invoke_timeout_seconds() == pytest.approx(6000.0)
    # The two clocks can never be equal: whatever is pinned, the margin holds.
    _clean_clocks.setenv("LEGBA_CONSULT_INVOKE_TIMEOUT_SECONDS", "4500")
    assert consult_runs.invoke_timeout_seconds() == pytest.approx(4800.0)


def test_the_registry_and_the_loop_read_the_same_clock(_clean_clocks) -> None:
    from legba.data import consult_clocks
    from legba.data.analysts import consult_round_protocol as cp

    assert cp.default_total_budget_seconds is consult_clocks.default_total_budget_seconds
    assert cp.default_final_floor_seconds is consult_clocks.default_final_floor_seconds
    _clean_clocks.setenv("LEGBA_CONSULT_FINAL_FLOOR_SECONDS", "123")
    assert cp.final_synthesis_budget(1.0) == pytest.approx(123.0)
    assert consult_runs.invoke_timeout_floor_seconds() == pytest.approx(480 + 123 + 300)


def test_consult_runs_imports_without_the_analysts_package() -> None:
    """The registry is the slim image: the floor must not drag analysts in."""
    src = str(pathlib.Path(__file__).resolve().parents[2] / "src")
    script = textwrap.dedent(
        """
        import sys
        import legba.data.registry.consult_runs as m
        assert m.invoke_timeout_seconds() > 0
        leaked = sorted(k for k in sys.modules if k.startswith("legba.data.analysts"))
        assert not leaked, leaked
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (src, os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0 and "OK" in result.stdout, result.stderr


# ---------------------------------------------------------------------------
# 4. Visible cost: the table estimate is stamped, and says so
# ---------------------------------------------------------------------------


def test_an_unflagged_estimate_is_stamped_as_a_table_estimate() -> None:
    """A cut run's last meter carries ``est_cost_usd`` but no ``priced`` flag."""
    meter = {"calls": 13, "input_tokens": 900_000, "output_tokens": 9_000, "est_cost_usd": 4.2}
    out = consult_turn_stamps.usage_receipt(meter, plane="fable", route="llm.anthropic.fable_5_1")
    assert out["cost_usd"] == pytest.approx(4.2)
    assert out["cost_reason"] == "table_estimate"
    assert out["est_cost_usd"] == pytest.approx(4.2)


def test_a_zero_or_unpriced_estimate_is_never_published_as_a_cost() -> None:
    zero = consult_turn_stamps.usage_receipt(
        {"input_tokens": 5, "est_cost_usd": 0.0}, plane="core", route="r",
    )
    assert zero["cost_usd"] is None and zero["cost_reason"] == "pricing_not_reported"
    unpriced = consult_turn_stamps.usage_receipt(
        {"input_tokens": 5, "est_cost_usd": 1.0, "priced": False}, plane="core", route="r",
    )
    assert unpriced["cost_usd"] is None and unpriced["cost_reason"] == "route_unpriced"
    priced = consult_turn_stamps.usage_receipt(
        {"input_tokens": 5, "est_cost_usd": 1.5, "priced": True}, plane="opus", route="r",
    )
    assert priced["cost_usd"] == pytest.approx(1.5) and priced["cost_reason"] is None
