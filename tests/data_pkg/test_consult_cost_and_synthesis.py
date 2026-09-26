# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The c8a0105c train: what a run is allowed to spend, and finishing a cut one.

The incident
============

2026-09-16 09:43-09:49Z, session f12ebbd4, request c8a0105c. The operator's own
run, on ``llm.anthropic.fable_5_1``. Eleven calls, ten rounds, fifty tool
calls, 283 cited refs — and then the final synthesis was handed a fixed 150s
slice, did not finish a long answer inside it, and was CANCELLED. The partial
generation was discarded and the operator received 487 characters of apology,
for roughly ten dollars.

Four things were wrong and each has a test here:

1. **The synthesis was cut to a drilling round's slice** and its output thrown
   away. It now gets the remaining budget under a floor, and a cut synthesis
   DELIVERS what it wrote — :func:`test_a_cut_synthesis_delivers_its_partial_text`.
2. **Nothing was watching the money.** Every clock passed; what made the run
   expensive was the transcript it replayed, which no clock can see —
   :func:`test_the_input_token_ceiling_ends_drilling_early`.
3. **The prompt grew without bound**, because each round appended its results
   and the model re-read all of them at input prices —
   :func:`test_compaction_shrinks_the_prompt_but_never_the_trace`.
4. **Ten rounds came from the front door's request-model default**, not from
   the kind — :func:`test_rounds_default_to_the_kind_not_ten`.

And the recovery: a run whose evidence was gathered and paid for can be
finished without drilling again. The load-bearing claim there is that the
replay sends the SAME PROMPT, and
:func:`test_an_exact_replay_is_byte_identical_to_the_original_request` is the
proof rather than the assertion.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps, run_method
from legba.data.stack.llm.stream_observer import publish_text_delta

# ---------------------------------------------------------------------------
# Doubles — the native (Anthropic-wire) route, because that is the priced one
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


class _NativeLLM:
    """Records every request. ``subprovider`` picks the wire shape."""

    #: Mirrors ``AnthropicProviderHandler.PRICE_TABLE`` shape closely enough
    #: for the spend guard's table lookup to be exercised for real.
    PRICE_TABLE: dict[str, Any] = {}

    def __init__(
        self, turns: list[_Response], subprovider: str = "anthropic",
    ) -> None:
        self.subprovider = subprovider
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    def _record(self, messages, kwargs: dict[str, Any]) -> None:
        # Deep-copy the messages: the loop REBUILDS ``messages`` each round but
        # a shallow record would still alias the block lists, and compaction
        # rewrites those. Without this the recorded history mutates under the
        # assertions and the byte-identity test silently passes on aliasing.
        self.calls.append({
            "messages": json.loads(json.dumps(list(messages), default=str)),
            **{k: v for k, v in kwargs.items() if k != "messages"},
        })

    async def chat_complete(self, messages, **kwargs: Any) -> _Response:
        self._record(messages, kwargs)
        # No ``tools`` on the wire means the loop WITHHELD them — the forced
        # final. A real model cannot emit a tool call it was not offered, so
        # neither does this double: without that rule a leftover scripted tool
        # turn becomes the "answer" and the run returns empty prose for a
        # reason that has nothing to do with what is under test.
        if self._turns and kwargs.get("tools"):
            return self._turns.pop(0)
        return _Response(content="<<<FINAL>>>\nuncertainty: 0.4\n\nthe answer")


class _StreamingCutLLM(_NativeLLM):
    """A handler whose FINAL turn streams text and then never returns.

    This is the c8a0105c shape reproduced honestly: the provider delivers real,
    billed tokens and the caller's deadline fires before the generation ends.
    It publishes through :func:`publish_text_delta`, the same seam the real
    Anthropic accumulator publishes from, so the test exercises the actual
    context-local sink rather than a stand-in for it.
    """

    def __init__(self, turns: list[_Response], partial: str) -> None:
        super().__init__(turns)
        self._partial = partial
        self.streamed = False

    async def chat_complete(self, messages, **kwargs: Any) -> _Response:
        if kwargs.get("tools"):
            return await super().chat_complete(messages, **kwargs)
        self._record(messages, kwargs)
        self.streamed = True
        for i in range(0, len(self._partial), 50):
            publish_text_delta(self._partial[i : i + 50])
            await asyncio.sleep(0)
        await asyncio.sleep(30)  # the deadline fires here
        raise AssertionError("unreachable — the synthesis must be cut")


class _BigSubstrate:
    """A fixture corpus: deterministic, and big enough to need bounding."""

    def __init__(self, rows: int = 40, width: int = 700) -> None:
        self._rows = rows
        self._width = width
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def search_signals(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("search_signals", dict(kwargs)))
        return {
            "count": self._rows,
            "rows": [
                {"id": f"sig-{i}", "title": f"signal {i}", "body": "x" * self._width}
                for i in range(self._rows)
            ],
            "refs": [],
        }

    async def query_facts(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("query_facts", dict(kwargs)))
        return {"count": 1, "rows": [{"subject": "a", "predicate": "b"}], "refs": []}


SEARCH = {"tool": "search_signals", "args": {"query": "iran"}}
FACTS = {"tool": "query_facts", "args": {"subject": "iran"}}


def _distinct(n: int) -> list[dict[str, Any]]:
    """``n`` calls the loop will NOT dedupe.

    ``_normalize_calls`` dedupes on ``(tool, args)``, so repeating one call
    collapses the batch — a batch-cap test built from identical calls measures
    the dedupe and not the cap.
    """
    return [
        {"tool": "search_signals", "args": {"query": f"topic-{i}"}} for i in range(n)
    ]


def _cut_synthesis(monkeypatch: pytest.MonkeyPatch, *, budget: float = 0.4) -> None:
    """Arrange a run whose SYNTHESIS is cut, quickly.

    Two pins, both needed, and the reason is the fix itself: the synthesis
    budget is ``max(remaining, floor)``, so shrinking the floor alone does
    nothing while ~480s of total budget remains — the remainder wins, which is
    the entire point of the change. To cut a synthesis you must shrink the
    TOTAL. ``_MIN_FINAL_SECONDS`` (15s, the "don't even start" floor) is pinned
    down as well so the test does not have to burn fifteen real seconds to get
    past it.
    """
    from legba.data.analysts import consult_on_demand

    monkeypatch.setenv("LEGBA_CONSULT_FINAL_FLOOR_SECONDS", "0.05")
    monkeypatch.setattr(consult_on_demand, "_MIN_FINAL_SECONDS", 0.02)
    monkeypatch.setenv("LEGBA_CONSULT_BUDGET_SECONDS", str(budget))


def _tool_turn(*calls: dict[str, Any], text: str = "") -> _Response:
    return _Response(
        content=text,
        finish_reason="tool_calls",
        tool_calls=[
            _ToolCall(
                id=f"call-{i}", name=c["tool"], arguments=dict(c.get("args") or {}),
            )
            for i, c in enumerate(calls)
        ],
    )


async def _run(
    llm: Any, substrate: Any = None, inputs: dict[str, Any] | None = None, **dep_kwargs: Any,
) -> tuple[Any, list[dict[str, Any]]]:
    steps: list[dict[str, Any]] = []

    async def _publish(step: dict[str, Any]) -> None:
        steps.append(step)

    deps = ConsultOnDemandDeps(
        llm=llm,
        substrate=substrate or _BigSubstrate(),
        step_publish=_publish,
        **dep_kwargs,
    )
    result = await run_method(
        [{"question": "what is happening?", **(inputs or {})}], {}, deps,
    )
    return result, steps


def _kinds(steps: list[dict[str, Any]]) -> list[str]:
    return [s.get("kind") for s in steps]


def _first(steps: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    return next((s for s in steps if s.get("kind") == kind), None)


# ---------------------------------------------------------------------------
# 1. The synthesis is never cut to a fixed slice, and a cut one is DELIVERED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_synthesis_gets_the_remainder_not_a_round_deadline() -> None:
    """``max(remaining, floor)`` — the bug was a ``min``.

    The old code gave the synthesis ``min(round_deadline, remaining)``, so a
    150s drilling deadline capped the one turn that writes the answer. Here the
    round deadline is a half-second and the synthesis still gets its floor.
    """
    from legba.data.analysts import consult_round_protocol as _cp

    assert _cp.final_synthesis_budget(12.0) == pytest.approx(240.0)
    assert _cp.final_synthesis_budget(600.0) == pytest.approx(600.0)


@pytest.mark.asyncio
async def test_a_cut_synthesis_delivers_its_partial_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heart of it: 11,000 characters of answer must not become an apology.

    The model streams real text and is then cut. Every one of those characters
    was generated and billed, so every one of them is delivered — behind a
    banner that says plainly that the answer stops mid-thought.
    """
    _cut_synthesis(monkeypatch)
    partial = "## Assessment\n\n" + ("The phase change is real. " * 200)
    llm = _StreamingCutLLM([_tool_turn(SEARCH)], partial=partial)

    result, steps = await _run(llm, inputs={"max_tool_rounds": 1})

    assert llm.streamed
    assert "partial_final" in _kinds(steps)
    assert "degraded_final" not in _kinds(steps), (
        "text was streamed, so the run had an answer to deliver — the "
        "replacement message is only honest when there is NOTHING"
    )
    answer = result.consult_response.answer
    assert "Synthesis interrupted at" in answer
    assert "The phase change is real." in answer, (
        "the partial text IS the product; a banner without it is the bug"
    )
    assert len(answer) > 4000
    # The payload advertises that this turn can be finished later.
    data = result.consult_response.data
    assert data["synthesis_status"] == "partial"
    assert data["resynthesizable"] is True
    assert data["replay_transcript"]["usable"] is True


@pytest.mark.asyncio
async def test_a_synthesis_that_produced_nothing_still_degrades_honestly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No text ⇒ no partial to deliver, and the apology is the honest answer."""
    _cut_synthesis(monkeypatch)

    class _SilentCut(_NativeLLM):
        async def chat_complete(self, messages, **kwargs: Any) -> _Response:
            if kwargs.get("tools"):
                return await super().chat_complete(messages, **kwargs)
            await asyncio.sleep(30)
            raise AssertionError("unreachable")

    result, steps = await _run(
        _SilentCut([_tool_turn(SEARCH)]), inputs={"max_tool_rounds": 1},
    )
    assert "degraded_final" in _kinds(steps)
    assert "partial_final" not in _kinds(steps)
    assert "ran out of its time budget" in result.consult_response.answer
    assert result.consult_response.data["synthesis_status"] == "none"


# ---------------------------------------------------------------------------
# 2. The spend ceiling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_input_token_ceiling_ends_drilling_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop on the money, long before the clock.

    Every one of c8a0105c's three clocks passed. 150 prompt tokens per call
    against a 260-token ceiling means the guard fires on round 3 — the run then
    synthesises with what it has instead of buying four more rounds.
    """
    monkeypatch.setenv("LEGBA_CONSULT_MAX_INPUT_TOKENS_PER_RUN", "260")
    llm = _NativeLLM([_tool_turn(SEARCH)] * 6)

    result, steps = await _run(llm, inputs={"max_tool_rounds": 6})

    hit = _first(steps, "spend_ceiling_reached")
    assert hit is not None, "the ceiling must stop the loop, not merely report"
    assert hit["input_tokens"] >= 260
    assert "input-token ceiling" in hit["reason"]
    # Drilling stopped early and the run STILL answered.
    assert result.consult_response.answer
    assert len(llm.calls) < 7


@pytest.mark.asyncio
async def test_the_cost_ceiling_uses_the_handlers_price_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dollar figure is the handler's own estimate, not a second opinion.

    The real handler stamps ``usage.cost_estimate_usd`` at parse time from
    ``PRICE_TABLE``; the guard sums those. Here the double stamps a per-call
    cost and the ceiling is crossed on the third call.
    """
    monkeypatch.setenv("LEGBA_CONSULT_MAX_COST_USD_PER_RUN", "2.50")
    llm = _NativeLLM([_tool_turn(SEARCH)] * 6)
    for turn in llm._turns:
        turn.usage = _Usage(cost_estimate_usd=1.00)

    _result, steps = await _run(llm, inputs={"max_tool_rounds": 6})

    hit = _first(steps, "spend_ceiling_reached")
    assert hit is not None
    assert "estimated-cost ceiling" in hit["reason"]
    assert hit["est_cost_usd"] >= 2.50


def test_the_spend_guard_reads_the_real_anthropic_price_table() -> None:
    """A wiring check against the SHIPPED table, not a fixture of it.

    The guard's fallback path computes from ``PRICE_TABLE`` when a response did
    not carry a stamped estimate. If the table key or the estimator's prefix
    match ever drifts, an expensive plane silently estimates $0 and the cost
    ceiling stops existing — which is exactly the failure this whole train is
    about, one level down.
    """
    from legba.data.analysts.consult_spend_guard import estimate_call_cost
    from legba.data.stack.llm.anthropic import AnthropicProviderHandler

    class _Handler:
        PRICE_TABLE = AnthropicProviderHandler.PRICE_TABLE

    usage = _Usage(
        prompt_tokens=100_000, completion_tokens=10_000, cost_estimate_usd=0.0,
        model="claude-fable-5-1",
    )
    cost = estimate_call_cost(_Handler(), _Response(usage=usage))
    # 100k in at $15/M + 10k out at $75/M = $1.50 + $0.75.
    assert cost == pytest.approx(2.25, rel=1e-6)
    assert "claude-fable-5" in AnthropicProviderHandler.PRICE_TABLE


# ---------------------------------------------------------------------------
# 3. Compaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compaction_shrinks_the_prompt_but_never_the_trace() -> None:
    """Older rounds ride as refs + a digest; the raw stays on the trace.

    The cost shape that produced the $10 bill: round *n* carried every earlier
    round's full tool bodies, re-read at input prices on every call.
    """
    llm = _NativeLLM([_tool_turn(SEARCH)] * 4)
    result, steps = await _run(llm, inputs={"max_tool_rounds": 4})

    assert "compaction" in _kinds(steps)

    # The LAST drilling request must not carry four full result payloads.
    last = llm.calls[-1]["messages"]
    compacted = [
        block
        for m in last
        if isinstance(m.get("content"), list)
        for block in m["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
        and '"compacted": true' in str(block.get("content", ""))
    ]
    assert compacted, "older rounds' bodies must be digested in the next prompt"

    # ...and the digest keeps what the answer cites.
    body = json.loads(compacted[0]["content"])
    assert body["compacted"] is True
    assert "summary" in body

    # The TRACE still carries the real per-call record.
    tool_steps = [s for s in steps if s.get("kind") == "tool_call"]
    assert len(tool_steps) >= 4
    assert all(s.get("result") is not None for s in tool_steps)


@pytest.mark.asyncio
async def test_one_round_of_results_is_bounded_as_a_ROUND_not_per_tool() -> None:
    """The per-round bound replaces the per-tool bound as the operative limit.

    Four 28 KB results used to be allowed 8 KB each — 32 KB into the prompt,
    and into every prompt after it. The round bound is 16 KB total.
    """
    from legba.data.stack.llm import tool_round_compaction as _tc

    llm = _NativeLLM([_tool_turn(*_distinct(4))])
    await _run(llm, _BigSubstrate(rows=60, width=500), inputs={"max_tool_rounds": 1})

    # The forced-final request carries the round's results.
    results = [
        block
        for m in llm.calls[-1]["messages"]
        if isinstance(m.get("content"), list)
        for block in m["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert results, "the round's tool results must reach the synthesis prompt"
    total = sum(len(str(b.get("content", ""))) for b in results)
    assert total <= _tc.DEFAULT_ROUND_RESULT_BOUND * 1.1, (
        f"the round's bodies totalled {total} against a "
        f"{_tc.DEFAULT_ROUND_RESULT_BOUND} bound"
    )


def test_compaction_is_idempotent() -> None:
    """The loop compacts after EVERY round, so round 2's digest is offered to
    this machinery again in rounds 3, 4, 5...

    Without idempotence each pass digests its own digest: the summary nests
    inside the next summary, drifts toward the character cut, and the step
    trace reports steady compaction work on a transcript that stopped changing.
    """
    from legba.data.stack.llm.tool_round_compaction import (
        compact_prior_tool_messages,
        digest_tool_body,
    )

    raw = json.dumps({"count": 3, "refs": ["r1", "r2"], "rows": [{"a": "x" * 500}]})
    once = digest_tool_body(raw)
    assert digest_tool_body(once) == once, "digesting a digest must be a no-op"
    assert json.loads(once)["refs"] == ["r1", "r2"], "refs survive verbatim"

    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "tool", "tool_call_id": "t1", "name": "search", "content": raw},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "tool", "tool_call_id": "t2", "name": "search", "content": raw},
    ]
    first, stats1 = compact_prior_tool_messages(messages)
    assert stats1.bodies_compacted == 1, "only the OLDER round is digested"
    second, stats2 = compact_prior_tool_messages(first)
    assert stats2.bodies_compacted == 0, "a second pass has nothing left to do"
    assert second == first


def test_the_round_allocation_is_max_min_fair() -> None:
    """A small result is never truncated to make room for a large sibling."""
    from legba.data.stack.llm.tool_round_compaction import allocate_body_limits

    limits = allocate_body_limits([200, 40_000], total_bound=16_000, min_each=600)
    assert limits[0] == 200, "the small result keeps its full size"
    assert limits[1] >= 15_000, "its slack is redistributed, not wasted"
    assert sum(limits) <= 16_000


@pytest.mark.asyncio
async def test_the_native_batch_is_capped_tighter_than_the_text_route() -> None:
    """Four calls a round on the native route, because they ride in every later
    prompt. A descriptor may raise it; the default may not."""
    llm = _NativeLLM([_tool_turn(*_distinct(5))])
    substrate = _BigSubstrate()
    await _run(llm, substrate, inputs={"max_tool_rounds": 1})
    assert len(substrate.calls) == 4, (
        f"five calls were offered; {len(substrate.calls)} ran"
    )


# ---------------------------------------------------------------------------
# 4. Rounds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rounds_default_to_the_kind_not_ten() -> None:
    """Absent an explicit request, the cap is the kind's 6 — never 10.

    Ten came from ``ConsultRequest.max_tool_rounds``'s ``default=10``, which
    made every chat request carry an explicit 10 and made the kind's own
    default unreachable from the panel.
    """
    from legba.data.analysts.consult_on_demand import MAX_TOOL_ROUNDS

    llm = _NativeLLM([_tool_turn(SEARCH)] * 12)
    _result, steps = await _run(llm)

    plan = _first(steps, "render_prompt")
    assert plan is not None
    assert plan["max_rounds"] == MAX_TOOL_ROUNDS == 6
    assert plan["rounds_source"] == "default"


@pytest.mark.asyncio
async def test_an_explicit_round_request_still_wins() -> None:
    """Asking for breadth deliberately is allowed; drifting into it is not."""
    llm = _NativeLLM([_tool_turn(SEARCH)] * 12)
    _result, steps = await _run(llm, inputs={"max_tool_rounds": 9})
    plan = _first(steps, "render_prompt")
    assert plan is not None
    assert plan["max_rounds"] == 9
    assert plan["rounds_source"] == "request"


@pytest.mark.asyncio
async def test_the_first_frame_carries_every_effective_cap() -> None:
    """An operator watching a run start sees what it may spend, before it does."""
    llm = _NativeLLM([_tool_turn(SEARCH)])
    _result, steps = await _run(llm, inputs={"max_tool_rounds": 2})
    plan = _first(steps, "render_prompt")
    assert plan is not None
    for key in (
        "max_rounds", "rounds_source", "native_batch_cap", "round_result_bytes",
        "total_budget_s", "final_floor_s", "max_input_tokens", "max_cost_usd",
    ):
        assert key in plan, f"the run's first frame must state {key}"


@pytest.mark.asyncio
async def test_each_round_reports_the_running_spend() -> None:
    """The live meter's data: running totals against their ceilings."""
    llm = _NativeLLM([_tool_turn(SEARCH)] * 3)
    _result, steps = await _run(llm, inputs={"max_tool_rounds": 3})
    calls = [s for s in steps if s.get("kind") == "llm_call"]
    assert calls
    usage = calls[-1]["usage"]
    assert usage["input_tokens"] == 100 * len(calls)
    assert usage["max_input_tokens"] > 0
    assert "est_cost_usd" in usage


# ---------------------------------------------------------------------------
# 5. Recovery — the replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_exact_replay_is_byte_identical_to_the_original_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE claim: the recovery sends the prompt the loop sent.

    Not a fresh prompt over summarised evidence — the same system string and
    the same message array, byte for byte, with only the model differing. The
    original run is recorded here and its synthesis request is captured off the
    handler double; the replay's request is then compared against it.
    """
    _cut_synthesis(monkeypatch)
    original = _StreamingCutLLM(
        [_tool_turn(SEARCH), _tool_turn(SEARCH)], partial="a partial answer " * 40,
    )
    result, _steps = await _run(original, inputs={"max_tool_rounds": 2})

    sent = original.calls[-1]
    transcript = result.consult_response.data["replay_transcript"]

    # What was recorded IS what was sent.
    assert transcript["messages"] == sent["messages"]
    assert transcript["system"] == sent["system"]

    # Now replay it on a DIFFERENT handler and compare the request.
    replay = _NativeLLM([])
    replayed, steps = await _run(
        replay,
        inputs={
            "synthesize_from": {
                "question": "what is happening?",
                "steps": [],
                "cited_refs": [],
                "replay_transcript": transcript,
            },
        },
    )

    assert len(replay.calls) == 1, "a replay is ONE call and never drills"
    assert replay.calls[0]["messages"] == sent["messages"], (
        "the replayed message array must be byte-identical to the original"
    )
    assert replay.calls[0]["system"] == sent["system"]
    assert "tools" not in replay.calls[0], (
        "the recovery must never offer tools — that would be new drilling"
    )
    assert replayed.consult_response.data["replay_fidelity"] == "exact"
    assert _first(steps, "resynthesis") is not None


@pytest.mark.asyncio
async def test_a_rebuilt_replay_reproduces_the_recorded_bounded_results() -> None:
    """Path (b): re-execution on an unchanged corpus reproduces the bodies.

    Every turn persisted before migration 0195 — the c8a0105c turn included —
    has only a TRACE: tool names, trimmed args, a small result digest. Never
    the bounded bodies the model read. So the transcript is rebuilt by
    re-executing the recorded calls and re-bounding the results with the same
    function the loop used. Against an unchanged corpus that must reproduce the
    original bodies exactly.
    """
    substrate = _BigSubstrate()
    original = _NativeLLM([_tool_turn(SEARCH, FACTS)])
    result, _steps = await _run(original, substrate, inputs={"max_tool_rounds": 1})

    def _bodies(messages: list[dict[str, Any]]) -> list[str]:
        return [
            str(block.get("content"))
            for m in messages
            if isinstance(m.get("content"), list)
            for block in m["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]

    original_bodies = _bodies(original.calls[-1]["messages"])
    assert original_bodies, "the original synthesis carried its tool results"

    # Replay with NO transcript — only the trace, as a pre-0195 turn has.
    replay = _NativeLLM([])
    replayed, steps = await _run(
        replay,
        _BigSubstrate(),  # an UNCHANGED corpus
        inputs={
            "synthesize_from": {
                "question": "what is happening?",
                "steps": result.intermediate_steps,
                "cited_refs": [],
            },
        },
    )

    assert len(replay.calls) == 1
    assert _bodies(replay.calls[0]["messages"]) == original_bodies, (
        "re-execution against an unchanged corpus must reproduce the exact "
        "bounded bodies the original model read"
    )
    data = replayed.consult_response.data
    assert data["replay_fidelity"] == "rebuilt"
    assert "re-executing 2 tool call" in data["replay_note"]
    assert "assistant free text" in data["replay_note"], (
        "a rebuild must NAME what it could not recover"
    )


@pytest.mark.asyncio
async def test_a_rebuilt_replay_reports_corpus_drift() -> None:
    """The corpus is live. A re-executed call that returns something different
    is reported per call rather than quietly folded into the answer."""
    substrate = _BigSubstrate(rows=10)
    original = _NativeLLM([_tool_turn(SEARCH)])
    result, _steps = await _run(original, substrate, inputs={"max_tool_rounds": 1})

    replay = _NativeLLM([])
    replayed, _steps2 = await _run(
        replay,
        _BigSubstrate(rows=25),  # the index grew
        inputs={
            "synthesize_from": {
                "question": "what is happening?",
                "steps": result.intermediate_steps,
                "cited_refs": [],
            },
        },
    )
    data = replayed.consult_response.data
    assert data["replay_drift"], "a changed result must be reported"
    assert data["replay_drift"][0]["recorded_count"] == 10
    assert data["replay_drift"][0]["replayed_count"] == 25
    assert "different results" in data["replay_note"]


@pytest.mark.asyncio
async def test_a_turn_with_no_evidence_refuses_rather_than_inventing() -> None:
    """No transcript and no calls ⇒ say so. Never ask a model to answer from
    nothing and present the result as a recovery."""
    replay = _NativeLLM([])
    replayed, _steps = await _run(
        replay,
        inputs={
            "synthesize_from": {
                "question": "what is happening?",
                "steps": [],
                "cited_refs": [],
            },
        },
    )
    assert len(replay.calls) == 0, "an un-recoverable turn must cost nothing"
    data = replayed.consult_response.data
    assert data["replay_fidelity"] == "unavailable"
    assert "cannot be re-synthesised" in replayed.consult_response.answer
