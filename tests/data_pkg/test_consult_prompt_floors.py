# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult prompt's run context and the answer's EVIDENCE FLOOR.

CONSULT_REVIEW_2026-09-28 §A / §B.4, the four things it found:

  * ``series_history`` / ``series_compare`` registered, granted, in
    ``_KNOWN_TOOLS``, on the native wire — and **invoked zero times, ever**,
    because the hand-written prompt catalogue stopped at ``list_sources`` and
    nothing ever said the platform held anything to read;
  * the ``substrate_read`` pack's own ``prompt_fragments`` and ``rules``
    injected only on the inline_target GATHER binding, never on the consult
    binding that is governed through the same pack;
  * ``uncertainty`` = the model's own header number, clamped and nothing else
    (a live session: two tool calls, one carrying a fabricated result in its
    args, published at ``0.30``);
  * "what would change my mind" as prose only, with nothing structured for a
    later change to turn into ``claim_watch`` rows.

The pins below are properties, not wording: the floor bands, the absence
lines, and the rule that an UNMEASURED run is never floored — because
flooring one would be the same fabrication pointing the other way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import UUID, uuid4

import pytest

from legba.data.analysts import consult_prompt_context as pctx
from legba.data.analysts.consult_evidence_floor import (
    UNCERTAINTY_FLOOR_BANDS,
    apply_uncertainty_floor,
    resolved_ref_count,
    uncertainty_floor_for,
)
from legba.data.analysts.consult_on_demand import (
    FINAL_SENTINEL,
    ConsultOnDemandDeps,
    _SYSTEM_PROMPT,
    run_method,
)
from legba.data.analysts.consult_reply_parsing import _parse_sentinel_final
from legba.data.provenance.models import ConsultResponsePayload


# ---------------------------------------------------------------------------
# Doubles — the substrate boundary stays real in shape, the LLM is scripted
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    prompt_tokens: int = 10
    completion_tokens: int = 5
    reasoning_tokens: int = 0


@dataclass
class _Response:
    content: str = ""
    usage: _Usage | None = None


class _ScriptedLLM:
    subprovider = "vllm-test"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append({"messages": messages, "system": system})
        content = self._responses.pop(0) if self._responses else json.dumps(
            {"final": True, "answer": "(exhausted)", "uncertainty": 1.0}
        )
        return _Response(content=content, usage=_Usage())


@dataclass
class _Substrate:
    """Only the surface these tests exercise.

    The substrate that HAS both readers. ``_NoReaderSubstrate`` below is the
    hand-built-embedder case — neither a holdings catalogue nor a ref
    classifier — which must keep the prompt and the uncertainty exactly as
    they were before this change.
    """

    signal_refs: list[UUID] = field(default_factory=list)
    holdings: dict[str, Any] | None = None
    holdings_raises: bool = False
    resolved: int | None = None
    classify_calls: int = 0

    async def search_signals(
        self, *, query: str, limit: int = 20, scope_predicate: str | None = None
    ) -> dict[str, Any]:
        return {
            "rows": [{"id": str(r), "title": "row"} for r in self.signal_refs],
            "refs": [str(r) for r in self.signal_refs],
            "count": len(self.signal_refs),
        }

    async def series_holdings(self, *, limit: int = 200) -> dict[str, Any]:
        if self.holdings_raises:
            raise RuntimeError("holdings read failed")
        return self.holdings or {"series": [], "series_count": 0, "subjects": []}

    async def classify_cited_refs(self, *, refs: list[str]) -> dict[str, Any]:
        self.classify_calls += 1
        n = len(refs) if self.resolved is None else self.resolved
        return {
            "by_origin_class": {"live": n},
            "resolved": n,
            "unresolved": max(0, len(refs) - n),
            "asked": len(refs),
        }


class _NoReaderSubstrate:
    """A substrate with neither a holdings reader nor a ref classifier."""

    async def search_signals(
        self, *, query: str, limit: int = 20, scope_predicate: str | None = None
    ) -> dict[str, Any]:
        return {"rows": [], "refs": [], "count": 0}


@dataclass
class _Identity:
    id: str = "substrate_read"


@dataclass
class _Pack:
    prompt_fragments: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    identity: _Identity = field(default_factory=_Identity)


@dataclass
class _Binding:
    pack: _Pack


def _payload(**kw: Any) -> ConsultResponsePayload:
    base: dict[str, Any] = {
        "question": "what is going on?",
        "answer": "## Bottom line\n\nsomething",
        "cited_substrate_refs": [],
        "uncertainty": 0.3,
        "unanswered_aspects": [],
        "data": {"rounds_used": 1},
    }
    base.update(kw)
    return ConsultResponsePayload(**base)


# ---------------------------------------------------------------------------
# 1 · The catalogue — the two series tools the prompt never named
# ---------------------------------------------------------------------------


def test_the_prompt_catalogue_now_names_both_series_tools():
    """Registered, granted, on the wire and invoked zero times: the catalogue
    stopped at list_sources."""
    assert "series_history(series_id, subject, since, until" in _SYSTEM_PROMPT
    assert "series_compare(series_id, subjects, since, until" in _SYSTEM_PROMPT
    # And it says what they are FOR, so a question about the past as numbers
    # routes to them instead of to model knowledge.
    assert "ONLY tools that read curated history" in _SYSTEM_PROMPT


def test_the_prompt_teaches_the_valid_period_obligation():
    """A number valid for 2016 is not evidence about now."""
    assert "HISTORY, never current reporting" in _SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 2 · The holdings line — present, absent, and unmeasured are three lines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_holdings_line_names_the_count_the_subjects_and_the_ids():
    substrate = _Substrate(
        holdings={
            "series": [
                {"series_id": "gdp_current_usd", "subjects": ["IL", "US"]},
                {"series_id": "population_total", "subjects": ["IR", "UA"]},
            ],
            "series_count": 2,
            "subjects": ["IL", "IR", "UA", "US"],
        }
    )
    block = await pctx.render_run_context(substrate=substrate, agency_binding=None)
    assert "Curated series held: 2 series for IL, IR, UA, US" in block
    assert "gdp_current_usd" in block and "population_total" in block
    assert "series_history(series_id, subject" in block
    assert pctx.RUN_CONTEXT_HEADER in block


@pytest.mark.asyncio
async def test_nothing_loaded_renders_the_honest_absence_never_a_zero():
    substrate = _Substrate(holdings={"series": [], "series_count": 0, "subjects": []})
    block = await pctx.render_run_context(substrate=substrate, agency_binding=None)
    assert "no curated series loaded" in block
    assert "0 series" not in block


@pytest.mark.asyncio
async def test_a_failed_holdings_read_says_NOT_MEASURED_not_none_loaded():
    """A read that failed is not evidence that the store is empty."""
    substrate = _Substrate(holdings_raises=True)
    block = await pctx.render_run_context(substrate=substrate, agency_binding=None)
    assert "NOT MEASURED" in block
    assert "no curated series loaded" not in block


@pytest.mark.asyncio
async def test_no_reader_and_no_binding_leaves_the_prompt_untouched():
    """A hand-built embedder's prompt is byte-identical to what it was."""
    block = await pctx.render_run_context(
        substrate=_NoReaderSubstrate(), agency_binding=None
    )
    # Nothing else to say: only the banner with today's date.
    assert block.strip().startswith(pctx.RUN_CONTEXT_HEADER + " Today is ")
    assert block.strip().endswith("(UTC).") and "\n" not in block.strip()
    frozen = await pctx.render_run_context(
        substrate=_NoReaderSubstrate(), agency_binding=None, frozen=True
    )
    assert frozen == ""


# ---------------------------------------------------------------------------
# 3 · The pack's own fragments + rules reach the consult prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pack_prompt_fragments_and_rules_are_injected_from_the_descriptor():
    binding = _Binding(
        pack=_Pack(
            prompt_fragments=["For the PAST as numbers:\n  series_history reads one."],
            rules=["Never fabricate substrate refs."],
        )
    )
    block = await pctx.render_run_context(
        substrate=_NoReaderSubstrate(), agency_binding=binding
    )
    assert "substrate_read" in block
    # Folded YAML blocks arrive multi-line; a prompt bullet is ONE line.
    assert "- For the PAST as numbers: series_history reads one." in block
    assert "Pack rules:" in block
    assert "- Never fabricate substrate refs." in block


@pytest.mark.asyncio
async def test_the_rendered_run_context_reaches_the_system_turn_of_a_real_run():
    """Through run_method, not by calling the renderer: the injection is only
    real if the LLM's `system` actually carries it."""
    substrate = _Substrate(
        holdings={
            "series": [{"series_id": "gdp_current_usd", "subjects": ["US"]}],
            "series_count": 1,
            "subjects": ["US"],
        }
    )
    llm = _ScriptedLLM([
        json.dumps({"final": True, "answer": "done", "uncertainty": 0.9})
    ])
    deps = ConsultOnDemandDeps(
        llm=llm,
        substrate=substrate,
        agency_binding=_Binding(pack=_Pack(rules=["Cite only returned UUIDs."])),
    )
    result = await run_method([{"question": "q"}], {}, deps)

    system = llm.calls[0]["system"]
    assert "Curated series held: 1 series for US" in system
    assert "Cite only returned UUIDs." in system
    # And the run's FIRST trace frame says the block rendered, so a live run
    # is checkable from its trace rather than from the prompt it did not keep.
    plan = [s for s in result.intermediate_steps if s.get("kind") == "render_prompt"]
    assert plan and plan[0]["run_context_chars"] > 0


# ---------------------------------------------------------------------------
# 4 · The ARC-WINDOW rule and the list_situations ranking sentences
# ---------------------------------------------------------------------------


def test_the_arc_window_rule_is_in_the_survey_block_with_a_90_day_floor():
    assert "query_events(since = today minus 90 days)" in _SYSTEM_PROMPT
    assert "list_situations(as_of = today minus 90 days)" in _SYSTEM_PROMPT
    assert "since_hours=2160" in _SYSTEM_PROMPT
    # The platform names the window in force at the head of the answer; the
    # model declares a reach beyond it as wider reach.
    assert "NAME the window you actually used" not in _SYSTEM_PROMPT
    assert "say so in the contract block's wider_reach" in _SYSTEM_PROMPT
    # It sits in the plan, after round 1 and before the tool catalogue.
    plan = _SYSTEM_PROMPT.index("Plan.\nRound 1")
    tools = _SYSTEM_PROMPT.index("Tools: records.")
    assert plan < _SYSTEM_PROMPT.index("A question about change over time") < tools


def test_the_prompt_describes_the_ranking_list_situations_actually_does():
    """The review: ORDER BY updated_at DESC, restamped hourly by the
    re-cluster, so the prompt promised an intensity ranking it did not do."""
    assert "Rows are ordered by intensity_score" in _SYSTEM_PROMPT
    assert "then by most recent event" in _SYSTEM_PROMPT
    assert "measures activity, not importance or likelihood of change" in _SYSTEM_PROMPT
    assert "it ranks the live frames by intensity_score and event_count" not in (
        _SYSTEM_PROMPT
    )


# ---------------------------------------------------------------------------
# 5 · The evidence floor — the table, both sides, and the unmeasured case
# ---------------------------------------------------------------------------


def test_the_floor_table_has_one_band_per_step():
    assert UNCERTAINTY_FLOOR_BANDS == ((1, 0.85), (5, 0.60), (15, 0.45))


@pytest.mark.parametrize(
    "resolved,expected",
    [
        (0, 0.85),
        (1, 0.60),
        (4, 0.60),
        (5, 0.45),
        (14, 0.45),
        (15, None),
        (80, None),
        (None, None),
    ],
)
def test_every_band(resolved, expected):
    assert uncertainty_floor_for(resolved) == expected


def test_the_floor_never_lowers_a_number():
    """It corrects false confidence; it does not manufacture confidence."""
    consult = _payload(uncertainty=0.95)
    assert apply_uncertainty_floor(consult, resolved=0) is consult


def test_an_applied_floor_is_stamped_with_from_to_and_resolved():
    consult = _payload(uncertainty=0.30)
    out = apply_uncertainty_floor(consult, resolved=0)
    assert out.uncertainty == pytest.approx(0.85)
    assert out.data["uncertainty_floor_applied"] == {
        "from": 0.30, "to": 0.85, "resolved": 0,
    }
    # The high-uncertainty invariant holds for a SERVER-declared number too.
    assert out.unanswered_aspects == [consult.question]


def test_an_unapplied_floor_leaves_no_stamp():
    """The key's name is a claim that something happened."""
    out = apply_uncertainty_floor(_payload(uncertainty=0.5), resolved=20)
    assert "uncertainty_floor_applied" not in out.data


def test_an_unmeasured_run_is_never_floored():
    consult = _payload(uncertainty=0.1)
    assert apply_uncertainty_floor(consult, resolved=None) is consult


@pytest.mark.asyncio
async def test_resolved_count_is_measured_zero_when_the_answer_cites_nothing():
    """The 0.85 band's case: a classifier IS present and the answer cites
    nothing. That is a measurement, not an absence of one."""
    classification, resolved = await resolved_ref_count(_Substrate(), [])
    assert (classification, resolved) == (None, 0)


@pytest.mark.asyncio
async def test_no_classifier_means_unmeasured_not_zero():
    classification, resolved = await resolved_ref_count(_NoReaderSubstrate(), ["x"])
    assert (classification, resolved) == (None, None)


@pytest.mark.asyncio
async def test_the_floor_bites_on_a_real_run_and_the_census_shares_the_read():
    """The recorded shape: a confident header over two resolved refs."""
    refs = [uuid4(), uuid4()]
    substrate = _Substrate(signal_refs=refs)
    llm = _ScriptedLLM([
        json.dumps({"tool": "search_signals", "args": {"query": "x"}}),
        json.dumps({
            "final": True,
            "answer": "## Bottom line\n\nconfident",
            "uncertainty": 0.30,
            "cited_refs": [str(r) for r in refs],
        }),
    ])
    result = await run_method(
        [{"question": "q"}],
        {},
        ConsultOnDemandDeps(llm=llm, substrate=substrate),
    )
    consult = result.consult_response
    assert consult.uncertainty == pytest.approx(0.60)      # 2 resolved -> <5
    assert consult.data["uncertainty_floor_applied"]["resolved"] == 2
    assert consult.data["uncertainty_floor_applied"]["from"] == pytest.approx(0.30)
    # The finding's confidence mirrors the FLOORED number, not the claimed one.
    assert result.finding.confidence == pytest.approx(0.40)
    # ONE classify read serves both the floor and the census.
    assert substrate.classify_calls == 1
    assert result.consult_response.data["provenance_census"]["live"] == 2


@pytest.mark.asyncio
async def test_a_run_with_no_classifier_keeps_the_models_own_number():
    llm = _ScriptedLLM([
        json.dumps({"final": True, "answer": "a", "uncertainty": 0.2})
    ])
    result = await run_method(
        [{"question": "q"}],
        {},
        ConsultOnDemandDeps(llm=llm, substrate=_NoReaderSubstrate()),
    )
    assert result.consult_response.uncertainty == pytest.approx(0.2)
    assert "uncertainty_floor_applied" not in result.consult_response.data


@pytest.mark.asyncio
async def test_the_floor_covers_the_resynthesis_path():
    """An answer written over evidence someone already paid for is exactly as
    thin as the evidence is."""
    refs = [uuid4()]
    substrate = _Substrate(signal_refs=refs)
    llm = _ScriptedLLM([
        f"{FINAL_SENTINEL}\nuncertainty: 0.15\ncited_refs: {refs[0]}\n\n"
        "## Bottom line\n\nrecovered"
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)
    result = await run_method(
        [{
            "question": "q",
            "synthesize_from": {
                "question": "q",
                "steps": [],
                "cited_refs": [str(refs[0])],
                "replay_transcript": {
                    "version": 1,
                    "usable": True,
                    "system": "recorded system turn",
                    "messages": [{"role": "user", "content": "q"}],
                },
            },
        }],
        {},
        deps,
    )
    consult = result.consult_response
    assert consult.data["replay_fidelity"] == "exact"
    assert consult.uncertainty == pytest.approx(0.60)       # 1 resolved -> <5
    assert consult.data["uncertainty_floor_applied"]["resolved"] == 1


@pytest.mark.asyncio
async def test_both_B4_facts_ride_the_DURABLE_step_trace():
    """The payload's ``data`` bag dies at the consult front door —
    ``consult_turns`` has no column for it and the chat path writes no
    analyst_outputs row — so the floor stamp and the watch candidates are also
    on the reflect FRAME, which is what a turn persists."""
    refs = [uuid4()]
    llm = _ScriptedLLM([
        json.dumps({"tool": "search_signals", "args": {"query": "x"}}),
        f"{FINAL_SENTINEL}\nuncertainty: 0.1\ncited_refs: {refs[0]}\n"
        "watch_candidates: the vote slips\n\n## Bottom line\n\nbody",
    ])
    result = await run_method(
        [{"question": "q"}],
        {},
        ConsultOnDemandDeps(llm=llm, substrate=_Substrate(signal_refs=refs)),
    )
    frame = next(
        s for s in result.intermediate_steps
        if s.get("kind") == "build_consult_response"
    )
    assert frame["resolved_refs"] == 1
    assert frame["uncertainty_floor_applied"]["to"] == pytest.approx(0.60)
    assert frame["watch_candidates"] == ["the vote slips"]


def test_the_prompt_carries_the_models_half_of_the_floor():
    assert "If you cite fewer than five rows, or your rows share one originator" in _SYSTEM_PROMPT
    assert "If you cite nothing, set uncertainty to at least 0.85" in _SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 6 · Confidence-with-a-resolution-test, and watch_candidates
# ---------------------------------------------------------------------------


def test_the_prompt_allows_a_number_only_beside_a_resolution_test():
    assert "Write a likelihood as a number in three places only" in _SYSTEM_PROMPT
    assert "Everywhere else use the likelihood words" in _SYSTEM_PROMPT


def test_watch_candidates_is_a_header_line_in_the_contract():
    assert "watch_candidates:" in _SYSTEM_PROMPT
    assert "at most 8" in _SYSTEM_PROMPT


def test_watch_candidates_parses_off_the_sentinel_header():
    payload = _parse_sentinel_final(
        f"{FINAL_SENTINEL}\n"
        "uncertainty: 0.4\n"
        "watch_candidates: a second outlet names the successor; the vote slips\n"
        "\n## Bottom line\n\nbody"
    )
    assert payload is not None
    assert payload["watch_candidates"] == [
        "a second outlet names the successor", "the vote slips",
    ]


@pytest.mark.asyncio
async def test_watch_candidates_reach_the_payload_capped_at_eight():
    items = "; ".join(f"indicator {i}" for i in range(12))
    llm = _ScriptedLLM([
        f"{FINAL_SENTINEL}\nuncertainty: 0.9\nwatch_candidates: {items}\n\n"
        "## Bottom line\n\nbody"
    ])
    result = await run_method(
        [{"question": "q"}],
        {},
        ConsultOnDemandDeps(llm=llm, substrate=_NoReaderSubstrate()),
    )
    watch = result.consult_response.data["watch_candidates"]
    assert len(watch) == 8
    assert watch[0] == "indicator 0"


@pytest.mark.asyncio
async def test_no_watch_line_means_the_key_is_absent_never_an_empty_list():
    """An empty list would read as "nothing would change this call"."""
    llm = _ScriptedLLM([
        json.dumps({"final": True, "answer": "a", "uncertainty": 0.9})
    ])
    result = await run_method(
        [{"question": "q"}],
        {},
        ConsultOnDemandDeps(llm=llm, substrate=_NoReaderSubstrate()),
    )
    assert "watch_candidates" not in result.consult_response.data
