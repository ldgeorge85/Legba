# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The journal_assessor's Wave-1 staged arc (plan §4.3 / §4.4 / §10).

No DB — every external dep is a stub (scripted LLM + fake governed binding). The
arc under test:

  GATHER → FIELD-NOTES seam (§4.3, in-voice cited handoff that REPLACES
  deep_consult's thin _evidence_brief) → tools-live NARRATE (§4.4) → REFLECT
  (§10, permissive per-claim citation flag — flag, never strip; perspective
  exempt) → DETERMINISTIC honesty post-step (§10, forced from substrate metrics
  even when the agent omits them).
"""

from __future__ import annotations

import logging
from typing import Any, Mapping
from uuid import uuid4

import pytest

from legba.data.analysts.agency.agency import AgencyOutcome
from legba.data.analysts.agency.tools import ToolResult
from legba.data.analysts.inline_target import InlineTargetDeps
from legba.data.analysts.journal_assessor import (
    NarrateToolCallLeakError,
    _apparatus_lead_flag,
    _extract_source_health_claims,
    _is_tool_call_leak,
    _reflect_claims,
    _rewrite_gathered_citations,
    _source_health_number_check,
    run_method,
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Usage:
    prompt_tokens = 10
    completion_tokens = 20
    reasoning_tokens = 0


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()


class _ScriptedLLM:
    """Pops scripted responses in order across GATHER + field-notes + narrate."""

    subprovider = "anthropic"

    def __init__(self, scripted: list[str]) -> None:
        self._scripted = list(scripted)
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
        content = self._scripted.pop(0) if self._scripted else '{"done": true}'
        return _Response(content)


class _FakeBinding:
    """Stand-in for the per-run journal_read AgencyToolBinding. ``run_tool``
    returns a canned admitted AgencyOutcome keyed by tool name."""

    def __init__(self, outputs: dict[str, dict[str, Any]] | None = None) -> None:
        self.outputs = outputs or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> AgencyOutcome:
        self.calls.append((tool_name, dict(args)))
        return AgencyOutcome(
            admitted=True,
            pack_id="journal_read",
            tool_name=tool_name,
            tool_result=ToolResult(
                status="completed",
                output=dict(self.outputs.get(tool_name, {})),
            ),
        )


# ---------------------------------------------------------------------------
# REFLECT — the permissive per-claim citation flag (§4.5 / §10)
# ---------------------------------------------------------------------------


def test_reflect_keeps_cited_factual_claim():
    ref = uuid4()
    body = f"The US-Iran nexus flipped polarity 11h ago [[ref:{ref}]]."
    claims, cited, flags = _reflect_claims(body)
    assert len(claims) == 1
    assert claims[0].kind == "fact"
    assert claims[0].refs == [ref]
    assert cited == [ref]
    assert flags == []  # a cited fact is not flagged


def test_reflect_flags_uncited_factual_claim_without_deleting():
    body = "Three country assessors went quiet this window."
    claims, cited, flags = _reflect_claims(body)
    assert len(claims) == 1
    # FLAGGED (kind fact, no ref, needs_citation marker) — but the text survives.
    assert claims[0].kind == "fact"
    assert claims[0].refs == []
    assert "needs_citation" in claims[0].text_span
    assert "country assessors went quiet" in claims[0].text_span  # NOT deleted
    assert "uncited_factual_span" in flags


def test_reflect_exempts_a_perspective_sentence():
    body = "I wonder whether the orphan plateau is actively maintained."
    claims, cited, flags = _reflect_claims(body)
    assert len(claims) == 1
    assert claims[0].kind == "perspective"
    assert claims[0].refs == []
    assert "needs_citation" not in claims[0].text_span  # exempt — not flagged
    assert flags == []


def test_reflect_speculation_marker_exempts_factual_looking_span():
    body = "The graph fractured into four camps [[spec]]."
    claims, cited, flags = _reflect_claims(body)
    assert claims[0].kind == "perspective"
    assert flags == []


def test_reflect_mixed_body_handles_each_span_independently():
    ref = uuid4()
    body = (
        f"The nexus flipped [[ref:{ref}]].\n\n"
        "Coverage dropped 30% this week.\n\n"
        "It makes me uneasy about what we're missing."
    )
    claims, cited, flags = _reflect_claims(body)
    kinds = [c.kind for c in claims]
    assert kinds == ["fact", "fact", "perspective"]
    assert cited == [ref]
    assert flags == ["uncited_factual_span"]  # only the middle span


# ---------------------------------------------------------------------------
# Full arc — field-notes seam → tools-live narrate → reflect → forced honesty
# ---------------------------------------------------------------------------


def _options(binding: Any) -> dict[str, Any]:
    return {
        "analyst_id": "journal_assessor",
        "agency_binding": binding,
        "gather_tool_bindings": {},
    }


@pytest.mark.asyncio
async def test_full_arc_emits_journal_with_forced_honesty_flags():
    ref = uuid4()
    # Calibration the honesty post-step will read: NOT-ready acute pilot + thin
    # exogenous → the port's get_calibration returns forecast_unproven/thin True.
    binding = _FakeBinding(outputs={
        "get_calibration": {
            "available": True,
            "forecast_unproven": True,
            "calibration_thin": True,
        },
    })
    # Scripted LLM: (1) GATHER round emits done; (2) field-notes; (3) narrate entry.
    # The entry deliberately OMITS any honesty caveat so the test proves the flags
    # are forced from the substrate, not parroted from the prose.
    scripted = [
        '{"done": true}',                                       # GATHER round 1
        f"Field notes: the nexus flipped [[ref:{ref}]].",       # field-notes seam
        f"# A quiet window\n\nThe nexus flipped [[ref:{ref}]].\n\n"  # narrate entry
        "I keep wondering what we're not seeing.",
    ]
    deps = InlineTargetDeps(
        llm=_ScriptedLLM(scripted),
        system_prompt="PERSONA",
        max_rounds=1,
        agency_binding=binding,
    )
    result = await run_method([{"title": "seed signal"}], _options(binding), deps)
    payload = result.finding
    # The entry was narrated.
    assert "nexus flipped" in payload.body
    # The cited factual span survived REFLECT with its ref bound.
    assert ref in payload.cited_substrate_refs
    fact_claims = [c for c in payload.claims if c.kind == "fact"]
    assert any(ref in c.refs for c in fact_claims)
    # The perspective sentence is a perspective claim (exempt).
    assert any(c.kind == "perspective" for c in payload.claims)
    # HONESTY: the flags were FORCED from the substrate calibration, even though
    # the narrative never mentioned them.
    assert set(payload.honesty_flags) == {"forecast_unproven", "calibration_thin"}
    # Off the chain.
    assert result.derived_from == []
    assert payload.entry_kind == "entry"
    # The honesty post-step consulted the substrate via the governed binding.
    assert any(name == "get_calibration" for name, _ in binding.calls)


@pytest.mark.asyncio
async def test_field_notes_seam_runs_between_gather_and_narrate():
    binding = _FakeBinding(outputs={"get_calibration": {
        "available": True, "forecast_unproven": True, "calibration_thin": True,
    }})
    scripted = [
        '{"done": true}',                 # GATHER
        "FIELD NOTES in my own voice",    # the seam (proves a distinct step exists)
        "The entry.",                     # narrate
    ]
    llm = _ScriptedLLM(scripted)
    deps = InlineTargetDeps(llm=llm, system_prompt="P", max_rounds=1, agency_binding=binding)
    result = await run_method([{"title": "s"}], _options(binding), deps)
    phases = [s.get("phase") for s in result.intermediate_steps]
    # The field_notes phase exists and sits between gather and narrate.
    assert "field_notes" in phases
    assert phases.index("gather") < phases.index("field_notes")
    assert phases.index("field_notes") < max(
        i for i, p in enumerate(phases) if p == "narrate"
    )


@pytest.mark.asyncio
async def test_narrate_keeps_tools_live_pulls_one_more_thread():
    """§4.4 — the narrate stage may emit a tool call; the loop runs it through the
    binding and asks again for the entry."""
    binding = _FakeBinding(outputs={
        "get_run_health": {"rows": [], "quiet_analysts": ["predictor"], "refs": []},
        "get_calibration": {
            "available": True, "forecast_unproven": True, "calibration_thin": True,
        },
    })
    scripted = [
        '{"done": true}',                                 # GATHER
        "field notes",                                    # seam
        '{"tool": "get_run_health", "args": {}}',         # narrate round 1: pull a thread
        "Final entry after checking run health.",         # narrate round 2: write it
    ]
    deps = InlineTargetDeps(
        llm=_ScriptedLLM(scripted), system_prompt="P", max_rounds=1, agency_binding=binding,
    )
    result = await run_method([{"title": "s"}], _options(binding), deps)
    assert "Final entry after checking run health" in result.finding.body
    # The narrate stage actually dispatched the live tool call.
    assert ("get_run_health", {}) in binding.calls
    narrate_tool_steps = [
        s for s in result.intermediate_steps
        if s.get("phase") == "narrate" and s.get("kind") == "tool_call"
    ]
    assert len(narrate_tool_steps) == 1


@pytest.mark.asyncio
async def test_honesty_conservative_when_no_binding():
    """No binding wired → the honesty post-step conservatively flags BOTH legs
    (absence of proof is not proof of skill)."""
    scripted = ["field notes", "An entry with no caveats at all."]
    deps = InlineTargetDeps(llm=_ScriptedLLM(scripted), system_prompt="P", max_rounds=1)
    result = await run_method([{"title": "s"}], {"analyst_id": "journal_assessor"}, deps)
    assert set(result.finding.honesty_flags) == {"forecast_unproven", "calibration_thin"}


@pytest.mark.asyncio
async def test_honesty_drops_flags_when_substrate_proves_the_leg():
    """When the substrate calibration says a leg IS proven, the corresponding flag
    is omitted — the post-step is forced FROM the metrics, not hardcoded."""
    binding = _FakeBinding(outputs={"get_calibration": {
        "available": True, "forecast_unproven": False, "calibration_thin": False,
    }})
    scripted = ['{"done": true}', "notes", "An honest entry."]
    deps = InlineTargetDeps(
        llm=_ScriptedLLM(scripted), system_prompt="P", max_rounds=1, agency_binding=binding,
    )
    result = await run_method([{"title": "s"}], _options(binding), deps)
    assert result.finding.honesty_flags == []


# ---------------------------------------------------------------------------
# V4 — the GATHER [N] → journal [[ref:uuid]] citation bridge
# ---------------------------------------------------------------------------


def _corpus_extension(n_to_uuid: dict[int, Any]) -> dict[int, dict[str, Any]]:
    """A minimal {N -> citation entry} map shaped like inline_target's GATHER
    citation-extension (only ``signal_id`` is load-bearing for the rewrite)."""
    return {
        n: {"signal_id": str(u), "title": f"doc {n}", "source": None,
            "snippet": None, "source_text": None, "source_truncated": False}
        for n, u in n_to_uuid.items()
    }


def test_v4_rewrites_gathered_ordinal_to_ref_marker():
    u = uuid4()
    body = "Grain corridor talks stalled again [1]."
    out, count = _rewrite_gathered_citations(body, _corpus_extension({1: u}))
    assert count == 1
    assert f"[[ref:{u}]]" in out
    assert "[1]" not in out


def test_v4_leaves_unmapped_ordinal_untouched_never_fabricates():
    # [2] has no extension entry — it must NOT be rewritten (no invented ref).
    u = uuid4()
    body = "First point [1]. Second point [2]."
    out, count = _rewrite_gathered_citations(body, _corpus_extension({1: u}))
    assert count == 1
    assert f"[[ref:{u}]]" in out
    assert "[2]" in out  # the unmapped ordinal survives verbatim


def test_v4_leaves_native_ref_markers_untouched():
    # The journal's native [[ref:uuid]] path (priming slice / instrument reads)
    # must be byte-for-byte unchanged — it never matches the ordinal regex.
    ref = uuid4()
    body = f"The nexus flipped [[ref:{ref}]]."
    out, count = _rewrite_gathered_citations(body, _corpus_extension({1: uuid4()}))
    assert count == 0
    assert out == body


def test_v4_normalizes_fullwidth_bracket_before_rewrite():
    # The core plane emits 【N】/［N］ non-deterministically — normalize FIRST so a
    # variant-bracketed gathered marker still rewrites (the fullwidth lesson).
    u = uuid4()
    body = "Heat strain in the Gulf 【1】 and rail stress ［1］."
    out, count = _rewrite_gathered_citations(body, _corpus_extension({1: u}))
    assert count == 2
    assert "【1】" not in out and "［1］" not in out
    assert out.count(f"[[ref:{u}]]") == 2


def test_v4_entry_with_no_extension_is_identity():
    body = "A plain entry with a stray [1] and no gathered docs."
    out, count = _rewrite_gathered_citations(body, {})
    assert count == 0
    assert out == body


def test_v4_no_ref_when_extension_entry_lacks_uuid():
    # An extension entry with no resolvable signal_id → leave the ordinal (a
    # corpus doc with no id is present-but-unmapped, never a fabricated ref).
    body = "Something happened [1]."
    ext = {1: {"signal_id": None, "title": "doc", "source": None,
               "snippet": None, "source_text": None, "source_truncated": False}}
    out, count = _rewrite_gathered_citations(body, ext)
    assert count == 0
    assert out == body


@pytest.mark.asyncio
async def test_v4_full_arc_gathered_corpus_doc_becomes_citable():
    """End-to-end: GATHER surfaces a corpus doc via search_corpus, the narrator
    cites it [1], and the post-NARRATE bridge turns it into a bound fact claim so
    it is journal-citable (J4 — the [N] no longer dies at render)."""
    doc_id = uuid4()
    binding = _FakeBinding(outputs={
        # The GATHER round's search_corpus returns one numbered corpus doc.
        "search_corpus": {"rows": [
            {"id": str(doc_id), "score": 9.1, "source": {
                "title": "Strait transit incident",
                "raw_body": "A tanker was detained overnight in the strait.",
            }},
        ]},
        "get_calibration": {
            "available": True, "forecast_unproven": True, "calibration_thin": True,
        },
    })
    scripted = [
        '{"tool": "search_corpus", "args": {"query": "strait"}}',   # GATHER round 1
        '{"done": true}',                                            # GATHER round 2
        "field notes: a tanker was detained [1].",                  # field-notes seam
        # narrate: cite the gathered corpus doc [1]
        "A tanker was detained overnight in the strait [1].",
    ]
    deps = InlineTargetDeps(
        llm=_ScriptedLLM(scripted), system_prompt="P", max_rounds=2, agency_binding=binding,
    )
    result = await run_method([{"title": "seed"}], _options(binding), deps)
    payload = result.finding
    # The gathered [N] was rewritten to a durable [[ref:uuid]] and bound as a fact.
    assert "[1]" not in payload.body
    assert f"[[ref:{doc_id}]]" in payload.body
    assert doc_id in payload.cited_substrate_refs
    fact_claims = [c for c in payload.claims if c.kind == "fact" and doc_id in c.refs]
    assert fact_claims, "the gathered corpus doc should be a bound fact claim"
    # The bridge recorded a trace step.
    assert any(
        s.get("kind") == "gathered_citation_bridge" for s in result.intermediate_steps
    )


# ---------------------------------------------------------------------------
# V2.2 — the DETERMINISTIC apparatus-lead honesty flag
# ---------------------------------------------------------------------------


def test_apparatus_lead_fires_on_checking_senses_opening():
    # The exact 07-23 regression shape.
    body = ("I start by checking the health of my senses this morning. "
            "Most feeds are fresh.")
    assert _apparatus_lead_flag(body) == ["apparatus_lead"]


def test_apparatus_lead_fires_on_opened_the_dashboard():
    body = "I opened the health dashboard first. Twelve feeds are wired."
    assert _apparatus_lead_flag(body) == ["apparatus_lead"]


def test_apparatus_lead_silent_on_world_first_opening():
    # The persona-compliant lead: names what the world did, apparatus is a
    # closing note.
    body = ("Khamenei signaled openness to talks overnight, the sharpest shift "
            "in weeks. My feeds stayed fresh throughout.")
    assert _apparatus_lead_flag(body) == []


def test_apparatus_lead_silent_on_world_first_with_start_open_verbs():
    # A WORLD lead that happens to use start/open/look-at framing (with no
    # apparatus subject) must NOT trip the flag — the verb alone is not the tell.
    for body in (
        "The week opens with a summit in Riyadh on Monday.",
        "Talks start with a handshake in Geneva.",
        "I look at the map and Sudan is burning.",
    ):
        assert _apparatus_lead_flag(body) == [], body


def test_apparatus_lead_silent_when_apparatus_is_a_closing_note():
    # A legal apparatus mention LATE in the entry must not trip the lead check.
    body = ("A grain-corridor deal collapsed in Istanbul.\n\n"
            "Late note: I checked source health and three feeds are stalled.")
    assert _apparatus_lead_flag(body) == []


def test_apparatus_lead_ignores_leading_title_line():
    # A markdown/bold title is not the lead — the first PROSE sentence is.
    body = ("# The Strait\n\nA tanker was detained overnight in the strait. "
            "The world moved.")
    assert _apparatus_lead_flag(body) == []


@pytest.mark.asyncio
async def test_apparatus_lead_flag_stamped_on_apparatus_opening_entry():
    """Full arc: an entry that OPENS apparatus-facing gets the deterministic
    apparatus_lead honesty flag ALONGSIDE the forced calibration flags — annotate,
    never block (the body is untouched)."""
    binding = _FakeBinding(outputs={"get_calibration": {
        "available": True, "forecast_unproven": True, "calibration_thin": True,
    }})
    scripted = [
        '{"done": true}',                                       # GATHER
        "field notes",                                          # seam
        "I start by checking the health of my senses. Feeds look fresh.",  # narrate
    ]
    deps = InlineTargetDeps(
        llm=_ScriptedLLM(scripted), system_prompt="P", max_rounds=1, agency_binding=binding,
    )
    result = await run_method([{"title": "s"}], _options(binding), deps)
    flags = set(result.finding.honesty_flags)
    assert "apparatus_lead" in flags
    # It ANNOTATES — the substrate flags are still present and the body is intact.
    assert {"forecast_unproven", "calibration_thin"} <= flags
    assert "checking the health of my senses" in result.finding.body


@pytest.mark.asyncio
async def test_apparatus_lead_flag_absent_on_world_first_entry():
    binding = _FakeBinding(outputs={"get_calibration": {
        "available": True, "forecast_unproven": True, "calibration_thin": True,
    }})
    scripted = [
        '{"done": true}',
        "field notes",
        "Khamenei signaled openness to talks overnight. My feeds stayed fresh.",
    ]
    deps = InlineTargetDeps(
        llm=_ScriptedLLM(scripted), system_prompt="P", max_rounds=1, agency_binding=binding,
    )
    result = await run_method([{"title": "s"}], _options(binding), deps)
    assert "apparatus_lead" not in set(result.finding.honesty_flags)


def test_apparatus_lead_flag_is_idempotent():
    # The stamp is guarded against duplicates (mirrors the @> array_append guard).
    body = "I start by checking the health of my senses. All quiet."
    flags = ["forecast_unproven"]
    for _flag in _apparatus_lead_flag(body):
        if _flag not in flags:
            flags.append(_flag)
    for _flag in _apparatus_lead_flag(body):  # second application — no dup
        if _flag not in flags:
            flags.append(_flag)
    assert flags.count("apparatus_lead") == 1


# ---------------------------------------------------------------------------
# task #236 — the deterministic NARRATE tool-call-leak guard (§4.4).
#
# Caught live 2026-07-24: lens_trend's inaugural run wrote a journal entry
# whose ENTIRE body was ``{"tool": "get_assessments", "args": {}}`` (39
# chars) — the core-plane model emitted tool-call JSON as its final NARRATE
# turn instead of prose, and (pre-fix) that junk became BOTH body and title.
# ---------------------------------------------------------------------------

# The exact live junk string from the incident.
_LIVE_JUNK = '{"tool": "get_assessments", "args": {}}'


# --- unit-level: the predicate itself -------------------------------------


def test_guard_fires_on_the_exact_live_junk_string():
    assert _is_tool_call_leak(_LIVE_JUNK) is True


def test_guard_fires_on_fenced_json_tool_call():
    fenced = '```json\n{"tool": "get_lens_reads", "args": {"since": null}}\n```'
    assert _is_tool_call_leak(fenced) is True


def test_guard_fires_on_openai_style_tool_calls_array():
    array_shape = '[{"tool_calls": [{"function": "get_run_health"}]}]'
    assert _is_tool_call_leak(array_shape) is True


def test_guard_fires_on_short_degenerate_json_below_prose_floor():
    # {} alone isn't tool-call-key-shaped (no keys at all) but IS whole-string
    # JSON under the min-prose floor — still exhaust, not an entry.
    assert _is_tool_call_leak("{}") is True


def test_guard_does_not_false_positive_on_prose_containing_a_json_snippet():
    # The check is WHOLE-STRING — prose that merely quotes/mentions a
    # JSON-shaped span mid-paragraph must NEVER trip it (unlike
    # inline_target._extract_json, which deliberately hunts for a JSON object
    # anywhere in the text — the opposite of what this guard needs).
    prose = (
        "The nexus flipped polarity overnight. A raw feed dump included a "
        'payload that looked like {"tool": "get_assessments"} in the debug '
        "trace, which was odd, but the underlying claim "
        "[[ref:11111111-1111-1111-1111-111111111111]] holds regardless. "
        "I keep wondering what else this window is missing."
    )
    assert _is_tool_call_leak(prose) is False


def test_guard_does_not_false_positive_on_ordinary_short_prose():
    # Short but NOT JSON at all — json.loads fails immediately, so the
    # min-prose floor (which only applies to successfully-parsed JSON) never
    # engages. A terse legitimate entry must never be treated as a leak.
    assert _is_tool_call_leak("Quiet morning. Nothing moved.") is False


def test_guard_does_not_false_positive_on_a_long_legitimate_entry():
    entry = (
        "# A quiet window\n\nThe nexus flipped polarity overnight "
        "[[ref:11111111-1111-1111-1111-111111111111]]. I keep wondering "
        "whether this is a genuine shift or noise in the feed. Three "
        "assessors went quiet this cycle, which makes me uneasy about what "
        "we're missing in the wider picture right now."
    )
    assert _is_tool_call_leak(entry) is False


# --- full arc: retry recovers ----------------------------------------------


@pytest.mark.asyncio
async def test_narrate_leak_triggers_one_retry_and_recovers():
    """The exact live incident shape: NARRATE's FINAL round (round_idx ==
    _NARRATE_MAX_TOOL_ROUNDS - 1, where a tool call can never be EXECUTED
    regardless of whether its name is recognized — see the
    `round_idx < _NARRATE_MAX_TOOL_ROUNDS - 1` guard in the tool-routing
    branch) emits the live junk. This is exactly why the pre-fix code wrote
    it verbatim as the entry: it fell through the tool-routing branch with
    nowhere else to go. The retry (with the hard prose-only instruction)
    returns clean prose, and THAT becomes the entry. No exception; the run
    completes normally."""
    ref = uuid4()
    binding = _FakeBinding(outputs={
        "get_run_health": {"rows": [], "quiet_analysts": [], "refs": []},
        "get_calibration": {
            "available": True, "forecast_unproven": True, "calibration_thin": True,
        },
    })
    scripted = [
        '{"done": true}',                                   # GATHER round 1
        "field notes: the nexus flipped.",                  # field-notes seam
        '{"tool": "get_run_health", "args": {}}',           # narrate round 1: EXECUTED
        _LIVE_JUNK,                                          # narrate round 2 (FINAL): LEAKS
        f"The nexus flipped polarity overnight [[ref:{ref}]]. "  # retry: clean
        "I keep wondering what we're not seeing.",
    ]
    llm = _ScriptedLLM(scripted)
    deps = InlineTargetDeps(
        llm=llm, system_prompt="P", max_rounds=1, agency_binding=binding,
    )
    result = await run_method([{"title": "s"}], _options(binding), deps)
    payload = result.finding
    # The recovered retry content is what got written — never the junk.
    assert "nexus flipped polarity overnight" in payload.body
    assert _LIVE_JUNK not in payload.body
    assert ref in payload.cited_substrate_refs
    # The title was derived from the RECOVERED body, not the junk.
    assert payload.title != _LIVE_JUNK
    # 5 LLM calls: GATHER + field-notes + narrate round1(tool) + round2(leak) + retry.
    assert len(llm.calls) == 5
    # The legitimate round-1 tool call still executed normally.
    assert ("get_run_health", {}) in binding.calls
    # The leak + recovery were traced.
    kinds = [s.get("kind") for s in result.intermediate_steps if s.get("phase") == "narrate"]
    assert "tool_call_leak" in kinds
    assert "tool_call_leak_recovered" in kinds
    assert "tool_call_leak_fatal" not in kinds


@pytest.mark.asyncio
async def test_narrate_leak_retry_appends_hard_instruction_not_a_new_round():
    """The retry must NOT consume a _NARRATE_MAX_TOOL_ROUNDS slot — it is a
    separate, later safety net. max_rounds=1 on the GATHER side is unrelated;
    this asserts the retry fires even though the narrate loop's own round cap
    (_NARRATE_MAX_TOOL_ROUNDS, module-level, not descriptor-configurable) was
    never widened for this test."""
    binding = _FakeBinding(outputs={"get_calibration": {
        "available": True, "forecast_unproven": False, "calibration_thin": False,
    }})
    scripted = [
        '{"done": true}',
        "notes",
        '{"name": "get_source_health", "arguments": {}}',  # leaks (sibling key shape)
        "A clean entry after the retry.",
    ]
    llm = _ScriptedLLM(scripted)
    deps = InlineTargetDeps(
        llm=llm, system_prompt="P", max_rounds=1, agency_binding=binding,
    )
    result = await run_method([{"title": "s"}], _options(binding), deps)
    assert result.finding.body == "A clean entry after the retry."


# --- full arc: fatal path ---------------------------------------------------


@pytest.mark.asyncio
async def test_narrate_leak_persists_through_retry_raises_fatal():
    """When the retry ALSO leaks, the run RAISES (hard_fail) rather than
    writing junk as the entry — a failed run self-retries next cadence; a
    written junk entry poisons the panel + verify ledger and does not."""
    binding = _FakeBinding(outputs={"get_calibration": {
        "available": True, "forecast_unproven": True, "calibration_thin": True,
    }})
    scripted = [
        '{"done": true}',
        "field notes",
        _LIVE_JUNK,                                   # narrate: LEAKS
        '{"tool": "get_run_health", "args": {}}',      # retry: LEAKS AGAIN
    ]
    deps = InlineTargetDeps(
        llm=_ScriptedLLM(scripted), system_prompt="P", max_rounds=1,
        agency_binding=binding,
    )
    with pytest.raises(NarrateToolCallLeakError):
        await run_method([{"title": "s"}], _options(binding), deps)


@pytest.mark.asyncio
async def test_narrate_leak_fatal_never_writes_a_finding():
    """Belt-and-braces on the fatal path: run_method raising means the caller
    (the actor run path) never receives an AnalystMethodResult to persist —
    there is no partial/junk payload constructed anywhere past the raise."""
    binding = _FakeBinding(outputs={"get_calibration": {
        "available": True, "forecast_unproven": True, "calibration_thin": True,
    }})
    scripted = ['{"done": true}', "notes", "{}", "{}"]  # both turns leak (min-prose floor)
    deps = InlineTargetDeps(
        llm=_ScriptedLLM(scripted), system_prompt="P", max_rounds=1,
        agency_binding=binding,
    )
    try:
        await run_method([{"title": "s"}], _options(binding), deps)
        assert False, "expected NarrateToolCallLeakError"
    except NarrateToolCallLeakError:
        pass  # exactly the expected failure mode — nothing else escaped


# ---------------------------------------------------------------------------
# S-1 — the source-health NUMBER guard (SWEEP_SYNTHESIS §T1-#1). A faculty lens
# fabricated "0 active feeds / the window is dark" while get_source_health
# returned 68/49; the entry shipped unflagged because the only guard re-called the
# tool instead of reading the prose, and the lens path skipped even that. The new
# guard extracts the whole-fleet counts the narrator WROTE and cross-checks them
# against the live tool, forcing `source_health_fabricated` on divergence (flag,
# never rewrite) — on EVERY tier.
# ---------------------------------------------------------------------------


def test_extract_source_health_claims_pulls_key_and_nl_forms():
    # the live "window is dark" fabrication — explicit key forms
    dark = _extract_source_health_claims(
        "The window is dark: active_total = 0, active_fresh = 0, "
        "active_stalled = 0, total_wired = 0."
    )
    assert dark["active_total"] == {0}
    assert dark["total_wired"] == {0}
    # the correct entry — natural language "N active feeds" + "total wired = N"
    ok = _extract_source_health_claims(
        "49 active feeds, of which 32 are fresh, 17 stalled (total wired = 68)."
    )
    assert ok["active_total"] == {49}
    assert ok["total_wired"] == {68}
    # the "3" fabrication — key forms again
    three = _extract_source_health_claims(
        "active_total = 3, active_fresh = 2, active_stalled = 1, total_wired = 3."
    )
    assert three["active_total"] == {3}
    assert three["total_wired"] == {3}
    # the "N total wired" leading form
    lead = _extract_source_health_claims("58 total wired this week.")
    assert lead["total_wired"] == {58}


def test_extract_source_health_claims_exempts_declared_subset():
    # a NAMED subset count carries its own scope (persona contract) — not a
    # whole-fleet claim, so it is NOT extracted as active_total.
    subset = _extract_source_health_claims(
        "Of the press-class subset, 3 active feeds carried the week."
    )
    assert "active_total" not in subset
    # the volatile fields (fresh/stalled/erroring) are intentionally NOT validated:
    # the natural-language "fresh"/"stalled" words never become claims.
    volatile = _extract_source_health_claims("32 are fresh, 17 stalled, 2 erroring.")
    assert volatile == {}


def test_extract_source_health_claims_empty_on_no_numbers():
    assert _extract_source_health_claims("A quiet week; nothing new to weigh.") == {}
    assert _extract_source_health_claims("") == {}


_SOURCE_HEALTH_68 = {
    "summary": {
        "total_wired": 68, "active_total": 49, "active_fresh": 37,
        "active_stalled": 12, "active_erroring": 2,
    },
}


@pytest.mark.asyncio
async def test_number_check_flags_fabricated_dark_window():
    binding = _FakeBinding(outputs={"get_source_health": _SOURCE_HEALTH_68})
    steps: list[dict[str, Any]] = []
    flags = await _source_health_number_check(
        binding,
        "The window is dark: 0 active feeds (active_total = 0, total_wired = 0).",
        steps=steps,
    )
    assert flags == ["source_health_fabricated"]
    assert any(name == "get_source_health" for name, _ in binding.calls)
    step = next(s for s in steps if s.get("kind") == "source_health_fabricated")
    assert {m["field"] for m in step["mismatches"]} == {"active_total", "total_wired"}


@pytest.mark.asyncio
async def test_number_check_passes_correct_numbers():
    binding = _FakeBinding(outputs={"get_source_health": _SOURCE_HEALTH_68})
    flags = await _source_health_number_check(
        binding, "49 active feeds (total wired = 68); the intake is broad.", steps=[],
    )
    assert flags == []


@pytest.mark.asyncio
async def test_number_check_no_claim_makes_no_tool_call():
    binding = _FakeBinding(outputs={"get_source_health": _SOURCE_HEALTH_68})
    flags = await _source_health_number_check(
        binding, "A quiet week under this prior; nothing to weigh.", steps=[],
    )
    assert flags == []
    assert binding.calls == []  # no numeric claim → never even consults the tool


@pytest.mark.asyncio
async def test_number_check_degrades_without_binding():
    assert await _source_health_number_check(
        None, "active_total = 0, total_wired = 0.", steps=[],
    ) == []


# --- full arc on the LENS tier (the path the sibling guard skips) -----------


def _lens_options(binding: Any) -> dict[str, Any]:
    return {
        "analyst_id": "lens_trend",
        "agency_binding": binding,
        "gather_tool_bindings": {},
    }


@pytest.mark.asyncio
async def test_full_arc_lens_flags_fabricated_source_health():
    """A LENS run whose prose declares "the window is dark" (0 feeds) while
    get_source_health returns 68/49 must ship `source_health_fabricated` — proving
    the guard runs on the lens entry_kind, exactly where _source_health_cross_check
    (gather-slice-keyed) no-ops."""
    binding = _FakeBinding(outputs={
        "get_calibration": {
            "available": True, "forecast_unproven": True, "calibration_thin": True,
        },
        "get_source_health": _SOURCE_HEALTH_68,
    })
    scripted = [
        '{"done": true}',                              # GATHER
        "field notes",                                 # seam
        "# Collection posture\n\nThe window is dark: 0 active feeds "
        "(active_total = 0, active_fresh = 0, active_stalled = 0, "
        "total_wired = 0). With no live intake I can weigh nothing this week.",
    ]
    deps = InlineTargetDeps(
        llm=_ScriptedLLM(scripted), system_prompt="LENS", max_rounds=1,
        agency_binding=binding,
    )
    # inputs carry NO real signal rows (no source_id/id) → the sibling cross_check's
    # distinct == 0 short-circuit fires; only the number guard runs on this tier.
    result = await run_method([{"title": "seed"}], _lens_options(binding), deps)
    payload = result.finding
    assert payload.entry_kind == "lens"
    assert "source_health_fabricated" in payload.honesty_flags
    # the prose was preserved verbatim — annotate, never strip.
    assert "the window is dark" in payload.body.lower()


# ---------------------------------------------------------------------------
# E-1 (2026-07-27 sweep) — the lens EMPTY-READ fallback. The Monday cycle's
# lens_capability shipped "(empty lens read)" with 0 claims while the
# chronicle/consolidation stayed substantive by reasoning over the verified
# tower corpus. An empty lens NARRATE now gets ONE fallback narrate carrying
# the tower-corpus redirect; a read STILL empty after it stays honestly empty.
# ---------------------------------------------------------------------------


def _capability_options(binding: Any) -> dict[str, Any]:
    return {
        "analyst_id": "lens_capability",
        "agency_binding": binding,
        "gather_tool_bindings": {},
    }


@pytest.mark.asyncio
async def test_empty_lens_narrate_falls_back_to_tower_corpus():
    """Empty slice + empty NARRATE → the fallback narrate runs (with the
    tower-corpus redirect in its prompt) and its cited prose becomes the read."""
    ref = uuid4()
    binding = _FakeBinding(outputs={"get_calibration": {
        "available": True, "forecast_unproven": True, "calibration_thin": True,
    }})
    scripted = [
        '{"done": true}',                       # GATHER
        "field notes over the tower top",       # seam
        "",                                     # NARRATE → EMPTY (the live kill)
        f"The tower's verified week: escalation held [[ref:{ref}]]. "
        "Under my prior the materiel picture did not move.",   # fallback narrate
    ]
    llm = _ScriptedLLM(scripted)
    deps = InlineTargetDeps(
        llm=llm, system_prompt="LENS", max_rounds=1, agency_binding=binding,
    )
    # inputs: NO renderable signal rows — the empty-slice priming shape.
    result = await run_method([], _capability_options(binding), deps)
    payload = result.finding
    assert payload.entry_kind == "lens"
    assert "escalation held" in payload.body
    assert payload.body != "(empty lens read)"
    assert ref in payload.cited_substrate_refs
    # The fallback prompt carried the tower-corpus redirect.
    fallback_prompt = llm.calls[3]["messages"][0]["content"]
    assert "VERIFIED TOWER CORPUS" in fallback_prompt
    # Trace: the fallback fired and recovered.
    kinds = [s.get("kind") for s in result.intermediate_steps
             if s.get("phase") == "narrate"]
    assert "empty_lens_fallback" in kinds
    assert "empty_lens_fallback_recovered" in kinds


@pytest.mark.asyncio
async def test_empty_lens_still_empty_after_fallback_stays_honest():
    """Fallback narrate ALSO empty → the read stays honestly '(empty lens
    read)' with 0 claims — never fabricated."""
    binding = _FakeBinding(outputs={"get_calibration": {
        "available": True, "forecast_unproven": True, "calibration_thin": True,
    }})
    scripted = [
        '{"done": true}',   # GATHER
        "field notes",      # seam
        "",                 # NARRATE → empty
        "",                 # fallback narrate → STILL empty
    ]
    deps = InlineTargetDeps(
        llm=_ScriptedLLM(scripted), system_prompt="LENS", max_rounds=1,
        agency_binding=binding,
    )
    result = await run_method([], _capability_options(binding), deps)
    payload = result.finding
    assert payload.entry_kind == "lens"
    assert payload.body == "(empty lens read)"
    assert payload.claims == []
    kinds = [s.get("kind") for s in result.intermediate_steps
             if s.get("phase") == "narrate"]
    assert "empty_lens_fallback_still_empty" in kinds


@pytest.mark.asyncio
async def test_empty_narrate_fallback_is_lens_only():
    """A non-VOICES tier (entry) with an empty NARRATE keeps its pre-existing
    honest empty body — the fallback never fires off the lens/lens_diff
    path."""
    binding = _FakeBinding(outputs={"get_calibration": {
        "available": True, "forecast_unproven": True, "calibration_thin": True,
    }})
    scripted = ['{"done": true}', "notes", ""]  # NARRATE empty, no 4th turn
    llm = _ScriptedLLM(scripted)
    deps = InlineTargetDeps(
        llm=llm, system_prompt="P", max_rounds=1, agency_binding=binding,
    )
    result = await run_method([{"title": "s"}], _options(binding), deps)
    assert result.finding.body == "(empty entry)"
    kinds = [s.get("kind") for s in result.intermediate_steps]
    assert "empty_lens_fallback" not in kinds
    assert len(llm.calls) == 3  # no extra narrate was spent


@pytest.mark.asyncio
async def test_nonempty_lens_narrate_never_triggers_fallback():
    binding = _FakeBinding(outputs={"get_calibration": {
        "available": True, "forecast_unproven": True, "calibration_thin": True,
    }})
    scripted = ['{"done": true}', "notes", "A substantive read under my prior."]
    llm = _ScriptedLLM(scripted)
    deps = InlineTargetDeps(
        llm=llm, system_prompt="LENS", max_rounds=1, agency_binding=binding,
    )
    result = await run_method([{"title": "s"}], _capability_options(binding), deps)
    assert result.finding.body == "A substantive read under my prior."
    kinds = [s.get("kind") for s in result.intermediate_steps]
    assert "empty_lens_fallback" not in kinds
    assert len(llm.calls) == 3


# ---------------------------------------------------------------------------
# P3 finding (2026-07-31 sweep) — the SAME E-1 empty-read gap, on lens_diff.
# A healthy roster (all four faculties ran) still shipped "(empty chorus
# diff)" because the E-1 fallback was wired to entry_kind == "lens" only.
# ---------------------------------------------------------------------------


def _lens_diff_options(binding: Any) -> dict[str, Any]:
    return {
        "analyst_id": "lens_diff",
        "agency_binding": binding,
        "gather_tool_bindings": {},
    }


@pytest.mark.asyncio
async def test_empty_lens_diff_narrate_falls_back_and_recovers():
    """Empty NARRATE on the chorus-diff tier gets ONE fallback narrate
    (the lens_diff-specific redirect, NOT the lens tier's tower-corpus one)
    and its cited prose becomes the read."""
    ref = uuid4()
    binding = _FakeBinding(outputs={"get_calibration": {
        "available": True, "forecast_unproven": True, "calibration_thin": True,
    }})
    scripted = [
        '{"done": true}',                       # GATHER
        "field notes over the four voices",     # seam
        "",                                      # NARRATE → EMPTY (the live kill)
        f"AGREE: all four faculties converge on the nexus holding "
        f"[[ref:{ref}]]. These are four declared priors, not the space of "
        "priors.",                               # fallback narrate
    ]
    llm = _ScriptedLLM(scripted)
    deps = InlineTargetDeps(
        llm=llm, system_prompt="LENS_DIFF", max_rounds=1, agency_binding=binding,
    )
    result = await run_method([], _lens_diff_options(binding), deps)
    payload = result.finding
    assert payload.entry_kind == "lens_diff"
    assert "all four faculties converge" in payload.body
    assert payload.body != "(empty chorus diff)"
    assert ref in payload.cited_substrate_refs
    # The fallback prompt carried the lens_diff-specific redirect (its own
    # instruments), NOT the lens tier's tower-corpus wording.
    fallback_prompt = llm.calls[3]["messages"][0]["content"]
    assert "get_lens_reads" in fallback_prompt
    assert "VERIFIED TOWER CORPUS" not in fallback_prompt
    # Trace: the fallback fired and recovered, tier-scoped kind names.
    kinds = [s.get("kind") for s in result.intermediate_steps
             if s.get("phase") == "narrate"]
    assert "empty_lens_diff_fallback" in kinds
    assert "empty_lens_diff_fallback_recovered" in kinds


@pytest.mark.asyncio
async def test_empty_lens_diff_still_empty_after_fallback_logs_warning(caplog):
    """Fallback narrate ALSO empty → the read stays honestly '(empty chorus
    diff)' with 0 claims (never fabricated) AND a WARNING is logged — a
    healthy roster shipping an empty diff is worth an operator's attention
    even though the run itself succeeds (trace-only, never fails the run)."""
    binding = _FakeBinding(outputs={"get_calibration": {
        "available": True, "forecast_unproven": True, "calibration_thin": True,
    }})
    scripted = [
        '{"done": true}',   # GATHER
        "field notes",      # seam
        "",                 # NARRATE → empty
        "",                 # fallback narrate → STILL empty
    ]
    deps = InlineTargetDeps(
        llm=_ScriptedLLM(scripted), system_prompt="LENS_DIFF", max_rounds=1,
        agency_binding=binding,
    )
    with caplog.at_level(logging.WARNING):
        result = await run_method([], _lens_diff_options(binding), deps)
    payload = result.finding
    assert payload.entry_kind == "lens_diff"
    assert payload.body == "(empty chorus diff)"
    assert payload.claims == []
    kinds = [s.get("kind") for s in result.intermediate_steps
             if s.get("phase") == "narrate"]
    assert "empty_lens_diff_fallback_still_empty" in kinds
    assert any(
        "empty_lens_diff_fallback.still_empty" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_full_arc_lens_clean_source_health_passes():
    binding = _FakeBinding(outputs={
        "get_calibration": {
            "available": True, "forecast_unproven": True, "calibration_thin": True,
        },
        "get_source_health": _SOURCE_HEALTH_68,
    })
    scripted = [
        '{"done": true}',
        "field notes",
        "# Collection posture\n\nThe fleet is healthy: 49 active feeds "
        "(total wired = 68). The intake is broad enough to weigh the week.",
    ]
    deps = InlineTargetDeps(
        llm=_ScriptedLLM(scripted), system_prompt="LENS", max_rounds=1,
        agency_binding=binding,
    )
    result = await run_method([{"title": "seed"}], _lens_options(binding), deps)
    assert result.finding.entry_kind == "lens"
    assert "source_health_fabricated" not in result.finding.honesty_flags
