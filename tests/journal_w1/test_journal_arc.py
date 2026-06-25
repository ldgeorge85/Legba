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

from typing import Any, Mapping
from uuid import uuid4

import pytest

from legba.data.analysts.agency.agency import AgencyOutcome
from legba.data.analysts.agency.tools import ToolResult
from legba.data.analysts.inline_target import InlineTargetDeps
from legba.data.analysts.journal_assessor import (
    _reflect_claims,
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
