# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The context ceiling a consult call's input respects.

Session 7c3e91a2 (model A, synthetic sample) answered on its ninth call carrying 136,833
prompt tokens; the 621,505 its trace shows under ``usage.input_tokens`` is the
run's RUNNING SUM over nine calls. The ceiling bounds what one call sends — the
answering call above all — by withholding whole tool results, oldest first,
never a cited one, never the last round.

What is proven here:

* the pure policy, on both provider wires (ids, roles, order intact);
* a RUN whose tool results exceed the ceiling: the forced final and the
  chat-path answering round both send under it, the planner-named result kept;
* under the ceiling the requests are BYTE-IDENTICAL to a run with no ceiling,
  and the stamp is absent; over it the stamp rides the data bag and the
  ``build_consult_response`` frame;
* the recovery replay honours the same ceiling with the turn's cited refs;
* the replay of 7c3e91a2's own per-call tool-result sizes;
* the descriptor knob is declared on the kind and validates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from legba.data.analysts import consult_context_ceiling as cc
from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps, run_method
from legba.data.option_catalog.handler_options import (
    known_kind_option_names,
    resolve_kind_options,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uuid(*parts: Any) -> str:
    return str(uuid5(NAMESPACE_URL, "f6/" + "/".join(str(p) for p in parts)))


def _body(refs: list[str], pad: int) -> str:
    return json.dumps({"count": len(refs), "refs": refs, "rows": ["x" * pad]})


def _anthropic_round(rnd: int, bodies: list[str], args: list[dict[str, Any]] | None = None):
    calls = [
        {"type": "tool_use", "id": f"r{rnd}-c{i}", "name": "search_signals",
         "input": (args or [{}] * len(bodies))[i]}
        for i in range(len(bodies))
    ]
    results = [
        {"type": "tool_result", "tool_use_id": f"r{rnd}-c{i}", "content": b}
        for i, b in enumerate(bodies)
    ]
    return [
        {"role": "assistant", "content": calls},
        {"role": "user", "content": results},
    ]


def _openai_round(rnd: int, bodies: list[str], args: list[dict[str, Any]] | None = None):
    return [
        {
            "role": "assistant", "content": None,
            "tool_calls": [
                {"id": f"r{rnd}-c{i}", "type": "function",
                 "function": {"name": "search_signals",
                              "arguments": json.dumps((args or [{}] * len(bodies))[i])}}
                for i in range(len(bodies))
            ],
        },
        *[
            {"role": "tool", "tool_call_id": f"r{rnd}-c{i}", "content": b}
            for i, b in enumerate(bodies)
        ],
    ]


def _bodies_of(messages: list[Any]) -> list[str]:
    out: list[str] = []
    for m in messages:
        if m.get("role") == "tool":
            out.append(m["content"])
        elif m.get("role") == "user" and isinstance(m.get("content"), list):
            out.extend(b["content"] for b in m["content"] if b.get("type") == "tool_result")
    return out


def _is_stub(body: str) -> bool:
    try:
        parsed = json.loads(body)
    except ValueError:
        return False
    return isinstance(parsed, dict) and parsed.get("dropped") is True


def _ids(messages: list[Any]) -> list[Any]:
    """Every role and correlation id, in order — what the providers enforce."""
    out: list[Any] = []
    for m in messages:
        out.append(m.get("role"))
        out.append(m.get("tool_call_id"))
        for b in m.get("content") if isinstance(m.get("content"), list) else []:
            out.append((b.get("type"), b.get("id"), b.get("tool_use_id")))
        for c in m.get("tool_calls") or []:
            out.append(c["id"])
    return out


# ---------------------------------------------------------------------------
# 1. The pure policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("wire", ["anthropic", "openai"])
def test_over_the_ceiling_drops_oldest_uncited_whole_and_keeps_the_last_round(wire):
    build = _anthropic_round if wire == "anthropic" else _openai_round
    cited = _uuid("cited")
    r1 = [_body([cited], 4000), _body([_uuid("r1b")], 4000)]
    r2 = [_body([_uuid("r2a")], 4000), _body([_uuid("r2b")], 4000)]
    r3 = [_body([_uuid("r3a")], 4000)]
    question = {"role": "user", "content": "Q " * 50}
    messages = [
        question,
        *build(1, r1),
        # The planner NAMES the cited ref in round 2's call args.
        *build(2, r2, args=[{"query": cited}, {"query": "other"}]),
        *build(3, r3),
        {"role": "user", "content": "Round cap reached."},
    ]
    before = cc.estimate_input_tokens("SYSTEM", messages)
    ceiling = before - 1_500  # one withheld 4 KB body is not enough; two are

    out, stamp = cc.apply_context_ceiling(messages, system="SYSTEM", ceiling=ceiling)

    assert stamp is not None
    assert stamp["input_tokens_before"] == before
    assert stamp["input_tokens_after"] <= ceiling
    assert stamp["input_tokens_after"] == cc.estimate_input_tokens("SYSTEM", out)
    assert stamp["ceiling"] == ceiling
    assert stamp["results_dropped"] == 2
    bodies = _bodies_of(out)
    # r1[0] cites the planner-named ref: KEPT byte-for-byte even though oldest.
    assert bodies[0] == r1[0]
    # Oldest UNCITED first: r1[1] then r2[0]; r2[1] survives (ceiling reached).
    assert _is_stub(bodies[1]) and _is_stub(bodies[2])
    assert bodies[3] == r2[1]
    # The last round is never withheld.
    assert bodies[4] == r3[0]
    # Whole, never partial: a stub carries nothing of the body.
    assert "x" * 50 not in bodies[1]
    # Question, instruction, assistant turns, ids, roles, order: untouched.
    assert out[0] is question and out[-1] is messages[-1]
    assert _ids(out) == _ids(messages)
    for o, m in zip(out, messages):
        if m.get("role") == "assistant":
            assert o is m
    # The input is never mutated.
    assert _bodies_of(messages)[1] == r1[1]


def test_under_the_ceiling_returns_the_same_object_and_no_stamp():
    messages = [{"role": "user", "content": "q"}, *_anthropic_round(1, [_body([], 100)])]
    out, stamp = cc.apply_context_ceiling(messages, system="s", ceiling=10_000)
    assert out is messages and stamp is None
    out, stamp = cc.apply_context_ceiling(messages, system="s", ceiling=None)
    assert out is messages and stamp is None


def test_when_only_protected_results_remain_it_sends_over_and_says_so():
    cited = _uuid("only")
    messages = [
        {"role": "user", "content": "q"},
        *_anthropic_round(1, [_body([cited], 8000)], args=[{"query": cited}]),
        *_anthropic_round(2, [_body([_uuid("x")], 8000)]),
    ]
    out, stamp = cc.apply_context_ceiling(messages, system="", ceiling=1_000)
    assert stamp is not None and stamp["results_dropped"] == 0
    assert stamp["input_tokens_after"] > stamp["ceiling"]
    assert _bodies_of(out) == _bodies_of(messages)


def test_explicit_cited_refs_protect_too():
    cited = _uuid("from-turn")
    messages = [
        {"role": "user", "content": "q"},
        *_anthropic_round(1, [_body([cited], 6000), _body([_uuid("u")], 6000)]),
        *_anthropic_round(2, [_body([], 100)]),
    ]
    out, stamp = cc.apply_context_ceiling(
        messages, system="", ceiling=2_500, cited_refs=[cited.upper()],
    )
    bodies = _bodies_of(out)
    assert stamp is not None and stamp["results_dropped"] == 1
    assert bodies[0] == _bodies_of(messages)[0] and _is_stub(bodies[1])


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, cc.DEFAULT_CONTEXT_CEILING_TOKENS or None),
        ({"context_ceiling_tokens": 90_000}, 90_000),
        ({"context_ceiling_tokens": 0}, None),
        ({"context_ceiling_tokens": -5}, cc.DEFAULT_CONTEXT_CEILING_TOKENS or None),
        ({"context_ceiling_tokens": "lots"}, cc.DEFAULT_CONTEXT_CEILING_TOKENS or None),
        ({"context_ceiling_tokens": True}, cc.DEFAULT_CONTEXT_CEILING_TOKENS or None),
    ],
)
def test_the_ceiling_resolves_from_options(options, expected):
    assert cc.resolve_ceiling(options) == expected


def test_the_knob_is_declared_on_the_consult_kind_and_validates():
    assert "context_ceiling_tokens" in known_kind_option_names("consult_on_demand")
    ok = resolve_kind_options("consult_on_demand", {"context_ceiling_tokens": 120_000})
    assert ok.rejected == () and ok.accepted == {"context_ceiling_tokens": 120_000}
    off = resolve_kind_options("consult_on_demand", {"context_ceiling_tokens": 0})
    assert off.accepted == {"context_ceiling_tokens": 0}
    for bad in (-1, 1.5, "160000", True, 3_000_000):
        res = resolve_kind_options("consult_on_demand", {"context_ceiling_tokens": bad})
        assert res.accepted == {} and res.rejected[0].cause == "invalid_value", bad


# ---------------------------------------------------------------------------
# 2. A run — the native (Anthropic-wire) route, the priced one
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
    model: str = "claude-test"


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


_FINAL = "<<<FINAL>>>\nuncertainty: 0.4\n\nthe answer"


class _LLM:
    PRICE_TABLE: dict[str, Any] = {}

    def __init__(self, turns: list[_Response]) -> None:
        self.subprovider = "anthropic"
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages, **kwargs: Any) -> _Response:
        self.calls.append({
            "messages": json.loads(json.dumps(list(messages), default=str)),
            **{k: v for k, v in kwargs.items() if k != "messages"},
        })
        if self._turns and kwargs.get("tools"):
            return self._turns.pop(0)
        return _Response(content=_FINAL)


class _Substrate:
    """search_signals returns 10 uuid-keyed rows per query, refs = those ids."""

    async def search_signals(self, *, query: str, **_kw: Any) -> dict[str, Any]:
        ids = [_uuid("sig", query, i) for i in range(10)]
        return {
            "count": 10,
            "rows": [{"id": i, "body": "y" * 900} for i in ids],
            "refs": ids,
        }


def _turn(query: str, rnd: int) -> _Response:
    return _Response(
        finish_reason="tool_calls",
        tool_calls=[_ToolCall(id=f"call-{rnd}", name="search_signals",
                              arguments={"query": query})],
    )


#: Round 3 NAMES a ref round 1 returned — the planner's own citation.
_NAMED = _uuid("sig", "alpha", 3)
_SCRIPT = ("alpha", "beta", _NAMED, "delta")


async def _run(options: dict[str, Any], turns: list[_Response], rounds: int = 4):
    steps: list[dict[str, Any]] = []

    async def _publish(step: dict[str, Any]) -> None:
        steps.append(step)

    llm = _LLM(turns)
    deps = ConsultOnDemandDeps(llm=llm, substrate=_Substrate(), step_publish=_publish)
    result = await run_method(
        [{"question": "what is happening?", "max_tool_rounds": rounds}], options, deps,
    )
    return llm, result, steps


def _script() -> list[_Response]:
    return [_turn(q, i) for i, q in enumerate(_SCRIPT)]


@pytest.fixture(autouse=True)
def _live_like_compaction(monkeypatch: pytest.MonkeyPatch) -> None:
    # The live box keeps every round in full (the 2026-09-16 revert); the
    # ceiling is the bound that still holds there.
    monkeypatch.setenv("LEGBA_CONSULT_KEEP_FULL_ROUNDS", "1000")


async def _final_input_and_body_tokens() -> tuple[int, int]:
    llm, _r, _s = await _run({"context_ceiling_tokens": 0}, _script())
    final = llm.calls[-1]
    total = cc.estimate_input_tokens(final["system"], final["messages"])
    body = len(json.dumps(_bodies_of(final["messages"])[0])) // 4
    return total, body


@pytest.mark.asyncio
async def test_a_run_over_the_ceiling_synthesises_under_it_and_keeps_the_cited():
    total, body = await _final_input_and_body_tokens()
    ceiling = total - int(1.5 * body)  # two whole results must go

    llm, result, steps = await _run({"context_ceiling_tokens": ceiling}, _script())

    final = llm.calls[-1]
    assert "tools" not in final, "the forced final is the call under test"
    assert cc.estimate_input_tokens(final["system"], final["messages"]) <= ceiling
    bodies = _bodies_of(final["messages"])
    assert len(bodies) == 4
    # Round 1 is OLDEST but carries the ref round 3 named: kept, whole.
    assert _NAMED in bodies[0] and not _is_stub(bodies[0])
    # Oldest uncited first: rounds 2 and 3 withheld; round 4 (last) kept.
    assert _is_stub(bodies[1]) and _is_stub(bodies[2])
    assert not _is_stub(bodies[3])

    stamp = result.consult_response.data["context_compaction"]
    assert stamp["results_dropped"] == 2 and stamp["ceiling"] == ceiling
    assert stamp["input_tokens_before"] == total
    assert stamp["input_tokens_after"] <= ceiling
    frame = next(s for s in steps if s.get("kind") == "build_consult_response")
    assert frame["context_compaction"] == stamp
    forced = next(s for s in steps if s.get("kind") == "forced_final")
    assert forced["context_compaction"] == stamp
    plan = next(s for s in steps if s.get("kind") == "render_prompt")
    assert plan["context_ceiling_tokens"] == ceiling
    # The trace keeps the full record: four tool calls, all with their refs.
    assert [s["result"]["refs"] for s in steps if s.get("kind") == "tool_call"] == [10] * 4


@pytest.mark.asyncio
async def test_the_chat_path_answering_round_honours_the_ceiling_too():
    total, body = await _final_input_and_body_tokens()
    ceiling = total - int(1.5 * body)
    turns = _script() + [_Response(content=_FINAL)]

    llm, result, _steps = await _run({"context_ceiling_tokens": ceiling}, turns, rounds=6)

    answering = llm.calls[-1]
    assert answering.get("tools"), "the answer came off a native round, not a forced final"
    assert not result.consult_response.data.get("forced_final")
    assert cc.estimate_input_tokens(
        answering["system"], answering["messages"],
    ) <= ceiling
    bodies = _bodies_of(answering["messages"])
    assert _NAMED in bodies[0] and not _is_stub(bodies[0])
    assert result.consult_response.data["context_compaction"]["results_dropped"] >= 1


@pytest.mark.asyncio
async def test_under_the_ceiling_every_request_is_byte_identical_and_unstamped():
    off_llm, off_result, off_steps = await _run({"context_ceiling_tokens": 0}, _script())
    on_llm, on_result, on_steps = await _run({}, _script())  # the code default (OFF)

    assert len(on_llm.calls) == len(off_llm.calls) == 5
    for on, off in zip(on_llm.calls, off_llm.calls):
        assert json.dumps(on, sort_keys=True) == json.dumps(off, sort_keys=True)
    assert "context_compaction" not in on_result.consult_response.data
    assert all("context_compaction" not in s for s in on_steps)
    assert on_result.consult_response.answer == off_result.consult_response.answer


# ---------------------------------------------------------------------------
# 3. The recovery replay honours the same ceiling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_recovery_replay_honours_the_ceiling_with_the_turns_cited_refs():
    cited = _uuid("turn-cited")
    messages = [
        {"role": "user", "content": "what is happening?"},
        *_anthropic_round(1, [_body([_uuid("old")], 6000), _body([cited], 6000)]),
        *_anthropic_round(2, [_body([_uuid("new")], 6000)]),
        {"role": "user", "content": "Round cap reached."},
    ]
    transcript = {
        "version": 1, "usable": True, "truncated": False,
        "system": "S", "messages": messages,
    }
    recovery = {
        "question": "what is happening?", "steps": [],
        "cited_refs": [cited], "replay_transcript": transcript,
    }
    steps: list[dict[str, Any]] = []

    async def _publish(step: dict[str, Any]) -> None:
        steps.append(step)

    llm = _LLM([])
    deps = ConsultOnDemandDeps(llm=llm, substrate=_Substrate(), step_publish=_publish)
    result = await run_method(
        [{"question": "q", "synthesize_from": recovery}],
        {"context_ceiling_tokens": cc.estimate_input_tokens("S", messages) - 500},
        deps,
    )
    sent = _bodies_of(llm.calls[0]["messages"])
    assert _is_stub(sent[0]), "the oldest uncited result goes first"
    assert cited in sent[1] and not _is_stub(sent[1]), "the turn's cited result stays"
    assert not _is_stub(sent[2]), "the last round stays"
    data = result.consult_response.data
    assert data["context_compaction"]["results_dropped"] == 1
    assert "context ceiling" in data["replay_note"]
    frame = next(s for s in steps if s.get("kind") == "resynthesis_uncertainty")
    assert frame["context_compaction"] == data["context_compaction"]


# ---------------------------------------------------------------------------
# 4. The 7c3e91a2 replay
# ---------------------------------------------------------------------------

#: Session 7c3e91a2's persisted turn — the tracked fixture the divergence suite
#: already reads.
_FIXTURE = (
    Path(__file__).parent / "fixtures" / "consult_divergence" / "sample"
    / "7c3e91a2_model_a.json"
)

#: The answer's 85 cited refs, classified on the live database (read-only,
#: 2026-10-03): 52 signals, 19 findings, 8 situations, 5 hypotheses, 1 event.
#: Which CALL returned each is not recorded, so they are placed in the family's
#: OLDEST results first — the placement that makes "oldest first" and "keep the
#: cited" pull hardest against each other.
_CITED_BY_FAMILY = {
    ("search_corpus", "vector_search", "inspect_event"): 52,
    ("list_findings",): 19,
    ("list_situations",): 8,
    ("query_hypotheses",): 5,
    ("query_events",): 1,
}


def _trace() -> tuple[list[int], list[int], list[list[tuple[str, int]]], list[str]]:
    """Per-call prompt / completion tokens, per-round calls, cited ids.

    ``usage`` on each ``llm_call`` frame is the RUN's running total, so a
    call's own figures are the differences between consecutive frames.
    """
    turn = json.loads(_FIXTURE.read_text())[1]
    steps = turn["steps"]
    cumulative = [
        (s["usage"]["input_tokens"], s["usage"]["output_tokens"])
        for s in steps if s.get("kind") == "llm_call"
    ]
    prompt = [b[0] - a[0] for a, b in zip([(0, 0), *cumulative], cumulative)]
    completion = [b[1] - a[1] for a, b in zip([(0, 0), *cumulative], cumulative)]
    rounds: dict[int, list[tuple[str, int]]] = {}
    for s in steps:
        if s.get("kind") == "tool_call":
            rounds.setdefault(s["round"], []).append((s["tool"], s["result"]["refs"]))
    cited = [str(c["id"]).lower() for c in turn["cited_refs"]]
    return prompt, completion, [rounds[r] for r in sorted(rounds)], cited


def test_the_7c3e91a2_answering_call_read_137k_not_621k():
    """The review's "final input 621k" is the run's running sum."""
    prompt, completion, rounds, cited = _trace()
    assert len(prompt) == 9 and len(rounds) == 8 and len(cited) == 85
    assert sum(prompt) == 621_505
    assert prompt[-1] == 136_833
    assert sum(prompt) + sum(completion) == 641_400  # the "641k tokens"


def _replay_7c3e91a2() -> tuple[list[dict[str, Any]], set[str], int]:
    prompt, completion, rounds, cited_ids = _trace()
    budget = dict(_CITED_BY_FAMILY)
    pool = list(cited_ids)
    cited: set[str] = set()
    messages: list[dict[str, Any]] = [{"role": "user", "content": "q" * (prompt[0] * 4)}]
    for r, calls in enumerate(rounds):
        # Round r's results = the growth into call r+1 less call r's own output.
        result_tokens = prompt[r + 1] - prompt[r] - completion[r]
        per_call_chars = result_tokens * 4 // len(calls)
        bodies: list[str] = []
        for c, (tool, nrefs) in enumerate(calls):
            family = next((f for f in budget if tool in f), None)
            take = min(nrefs, budget.get(family, 0)) if family else 0
            if family:
                budget[family] -= take
            refs = [pool.pop(0) for _ in range(take)]
            cited.update(refs)
            refs += [_uuid("7c3e91a2", r, c, k) for k in range(nrefs - take)]
            head = json.dumps({"tool": tool, "refs": refs, "rows": []})
            pad = max(0, per_call_chars - len(head) - 2)
            bodies.append(head[:-2] + '"' + "z" * pad + '"]}')
        round_msgs = _anthropic_round(r + 1, bodies)
        round_msgs[0]["content"].insert(
            0, {"type": "text", "text": "t" * (completion[r] * 4)},
        )
        messages.extend(round_msgs)
    assert sum(budget.values()) == 0 and not pool, "all 85 cited refs placed"
    return messages, cited, prompt[-1]


ANTHROPIC_PLANE_CEILING = 160_000  # the value to set on a 200k-window plane


def test_the_7c3e91a2_replay_sits_under_the_anthropic_plane_ceiling_untouched():
    messages, cited, answering = _replay_7c3e91a2()
    assert len(cited) == 85
    estimate = cc.estimate_input_tokens("", messages)
    # The replay reproduces the answering call's size (136,833 tokens) within 2%.
    assert abs(estimate - answering) / answering < 0.02
    # At 160k (the Anthropic-plane setting) AND at the code default (off),
    # the request is byte-identical and nothing is stamped.
    for ceiling in (ANTHROPIC_PLANE_CEILING, cc.resolve_ceiling({})):
        out, stamp = cc.apply_context_ceiling(
            messages, system="", ceiling=ceiling, cited_refs=cited,
        )
        assert out is messages and stamp is None


@pytest.mark.parametrize("ceiling", [100_000, 64_000])
def test_the_7c3e91a2_replay_under_a_biting_ceiling_keeps_every_cited_result(ceiling):
    messages, cited, _answering = _replay_7c3e91a2()
    out, stamp = cc.apply_context_ceiling(
        messages, system="", ceiling=ceiling, cited_refs=cited,
    )
    assert stamp is not None
    assert stamp["input_tokens_after"] <= ceiling
    before_bodies, after_bodies = _bodies_of(messages), _bodies_of(out)
    dropped = [i for i, b in enumerate(after_bodies) if _is_stub(b)]
    assert stamp["results_dropped"] == len(dropped) > 0
    for i, body in enumerate(before_bodies):
        if any(ref in body for ref in cited):
            assert after_bodies[i] == body, f"cited result {i} must be kept whole"
    # The last round (three calls) is never withheld.
    assert all(not _is_stub(b) for b in after_bodies[-3:])
    # Oldest first: every kept UNCITED result is newer than every dropped one,
    # except where the ceiling was reached before reaching it.
    uncited_kept = [
        i for i, b in enumerate(after_bodies[:-3])
        if not _is_stub(b) and not any(ref in b for ref in cited)
    ]
    assert not uncited_kept or min(uncited_kept) > max(dropped)


def test_the_code_default_is_off_per_the_2026_09_16_revert():
    # The operator reverted every capacity cut on 2026-09-16; the ceiling is
    # opt-in per descriptor, never a silent default on a live plane.
    assert cc.DEFAULT_CONTEXT_CEILING_TOKENS == 0
    assert cc.resolve_ceiling({}) is None
    assert cc.resolve_ceiling(None) is None
