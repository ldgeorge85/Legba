# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-178 tests for the consult_on_demand analyst kind.

Covers:
  * Identity constants — KIND_NAME, SCHEMA_VERSION, HANDLER_VERSION,
    PROMPT_MODULE_PATH, MAX_TOOL_ROUNDS.
  * Happy path — substrate-resolvable question → planner emits a tool
    call → tool returns matching rows → final synthesis carries
    ``cited_substrate_refs`` with the substrate UUIDs the tool returned.
  * No-substrate path — question for which the tool returns nothing →
    planner emits ``final`` with high ``uncertainty`` + populated
    ``unanswered_aspects``.
  * Round cap — planner keeps asking for tools indefinitely; the kind
    forces a final synthesis turn after MAX_TOOL_ROUNDS.
  * Malformed planner output mid-loop — planner recovers via the
    self-correction prompt.
  * LLM error propagation — exceptions bubble to the runtime per
    kind_contracts §7.
  * Hallucinated UUID guard — LLM emits cited_refs containing UUIDs the
    tools never returned; the kind filters to confirmed refs only.
  * Empty/invalid input — explicit ValueError.
  * ConsultOnDemandRunner adapter — AnalystRunFn shape.
  * FindingPayload wrapping — the consult response surfaces in
    ``finding.data["consult_response"]`` so the existing
    OutputKind.FINDING write path stays compatible.

Tests use typed LLM + Substrate test doubles (no mocks at substrate
boundary — the test doubles ARE the substrate from the kind's
perspective; real Postgres-backed substrate integration is the
runtime's job, not this unit suite).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import UUID, uuid4

import pytest

from legba.data.analysts.consult_on_demand import (
    AnalystMethodResult,
    ConsultOnDemandDeps,
    ConsultOnDemandRunner,
    HANDLER_VERSION,
    KIND_NAME,
    MAX_TOOL_ROUNDS,
    PROMPT_MODULE_PATH,
    SCHEMA_VERSION,
    _extract_json,
    build_prompt_module,
    run_method,
)
from legba.data.provenance.models import (
    ConsultResponsePayload,
    FindingPayload,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    prompt_tokens: int = 100
    completion_tokens: int = 50
    reasoning_tokens: int = 0


@dataclass
class _Response:
    content: str = ""
    usage: _Usage | None = None


class _ScriptedLLMHandler:
    """LLM test double that emits a pre-scripted sequence of responses.

    Each call returns the next entry in ``responses``.  When the list is
    exhausted, falls back to ``default`` (a high-uncertainty final
    payload — useful for the round-cap test where the kind forces an
    extra turn).
    """

    subprovider = "vllm-test"

    def __init__(
        self,
        responses: list[str],
        *,
        default: str | None = None,
        raise_on_call: type[BaseException] | None = None,
    ) -> None:
        self._responses = list(responses)
        self._default = default or json.dumps({
            "final": True,
            "answer": "(default fallback)",
            "uncertainty": 1.0,
            "cited_refs": [],
            "unanswered_aspects": ["script exhausted"],
        })
        self._raise_on_call = raise_on_call
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
        self.calls.append({
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
        })
        if self._raise_on_call is not None:
            raise self._raise_on_call("simulated LLM transport failure")
        if self._responses:
            content = self._responses.pop(0)
        else:
            content = self._default
        return _Response(content=content, usage=_Usage())


@dataclass
class _SubstrateStub:
    """Stub conforming to ``SubstrateQueryPort``.

    Each tool method returns a canned payload set per-test.  The stub
    also records all calls so tests can assert what the planner did.
    """

    signal_rows: list[dict[str, Any]] = field(default_factory=list)
    fact_rows: list[dict[str, Any]] = field(default_factory=list)
    entity_profile: dict[str, Any] | None = None
    vector_rows: list[dict[str, Any]] = field(default_factory=list)
    context_rows: list[dict[str, Any]] = field(default_factory=list)
    signal_refs: list[UUID] = field(default_factory=list)
    fact_refs: list[UUID] = field(default_factory=list)
    vector_refs: list[UUID] = field(default_factory=list)
    context_refs: list[UUID] = field(default_factory=list)
    nexus_rows: list[dict[str, Any]] = field(default_factory=list)
    nexus_refs: list[UUID] = field(default_factory=list)
    hypothesis_rows: list[dict[str, Any]] = field(default_factory=list)
    hypothesis_refs: list[UUID] = field(default_factory=list)
    timeline_items: list[dict[str, Any]] = field(default_factory=list)
    timeline_refs: list[UUID] = field(default_factory=list)
    compare_targets_out: dict[str, Any] | None = None
    finding_rows: list[dict[str, Any]] = field(default_factory=list)
    finding_refs: list[UUID] = field(default_factory=list)
    situation_rows: list[dict[str, Any]] = field(default_factory=list)
    situation_refs: list[UUID] = field(default_factory=list)
    prediction_rows: list[dict[str, Any]] = field(default_factory=list)
    prediction_refs: list[UUID] = field(default_factory=list)
    target_rows: list[dict[str, Any]] = field(default_factory=list)
    target_refs: list[UUID] = field(default_factory=list)
    source_rows: list[dict[str, Any]] = field(default_factory=list)
    source_refs: list[UUID] = field(default_factory=list)
    raise_on: str | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def search_signals(
        self,
        *,
        query: str,
        category: str | None = None,
        limit: int = 20,
        scope_predicate: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((
            "search_signals",
            {"query": query, "category": category, "limit": limit,
             "scope_predicate": scope_predicate},
        ))
        if self.raise_on == "search_signals":
            raise RuntimeError("substrate down")
        return {
            "rows": self.signal_rows,
            "refs": [str(r) for r in self.signal_refs],
            "count": len(self.signal_rows),
        }

    async def query_facts(
        self,
        *,
        subject: str | None = None,
        predicate: str | None = None,
        value: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        self.calls.append((
            "query_facts",
            {"subject": subject, "predicate": predicate, "value": value,
             "limit": limit},
        ))
        if self.raise_on == "query_facts":
            raise RuntimeError("substrate down")
        return {
            "rows": self.fact_rows,
            "refs": [str(r) for r in self.fact_refs],
            "count": len(self.fact_rows),
        }

    async def inspect_entity(self, *, name: str) -> dict[str, Any]:
        self.calls.append(("inspect_entity", {"name": name}))
        if self.raise_on == "inspect_entity":
            raise RuntimeError("substrate down")
        if self.entity_profile is None:
            return {"found": False, "refs": []}
        return {"found": True, **self.entity_profile}

    async def vector_search(self, *, query: str, limit: int = 10) -> dict[str, Any]:
        self.calls.append(("vector_search", {"query": query, "limit": limit}))
        if self.raise_on == "vector_search":
            raise RuntimeError("substrate down")
        return {
            "rows": self.vector_rows,
            "refs": [str(r) for r in self.vector_refs],
            "count": len(self.vector_rows),
        }

    async def search_context(
        self,
        *,
        query: str,
        corpus: str | None = None,
        country: str | None = None,
        k: int = 6,
    ) -> dict[str, Any]:
        self.calls.append((
            "search_context",
            {"query": query, "corpus": corpus, "country": country, "k": k},
        ))
        if self.raise_on == "search_context":
            raise RuntimeError("substrate down")
        return {
            "rows": self.context_rows,
            "refs": [str(r) for r in self.context_refs],
            "count": len(self.context_rows),
        }

    async def query_nexuses(
        self,
        *,
        subject: str | None = None,
        obj: str | None = None,
        rel_type: str | None = None,
        polarity: int | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        self.calls.append((
            "query_nexuses",
            {"subject": subject, "object": obj, "rel_type": rel_type,
             "polarity": polarity, "limit": limit},
        ))
        if self.raise_on == "query_nexuses":
            raise RuntimeError("substrate down")
        return {
            "rows": self.nexus_rows,
            "refs": [str(r) for r in self.nexus_refs],
            "count": len(self.nexus_rows),
        }

    async def query_hypotheses(
        self,
        *,
        target_id: str | None = None,
        status: str | None = None,
        situation_id: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        self.calls.append((
            "query_hypotheses",
            {"target_id": target_id, "status": status,
             "situation_id": situation_id, "limit": limit},
        ))
        if self.raise_on == "query_hypotheses":
            raise RuntimeError("substrate down")
        return {
            "rows": self.hypothesis_rows,
            "refs": [str(r) for r in self.hypothesis_refs],
            "count": len(self.hypothesis_rows),
        }

    async def get_timeline(self, *, subject: str, limit: int = 40) -> dict[str, Any]:
        self.calls.append(("get_timeline", {"subject": subject, "limit": limit}))
        if self.raise_on == "get_timeline":
            raise RuntimeError("substrate down")
        return {
            "subject": subject,
            "items": self.timeline_items,
            "refs": [str(r) for r in self.timeline_refs],
        }

    async def compare_targets(self, *, target_ids: list[str]) -> dict[str, Any]:
        self.calls.append(("compare_targets", {"target_ids": list(target_ids)}))
        if self.raise_on == "compare_targets":
            raise RuntimeError("substrate down")
        if self.compare_targets_out is not None:
            return self.compare_targets_out
        if len(target_ids) < 2:
            return {
                "targets": [],
                "refs": [],
                "error": "compare_targets requires at least two distinct target_ids",
            }
        return {
            "targets": [{"target_id": t} for t in target_ids],
            "refs": [],
            "compared": list(target_ids),
        }

    async def list_findings(
        self,
        *,
        target_id: str | None = None,
        analyst_id: str | None = None,
        severity: str | None = None,
        since_hours: int | None = None,
        include_superseded: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        self.calls.append((
            "list_findings",
            {"target_id": target_id, "analyst_id": analyst_id,
             "severity": severity, "since_hours": since_hours,
             "include_superseded": include_superseded, "limit": limit},
        ))
        if self.raise_on == "list_findings":
            raise RuntimeError("substrate down")
        return {
            "rows": self.finding_rows,
            "refs": [str(r) for r in self.finding_refs],
            "count": len(self.finding_rows),
        }

    async def list_situations(
        self,
        *,
        status: str | None = None,
        target_id: str | None = None,
        since_hours: int | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        self.calls.append((
            "list_situations",
            {"status": status, "target_id": target_id,
             "since_hours": since_hours, "limit": limit},
        ))
        if self.raise_on == "list_situations":
            raise RuntimeError("substrate down")
        return {
            "rows": self.situation_rows,
            "refs": [str(r) for r in self.situation_refs],
            "count": len(self.situation_rows),
        }

    async def query_predictions(
        self,
        *,
        target_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        self.calls.append((
            "query_predictions",
            {"target_id": target_id, "status": status, "limit": limit},
        ))
        if self.raise_on == "query_predictions":
            raise RuntimeError("substrate down")
        return {
            "rows": self.prediction_rows,
            "refs": [str(r) for r in self.prediction_refs],
            "count": len(self.prediction_rows),
        }

    async def list_targets(self, *, active_only: bool = True) -> dict[str, Any]:
        self.calls.append(("list_targets", {"active_only": active_only}))
        if self.raise_on == "list_targets":
            raise RuntimeError("substrate down")
        return {
            "rows": self.target_rows,
            "refs": [str(r) for r in self.target_refs],
            "count": len(self.target_rows),
        }

    async def list_sources(
        self,
        *,
        active_only: bool = True,
        silent_only: bool = False,
        silent_hours: int = 48,
    ) -> dict[str, Any]:
        self.calls.append((
            "list_sources",
            {"active_only": active_only, "silent_only": silent_only,
             "silent_hours": silent_hours},
        ))
        if self.raise_on == "list_sources":
            raise RuntimeError("substrate down")
        return {
            "rows": self.source_rows,
            "refs": [str(r) for r in self.source_refs],
            "count": len(self.source_rows),
        }


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_kind_identity_constants():
    """KIND_NAME, SCHEMA_VERSION, HANDLER_VERSION, PROMPT_MODULE_PATH, +
    MAX_TOOL_ROUNDS match the L-178 contract."""
    assert KIND_NAME == "consult_on_demand"
    assert SCHEMA_VERSION == "legba/analyst.consult_on_demand/1-0-0"
    assert HANDLER_VERSION == "0.1.0"
    assert PROMPT_MODULE_PATH == "legba.prompts.consult_on_demand.v1"
    assert MAX_TOOL_ROUNDS == 6


def test_kind_in_analyst_kind_registry():
    """The enum + open-taxonomy registry knows about consult_on_demand
    (L-241 made AnalystKind extensible, but the built-in value lives in
    the enum already)."""
    from legba.data.schemas.analyst import (
        ANALYST_KIND_REGISTRY,
        AnalystKind,
    )

    assert ANALYST_KIND_REGISTRY.is_valid("consult_on_demand")
    assert AnalystKind.CONSULT_ON_DEMAND.value == "consult_on_demand"


# ---------------------------------------------------------------------------
# Happy path — substrate-resolvable question
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_substrate_resolvable_question():
    """Question with substrate context:
       Round 1 — LLM calls search_signals
       Round 2 — LLM emits final answer citing the returned UUIDs.

    Assert citations come from the tool's refs, the question is echoed,
    uncertainty is low, unanswered_aspects empty, and the
    FindingPayload carries the structured response in `data`.
    """
    sig_ids = [uuid4() for _ in range(2)]
    substrate = _SubstrateStub(
        signal_rows=[
            {"id": str(sig_ids[0]), "title": "Itaipu hydro upgrade complete"},
            {"id": str(sig_ids[1]), "title": "Northeast wind capacity record"},
        ],
        signal_refs=sig_ids,
    )
    llm = _ScriptedLLMHandler([
        json.dumps({
            "tool": "search_signals",
            "args": {"query": "Brazil energy", "limit": 10},
        }),
        json.dumps({
            "final": True,
            "answer": (
                "Brazil's grid had two notable events: the Itaipu hydro "
                "upgrade completed and a wind-capacity record was set."
            ),
            "uncertainty": 0.2,
            "cited_refs": [str(r) for r in sig_ids],
            "unanswered_aspects": [],
        }),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)

    inputs = [{"question": "What recently happened in Brazil's energy sector?"}]
    options = {"analyst_id": "analyst.consult.default", "run_id": uuid4()}

    result = await run_method(inputs, options, deps)

    # Result shape
    assert isinstance(result, AnalystMethodResult)
    assert isinstance(result.finding, FindingPayload)
    assert isinstance(result.consult_response, ConsultResponsePayload)

    consult = result.consult_response
    assert consult.question.startswith("What recently happened")
    assert "Itaipu" in consult.answer or "wind" in consult.answer
    assert consult.uncertainty == pytest.approx(0.2)
    assert set(consult.cited_substrate_refs) == set(sig_ids)
    assert consult.unanswered_aspects == []

    # The finding wrapper surfaces the consult response payload.
    assert "consult_response" in result.finding.data
    assert result.finding.data["consult_response"]["question"] == consult.question
    assert "consult_on_demand" in result.finding.tags
    # Confidence = 1 - uncertainty.
    assert result.finding.confidence == pytest.approx(0.8)

    # Lineage from the tool's refs.
    assert set(result.derived_from) == set(sig_ids)

    # Aggregate usage = 2 LLM calls × 100/50 tokens.
    assert result.usage["prompt_tokens"] == 200
    assert result.usage["completion_tokens"] == 100

    # The substrate stub got called exactly once via search_signals.
    assert [c[0] for c in substrate.calls] == ["search_signals"]
    assert substrate.calls[0][1]["query"] == "Brazil energy"

    # Intermediate steps trace covers PLAN, REASON×2, ACT, REFLECT,
    # NARRATE.  (No WAKE/PERSIST — that's the inline_target cycle
    # envelope; consult is a single-turn ReAct loop.)
    phases = [s["phase"] for s in result.intermediate_steps]
    assert "plan" in phases
    assert "reason" in phases
    assert "act" in phases
    assert phases.count("reason") == 2          # two LLM rounds
    assert phases[-1] == "narrate"


# ---------------------------------------------------------------------------
# No substrate context — high uncertainty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_substrate_context_high_uncertainty():
    """Question that the tools can't resolve:
       Round 1 — LLM calls search_signals; tool returns empty
       Round 2 — LLM emits final with uncertainty=0.9 and unanswered
                 aspects populated.

    Assert no citations, uncertainty >= 0.7, unanswered_aspects non-empty,
    finding has the "has_unanswered" tag.
    """
    substrate = _SubstrateStub()  # all stores empty
    llm = _ScriptedLLMHandler([
        json.dumps({
            "tool": "search_signals",
            "args": {"query": "Antarctic seismic anomaly"},
        }),
        json.dumps({
            "final": True,
            "answer": (
                "Substrate has no indexed signals about an Antarctic "
                "seismic anomaly."
            ),
            "uncertainty": 0.9,
            "cited_refs": [],
            "unanswered_aspects": [
                "Whether an Antarctic seismic anomaly occurred",
                "Magnitude and location of any such event",
            ],
        }),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)

    inputs = [{"question": "Is there an Antarctic seismic anomaly?"}]
    result = await run_method(inputs, {"analyst_id": "consult.x"}, deps)

    consult = result.consult_response
    assert consult.cited_substrate_refs == []
    assert consult.uncertainty == pytest.approx(0.9)
    assert len(consult.unanswered_aspects) == 2
    assert any("seismic" in a.lower() for a in consult.unanswered_aspects)

    # Finding wrapping — has_unanswered tag attached.
    assert "has_unanswered" in result.finding.tags
    assert result.finding.confidence == pytest.approx(1.0 - 0.9)

    # No lineage to attach.
    assert result.derived_from == []


@pytest.mark.asyncio
async def test_high_uncertainty_empty_unanswered_falls_back_to_question():
    """When the LLM signals high uncertainty (>=0.7) but leaves
    unanswered_aspects empty, the kind backfills with the question itself
    so downstream surfaces always have something to display."""
    substrate = _SubstrateStub()
    llm = _ScriptedLLMHandler([
        json.dumps({
            "final": True,
            "answer": "Unable to assess.",
            "uncertainty": 0.85,
            "cited_refs": [],
            "unanswered_aspects": [],
        }),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)
    result = await run_method(
        [{"question": "What is the current state of X?"}],
        {"analyst_id": "consult.x"},
        deps,
    )
    assert result.consult_response.unanswered_aspects == [
        "What is the current state of X?",
    ]


# ---------------------------------------------------------------------------
# Hallucinated UUID guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hallucinated_uuids_filtered():
    """LLM emits cited_refs containing UUIDs that no tool ever returned.
    The kind filters down to the tool-confirmed subset.  When the LLM
    NARROWS the citation set to a confirmed subset, we honor that
    narrowing.  When the LLM emits zero confirmed refs, we fall back to
    the full collected set."""
    real_ref = uuid4()
    other_real_ref = uuid4()
    hallucinated = uuid4()
    substrate = _SubstrateStub(
        signal_rows=[{"id": str(real_ref), "title": "x"},
                     {"id": str(other_real_ref), "title": "y"}],
        signal_refs=[real_ref, other_real_ref],
    )
    llm = _ScriptedLLMHandler([
        json.dumps({"tool": "search_signals", "args": {"query": "x"}}),
        json.dumps({
            "final": True,
            "answer": "Two events, citing partial subset plus a fake.",
            "uncertainty": 0.3,
            # Planner narrows + hallucinates: real_ref is real,
            # hallucinated is not.
            "cited_refs": [str(real_ref), str(hallucinated)],
            "unanswered_aspects": [],
        }),
    ])
    result = await run_method(
        [{"question": "What happened?"}],
        {"analyst_id": "consult.x"},
        ConsultOnDemandDeps(llm=llm, substrate=substrate),
    )
    # The hallucinated UUID is filtered out; the LLM-narrowed real_ref
    # is honored (planner narrowed from 2 to 1 — that's a legitimate
    # narrowing, not invention).
    assert result.consult_response.cited_substrate_refs == [real_ref]
    # derived_from still carries the full collected set (audit trail).
    assert set(result.derived_from) == {real_ref, other_real_ref}


# ---------------------------------------------------------------------------
# Round cap — forced final
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_cap_forces_final_turn():
    """LLM keeps asking for tools forever.  After MAX_TOOL_ROUNDS we
    issue ONE more synthesis turn with a forced-final system prompt."""
    sig_id = uuid4()
    substrate = _SubstrateStub(
        signal_rows=[{"id": str(sig_id), "title": "an event"}],
        signal_refs=[sig_id],
    )
    # Pad the script with MAX_TOOL_ROUNDS tool calls + one final.
    tool_call = json.dumps({
        "tool": "search_signals",
        "args": {"query": "anything"},
    })
    final_call = json.dumps({
        "final": True,
        "answer": "Reached cap.",
        "uncertainty": 0.6,
        "cited_refs": [str(sig_id)],
        "unanswered_aspects": ["needed more rounds"],
    })
    responses = [tool_call] * MAX_TOOL_ROUNDS + [final_call]
    llm = _ScriptedLLMHandler(responses)
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)

    result = await run_method(
        [{"question": "Detailed multi-step query"}],
        {"analyst_id": "consult.x"},
        deps,
    )

    # We made MAX_TOOL_ROUNDS + 1 LLM calls (the cap rounds + the
    # forced final).
    assert len(llm.calls) == MAX_TOOL_ROUNDS + 1

    # The forced-final turn had a different system prompt (carries the
    # "tool-round cap" guidance).
    forced_call = llm.calls[-1]
    assert "tool-round cap" in (forced_call["system"] or "")

    # The intermediate_steps trace records the forced_final reason step.
    kinds = [s.get("kind") for s in result.intermediate_steps]
    assert "forced_final" in kinds

    # Result was honored.
    assert result.consult_response.answer == "Reached cap."
    # data.forced_final is True.
    assert result.consult_response.data.get("forced_final") is True


@pytest.mark.asyncio
async def test_round_cap_forced_final_empty_response_degrades():
    """If the forced-final turn produces NOTHING (empty/whitespace — no JSON and
    no prose to salvage), the kind returns a degraded ConsultResponsePayload with
    uncertainty=1.0, the original question in unanswered_aspects, and the raw
    final reply captured in `data.raw_final` for audit."""
    sig_id = uuid4()
    substrate = _SubstrateStub(
        signal_rows=[{"id": str(sig_id), "title": "x"}],
        signal_refs=[sig_id],
    )
    tool_call = json.dumps({"tool": "search_signals", "args": {"query": "x"}})
    responses = [tool_call] * MAX_TOOL_ROUNDS + [""]  # empty forced-final
    llm = _ScriptedLLMHandler(responses)
    result = await run_method(
        [{"question": "Original Q"}],
        {"analyst_id": "consult.x"},
        ConsultOnDemandDeps(llm=llm, substrate=substrate),
    )
    consult = result.consult_response
    assert consult.uncertainty == 1.0
    assert consult.answer == ""
    assert "Original Q" in consult.unanswered_aspects
    assert "raw_final" in consult.data
    assert consult.data["raw_final"] == ""
    assert consult.data["forced_final"] is True


@pytest.mark.asyncio
async def test_round_cap_forced_final_salvages_prose():
    """If the forced-final turn writes a PROSE answer (no JSON wrapper), that
    prose IS the terminal answer — salvage it (uncertainty 0.6) rather than
    discarding a real answer as '(no answer produced)'."""
    sig_id = uuid4()
    substrate = _SubstrateStub(
        signal_rows=[{"id": str(sig_id), "title": "x"}],
        signal_refs=[sig_id],
    )
    tool_call = json.dumps({"tool": "search_signals", "args": {"query": "x"}})
    prose = "BLUF: escalation risk is elevated based on the signals reviewed."
    responses = [tool_call] * MAX_TOOL_ROUNDS + [prose]
    llm = _ScriptedLLMHandler(responses)
    result = await run_method(
        [{"question": "Original Q"}],
        {"analyst_id": "consult.x"},
        ConsultOnDemandDeps(llm=llm, substrate=substrate),
    )
    consult = result.consult_response
    assert consult.answer == prose
    assert consult.uncertainty == 0.6
    assert consult.data["forced_final"] is True


# ---------------------------------------------------------------------------
# Self-correction on unparseable mid-loop output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_correction_on_unparseable_midloop():
    """LLM emits garbage in round 1 → kind appends a correction prompt
    → round 2 produces a final answer.  No exception bubbles."""
    substrate = _SubstrateStub()
    llm = _ScriptedLLMHandler([
        "this is not json — the model is confused",
        json.dumps({
            "final": True,
            "answer": "Recovered.",
            "uncertainty": 0.5,
            "cited_refs": [],
            "unanswered_aspects": [],
        }),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)

    result = await run_method(
        [{"question": "Recoverable question"}],
        {"analyst_id": "consult.x"},
        deps,
    )
    assert result.consult_response.answer == "Recovered."

    # The second LLM call's last user message should be the correction
    # prompt.
    second_msgs = llm.calls[1]["messages"]
    last_user = next(m for m in reversed(second_msgs) if m["role"] == "user")
    assert "strict-JSON" in last_user["content"]


# ---------------------------------------------------------------------------
# Tool errors fold into the loop (don't crash)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_error_recovers():
    """A tool that raises is caught and surfaced as a structured error
    into the conversation.  The planner sees the error and can recover."""
    substrate = _SubstrateStub(raise_on="search_signals")
    llm = _ScriptedLLMHandler([
        json.dumps({"tool": "search_signals", "args": {"query": "x"}}),
        json.dumps({
            "final": True,
            "answer": "Tool failed but I can still respond.",
            "uncertainty": 0.7,
            "cited_refs": [],
            "unanswered_aspects": ["the failing-tool query"],
        }),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)
    result = await run_method(
        [{"question": "tolerant?"}],
        {"analyst_id": "consult.x"},
        deps,
    )
    # The kind delivered the synthesized answer (no exception).
    assert "Tool failed" in result.consult_response.answer

    # The error was passed back to the LLM in the second turn's
    # tool-role message.
    second_msgs = llm.calls[1]["messages"]
    tool_msg = next(m for m in second_msgs if m["role"] == "tool")
    assert "tool_failed" in tool_msg["content"]


@pytest.mark.asyncio
async def test_unknown_tool_recovers():
    """LLM requests a tool name that isn't whitelisted.  The dispatcher
    returns an `unknown_tool` structured error rather than raising."""
    substrate = _SubstrateStub()
    llm = _ScriptedLLMHandler([
        json.dumps({
            "tool": "drop_database",   # not whitelisted
            "args": {},
        }),
        json.dumps({
            "final": True,
            "answer": "Recovered after invalid tool",
            "uncertainty": 0.5,
            "cited_refs": [],
            "unanswered_aspects": [],
        }),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)
    result = await run_method(
        [{"question": "test"}],
        {"analyst_id": "consult.x"},
        deps,
    )
    assert result.consult_response.answer.startswith("Recovered")
    # No substrate call happened — dispatcher rejected the unknown tool
    # before invoking anything.
    assert substrate.calls == []


# ---------------------------------------------------------------------------
# LLM error propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_error_propagates():
    """A raised exception in the LLM bubbles up — the runtime classifies
    it (kind_contracts §7).  The kind handler MUST NOT swallow."""
    class _SimulatedTransport(RuntimeError):
        pass

    substrate = _SubstrateStub()
    llm = _ScriptedLLMHandler([], raise_on_call=_SimulatedTransport)
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)
    with pytest.raises(_SimulatedTransport):
        await run_method(
            [{"question": "anything"}],
            {"analyst_id": "consult.x"},
            deps,
        )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_inputs_raises():
    """Inputs without a `question` field is a programming error
    (not a substrate one) — raise loudly so the dispatcher surface
    surfaces a 400."""
    substrate = _SubstrateStub()
    llm = _ScriptedLLMHandler([])
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)

    with pytest.raises(ValueError, match="inputs"):
        await run_method([], {"analyst_id": "consult.x"}, deps)

    with pytest.raises(ValueError, match="question"):
        await run_method(
            [{"other_key": "y"}], {"analyst_id": "consult.x"}, deps,
        )

    with pytest.raises(ValueError, match="non-empty"):
        await run_method(
            [{"question": "   "}], {"analyst_id": "consult.x"}, deps,
        )


# ---------------------------------------------------------------------------
# Scope predicate is plumbed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_predicate_plumbed_to_search_signals():
    """When inputs[0] carries scope_predicate, it gets forwarded to
    SubstrateQueryPort.search_signals so the substrate can apply the
    Starlark filter."""
    sig_id = uuid4()
    substrate = _SubstrateStub(
        signal_rows=[{"id": str(sig_id), "title": "scoped"}],
        signal_refs=[sig_id],
    )
    llm = _ScriptedLLMHandler([
        json.dumps({"tool": "search_signals", "args": {"query": "x"}}),
        json.dumps({
            "final": True,
            "answer": "scoped result",
            "uncertainty": 0.3,
            "cited_refs": [str(sig_id)],
            "unanswered_aspects": [],
        }),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)
    await run_method(
        [{"question": "q", "scope_predicate": "scope_geo('BR')"}],
        {"analyst_id": "consult.x"},
        deps,
    )
    search_call = next(c for c in substrate.calls if c[0] == "search_signals")
    assert search_call[1]["scope_predicate"] == "scope_geo('BR')"


# ---------------------------------------------------------------------------
# Runner adapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consult_on_demand_runner_call_shape():
    """ConsultOnDemandRunner is the AnalystRunFn adapter the runtime
    keeps on _AnalystDeps.run_method. It accepts (inputs, options) and
    returns AnalystMethodResult — matching the spike's
    LLMAnalystRunner.__call__."""
    sig_id = uuid4()
    substrate = _SubstrateStub(
        signal_rows=[{"id": str(sig_id), "title": "x"}],
        signal_refs=[sig_id],
    )
    llm = _ScriptedLLMHandler([
        json.dumps({
            "final": True,
            "answer": "yes",
            "uncertainty": 0.3,
            "cited_refs": [str(sig_id)],
            "unanswered_aspects": [],
        }),
    ])
    runner = ConsultOnDemandRunner(
        llm, substrate, max_tokens=512, temperature=0.4,
    )

    result = await runner(
        [{"question": "Brief"}],
        {"analyst_id": "consult.x"},
    )
    assert isinstance(result, AnalystMethodResult)
    assert llm.calls[0]["max_tokens"] == 512
    assert llm.calls[0]["temperature"] == 0.4


# ---------------------------------------------------------------------------
# Misc unit helpers
# ---------------------------------------------------------------------------


def test_extract_json_handles_markdown_fences():
    raw = '```json\n{"final": true, "answer": "x", "uncertainty": 0.4}\n```'
    parsed = _extract_json(raw)
    assert parsed is not None
    assert parsed["final"] is True
    assert parsed["answer"] == "x"


def test_extract_json_handles_trailing_prose():
    raw = '{"final": true, "answer": "x"} then a bunch of explanation'
    parsed = _extract_json(raw)
    assert parsed is not None
    assert parsed["answer"] == "x"


def test_extract_json_handles_leading_prose():
    # The model sometimes prefixes the JSON with a sentence; start at the brace.
    raw = 'Here is the final JSON:\n{"final": true, "answer": "x", "uncertainty": 0.3}'
    parsed = _extract_json(raw)
    assert parsed is not None
    assert parsed["final"] is True
    assert parsed["answer"] == "x"


def test_extract_json_returns_none_on_garbage():
    assert _extract_json("nope, not json") is None
    assert _extract_json("") is None
    assert _extract_json("[1, 2, 3]") is None  # not an object


def test_extract_json_string_aware_brace_inside_string_value():
    """REGRESSION: a naive depth-counter closes on the first ``}`` even when it
    is INSIDE a quoted string value, truncating the object into invalid JSON.
    The string-aware matcher must treat in-string braces as literal and parse
    the whole object."""
    raw = '{"final": true, "answer": "the balanced set is {a} but not }b{"}'
    parsed = _extract_json(raw)
    assert parsed is not None
    assert parsed["final"] is True
    assert parsed["answer"] == "the balanced set is {a} but not }b{"


def test_extract_json_string_aware_honors_escaped_quote():
    """An escaped quote (\\") inside a string value must NOT be read as the
    string terminator, so a brace after it stays in-string and doesn't shift
    the structural depth."""
    raw = r'{"answer": "he said \"done}\" and left", "uncertainty": 0.3}'
    parsed = _extract_json(raw)
    assert parsed is not None
    assert parsed["answer"] == 'he said "done}" and left'
    assert parsed["uncertainty"] == 0.3


def test_extract_json_string_aware_with_trailing_prose():
    """Brace-in-string plus trailing prose past the real close still parses."""
    raw = (
        '{"tool": "search_signals", "args": {"query": "set {x,y} and }z{"}} '
        "then the model rambled on"
    )
    parsed = _extract_json(raw)
    assert parsed is not None
    assert parsed["tool"] == "search_signals"
    assert parsed["args"]["query"] == "set {x,y} and }z{"


def test_consult_response_payload_schema_invariants():
    """ConsultResponsePayload enforces the shape L-178 specifies."""
    p = ConsultResponsePayload(
        question="Q?",
        answer="A.",
        cited_substrate_refs=[uuid4(), uuid4()],
        uncertainty=0.4,
        unanswered_aspects=["one"],
    )
    assert p.kind_marker == "consult_response"
    assert len(p.cited_substrate_refs) == 2

    # uncertainty bounded [0, 1].
    from pydantic import ValidationError as _VE
    with pytest.raises(_VE):
        ConsultResponsePayload(question="Q", uncertainty=1.5)
    with pytest.raises(_VE):
        ConsultResponsePayload(question="Q", uncertainty=-0.1)

    # extra='forbid' — unknown fields rejected.
    with pytest.raises(_VE):
        ConsultResponsePayload(question="Q", bogus_field="x")


# ---------------------------------------------------------------------------
# DSPy module (Wave B prereq #4 — backfilled 2026-05-21)
# ---------------------------------------------------------------------------


def test_build_prompt_module_returns_dspy_module():
    """Wave B prereq #4: build_prompt_module returns a real dspy.Module
    instance (the per-round ReAct step) instead of raising ``not yet wired``.

    The outer ReAct loop in run_method() still orchestrates tool dispatch
    in pure Python; only the per-round LLM-bearing step is the DSPy
    optimization surface.
    """
    pytest.importorskip("dspy")
    from legba.prompts.consult_on_demand.v1 import ConsultOnDemandRound
    mod = build_prompt_module()
    assert isinstance(mod, ConsultOnDemandRound)


# ===========================================================================
# Piece 1 — Consult chat rework
# ===========================================================================

from legba.data.analysts.consult_on_demand import (  # noqa: E402
    CHAT_DEFAULT_ROUNDS,
    ROUNDS_CEILING,
    _SYSTEM_PROMPT,
)


# ---------------------------------------------------------------------------
# T1 — max_tool_rounds threads to the effective per-run round count (D1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rounds_override_caps_loop():
    """``inputs[0]['max_tool_rounds']`` overrides ``deps.max_rounds`` for this
    run. With max_tool_rounds=2 and a planner that never finals, we expect
    exactly 2 loop rounds + 1 forced-final = 3 LLM calls."""
    substrate = _SubstrateStub()
    tool_call = json.dumps({"tool": "search_signals", "args": {"query": "x"}})
    # Plenty of tool calls scripted; the final fallback closes the forced turn.
    llm = _ScriptedLLMHandler([tool_call] * 10)
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)

    result = await run_method(
        [{"question": "q", "max_tool_rounds": 2}],
        {"analyst_id": "consult.x"},
        deps,
    )
    # 2 loop rounds + 1 forced final.
    assert len(llm.calls) == 3
    assert result.consult_response.data.get("rounds_used") == 2
    assert result.consult_response.data.get("forced_final") is True


@pytest.mark.asyncio
async def test_rounds_override_clamped_to_ceiling():
    """A request for an absurd round count clamps to ROUNDS_CEILING (30)."""
    substrate = _SubstrateStub()
    tool_call = json.dumps({"tool": "search_signals", "args": {"query": "x"}})
    # Script enough tool calls to exceed the ceiling; the forced final closes.
    llm = _ScriptedLLMHandler([tool_call] * (ROUNDS_CEILING + 5))
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)

    await run_method(
        [{"question": "q", "max_tool_rounds": 999}],
        {"analyst_id": "consult.x"},
        deps,
    )
    # ROUNDS_CEILING loop rounds + 1 forced final.
    assert len(llm.calls) == ROUNDS_CEILING + 1


@pytest.mark.asyncio
async def test_rounds_absent_uses_deps_default():
    """With no override, the loop honors ``deps.max_rounds`` (6 here)."""
    substrate = _SubstrateStub()
    tool_call = json.dumps({"tool": "search_signals", "args": {"query": "x"}})
    llm = _ScriptedLLMHandler([tool_call] * 10)
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)  # max_rounds=6
    assert deps.max_rounds == MAX_TOOL_ROUNDS == 6

    await run_method(
        [{"question": "q"}],
        {"analyst_id": "consult.x"},
        deps,
    )
    # 6 loop rounds + 1 forced final.
    assert len(llm.calls) == 7


@pytest.mark.asyncio
async def test_rounds_override_via_options():
    """The override is also honored when supplied via ``options`` rather than
    the question row."""
    substrate = _SubstrateStub()
    tool_call = json.dumps({"tool": "search_signals", "args": {"query": "x"}})
    llm = _ScriptedLLMHandler([tool_call] * 10)
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)
    await run_method(
        [{"question": "q"}],
        {"analyst_id": "consult.x", "max_tool_rounds": 1},
        deps,
    )
    assert len(llm.calls) == 2  # 1 loop + 1 forced final


def test_rounds_constants():
    assert CHAT_DEFAULT_ROUNDS == 10
    assert ROUNDS_CEILING == 30


# ---------------------------------------------------------------------------
# F1 model picker — per-request LLM plane override in the run path
# ---------------------------------------------------------------------------


def _final_answer(text: str = "ok") -> str:
    return json.dumps({
        "final": True,
        "answer": text,
        "uncertainty": 0.2,
        "cited_refs": [],
        "unanswered_aspects": [],
    })


@pytest.mark.asyncio
async def test_llm_override_uses_resolved_handler():
    """When the question row carries ``llm_component_override`` AND a resolver is
    wired, the run uses the FRESHLY RESOLVED handler for its LLM calls — not the
    cached primary — and the resolved plane's subprovider surfaces on the payload.
    """
    primary = _ScriptedLLMHandler([_final_answer("from primary")])
    primary.subprovider = "opus-primary"
    override = _ScriptedLLMHandler([_final_answer("from core")])
    override.subprovider = "core-override"

    resolved: list[str] = []

    async def _resolver(component_id: str):
        resolved.append(component_id)
        return override

    deps = ConsultOnDemandDeps(
        llm=primary, substrate=_SubstrateStub(), resolve_llm_component=_resolver,
    )
    result = await run_method(
        [{"question": "q", "llm_component_override": "llm.primary.openai_compat"}],
        {"analyst_id": "consult.x"},
        deps,
    )

    # The override handler answered; the cached primary was never called.
    assert resolved == ["llm.primary.openai_compat"]
    assert len(override.calls) == 1
    assert len(primary.calls) == 0
    assert result.consult_response.answer == "from core"
    assert result.consult_response.data.get("subprovider") == "core-override"


@pytest.mark.asyncio
async def test_no_override_keeps_cached_primary():
    """Absent an override, the resolver is NEVER called and the cached primary
    handler answers — the default-preserving contract."""
    primary = _ScriptedLLMHandler([_final_answer("from primary")])
    primary.subprovider = "opus-primary"

    called = False

    async def _resolver(component_id: str):
        nonlocal called
        called = True
        raise AssertionError("resolver must not run without an override")

    deps = ConsultOnDemandDeps(
        llm=primary, substrate=_SubstrateStub(), resolve_llm_component=_resolver,
    )
    result = await run_method(
        [{"question": "q"}], {"analyst_id": "consult.x"}, deps,
    )
    assert called is False
    assert len(primary.calls) == 1
    assert result.consult_response.data.get("subprovider") == "opus-primary"


@pytest.mark.asyncio
async def test_override_with_no_resolver_fails_closed():
    """An override on the question row with NO resolver wired FAILS CLOSED — it
    must never silently fall back to (and bill) the cached primary (F-A). The
    primary is never called; a clear ``llm plane`` error is raised (the actor
    surfaces it as a hard failure)."""
    primary = _ScriptedLLMHandler([_final_answer("from primary")])
    primary.subprovider = "opus-primary"
    deps = ConsultOnDemandDeps(llm=primary, substrate=_SubstrateStub())
    with pytest.raises(ValueError, match="llm plane"):
        await run_method(
            [{"question": "q", "llm_component_override": "llm.primary.openai_compat"}],
            {"analyst_id": "consult.x"},
            deps,
        )
    assert len(primary.calls) == 0


@pytest.mark.asyncio
async def test_override_resolve_failure_fails_closed():
    """If the resolver RAISES (e.g. the core plane is down), the run FAILS CLOSED
    — it must NOT degrade to the cached primary (F-A: that would silently bill
    Opus while echoing 'core' + recording $0). The error names the unavailable
    plane; the primary is never called."""
    primary = _ScriptedLLMHandler([_final_answer("from primary")])
    primary.subprovider = "opus-primary"

    async def _resolver(component_id: str):
        raise RuntimeError("core plane unreachable")

    deps = ConsultOnDemandDeps(
        llm=primary, substrate=_SubstrateStub(), resolve_llm_component=_resolver,
    )
    with pytest.raises(ValueError, match="unavailable"):
        await run_method(
            [{"question": "q", "llm_component_override": "llm.primary.openai_compat"}],
            {"analyst_id": "consult.x"},
            deps,
        )
    assert len(primary.calls) == 0


# ---------------------------------------------------------------------------
# T2 — broad-first system prompt (D2) without clobbering the loop protocol
# ---------------------------------------------------------------------------


def test_system_prompt_is_broad_first():
    # The strategy now leads with the platform's OWN finished intelligence
    # (findings/situations) and falls back to a broad raw-substrate survey to
    # verify / fill gaps rather than re-derive from scratch.
    assert "list_findings / list_situations" in _SYSTEM_PROMPT
    assert "survey broadly" in _SYSTEM_PROMPT


def test_system_prompt_keeps_loop_protocol():
    # The JSON loop-protocol contract the parser depends on must survive.
    assert "Loop protocol:" in _SYSTEM_PROMPT
    assert '{"tool": "<name>", "args": {...}}' in _SYSTEM_PROMPT
    assert '{"final": true' in _SYSTEM_PROMPT
    # Output-discipline (JSON only, no fences) now lives in the shared tradecraft
    # preamble that is composed into _SYSTEM_PROMPT.
    assert "no prose, no markdown fences" in _SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# T3 — multi-turn message seeding (D6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prior_messages_seed_conversation():
    """Prior turns on ``inputs[0]['messages']`` seed the ReAct conversation;
    the rendered current question is appended last."""
    substrate = _SubstrateStub()
    llm = _ScriptedLLMHandler([
        json.dumps({
            "final": True, "answer": "ok", "uncertainty": 0.3,
            "cited_refs": [], "unanswered_aspects": [],
        }),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)
    await run_method(
        [{
            "question": "and now?",
            "messages": [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
            ],
        }],
        {"analyst_id": "consult.x"},
        deps,
    )
    first_msgs = llm.calls[0]["messages"]
    assert first_msgs[0] == {"role": "user", "content": "first question"}
    assert first_msgs[1] == {"role": "assistant", "content": "first answer"}
    # Last message is the rendered current question.
    assert first_msgs[-1]["role"] == "user"
    assert "and now?" in first_msgs[-1]["content"]


@pytest.mark.asyncio
async def test_prior_messages_filter_bad_roles_and_clamp():
    """Non-user/assistant roles are dropped; oversized content is clamped."""
    substrate = _SubstrateStub()
    llm = _ScriptedLLMHandler([
        json.dumps({
            "final": True, "answer": "ok", "uncertainty": 0.3,
            "cited_refs": [], "unanswered_aspects": [],
        }),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)
    big = "z" * 20000
    await run_method(
        [{
            "question": "q",
            "messages": [
                {"role": "system", "content": "injected system"},
                {"role": "tool", "content": "tool junk"},
                {"role": "user", "content": big},
            ],
        }],
        {"analyst_id": "consult.x"},
        deps,
    )
    seeded = [
        m for m in llm.calls[0]["messages"][:-1]
    ]  # everything except the current question
    # Only the user message survives; system/tool dropped.
    assert len(seeded) == 1
    assert seeded[0]["role"] == "user"
    assert len(seeded[0]["content"]) == 16000  # clamped


# ---------------------------------------------------------------------------
# T4 — step publish mirrors the durable trace 1:1 (D5, single source of truth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_publish_mirrors_intermediate_steps():
    """When ``step_publish`` is wired, every recorded step is published; the
    collected frames equal ``result.intermediate_steps`` exactly."""
    sig_id = uuid4()
    substrate = _SubstrateStub(
        signal_rows=[{"id": str(sig_id), "title": "x"}],
        signal_refs=[sig_id],
    )
    llm = _ScriptedLLMHandler([
        json.dumps({"tool": "search_signals", "args": {"query": "x"}}),
        json.dumps({
            "final": True, "answer": "done", "uncertainty": 0.3,
            "cited_refs": [str(sig_id)], "unanswered_aspects": [],
        }),
    ])
    collected: list[dict[str, Any]] = []

    async def _publish(step: dict[str, Any]) -> None:
        collected.append(step)

    deps = ConsultOnDemandDeps(
        llm=llm, substrate=substrate, step_publish=_publish,
    )
    result = await run_method(
        [{"question": "q"}],
        {"analyst_id": "consult.x"},
        deps,
    )
    assert collected == result.intermediate_steps
    # Sanity: the trace actually has the loop phases.
    phases = [s["phase"] for s in collected]
    assert "plan" in phases and "act" in phases and phases[-1] == "narrate"


@pytest.mark.asyncio
async def test_step_publish_failure_never_breaks_run():
    """A publisher that raises must not abort the run (telemetry is best-
    effort)."""
    substrate = _SubstrateStub()
    llm = _ScriptedLLMHandler([
        json.dumps({
            "final": True, "answer": "ok", "uncertainty": 0.3,
            "cited_refs": [], "unanswered_aspects": [],
        }),
    ])

    async def _boom(step: dict[str, Any]) -> None:
        raise RuntimeError("nats down")

    deps = ConsultOnDemandDeps(
        llm=llm, substrate=substrate, step_publish=_boom,
    )
    result = await run_method(
        [{"question": "q"}],
        {"analyst_id": "consult.x"},
        deps,
    )
    # Run still completed.
    assert result.consult_response.answer == "ok"


# ---------------------------------------------------------------------------
# S8-T6 — consult observability: durable step trace + raw-unparseable salvage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_carries_full_step_trace_in_payload_data():
    """The FULL per-round ReAct step trace rides on
    ``consult_response.data['steps']`` so the consult front door can persist it
    into ``consult_turns.steps`` (previously never populated). The projected
    ``tool_calls`` summary lives alongside it, unchanged."""
    sig_id = uuid4()
    substrate = _SubstrateStub(
        signal_rows=[{"id": str(sig_id), "title": "x"}],
        signal_refs=[sig_id],
    )
    llm = _ScriptedLLMHandler([
        json.dumps({"tool": "search_signals", "args": {"query": "x"}}),
        json.dumps({
            "final": True, "answer": "done", "uncertainty": 0.3,
            "cited_refs": [str(sig_id)], "unanswered_aspects": [],
        }),
    ])
    result = await run_method(
        [{"question": "q"}],
        {"analyst_id": "consult.x"},
        ConsultOnDemandDeps(llm=llm, substrate=substrate),
    )
    steps = result.consult_response.data.get("steps")
    assert isinstance(steps, list) and steps, "steps trace must be persisted"
    phases = [s.get("phase") for s in steps]
    assert "plan" in phases and "reason" in phases and "act" in phases
    # The persisted steps mirror the returned intermediate_steps (source of truth).
    assert steps == result.intermediate_steps[: len(steps)]
    # The projected tool_calls summary is still there for the SPA.
    assert result.consult_response.data.get("tool_calls")


@pytest.mark.asyncio
async def test_unparseable_midloop_persists_raw_in_step_trace():
    """A round whose JSON fails to parse records the RAW reply on the
    ``unparseable`` step so it leaves a debuggable trail instead of vanishing;
    the loop still recovers to a final answer."""
    substrate = _SubstrateStub()
    garbage = "this is not json — the model rambled {unterminated"
    llm = _ScriptedLLMHandler([
        garbage,
        json.dumps({
            "final": True, "answer": "Recovered.", "uncertainty": 0.5,
            "cited_refs": [], "unanswered_aspects": [],
        }),
    ])
    result = await run_method(
        [{"question": "q"}],
        {"analyst_id": "consult.x"},
        ConsultOnDemandDeps(llm=llm, substrate=substrate),
    )
    assert result.consult_response.answer == "Recovered."
    unparse = [
        s for s in result.intermediate_steps if s.get("kind") == "unparseable"
    ]
    assert unparse, "expected an unparseable step"
    assert unparse[0]["raw"] == garbage
    # And it rode into the durable payload trace, not just the in-memory list.
    persisted = result.consult_response.data.get("steps") or []
    assert any(
        s.get("kind") == "unparseable" and s.get("raw") == garbage
        for s in persisted
    )


# ---------------------------------------------------------------------------
# S4 read tools — ReAct integration (T10/T11)
#
# These drive the SAME single-turn ReAct loop through the four S4 readers
# (query_nexuses / query_hypotheses / get_timeline / compare_targets) via
# the ungoverned direct-port path, asserting the planner's tool args reach
# the port and the returned refs flow into the consult response's
# cited_substrate_refs.  The port's real SQL is covered separately in
# tests/runtime/test_substrate_query_port.py (hand-seeded rows).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_react_query_nexuses_round_trips_refs():
    """Planner calls query_nexuses; the returned nexus refs surface as
    citations and the tool args (subject/object/polarity) reach the port."""
    nexus_ids = [uuid4(), uuid4()]
    substrate = _SubstrateStub(
        nexus_rows=[
            {"id": str(nexus_ids[0]), "subject": "Atlantis",
             "object": "Eldoria", "rel_type": "AlliedWith", "polarity": 1},
            {"id": str(nexus_ids[1]), "subject": "Atlantis",
             "object": "Borealia", "rel_type": "HostileTo", "polarity": -1},
        ],
        nexus_refs=nexus_ids,
    )
    llm = _ScriptedLLMHandler([
        json.dumps({
            "tool": "query_nexuses",
            "args": {"subject": "Atlantis", "polarity": -1, "limit": 10},
        }),
        json.dumps({
            "final": True,
            "answer": "Atlantis is hostile to Borealia and allied with Eldoria.",
            "uncertainty": 0.25,
            "cited_refs": [str(r) for r in nexus_ids],
            "unanswered_aspects": [],
        }),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)

    result = await run_method(
        [{"question": "Who is Atlantis hostile to?"}],
        {"analyst_id": "consult.x"},
        deps,
    )

    assert [c[0] for c in substrate.calls] == ["query_nexuses"]
    args = substrate.calls[0][1]
    assert args["subject"] == "Atlantis"
    assert args["polarity"] == -1
    assert set(result.consult_response.cited_substrate_refs) == set(nexus_ids)
    assert set(result.derived_from) == set(nexus_ids)


@pytest.mark.asyncio
async def test_react_query_hypotheses_round_trips_refs():
    """Planner calls query_hypotheses; ACH refs flow into the citation
    set and the target_id/status filters reach the port."""
    hyp_ids = [uuid4()]
    substrate = _SubstrateStub(
        hypothesis_rows=[
            {"id": str(hyp_ids[0]), "thesis": "Coup will fail",
             "status": "confirmed", "evidence_balance": 3,
             "resolved_outcome": 1, "resolved_by": "subsequent_facts"},
        ],
        hypothesis_refs=hyp_ids,
    )
    llm = _ScriptedLLMHandler([
        json.dumps({
            "tool": "query_hypotheses",
            "args": {"target_id": "country.utopia", "status": "confirmed"},
        }),
        json.dumps({
            "final": True,
            "answer": "One confirmed hypothesis: the coup will fail.",
            "uncertainty": 0.3,
            "cited_refs": [str(r) for r in hyp_ids],
            "unanswered_aspects": [],
        }),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)

    result = await run_method(
        [{"question": "What ACH hypotheses are confirmed for Utopia?"}],
        {"analyst_id": "consult.x"},
        deps,
    )

    assert [c[0] for c in substrate.calls] == ["query_hypotheses"]
    args = substrate.calls[0][1]
    assert args["target_id"] == "country.utopia"
    assert args["status"] == "confirmed"
    assert set(result.consult_response.cited_substrate_refs) == set(hyp_ids)


@pytest.mark.asyncio
async def test_react_get_timeline_round_trips_refs():
    """Planner calls get_timeline; the merged timeline item refs surface as
    citations and the subject reaches the port."""
    item_ids = [uuid4(), uuid4()]
    substrate = _SubstrateStub(
        timeline_items=[
            {"kind": "signal", "id": str(item_ids[0]),
             "at": "2026-05-01T00:00:00+00:00", "title": "Pact signed"},
            {"kind": "fact", "id": str(item_ids[1]),
             "at": "2026-01-01T00:00:00+00:00", "predicate": "capital"},
        ],
        timeline_refs=item_ids,
    )
    llm = _ScriptedLLMHandler([
        json.dumps({
            "tool": "get_timeline",
            "args": {"subject": "Timelinia", "limit": 20},
        }),
        json.dumps({
            "final": True,
            "answer": "Timelinia signed a pact in May after a Jan capital fact.",
            "uncertainty": 0.2,
            "cited_refs": [str(r) for r in item_ids],
            "unanswered_aspects": [],
        }),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)

    result = await run_method(
        [{"question": "What is the recent timeline for Timelinia?"}],
        {"analyst_id": "consult.x"},
        deps,
    )

    assert [c[0] for c in substrate.calls] == ["get_timeline"]
    assert substrate.calls[0][1]["subject"] == "Timelinia"
    assert set(result.consult_response.cited_substrate_refs) == set(item_ids)


@pytest.mark.asyncio
async def test_react_compare_targets_needs_multiple_ids():
    """compare_targets requires >=2 target_ids: a single-id call returns a
    structured error the planner sees and recovers from by re-calling with
    two ids."""
    substrate = _SubstrateStub()
    llm = _ScriptedLLMHandler([
        # Round 1 — planner mistakenly passes one id; port returns an error.
        json.dumps({
            "tool": "compare_targets",
            "args": {"target_ids": ["country.utopia"]},
        }),
        # Round 2 — planner corrects with two ids.
        json.dumps({
            "tool": "compare_targets",
            "args": {"target_ids": ["country.utopia", "country.arcadia"]},
        }),
        json.dumps({
            "final": True,
            "answer": "Compared Utopia and Arcadia rollups.",
            "uncertainty": 0.3,
            "cited_refs": [],
            "unanswered_aspects": [],
        }),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate)

    result = await run_method(
        [{"question": "Compare Utopia and Arcadia."}],
        {"analyst_id": "consult.x"},
        deps,
    )

    # Both compare_targets calls reached the port.
    assert [c[0] for c in substrate.calls] == [
        "compare_targets", "compare_targets",
    ]
    assert substrate.calls[0][1]["target_ids"] == ["country.utopia"]
    assert substrate.calls[1][1]["target_ids"] == [
        "country.utopia", "country.arcadia",
    ]
    assert result.consult_response.answer.startswith("Compared")


@pytest.mark.asyncio
async def test_react_new_tools_are_known_and_dispatchable():
    """The four S4 readers are in _KNOWN_TOOLS and dispatchable — an unknown
    name still surfaces the structured unknown_tool error (regression guard
    that the additions didn't break the unknown-name path)."""
    from legba.data.analysts.consult_on_demand import _KNOWN_TOOLS, _dispatch_tool

    for name in (
        "query_nexuses", "query_hypotheses", "get_timeline", "compare_targets",
    ):
        assert name in _KNOWN_TOOLS

    substrate = _SubstrateStub()
    out = await _dispatch_tool(
        substrate, name="not_a_real_tool", args={}, scope_predicate=None,
    )
    assert out["error"].startswith("unknown_tool")


@pytest.mark.asyncio
async def test_finished_intelligence_tools_are_known_and_dispatchable():
    """The five finished-intelligence / navigation readers (palette expansion)
    are in _KNOWN_TOOLS, dispatch to the right port method with the right args,
    and pass their {rows, refs, count} envelope back through unchanged."""
    from legba.data.analysts.consult_on_demand import _KNOWN_TOOLS, _dispatch_tool

    for name in (
        "list_findings", "list_situations", "query_predictions",
        "list_targets", "list_sources",
    ):
        assert name in _KNOWN_TOOLS

    fid = uuid4()
    sid = uuid4()
    pid = uuid4()
    substrate = _SubstrateStub(
        finding_rows=[{"output_id": str(fid), "title": "Iran assessment"}],
        finding_refs=[fid],
        situation_rows=[{"situation_id": str(sid), "name": "Strait tension"}],
        situation_refs=[sid],
        prediction_rows=[{"output_id": str(pid), "point_estimate": 12.0}],
        prediction_refs=[pid],
        target_rows=[{"target_id": "country_g20_ir", "name": "Iran"}],
        source_rows=[{"source_id": "nws", "name": "NWS", "silent_hours": 2}],
    )

    out = await _dispatch_tool(
        substrate, name="list_findings",
        args={"target_id": "country_g20_ir", "since_hours": "48", "limit": 5},
        scope_predicate=None,
    )
    assert out["count"] == 1 and out["refs"] == [str(fid)]
    assert substrate.calls[-1] == (
        "list_findings",
        {"target_id": "country_g20_ir", "analyst_id": None, "severity": None,
         "since_hours": 48, "include_superseded": False, "limit": 5},
    )

    out = await _dispatch_tool(
        substrate, name="list_situations",
        args={"status": "open"}, scope_predicate=None,
    )
    assert out["refs"] == [str(sid)]
    assert substrate.calls[-1][0] == "list_situations"
    assert substrate.calls[-1][1]["status"] == "open"

    out = await _dispatch_tool(
        substrate, name="query_predictions",
        args={"target_id": "country_g20_ir"}, scope_predicate=None,
    )
    assert out["refs"] == [str(pid)]
    assert substrate.calls[-1][0] == "query_predictions"

    out = await _dispatch_tool(
        substrate, name="list_targets", args={}, scope_predicate=None,
    )
    assert out["rows"][0]["target_id"] == "country_g20_ir"
    assert substrate.calls[-1] == ("list_targets", {"active_only": True})

    out = await _dispatch_tool(
        substrate, name="list_sources",
        args={"silent_only": True}, scope_predicate=None,
    )
    assert out["rows"][0]["source_id"] == "nws"
    assert substrate.calls[-1][1]["silent_only"] is True


@pytest.mark.asyncio
async def test_finished_intelligence_tool_errors_are_structured():
    """A port failure on a finished-intelligence tool surfaces the structured
    error envelope, not an exception that aborts the loop."""
    from legba.data.analysts.consult_on_demand import _dispatch_tool

    substrate = _SubstrateStub(raise_on="list_findings")
    out = await _dispatch_tool(
        substrate, name="list_findings", args={}, scope_predicate=None,
    )
    assert "error" in out


@pytest.mark.asyncio
async def test_search_context_is_known_and_dispatchable():
    """S5-T4: search_context is in _KNOWN_TOOLS, dispatches to the port with
    the corpus/country/k args coerced, and passes its {rows, refs, count}
    envelope back unchanged (so the consult loop can collect the chunk refs)."""
    from legba.data.analysts.consult_on_demand import _KNOWN_TOOLS, _dispatch_tool

    assert "search_context" in _KNOWN_TOOLS

    cid = uuid4()
    substrate = _SubstrateStub(
        context_rows=[{
            "chunk_id": str(cid),
            "corpus": "world_context",
            "doc_id": "iran-brief",
            "title": "Iran leadership brief",
            "section": "Succession",
            "countries": ["ir"],
            "source_url": "https://example.invalid/iran",
            "effective_date": "2026-01-01",
            "text": "Background prior on Iran succession dynamics.",
            "score": 0.91,
        }],
        context_refs=[cid],
    )

    out = await _dispatch_tool(
        substrate, name="search_context",
        args={"query": "iran succession", "corpus": "world_context",
              "country": "ir", "k": "4"},
        scope_predicate=None,
    )
    assert out["count"] == 1 and out["refs"] == [str(cid)]
    assert out["rows"][0]["corpus"] == "world_context"
    # Args are coerced (k -> int) and the non-substrate scope_predicate is NOT
    # forwarded (search_context takes no scope_predicate).
    assert substrate.calls[-1] == (
        "search_context",
        {"query": "iran succession", "corpus": "world_context",
         "country": "ir", "k": 4},
    )


@pytest.mark.asyncio
async def test_search_context_error_is_structured():
    """A port failure on search_context surfaces the structured error envelope
    rather than an exception that aborts the loop."""
    from legba.data.analysts.consult_on_demand import _dispatch_tool

    substrate = _SubstrateStub(raise_on="search_context")
    out = await _dispatch_tool(
        substrate, name="search_context", args={"query": "x"}, scope_predicate=None,
    )
    assert "error" in out


# ===========================================================================
# Consult-flow optimization (2026-06-24): double-envelope unwrap, survey-then-
# drill prompt, multi-tool batch per round, tool-trace surfacing.
# ===========================================================================


# ---------------------------------------------------------------------------
# Double-envelope unwrap — the planner sometimes nests a whole {"final":...}
# envelope INSIDE the answer string, so the UI renders a raw JSON block.
# ---------------------------------------------------------------------------


def test_unwrap_double_envelope_clean_nested():
    """Well-formed nested envelope (inner newlines escaped, so it parses) →
    lift the inner answer prose."""
    from legba.data.analysts.consult_on_demand import _unwrap_double_envelope

    # The answer FIELD value is the inner JSON TEXT (escaped newlines = valid).
    inner_text = json.dumps({
        "final": True,
        "answer": "## Bottom line\n\nThe world is tense.",
        "uncertainty": 0.3,
    })
    assert _unwrap_double_envelope(inner_text) == "## Bottom line\n\nThe world is tense."


def test_unwrap_double_envelope_malformed_unescaped_newlines():
    """Inner has REAL (unescaped) newlines → json.loads on it fails; regex-lift
    the answer body, anchoring on the trailing uncertainty key — and the raw
    {"final"...} text never leaks into the prose."""
    from legba.data.analysts.consult_on_demand import _unwrap_double_envelope

    nested = (
        '{"final": true, "answer": "## Heading\n\n- bullet one\n- bullet two",'
        ' "uncertainty": 0.4}'
    )
    out = _unwrap_double_envelope(nested)
    assert out == "## Heading\n\n- bullet one\n- bullet two"
    assert '"final"' not in out and '"uncertainty"' not in out


def test_unwrap_double_envelope_malformed_no_tail_key():
    """Malformed inner with NO field after answer → anchor on the closing brace."""
    from legba.data.analysts.consult_on_demand import _unwrap_double_envelope

    nested = '{"final": true, "answer": "line A\nline B"}'
    assert _unwrap_double_envelope(nested) == "line A\nline B"


def test_unwrap_double_envelope_plain_markdown_untouched():
    """A normal markdown answer (not envelope-shaped) is returned verbatim."""
    from legba.data.analysts.consult_on_demand import _unwrap_double_envelope

    md = "## Bottom line\n\nIran-Israel tensions remain elevated. **No** change."
    assert _unwrap_double_envelope(md) == md


def test_unwrap_double_envelope_brace_without_final_untouched():
    """A brace-leading answer that is NOT a final-envelope (no final/answer keys)
    is left alone — no over-eager unwrap that eats legitimate content."""
    from legba.data.analysts.consult_on_demand import _unwrap_double_envelope

    txt = '{"note": "this is literally about a JSON object"} and more prose'
    assert _unwrap_double_envelope(txt) == txt


def test_unwrap_double_envelope_preserves_utf8():
    """Salvage must not corrupt real UTF-8 (em-dash, smart quotes) — the reason
    we hand-decode escapes instead of unicode_escape."""
    from legba.data.analysts.consult_on_demand import _unwrap_double_envelope

    nested = (
        '{"final": true, "answer": "Iran — “tense” state\nrising",'
        ' "uncertainty": 0.5}'
    )
    out = _unwrap_double_envelope(nested)
    assert "—" in out and "“tense”" in out
    assert "rising" in out


def test_unwrap_double_envelope_decoy_tailkey_not_truncated():
    """A malformed nested envelope whose inner answer PROSE contains a decoy
    '","uncertainty":' sequence (e.g. it quotes a JSON example) must anchor on
    the RIGHTMOST (real) tail boundary, not the in-prose decoy — so the
    legitimate answer is NOT truncated. (Review HIGH finding #2.)"""
    from legba.data.analysts.consult_on_demand import _unwrap_double_envelope

    nested = (
        '{"final": true, "answer": "## Heading\n\nExample: '
        '{"a":"x","uncertainty":0}. Done.\n\n- bullet", "uncertainty": 0.2}'
    )
    out = _unwrap_double_envelope(nested)
    assert out.startswith("## Heading")
    assert "Done." in out and "- bullet" in out   # the tail prose survived


def test_unwrap_double_envelope_returns_original_when_anchor_would_eat_most():
    """If the only tail anchor sits near the START (a false positive that would
    discard most of the body), degrade to the ORIGINAL — never eat content.
    (Review finding #6 — the 'never eat content' guarantee.)"""
    from legba.data.analysts.consult_on_demand import _unwrap_double_envelope

    nested = (
        '{"final": true, "answer": "x","uncertainty": then a very long legitimate '
        'continuation of the real answer that makes up the bulk of the content and '
        'must not be discarded by a false early anchor here and here and here"}'
    )
    out = _unwrap_double_envelope(nested)
    assert out == nested   # the length guard refused to truncate


def test_unwrap_double_envelope_recovers_truncated_envelope():
    """A forced-final envelope truncated by max_tokens mid-string (no closing
    brace, no tail key) → strip the `{"final":...,"answer":"` prefix and return
    the recovered markdown rather than render raw JSON. (Live-observed case:
    answer cut at the token cap before its closing `"}`.)"""
    from legba.data.analysts.consult_on_demand import _unwrap_double_envelope

    truncated = (
        '{"final": true, "answer": "## World Report\\n\\nBLUF: tense.\\n\\n'
        'More detail that was cut off mid-sen'
    )
    out = _unwrap_double_envelope(truncated)
    assert out.startswith("## World Report")
    assert "BLUF: tense." in out
    assert not out.startswith("{")   # the JSON prefix is gone


def test_unescape_json_str_decodes_unicode_escapes():
    """The malformed-path unescape decodes \\uXXXX (review LOW finding #4) while
    leaving real multibyte literals untouched."""
    from legba.data.analysts.consult_on_demand import _unescape_json_str

    assert _unescape_json_str("Iran \\u2014 tense") == "Iran — tense"
    assert _unescape_json_str("a\\nb\\tc") == "a\nb\tc"
    assert _unescape_json_str("café —") == "café —"   # real UTF-8 untouched


@pytest.mark.asyncio
async def test_double_envelope_repaired_end_to_end():
    """A planner that double-wraps its final answer → the kind unwraps it so the
    consult payload's answer is clean prose, not a raw JSON block."""
    inner_text = json.dumps({
        "final": True,
        "answer": "## BLUF\n\nEscalation risk is elevated.",
        "uncertainty": 0.4,
    })
    llm = _ScriptedLLMHandler([
        json.dumps({"final": True, "answer": inner_text, "uncertainty": 0.4}),
    ])
    result = await run_method(
        [{"question": "q"}], {"analyst_id": "consult.x"},
        ConsultOnDemandDeps(llm=llm, substrate=_SubstrateStub()),
    )
    assert result.consult_response.answer == "## BLUF\n\nEscalation risk is elevated."


# ---------------------------------------------------------------------------
# Multi-tool batch — {"tools":[...]} runs concurrently, ONE round.
# ---------------------------------------------------------------------------


def test_normalize_calls_single_shape():
    from legba.data.analysts.consult_on_demand import _normalize_calls

    assert _normalize_calls({"tool": "search_signals", "args": {"query": "x"}}) == [
        {"tool": "search_signals", "args": {"query": "x"}},
    ]


def test_normalize_calls_batch_shape():
    from legba.data.analysts.consult_on_demand import _normalize_calls

    out = _normalize_calls({"tools": [
        {"tool": "list_situations", "args": {}},
        {"tool": "list_findings", "args": {"limit": 5}},
    ]})
    assert [c["tool"] for c in out] == ["list_situations", "list_findings"]
    assert out[1]["args"] == {"limit": 5}


def test_normalize_calls_dedupes_and_caps():
    from legba.data.analysts.consult_on_demand import (
        MAX_TOOLS_PER_BATCH,
        _normalize_calls,
    )

    dupes = {"tools": [{"tool": "list_findings", "args": {"limit": 5}}] * 3}
    assert _normalize_calls(dupes) == [{"tool": "list_findings", "args": {"limit": 5}}]

    many = {"tools": [
        {"tool": f"t{i}", "args": {"i": i}} for i in range(MAX_TOOLS_PER_BATCH + 4)
    ]}
    assert len(_normalize_calls(many)) == MAX_TOOLS_PER_BATCH


def test_normalize_calls_drops_malformed_and_empty():
    from legba.data.analysts.consult_on_demand import _normalize_calls

    out = _normalize_calls({"tools": [
        {"tool": "", "args": {}},        # no name → dropped
        "not a dict",                    # not a mapping → dropped
        {"tool": "ok", "args": "bad"},   # non-mapping args → coerced to {}
    ]})
    assert out == [{"tool": "ok", "args": {}}]
    # Neither tool nor tools → empty (caller asks for a correction).
    assert _normalize_calls({"final": True}) == []
    assert _normalize_calls({"foo": "bar"}) == []


@pytest.mark.asyncio
async def test_batch_tools_run_in_one_round():
    """A {"tools":[...]} round runs every call, coalesces results, collects refs
    from ALL of them, and counts as ONE reason round (the latency lever)."""
    fid = uuid4()
    sid = uuid4()
    substrate = _SubstrateStub(
        finding_rows=[{"output_id": str(fid), "title": "Iran assessment"}],
        finding_refs=[fid],
        situation_rows=[{"situation_id": str(sid), "name": "Strait tension"}],
        situation_refs=[sid],
    )
    llm = _ScriptedLLMHandler([
        json.dumps({"tools": [
            {"tool": "list_situations", "args": {"limit": 20}},
            {"tool": "list_findings", "args": {"limit": 10}},
        ]}),
        json.dumps({
            "final": True,
            "answer": "## World\n\nTwo active frames.",
            "uncertainty": 0.3,
            "cited_refs": [str(fid), str(sid)],
            "unanswered_aspects": [],
        }),
    ])
    result = await run_method(
        [{"question": "How's the world looking?"}],
        {"analyst_id": "consult.x"},
        ConsultOnDemandDeps(llm=llm, substrate=substrate),
    )
    consult = result.consult_response
    # Both tools ran (the single batch round).
    assert {c[0] for c in substrate.calls} == {"list_situations", "list_findings"}
    # Refs collected from BOTH calls.
    assert set(consult.cited_substrate_refs) == {fid, sid}
    # Two LLM reason rounds total (batch + final), NOT three — the batch is one.
    reason_rounds = [
        s for s in result.intermediate_steps
        if s["phase"] == "reason" and s["kind"] == "llm_call"
    ]
    assert len(reason_rounds) == 2
    # Both act steps belong to round 1 (the single batch round).
    act_steps = [s for s in result.intermediate_steps if s["kind"] == "tool_call"]
    assert len(act_steps) == 2
    assert all(s["round"] == 1 for s in act_steps)


@pytest.mark.asyncio
async def test_batch_one_call_fails_others_survive():
    """One failing call in a batch yields a structured error for THAT call only;
    the sibling still returns its rows and the round proceeds (error isolation)."""
    sid = uuid4()
    substrate = _SubstrateStub(
        situation_rows=[{"situation_id": str(sid), "name": "ok"}],
        situation_refs=[sid],
        raise_on="list_findings",   # this sibling blows up
    )
    llm = _ScriptedLLMHandler([
        json.dumps({"tools": [
            {"tool": "list_situations", "args": {}},
            {"tool": "list_findings", "args": {}},
        ]}),
        json.dumps({"final": True, "answer": "ok", "uncertainty": 0.4,
                    "cited_refs": [str(sid)], "unanswered_aspects": []}),
    ])
    result = await run_method(
        [{"question": "q"}], {"analyst_id": "consult.x"},
        ConsultOnDemandDeps(llm=llm, substrate=substrate),
    )
    # The good call's ref survived; the run didn't abort.
    assert sid in result.consult_response.cited_substrate_refs
    by_tool = {
        s["tool"]: s for s in result.intermediate_steps if s["kind"] == "tool_call"
    }
    assert by_tool["list_situations"]["ok"] is True
    assert by_tool["list_findings"]["ok"] is False


# ---------------------------------------------------------------------------
# Governed-path _run_one_call — outcome mapping + error isolation.
# ---------------------------------------------------------------------------


@dataclass
class _FakeToolResult:
    status: str
    output: dict[str, Any]
    error: str | None = None


@dataclass
class _FakeOutcome:
    admitted: bool
    block_cause: str | None = None
    detail: str | None = None
    tool_result: Any = None


class _FakeBinding:
    """Minimal AgencyToolBinding stub: tool name → scripted outcome."""

    def __init__(
        self, outcomes: dict[str, Any], *, raise_on: set[str] | None = None,
    ) -> None:
        self._outcomes = outcomes
        self._raise_on = raise_on or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(self, tool_name: str, args: dict[str, Any]) -> Any:
        self.calls.append((tool_name, dict(args)))
        if tool_name in self._raise_on:
            raise RuntimeError("binding boom")
        return self._outcomes[tool_name]


@pytest.mark.asyncio
async def test_run_one_call_governed_maps_outcomes_and_isolates_errors():
    """The governed path maps admitted/blocked/failed outcomes to tool_result +
    meta, injects scope_predicate, and folds an unexpected binding raise into a
    structured error instead of propagating it (so a batch sibling can't abort
    the round)."""
    from legba.data.analysts.consult_on_demand import _run_one_call

    binding = _FakeBinding(
        {
            "a": _FakeOutcome(
                admitted=True,
                tool_result=_FakeToolResult(
                    status="ok", output={"rows": [], "refs": [], "count": 0},
                ),
            ),
            "b": _FakeOutcome(admitted=False, block_cause="not_allowed", detail="nope"),
            "c": _FakeOutcome(
                admitted=True,
                tool_result=_FakeToolResult(status="failed", output={}, error="kaboom"),
            ),
        },
        raise_on={"d"},
    )
    deps = ConsultOnDemandDeps(
        llm=_ScriptedLLMHandler([]),
        substrate=_SubstrateStub(),
        agency_binding=binding,
    )

    r, meta = await _run_one_call(
        deps, tool_name="a", tool_args={"q": 1}, scope_predicate="s", analyst_id="x",
    )
    assert "error" not in r and meta == {"governed": True, "admitted": True}
    # scope_predicate is INJECTED (caller-pinned) into the governed args.
    assert binding.calls[-1] == ("a", {"q": 1, "scope_predicate": "s"})

    r, meta = await _run_one_call(
        deps, tool_name="b", tool_args={}, scope_predicate=None, analyst_id="x",
    )
    assert r["error"].startswith("tool_blocked: not_allowed") and meta["admitted"] is False

    r, meta = await _run_one_call(
        deps, tool_name="c", tool_args={}, scope_predicate=None, analyst_id="x",
    )
    assert r["error"].startswith("tool_failed: kaboom") and meta["admitted"] is True

    r, meta = await _run_one_call(
        deps, tool_name="d", tool_args={}, scope_predicate=None, analyst_id="x",
    )
    assert r["error"].startswith("tool_failed:") and meta == {
        "governed": True, "admitted": False,
    }


# ---------------------------------------------------------------------------
# Trace visibility — the tool log lands in consult_response.data["tool_calls"].
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wall_budget_forces_final_after_survey():
    """When the wall-clock budget is exceeded, the loop stops drilling and forces
    the final synthesis — so a broad question RETURNS instead of 504-ing. The
    first (survey) round still runs. (Budget = -1 forces the guard at round 1.)"""
    sid = uuid4()
    substrate = _SubstrateStub(
        situation_rows=[{"situation_id": str(sid)}], situation_refs=[sid],
    )
    llm = _ScriptedLLMHandler([
        json.dumps({"tool": "list_situations", "args": {}}),   # round 0 survey
        json.dumps({"final": True, "answer": "## Partial\n\nbest-effort under budget",
                    "uncertainty": 0.5, "cited_refs": [], "unanswered_aspects": []}),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate, wall_budget_seconds=-1.0)
    result = await run_method([{"question": "q"}], {"analyst_id": "x"}, deps)

    steps = result.intermediate_steps
    # The survey round ran, the guard fired, and we forced a final.
    assert any(s.get("kind") == "tool_call" for s in steps)
    assert any(s.get("kind") == "wall_budget_reached" for s in steps)
    assert result.consult_response.data["forced_final"] is True
    assert "best-effort" in result.consult_response.answer


@pytest.mark.asyncio
async def test_wall_budget_does_not_fire_on_fast_run():
    """A normal fast run (generous budget) completes naturally — the guard never
    fires and adds no behavior."""
    sid = uuid4()
    substrate = _SubstrateStub(
        situation_rows=[{"situation_id": str(sid)}], situation_refs=[sid],
    )
    llm = _ScriptedLLMHandler([
        json.dumps({"tool": "list_situations", "args": {}}),
        json.dumps({"final": True, "answer": "done", "uncertainty": 0.3,
                    "cited_refs": [], "unanswered_aspects": []}),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate, wall_budget_seconds=600.0)
    result = await run_method([{"question": "q"}], {"analyst_id": "x"}, deps)
    steps = result.intermediate_steps
    assert not any(s.get("kind") == "wall_budget_reached" for s in steps)
    assert result.consult_response.data["forced_final"] is False


@pytest.mark.asyncio
async def test_tool_trace_surfaced_in_consult_data():
    """The per-round tool trace lands in consult_response.data['tool_calls'] so
    the consult front door surfaces 'what it did' instead of tool_calls:[]."""
    sid = uuid4()
    substrate = _SubstrateStub(
        situation_rows=[{"situation_id": str(sid)}], situation_refs=[sid],
    )
    llm = _ScriptedLLMHandler([
        json.dumps({"tool": "list_situations", "args": {"limit": 20}}),
        json.dumps({"final": True, "answer": "a", "uncertainty": 0.3,
                    "cited_refs": [], "unanswered_aspects": []}),
    ])
    result = await run_method(
        [{"question": "q"}], {"analyst_id": "consult.x"},
        ConsultOnDemandDeps(llm=llm, substrate=substrate),
    )
    trace = result.consult_response.data.get("tool_calls")
    assert isinstance(trace, list) and len(trace) == 1
    entry = trace[0]
    assert entry["tool"] == "list_situations"
    assert entry["args"] == {"limit": 20}
    assert entry["ok"] is True
    assert entry["round"] == 1


# ---------------------------------------------------------------------------
# System-prompt contract additions (batch shape, survey mandate, md answer).
# ---------------------------------------------------------------------------


def test_system_prompt_advertises_batch_shape():
    assert '{"tools":' in _SYSTEM_PROMPT
    assert "run concurrently" in _SYSTEM_PROMPT


def test_system_prompt_mandates_situation_survey_for_broad():
    assert "SURVEY THEN DRILL" in _SYSTEM_PROMPT
    assert "never called list_situations is INCOMPLETE" in _SYSTEM_PROMPT


def test_system_prompt_requires_markdown_answer():
    assert "MARKDOWN prose" in _SYSTEM_PROMPT
    assert 'nested {"final": ...} envelope' in _SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# R2 / W2-T3 — truncation honesty: _bounded_tool_json replaces the blind
# json.dumps(result)[:N] chop on every tool-result message (consult GATHER loop,
# inline_target GATHER, journal NARRATE). Contract: ALWAYS valid JSON, ALWAYS
# <= limit, and an explicit "truncated": true marker whenever anything dropped.
# ---------------------------------------------------------------------------


def test_bounded_tool_json_small_result_passes_through_unmarked():
    from legba.data.analysts.consult_on_demand import _bounded_tool_json

    result = {"rows": [{"id": "a", "title": "t"}], "refs": ["a"], "count": 1}
    out = _bounded_tool_json(result, 8000)
    assert json.loads(out) == result  # byte-for-byte semantics, no marker


def test_bounded_tool_json_drops_whole_rows_and_marks_truncated():
    from legba.data.analysts.consult_on_demand import _bounded_tool_json

    rows = [{"id": f"row-{i}", "body": "x" * 200} for i in range(60)]
    result = {"rows": rows, "refs": [r["id"] for r in rows], "count": 60}
    out = _bounded_tool_json(result, 4000)
    assert len(out) <= 4000
    parsed = json.loads(out)  # VALID JSON — the whole point
    assert parsed["truncated"] is True
    assert parsed["rows_total"] == 60
    assert 0 < len(parsed["rows"]) < 60
    # Surviving rows are INTACT (whole trailing rows dropped, none chopped).
    for row in parsed["rows"]:
        assert set(row) == {"id", "body"}
        assert len(row["body"]) == 200
    # Non-row keys survive alongside the marker.
    assert parsed["count"] == 60


def test_bounded_tool_json_giant_single_row_falls_back_to_envelope():
    from legba.data.analysts.consult_on_demand import _bounded_tool_json

    result = {"rows": [{"id": "big", "body": "y" * 10_000}]}
    out = _bounded_tool_json(result, 2000)
    assert len(out) <= 2000
    parsed = json.loads(out)
    assert parsed["truncated"] is True
    assert parsed["raw_prefix"].startswith('{"rows"')


def test_bounded_tool_json_items_key_and_non_dict_fallback():
    from legba.data.analysts.consult_on_demand import _bounded_tool_json

    # get_timeline-shaped result truncates on "items".
    items = [{"n": i, "pad": "z" * 300} for i in range(30)]
    out = _bounded_tool_json({"items": items}, 3000)
    parsed = json.loads(out)
    assert parsed["truncated"] is True
    assert parsed["items_total"] == 30
    assert len(out) <= 3000
    # A non-mapping result still yields valid bounded JSON.
    out2 = _bounded_tool_json(["e" * 500] * 20, 1000)
    assert len(out2) <= 1000
    assert json.loads(out2)["truncated"] is True


def test_bounded_tool_json_unserializable_degrades_honestly():
    from legba.data.analysts.consult_on_demand import _bounded_tool_json

    out = _bounded_tool_json({"bad": object()}, 500)
    assert len(out) <= 500
    parsed = json.loads(out)
    assert parsed["truncated"] is True
    assert parsed["error"] == "unserializable tool result"
