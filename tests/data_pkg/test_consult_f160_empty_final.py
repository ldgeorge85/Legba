# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W11-F160 — consult turn 91b6260c stored its header text as the answer.

2026-11-05 23:06Z, session 567daed8. The forced final wrote ``<<<FINAL>>>``,
``uncertainty: 0.55`` and then a ``cited_refs:`` line that listed ~101 distinct
ids and looped until the 32,768-token output cap: 969 ids, 102 unique, cut
mid-uuid, no blank line, no prose. ``_parse_sentinel_final`` returned None on
the empty body, the salvage stored ``body or text`` — the header text — as the
answer, and ``synthesis_status`` stayed ``"complete"``.

Pinned here:

1. A header-only final is NO answer: empty answer, ``synthesis_status="none"``,
   re-synthesizable, the evidence (replay transcript) kept for the button.
2. A final cut at the output cap (``finish_reason="length"``, or completion
   tokens at the cap when no reason came back) is ``"partial"`` with its prose.
3. ``cited_refs`` parse ordered-unique, stop at the first repeat, drop a cut
   trailing fragment, and keep at most 30 — and a degenerate header line does
   not hide prose written after it.
4. A normal answer parses exactly as before.
5. The prompts say so (W11-F160 + the W11-F162 ledger line).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from legba.data.analysts import consult_round_protocol as _cp
from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps, run_method
from legba.data.analysts.consult_reply_parsing import (
    CITED_REFS_MAX,
    FINAL_SENTINEL,
    _parse_sentinel_final,
    final_payload_from_text,
    split_ref_tokens,
)

# ---------------------------------------------------------------------------
# The replayed shape
# ---------------------------------------------------------------------------

_IDS = [f"{i:08x}-7dc9-435e-b426-{i:012x}" for i in range(1, 103)]
_CUT_FRAGMENT = "8050083e-7dc9-435e-b426-d"


def _looping_refs() -> list[str]:
    """969 ids, 102 unique, 101 unique within the first 102 — enumerate, then
    cycle (the 102nd distinct id turns up only inside the loop)."""
    seq = _IDS[:101] + [_IDS[0]]
    cycle = _IDS[:101] + [_IDS[101]]
    while len(seq) < 969:
        seq.extend(cycle)
    return seq[:969]


def _degenerate(prose: str = "") -> str:
    refs = ", ".join([*_looping_refs(), _CUT_FRAGMENT])
    text = f"{FINAL_SENTINEL}\nuncertainty: 0.55\ncited_refs: {refs}"
    return text + (f"\n\n{prose}" if prose else "")


def test_the_replayed_shape_is_what_the_incident_stored() -> None:
    seq = _looping_refs()
    assert len(seq) == 969
    assert len(set(seq)) == 102
    assert len(set(seq[:102])) == 101


def test_a_header_only_final_has_no_answer_and_bounded_refs() -> None:
    raw = _degenerate()
    assert _parse_sentinel_final(raw) is None
    payload = final_payload_from_text(raw)
    assert payload["answer"] == "", "header text must never be stored as the answer"
    assert payload["uncertainty"] == 0.55
    refs = payload["cited_refs"]
    assert 0 < len(refs) <= CITED_REFS_MAX == 30
    assert len({r.lower() for r in refs}) == len(refs)
    assert refs == _IDS[:30]
    ids, malformed = split_ref_tokens(refs)
    assert malformed == [] and len(ids) == len(refs)


def test_a_degenerate_header_line_still_finds_the_prose_after_it() -> None:
    raw = _degenerate("## Bottom line\nThe substance survived.")
    payload = final_payload_from_text(raw)
    assert payload["answer"] == "## Bottom line\nThe substance survived."
    assert payload["cited_refs"] == _IDS[:30]


def test_a_blank_line_between_headers_does_not_push_a_header_into_the_answer() -> None:
    refs = ", ".join(_looping_refs())
    for raw in (
        f"{FINAL_SENTINEL}\nuncertainty: 0.4\n\ncited_refs: {refs}",
        f"uncertainty: 0.4\n\ncited_refs: {refs}",  # no sentinel: the salvage path
    ):
        payload = final_payload_from_text(raw)
        assert payload["answer"] == ""
        assert payload["uncertainty"] == 0.4
        assert len(payload["cited_refs"]) == CITED_REFS_MAX


def test_a_bare_sentinel_is_no_answer_either() -> None:
    assert final_payload_from_text(f"{FINAL_SENTINEL}\n")["answer"] == ""


def test_cited_refs_stop_at_the_first_repeat_and_drop_a_cut_tail() -> None:
    a, b, c = _IDS[:3]
    head = f"{FINAL_SENTINEL}\ncited_refs: "
    parsed = _parse_sentinel_final(f"{head}{a}, {b}, {a}, {c}\n\nbody")
    assert parsed["cited_refs"] == [a, b]
    parsed = _parse_sentinel_final(f"{head}{a}, {b}, {c[:20]}\n\nbody")
    assert parsed["cited_refs"] == [a, b]
    parsed = _parse_sentinel_final(f"{head}{a}, {b}, {c[:5]}\n\nbody")
    assert parsed["cited_refs"] == [a, b]
    # A short NON-uuid label is still reported downstream, not dropped.
    parsed = _parse_sentinel_final(f"{head}{a}, sit-001\n\nbody")
    assert parsed["cited_refs"] == [a, "sit-001"]
    many = ", ".join(_IDS[:45])
    assert _parse_sentinel_final(f"{head}{many}\n\nbody")["cited_refs"] == _IDS[:30]


# ---------------------------------------------------------------------------
# A normal answer is unchanged (regression)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            f"{FINAL_SENTINEL}\nuncertainty: 0.35\ncited_refs: {_IDS[0]}, {_IDS[1]}\n"
            "unanswered_aspects: one gap; another gap\n"
            "watch_candidates: a vote by 2026-11-30\n\n## Bottom line\nProse [1].\n",
            {
                "final": True, "uncertainty": 0.35,
                "cited_refs": [_IDS[0], _IDS[1]],
                "unanswered_aspects": ["one gap", "another gap"],
                "watch_candidates": ["a vote by 2026-11-30"],
                "answer": "## Bottom line\nProse [1].",
            },
        ),
        (
            f"{FINAL_SENTINEL}\ncited_refs: 【{_IDS[0]}】, [{_IDS[1]}], sit-001\n\nbody",
            {
                "final": True,
                "cited_refs": [f"【{_IDS[0]}】", f"[{_IDS[1]}]", "sit-001"],
                "answer": "body",
            },
        ),
        (
            "uncertainty: 0.2\n\nA headerless salvage keeps its prose.",
            {
                "final": True, "uncertainty": 0.2,
                "answer": "A headerless salvage keeps its prose.",
            },
        ),
        (
            "Plain prose with no headers at all.",
            {"final": True, "uncertainty": 0.6, "answer": "Plain prose with no headers at all."},
        ),
    ],
)
def test_a_normal_answer_parses_exactly_as_before(raw: str, expected: dict[str, Any]) -> None:
    assert final_payload_from_text(raw) == expected


# ---------------------------------------------------------------------------
# The loop: status + evidence
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
    finish_reason: Any = "stop"
    tool_calls: list[_ToolCall] = field(default_factory=list)
    usage: _Usage = field(default_factory=_Usage)
    raw_response: dict[str, Any] | None = None


class _LLM:
    """Native (Anthropic-wire) double: round 1 calls a tool, then ``final``."""

    PRICE_TABLE: dict[str, Any] = {}

    def __init__(self, final: _Response, *, tool_first: bool = True) -> None:
        self.subprovider = "anthropic"
        self._final = final
        self._tool_first = tool_first
        self.calls: list[bool] = []

    async def chat_complete(self, messages: Any, **kwargs: Any) -> _Response:
        self.calls.append(bool(kwargs.get("tools")))
        if self._tool_first and len(self.calls) == 1:
            return _Response(
                finish_reason="tool_calls",
                tool_calls=[_ToolCall("call-0", "search_signals", {"query": "iran"})],
            )
        return self._final


class _Substrate:
    async def search_signals(self, **kwargs: Any) -> dict[str, Any]:
        return {"count": 1, "rows": [{"id": "sig-1", "title": "t"}], "refs": []}


async def _run(llm: _LLM, *, rounds: int = 1, max_tokens: int = 32768):
    steps: list[dict[str, Any]] = []

    async def _publish(step: dict[str, Any]) -> None:
        steps.append(step)

    deps = ConsultOnDemandDeps(
        llm=llm, substrate=_Substrate(), step_publish=_publish, max_tokens=max_tokens,
    )
    result = await run_method(
        [{"question": "what is due?", "max_tool_rounds": rounds}], {}, deps,
    )
    return result.consult_response, steps


def _step(steps: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    return next((s for s in steps if s.get("kind") == kind), None)


@pytest.mark.asyncio
async def test_a_header_only_forced_final_is_not_synthesized() -> None:
    final = _Response(
        content=_degenerate(), finish_reason="length",
        usage=_Usage(prompt_tokens=143_677, completion_tokens=32_768),
    )
    llm = _LLM(final)
    resp, steps = await _run(llm)

    assert llm.calls == [True, False], "one drilling round + one forced final"
    assert resp.answer == ""
    assert "cited_refs" not in resp.answer and "uncertainty" not in resp.answer
    assert resp.data["synthesis_status"] == "none"
    assert resp.data["resynthesizable"] is True
    assert resp.data["replay_transcript"]["usable"] is True, (
        "the evidence must stay available for Synthesize from evidence"
    )
    step = _step(steps, "degraded_final")
    assert step is not None and step["reason"] == "empty_answer_body"
    assert step["finish_reason"] == "length" and step["output_cap_hit"] is True


@pytest.mark.asyncio
async def test_a_forced_final_cut_at_the_cap_is_partial_with_its_prose() -> None:
    final = _Response(
        content=(
            f"{FINAL_SENTINEL}\nuncertainty: 0.3\ncited_refs: {_IDS[0]}\n\n"
            "## Bottom line\nThe ceasefire is holding but the north is"
        ),
        finish_reason="length",
        usage=_Usage(completion_tokens=32_768),
    )
    resp, steps = await _run(_LLM(final))

    assert resp.data["synthesis_status"] == "partial"
    assert resp.data["resynthesizable"] is True
    assert "The ceasefire is holding but the north is" in resp.answer
    assert "Synthesis interrupted at" in resp.answer
    step = _step(steps, "partial_final")
    assert step is not None and step["reason"] == "output_token_cap"


@pytest.mark.asyncio
async def test_no_finish_reason_falls_back_to_the_completion_count() -> None:
    body = f"{FINAL_SENTINEL}\nuncertainty: 0.3\n\n## Bottom line\nCut here"
    capped = _Response(content=body, finish_reason=None, usage=_Usage(completion_tokens=4096))
    resp, _ = await _run(_LLM(capped), max_tokens=4096)
    assert resp.data["synthesis_status"] == "partial"

    # A reason that says it STOPPED wins over the count.
    stopped = _Response(content=body, finish_reason="stop", usage=_Usage(completion_tokens=4096))
    resp, _ = await _run(_LLM(stopped), max_tokens=4096)
    assert resp.data["synthesis_status"] == "complete"
    assert resp.answer == "## Bottom line\nCut here"


@pytest.mark.asyncio
async def test_a_header_only_answer_round_settles_none_without_a_second_burn() -> None:
    """Native route: a round with no tool call IS the answer. Header-only, it is
    no answer — and the forced final is NOT bought on top of it."""
    llm = _LLM(_Response(content=_degenerate(), finish_reason="length",
                         usage=_Usage(completion_tokens=32_768)), tool_first=False)
    resp, steps = await _run(llm, rounds=3)

    assert llm.calls == [True]
    assert resp.answer == ""
    assert resp.data["synthesis_status"] == "none"
    assert resp.data["resynthesizable"] is True
    assert _step(steps, "degraded_final")["reason"] == "empty_answer_body"


@pytest.mark.asyncio
async def test_a_normal_forced_final_is_complete_and_unchanged() -> None:
    final = _Response(
        content=f"{FINAL_SENTINEL}\nuncertainty: 0.25\n\n## Bottom line\nAll good.",
        usage=_Usage(completion_tokens=900),
    )
    resp, steps = await _run(_LLM(final))
    assert resp.data["synthesis_status"] == "complete"
    assert resp.data["resynthesizable"] is False
    assert resp.answer == "## Bottom line\nAll good."
    assert _step(steps, "degraded_final") is None and _step(steps, "partial_final") is None


def test_output_cap_hit_reads_length_or_the_count() -> None:
    assert _cp.output_cap_hit(_Response(finish_reason="length"), {}, 100)
    assert _cp.output_cap_hit(_Response(finish_reason=None), {"completion_tokens": 100}, 100)
    assert _cp.output_cap_hit(_Response(finish_reason="unknown"), {"completion_tokens": 120}, 100)
    assert not _cp.output_cap_hit(_Response(finish_reason=None), {"completion_tokens": 99}, 100)
    assert not _cp.output_cap_hit(_Response(finish_reason="stop"), {"completion_tokens": 500}, 100)
    assert not _cp.output_cap_hit(_Response(finish_reason=None), {"completion_tokens": 500}, None)


# ---------------------------------------------------------------------------
# The prompts
# ---------------------------------------------------------------------------


def test_the_prompts_carry_the_new_lines() -> None:
    from legba.data.analysts import consult_transcript as _tx
    from legba.data.analysts.consult_bakeoff_prompts import (
        RAW_SYSTEM_PROMPT,
        VERIFIED_SYSTEM_PROMPT,
    )
    from legba.data.analysts.consult_system_prompt import _SYSTEM_PROMPT

    contract = "AT MOST 30, each id ONCE, all on that one line"
    required = "The answer prose under that blank line is REQUIRED"
    ledger = (
        "There is no forecast-ledger or due-date resolver reader on this "
        "palette; if the question asks for one, say so once in the answer and "
        "use query_hypotheses / list_findings instead — do not search for it."
    )
    for prompt in (_SYSTEM_PROMPT, RAW_SYSTEM_PROMPT, VERIFIED_SYSTEM_PROMPT):
        assert contract in prompt and required in prompt
    assert ledger in _SYSTEM_PROMPT
    synth = _tx.synthesis_system(_SYSTEM_PROMPT, FINAL_SENTINEL)
    assert (
        "List at most 30 cited_refs, each once, on one line, then a blank line, "
        "then the answer prose — the answer prose is required."
    ) in synth
