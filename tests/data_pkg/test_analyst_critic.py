# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-175 tests for the critic analyst kind.

Covers:
  * Module surface — KIND_NAME, OUTPUT_KIND, READ_SLICE, run_method,
    build_prompt_module, _assert_heterogeneous.
  * Happy path — analyzed output + rubric → scored CritiquePayload.
  * Self-correlation guard — same judge_model + analyzed_model raises.
  * allow_self_correlated escape hatch — overrides the guard.
  * Missing rubric — MissingRubricError raised.
  * JSON parse robustness — markdown fences, trailing prose, malformed
    JSON fallback.
  * revision_delta optional — empty string normalizes to None.
  * derived_from — includes the analyzed_output_id + any context refs
    surfaced via options.
  * READ_SLICE — adapter handles target_filter as UUID + analyzed_output_id
    arg path; empty/missing → [].

Tests use a typed LLM test double conforming to ``LLMHandlerLike``
(same pattern as the sibling kind tests).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID, uuid4

import pytest

from legba.data.analysts import critic as critic_mod
from legba.data.analysts.critic import (
    AnalystMethodResult,
    CriticDeps,
    CriticDepsProtocol,
    CriticRunner,
    HANDLER_VERSION,
    KIND_NAME,
    LLMHandlerLike,
    MissingRubricError,
    OUTPUT_KIND,
    PROMPT_MODULE_PATH,
    READ_SLICE,
    SCHEMA_VERSION,
    SelfCorrelatedJudgeError,
    _assert_heterogeneous,
    _coerce_critique,
    _render_analyzed_output,
    _render_user_prompt,
    _strip_code_fence,
    build_prompt_module,
    run_method,
)
from legba.data.provenance.kinds import OutputKind
from legba.data.provenance.models import CritiquePayload


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    prompt_tokens: int = 80
    completion_tokens: int = 60
    reasoning_tokens: int = 0


@dataclass
class _Response:
    content: str = ""
    usage: _Usage | None = None


class _StubLLMHandler:
    """Typed test double conforming to ``LLMHandlerLike``.

    ``subprovider`` defaults to ``"judge_model_X"`` so the heterogeneity
    guard has a non-empty string to compare against.
    """

    def __init__(
        self,
        *,
        subprovider: str = "judge_model_X",
        content_override: str | None = None,
        raise_on_call: type[BaseException] | None = None,
    ) -> None:
        self.subprovider = subprovider
        self._content_override = content_override
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
        if self._content_override is not None:
            return _Response(content=self._content_override, usage=_Usage())
        scored = {
            "scores": {
                "specificity": 0.8,
                "evidence_density": 0.7,
                "calibration": 0.6,
            },
            "overall_score": 0.7,
            "revision_delta": (
                "Cite signal IDs inline in the body so lineage queries "
                "can attribute claims."
            ),
            "confidence": 0.75,
        }
        return _Response(content=json.dumps(scored), usage=_Usage())


def _analyzed_row(
    *,
    id_: UUID | None = None,
    analyst_id: str = "analyst.india_energy",
    analyst_version: str = "abcdef0123456789",
    title: str = "Itaipu hydro upgrade complete",
    body: str = "Brazil's Itaipu plant turbine upgrade landed today.",
    confidence: float = 0.85,
    evidence: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": id_ or uuid4(),
        "kind": "finding",
        "analyst_id": analyst_id,
        "analyst_version": analyst_version,
        "title": title,
        "body": body,
        "confidence": confidence,
        "evidence": evidence or ["Itaipu press release 2026-05-19"],
        "tags": tags or ["energy", "infrastructure"],
        "data": {},
    }


def _rubric_block() -> str:
    """A canonical JSON-shaped rubric for tests.

    The kind treats the rubric as free-form text passed straight to the
    LLM; the JSON shape is just convention.
    """
    return json.dumps({
        "dimensions": {
            "specificity": {
                "weight": 0.4,
                "description": "Are claims tied to named entities, dates, places?",
            },
            "evidence_density": {
                "weight": 0.3,
                "description": "Ratio of cited evidence to claims.",
            },
            "calibration": {
                "weight": 0.3,
                "description": "Does the analyst's confidence match the evidence?",
            },
        },
        "aggregation": "weighted_mean",
    })


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_kind_identity_constants():
    """KIND_NAME, SCHEMA_VERSION, HANDLER_VERSION, PROMPT_MODULE_PATH,
    OUTPUT_KIND match the L-175 contract."""
    assert KIND_NAME == "critic"
    assert SCHEMA_VERSION == "legba/analyst.critic/1-0-0"
    assert HANDLER_VERSION == "0.1.0"
    assert PROMPT_MODULE_PATH == "legba.prompts.critic.v1"
    assert OUTPUT_KIND == OutputKind.CRITIQUE
    assert OUTPUT_KIND.value == "critique"


def test_module_exports_required_surface():
    assert hasattr(critic_mod, "KIND_NAME")
    assert hasattr(critic_mod, "run_method")
    assert hasattr(critic_mod, "build_prompt_module")
    assert hasattr(critic_mod, "OUTPUT_KIND")
    assert hasattr(critic_mod, "READ_SLICE")
    assert hasattr(critic_mod, "CriticRunner")
    import inspect
    assert inspect.iscoroutinefunction(run_method)


def test_shared_types_with_inline_target_sibling():
    """The kind's result + LLM port types are the SAME Python objects as
    inline_target's so the runtime actor wrapper can dispatch either kind
    through one code path."""
    from legba.data.analysts import inline_target

    assert AnalystMethodResult is inline_target.AnalystMethodResult
    assert LLMHandlerLike is inline_target.LLMHandlerLike


def test_critic_discovered_by_package_walker():
    """The package's ``discover_analyst_kinds`` picks up the critic kind
    automatically — no per-add change in dapr_actors needed."""
    from legba.data.analysts import discover_analyst_kinds

    registry = discover_analyst_kinds()
    assert "critic" in registry
    handler = registry["critic"]
    assert handler.kind_name == "critic"
    assert handler.output_kind == OutputKind.CRITIQUE
    assert handler.read_slice is not None  # critic exposes a custom READ_SLICE
    assert handler.build_prompt_module is not None


def test_critic_deps_protocol_satisfied_by_simple_stub():
    """A bare object with an ``llm`` attribute satisfies the Protocol."""
    llm = _StubLLMHandler()
    deps = CriticDeps(llm=llm)
    assert isinstance(deps, CriticDepsProtocol)


# ---------------------------------------------------------------------------
# Heterogeneity guard
# ---------------------------------------------------------------------------


def test_heterogeneity_guard_passes_distinct_models():
    # Different models — no raise.
    _assert_heterogeneous("openai:gpt-4o", "anthropic:claude-3-5-sonnet")


def test_heterogeneity_guard_raises_same_model():
    with pytest.raises(SelfCorrelatedJudgeError) as excinfo:
        _assert_heterogeneous("openai:gpt-4o", "openai:gpt-4o")
    msg = str(excinfo.value)
    assert "self-correlate" in msg
    assert "allow_self_correlated" in msg


def test_heterogeneity_guard_case_insensitive_strip():
    """Same model with different casing / whitespace still trips the guard."""
    with pytest.raises(SelfCorrelatedJudgeError):
        _assert_heterogeneous("OpenAI:gpt-4o ", "  openai:gpt-4o")


def test_heterogeneity_guard_escape_hatch():
    # allow_self_correlated=True bypasses the check.
    _assert_heterogeneous(
        "openai:gpt-4o", "openai:gpt-4o", allow_self_correlated=True,
    )


def test_heterogeneity_guard_missing_models_does_not_raise():
    """Empty strings → guard logs warning but doesn't raise (audit gap
    surfaces in trace rather than blocking the run)."""
    # Neither raise — we just warn.
    _assert_heterogeneous("", "judge")
    _assert_heterogeneous("analyzed", "")
    _assert_heterogeneous("", "")


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def test_render_analyzed_output_includes_all_fields():
    row = _analyzed_row()
    block = _render_analyzed_output(row)
    assert "TITLE:" in block
    assert "Itaipu hydro upgrade complete" in block
    assert "CONFIDENCE: 0.85" in block
    assert "BODY:" in block
    assert "Brazil's Itaipu plant" in block
    assert "EVIDENCE (1 items)" in block
    assert "Itaipu press release" in block
    assert "TAGS: energy, infrastructure" in block


def test_render_analyzed_output_handles_nested_payload():
    """Tolerates the nested ``data->'title'`` shape from analyst_outputs
    rows where the payload is JSONB-nested rather than column-projected."""
    row = {
        "id": uuid4(),
        "analyst_id": "analyst.x",
        "analyst_version": "v1",
        "data": {
            "title": "Nested title",
            "body": "Nested body",
            "confidence": 0.6,
            "evidence": ["e1"],
            "tags": ["t1"],
        },
    }
    block = _render_analyzed_output(row)
    assert "Nested title" in block
    assert "Nested body" in block
    assert "CONFIDENCE: 0.60" in block
    assert "TAGS: t1" in block


def test_render_user_prompt_carries_rubric_and_analyst_id():
    block = "TITLE: foo\nBODY:\nbar"
    rubric = "score on clarity 0-1"
    prompt = _render_user_prompt(block, rubric, "analyst.x")
    assert "ANALYZED ANALYST ID: analyst.x" in prompt
    assert "=== RUBRIC ===" in prompt
    assert "score on clarity" in prompt
    assert "=== ANALYZED OUTPUT ===" in prompt
    assert "TITLE: foo" in prompt


def test_render_user_prompt_truncates_long_rubric():
    huge = "x" * 10_000
    prompt = _render_user_prompt("body", huge, "a")
    # Rubric capped at 4000 chars.
    assert prompt.count("x") == 4000


# ---------------------------------------------------------------------------
# JSON parsing robustness
# ---------------------------------------------------------------------------


def test_strip_code_fence_strips_json_fence():
    raw = '```json\n{"scores": {"a": 0.5}, "overall_score": 0.5}\n```'
    out = _strip_code_fence(raw)
    assert out.startswith("{")
    assert out.endswith("}")
    assert '"scores"' in out


def test_strip_code_fence_strips_trailing_prose():
    raw = '{"a": 1} this is some trailing prose the LLM emitted'
    out = _strip_code_fence(raw)
    assert out == '{"a": 1}'


def test_coerce_critique_happy_path():
    raw = json.dumps({
        "scores": {"specificity": 0.9, "calibration": 0.6},
        "overall_score": 0.75,
        "revision_delta": "Add named-entity attribution.",
        "confidence": 0.8,
    })
    oid = uuid4()
    payload = _coerce_critique(
        raw,
        fallback_title="t",
        analyzed_output_id=oid,
        analyzed_analyst_id="analyst.x",
        analyzed_analyst_version="v1",
        analyzed_model="model.a",
        judge_model="model.b",
    )
    assert isinstance(payload, CritiquePayload)
    assert payload.scores == {"specificity": 0.9, "calibration": 0.6}
    assert payload.overall_score == 0.75
    assert payload.confidence == 0.8
    assert payload.revision_delta == "Add named-entity attribution."
    assert payload.analyzed_output_id == oid
    assert payload.analyzed_analyst_id == "analyst.x"
    assert payload.analyzed_model == "model.a"
    assert payload.judge_model == "model.b"
    assert "critic" in payload.tags
    assert "unstructured" not in payload.tags


def test_coerce_critique_markdown_fenced_json():
    raw = '```json\n' + json.dumps({
        "scores": {"x": 0.5},
        "overall_score": 0.5,
        "confidence": 0.5,
    }) + '\n```'
    payload = _coerce_critique(
        raw,
        fallback_title="t",
        analyzed_output_id=None,
        analyzed_analyst_id="a",
        analyzed_analyst_version="v",
        analyzed_model="m1",
        judge_model="m2",
    )
    assert payload.scores == {"x": 0.5}
    assert "unstructured" not in payload.tags


def test_coerce_critique_trailing_prose():
    raw = json.dumps({
        "scores": {"x": 0.5},
        "overall_score": 0.5,
        "confidence": 0.5,
    }) + "\n\nThe rubric was clear so I was confident."
    payload = _coerce_critique(
        raw,
        fallback_title="t",
        analyzed_output_id=None,
        analyzed_analyst_id="a",
        analyzed_analyst_version="v",
        analyzed_model="m1",
        judge_model="m2",
    )
    assert payload.scores == {"x": 0.5}
    assert "unstructured" not in payload.tags


def test_coerce_critique_malformed_json_falls_back():
    raw = "not even close to JSON: { broken syntax"
    payload = _coerce_critique(
        raw,
        fallback_title="fallback title",
        analyzed_output_id=None,
        analyzed_analyst_id="a",
        analyzed_analyst_version="v",
        analyzed_model="m1",
        judge_model="m2",
    )
    assert payload.title == "fallback title"
    assert "unstructured" in payload.tags
    assert payload.scores == {}
    assert payload.overall_score == 0.0


def test_coerce_critique_non_object_json():
    """Top-level JSON array (or anything not a dict) → unstructured fallback."""
    raw = json.dumps([1, 2, 3])
    payload = _coerce_critique(
        raw,
        fallback_title="t",
        analyzed_output_id=None,
        analyzed_analyst_id="a",
        analyzed_analyst_version="v",
        analyzed_model="m1",
        judge_model="m2",
    )
    assert "unstructured" in payload.tags
    assert payload.scores == {}


def test_coerce_critique_revision_delta_empty_normalizes_to_none():
    """Whitespace-only revision_delta normalizes to None."""
    raw = json.dumps({
        "scores": {"x": 0.5},
        "overall_score": 0.5,
        "revision_delta": "   ",
        "confidence": 0.5,
    })
    payload = _coerce_critique(
        raw,
        fallback_title="t",
        analyzed_output_id=None,
        analyzed_analyst_id="a",
        analyzed_analyst_version="v",
        analyzed_model="m1",
        judge_model="m2",
    )
    assert payload.revision_delta is None


def test_coerce_critique_revision_delta_missing_is_none():
    """revision_delta absent from the LLM response normalizes to None."""
    raw = json.dumps({
        "scores": {"x": 0.5},
        "overall_score": 0.5,
        "confidence": 0.5,
    })
    payload = _coerce_critique(
        raw,
        fallback_title="t",
        analyzed_output_id=None,
        analyzed_analyst_id="a",
        analyzed_analyst_version="v",
        analyzed_model="m1",
        judge_model="m2",
    )
    assert payload.revision_delta is None


def test_coerce_critique_scores_clamped_to_unit_interval():
    """LLM-emitted out-of-range scores get clamped, not rejected."""
    raw = json.dumps({
        "scores": {"too_high": 1.5, "negative": -0.3, "ok": 0.7},
        "overall_score": 2.0,  # also clamped
        "confidence": -1.0,  # also clamped
    })
    payload = _coerce_critique(
        raw,
        fallback_title="t",
        analyzed_output_id=None,
        analyzed_analyst_id="a",
        analyzed_analyst_version="v",
        analyzed_model="m1",
        judge_model="m2",
    )
    assert payload.scores == {"too_high": 1.0, "negative": 0.0, "ok": 0.7}
    assert payload.overall_score == 1.0
    assert payload.confidence == 0.0


def test_coerce_critique_scores_drops_non_string_keys():
    """Non-string keys in scores dict drop silently."""
    raw = json.dumps({
        "scores": {"valid": 0.5, "": 0.3, "another_valid": 0.7},
        "overall_score": 0.6,
        "confidence": 0.5,
    })
    payload = _coerce_critique(
        raw,
        fallback_title="t",
        analyzed_output_id=None,
        analyzed_analyst_id="a",
        analyzed_analyst_version="v",
        analyzed_model="m1",
        judge_model="m2",
    )
    assert payload.scores == {"valid": 0.5, "another_valid": 0.7}


# ---------------------------------------------------------------------------
# run_method happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_method_happy_path():
    """One analyzed output + rubric → structured CritiquePayload via the
    stub LLM (which returns a canned scored JSON)."""
    analyzed = _analyzed_row()
    llm = _StubLLMHandler(subprovider="judge_X")
    deps = CriticDeps(llm=llm)
    result = await run_method(
        [analyzed],
        {
            "analyst_id": "analyst.critic",
            "analyst_version": "v1",
            "run_id": uuid4(),
            "rubric": _rubric_block(),
            "analyzed_model": "model_under_test",
            "judge_model": "judge_X",
        },
        deps,
    )
    assert isinstance(result, AnalystMethodResult)
    critique = result.finding
    assert isinstance(critique, CritiquePayload)
    assert critique.analyzed_output_id == analyzed["id"]
    assert critique.analyzed_analyst_id == analyzed["analyst_id"]
    assert critique.analyzed_analyst_version == analyzed["analyst_version"]
    assert critique.analyzed_model == "model_under_test"
    assert critique.judge_model == "judge_X"
    assert critique.scores == {
        "specificity": 0.8, "evidence_density": 0.7, "calibration": 0.6,
    }
    assert critique.overall_score == 0.7
    assert critique.confidence == 0.75
    assert critique.revision_delta is not None
    assert "Cite signal IDs" in critique.revision_delta
    # derived_from carries the analyzed-output id
    assert result.derived_from == [analyzed["id"]]
    # Usage propagated
    assert result.usage["prompt_tokens"] == 80
    assert result.usage["completion_tokens"] == 60
    # Phase trace populated
    phases = [step["phase"] for step in result.intermediate_steps]
    assert "orient" in phases
    assert "plan" in phases
    assert "reason" in phases
    assert "reflect" in phases
    assert "persist" in phases
    # Tag stamped with analyzed:<analyst_id>
    assert any(t.startswith("analyzed:") for t in critique.tags)


@pytest.mark.asyncio
async def test_run_method_derived_from_includes_context_refs():
    """Context UUIDs passed via options['context_refs'] join the analyzed
    output id in derived_from (lineage roots for citation queries)."""
    analyzed = _analyzed_row()
    ctx1, ctx2 = uuid4(), uuid4()
    llm = _StubLLMHandler(subprovider="judge_X")
    result = await run_method(
        [analyzed],
        {
            "analyst_id": "analyst.critic",
            "rubric": _rubric_block(),
            "analyzed_model": "m1",
            "judge_model": "judge_X",
            "context_refs": [ctx1, ctx2],
        },
        CriticDeps(llm=llm),
    )
    assert analyzed["id"] in result.derived_from
    assert ctx1 in result.derived_from
    assert ctx2 in result.derived_from


@pytest.mark.asyncio
async def test_run_method_accepts_bare_llm_handler():
    """The 3-arg run_method coerces a bare LLM handler into CriticDeps."""
    analyzed = _analyzed_row()
    llm = _StubLLMHandler(subprovider="judge_X")
    result = await run_method(
        [analyzed],
        {
            "rubric": _rubric_block(),
            "analyzed_model": "m1",
            "judge_model": "judge_X",
        },
        llm,
    )
    assert isinstance(result.finding, CritiquePayload)
    assert result.finding.judge_model == "judge_X"


@pytest.mark.asyncio
async def test_critic_runner_two_arg_call_shape():
    """The CriticRunner adapter matches the spike's 2-arg AnalystRunFn shape."""
    analyzed = _analyzed_row()
    llm = _StubLLMHandler(subprovider="judge_X")
    runner = CriticRunner(llm)
    result = await runner(
        [analyzed],
        {
            "rubric": _rubric_block(),
            "analyzed_model": "m1",
            "judge_model": "judge_X",
        },
    )
    assert isinstance(result.finding, CritiquePayload)


# ---------------------------------------------------------------------------
# Self-correlation guard end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_method_raises_when_models_match():
    """Same analyzed_model == judge_model → SelfCorrelatedJudgeError."""
    analyzed = _analyzed_row()
    llm = _StubLLMHandler(subprovider="same_model")
    with pytest.raises(SelfCorrelatedJudgeError):
        await run_method(
            [analyzed],
            {
                "rubric": _rubric_block(),
                "analyzed_model": "same_model",
                "judge_model": "same_model",
            },
            CriticDeps(llm=llm),
        )


@pytest.mark.asyncio
async def test_run_method_judge_model_defaults_to_llm_subprovider():
    """When options['judge_model'] absent, the kind uses llm.subprovider."""
    analyzed = _analyzed_row()
    llm = _StubLLMHandler(subprovider="analyzed_model")
    with pytest.raises(SelfCorrelatedJudgeError):
        # Default judge_model = llm.subprovider = "analyzed_model" matches
        # the analyzed_model option → guard trips.
        await run_method(
            [analyzed],
            {
                "rubric": _rubric_block(),
                "analyzed_model": "analyzed_model",
            },
            CriticDeps(llm=llm),
        )


@pytest.mark.asyncio
async def test_run_method_allow_self_correlated_bypasses_guard():
    """Per L-105 escape hatch: explicit opt-in lets self-correlated runs land."""
    analyzed = _analyzed_row()
    llm = _StubLLMHandler(subprovider="same_model")
    result = await run_method(
        [analyzed],
        {
            "rubric": _rubric_block(),
            "analyzed_model": "same_model",
            "judge_model": "same_model",
            "allow_self_correlated": True,
        },
        CriticDeps(llm=llm),
    )
    assert isinstance(result.finding, CritiquePayload)
    assert result.finding.judge_model == "same_model"
    assert result.finding.analyzed_model == "same_model"


# ---------------------------------------------------------------------------
# Missing rubric
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_method_raises_missing_rubric():
    analyzed = _analyzed_row()
    llm = _StubLLMHandler(subprovider="judge_X")
    with pytest.raises(MissingRubricError) as excinfo:
        await run_method(
            [analyzed],
            {
                "analyzed_model": "m1",
                "judge_model": "judge_X",
                # rubric intentionally omitted
            },
            CriticDeps(llm=llm),
        )
    assert "no eval.rubric" in str(excinfo.value)


@pytest.mark.asyncio
async def test_run_method_raises_blank_rubric():
    """Whitespace-only rubric counts as missing."""
    analyzed = _analyzed_row()
    llm = _StubLLMHandler(subprovider="judge_X")
    with pytest.raises(MissingRubricError):
        await run_method(
            [analyzed],
            {
                "rubric": "   \n  ",
                "analyzed_model": "m1",
                "judge_model": "judge_X",
            },
            CriticDeps(llm=llm),
        )


# ---------------------------------------------------------------------------
# Empty inputs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_method_empty_inputs_returns_noop_critique():
    """Empty input list → low-confidence stub critique, no LLM call.

    The runtime would normally NOOP before us; this is the defensive path."""
    llm = _StubLLMHandler()
    result = await run_method(
        [],
        {
            "rubric": _rubric_block(),
            "analyzed_model": "m1",
            "judge_model": "judge_X",
        },
        CriticDeps(llm=llm),
    )
    assert isinstance(result.finding, CritiquePayload)
    assert result.finding.confidence == 0.0
    assert "empty_slice" in result.finding.tags
    assert result.derived_from == []
    # No LLM call should have been made.
    assert llm.calls == []


# ---------------------------------------------------------------------------
# LLM error propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_method_llm_error_propagates():
    """Per kind_contracts §7: the kind does NOT swallow LLM errors. The
    runtime classifies them as transient / hard / budget failures."""
    analyzed = _analyzed_row()

    class _SimulatedTransport(RuntimeError):
        pass

    llm = _StubLLMHandler(
        subprovider="judge_X",
        raise_on_call=_SimulatedTransport,
    )
    with pytest.raises(_SimulatedTransport):
        await run_method(
            [analyzed],
            {
                "rubric": _rubric_block(),
                "analyzed_model": "m1",
                "judge_model": "judge_X",
            },
            CriticDeps(llm=llm),
        )


# ---------------------------------------------------------------------------
# READ_SLICE adapter
# ---------------------------------------------------------------------------


class _FakeConn:
    """Conn stub for READ_SLICE; implements ``fetchrow`` only."""

    def __init__(self, rows_by_id: dict[UUID, dict[str, Any]]) -> None:
        self._rows = rows_by_id
        self.fetchrow_calls: list[Any] = []

    async def fetchrow(self, query: str, *params: Any) -> Any:
        self.fetchrow_calls.append((query, params))
        if not params:
            return None
        return self._rows.get(params[0])


@pytest.mark.asyncio
async def test_read_slice_resolves_target_filter_as_uuid():
    """When target_filter is the analyzed-output UUID, READ_SLICE returns it."""
    oid = uuid4()
    row = _analyzed_row(id_=oid)
    conn = _FakeConn({oid: row})

    rows = await READ_SLICE(conn, descriptor=None, target_filter=str(oid))
    assert len(rows) == 1
    assert rows[0]["id"] == oid


@pytest.mark.asyncio
async def test_read_slice_resolves_explicit_kwarg():
    """analyzed_output_id kwarg wins over target_filter when both present."""
    explicit = uuid4()
    row = _analyzed_row(id_=explicit)
    conn = _FakeConn({explicit: row})

    rows = await READ_SLICE(
        conn,
        descriptor=None,
        target_filter="not-a-uuid",
        analyzed_output_id=explicit,
    )
    assert len(rows) == 1
    assert rows[0]["id"] == explicit


@pytest.mark.asyncio
async def test_read_slice_returns_empty_when_no_id():
    """Missing analyzed_output_id and target_filter → empty list."""
    conn = _FakeConn({})
    rows = await READ_SLICE(conn, descriptor=None, target_filter=None)
    assert rows == []
    # No fetch attempted.
    assert conn.fetchrow_calls == []


@pytest.mark.asyncio
async def test_read_slice_returns_empty_when_row_not_found():
    """UUID resolves but the row doesn't exist → empty list (NOOP path)."""
    oid = uuid4()
    conn = _FakeConn({})  # empty
    rows = await READ_SLICE(conn, descriptor=None, target_filter=str(oid))
    assert rows == []


# ---------------------------------------------------------------------------
# DSPy module path (real dspy)
# ---------------------------------------------------------------------------


def test_build_prompt_module_returns_dspy_module():
    pytest.importorskip("dspy")
    from legba.prompts.critic.v1 import CriticJudge
    pm = build_prompt_module()
    assert isinstance(pm, CriticJudge)


def test_build_prompt_module_signature_fields():
    pytest.importorskip("dspy")
    from legba.prompts.critic.v1 import CriticSignature
    fields = CriticSignature.model_fields
    for fname in (
        "analyzed_output", "rubric", "analyzed_analyst_id",
        "rationale", "scores", "overall_score", "revision_delta", "confidence",
    ):
        assert fname in fields, f"missing field: {fname}"


# ---------------------------------------------------------------------------
# CritiquePayload back-compat
# ---------------------------------------------------------------------------


def test_critique_payload_back_compat_with_wave_a_shape():
    """The pre-extension Wave A construction ``CritiquePayload(title=...,
    target_ref=...)`` still validates after the L-175 fields landed."""
    target = uuid4()
    payload = CritiquePayload(
        title="narrative critique",
        target_ref=target,
        rubric="legacy free-form rubric string",
    )
    assert payload.title == "narrative critique"
    assert payload.target_ref == target
    assert payload.rubric == "legacy free-form rubric string"
    # New L-175 fields default to empty/None.
    assert payload.analyzed_output_id is None
    assert payload.analyzed_analyst_id == ""
    assert payload.scores == {}
    assert payload.overall_score is None
    assert payload.revision_delta is None


# ---------------------------------------------------------------------------
# L-175 tool-loop path — native Anthropic tool_use ReAct loop
# ---------------------------------------------------------------------------
#
# These tests exercise the critic's tool-threading code path (added in
# the L-175 close-out): when the descriptor's ``method.tools_whitelist``
# is non-empty AND the runtime resolved at least one tool callable, the
# critic uses ``_reason_via_llm_with_tools`` (native Anthropic tool_use
# blocks) instead of the single-turn ``_reason_via_llm`` path.  The
# fallback test in this section verifies that an empty whitelist still
# hits the original single-turn path (no regression).


@dataclass
class _ToolCall:
    """Test-double for an Anthropic ``LLMToolCall``-shaped dataclass.

    The critic's loop accepts either the dataclass shape (production
    via :class:`AnthropicProviderHandler`) or a plain dict — this
    class mirrors the dataclass surface used in the kind handler.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class _ToolResponse:
    """LLM response with both text content + native tool_use blocks.

    Mirrors the parsed :class:`LLMResponse` shape from
    :mod:`legba.data.stack.llm.base`.
    """

    content: str = ""
    tool_calls: list[_ToolCall] = None  # type: ignore[assignment]
    usage: _Usage | None = None
    finish_reason: str = "stop"
    raw_response: dict[str, Any] | None = None


class _ToolLoopLLMHandler:
    """LLM stub that emits one tool_use call then a final critique.

    Round 1 (no tool_result in messages yet): returns a tool_use block
    requesting ``mnemosyne_trust_query`` for a peer DID.

    Round 2 (after tool_result block lands in messages): returns the
    final scored-JSON critique with no tool_use blocks.

    Mirrors the Anthropic ``LLMResponse`` shape so the kind's
    ``_extract_text_and_tool_uses`` helper can pull the surface from
    either dataclass or duck-typed responses.
    """

    subprovider = "anthropic"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        tools: list[Mapping[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "system": system,
        })
        # Round-detection: count tool_result blocks already in the
        # conversation. Zero → emit a tool_use; one or more → final.
        tool_result_seen = False
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_result"
                    ):
                        tool_result_seen = True
                        break
            if tool_result_seen:
                break

        if not tool_result_seen:
            # First round — request a trust query.
            return _ToolResponse(
                content="I will check the source's trust score first.",
                tool_calls=[
                    _ToolCall(
                        id="toolu_round1",
                        name="mnemosyne_trust_query",
                        arguments={
                            "peer_did": "did:key:z6MkSamplePeerDIDForTest",
                            "scope": "general",
                        },
                    )
                ],
                usage=_Usage(prompt_tokens=120, completion_tokens=30),
                finish_reason="tool_calls",
            )

        # Second round — produce the final JSON critique.
        scored = {
            "scores": {
                "specificity": 0.85,
                "evidence_density": 0.7,
                "calibration": 0.75,
            },
            "overall_score": 0.78,
            "revision_delta": (
                "Cite the federation trust score "
                "(weight=0.8, hops=2) inline in the body."
            ),
            "confidence": 0.82,
        }
        return _ToolResponse(
            content=json.dumps(scored),
            tool_calls=[],
            usage=_Usage(prompt_tokens=200, completion_tokens=70),
            finish_reason="stop",
        )


class _ForeverToolLoopLLMHandler:
    """LLM stub that keeps requesting tools forever.

    Used to exercise the ``max_tool_rounds`` cap + forced-final path.
    Every round (including after the cap is hit) the model would
    request another tool_use — the critic's loop forces a final synth
    turn (tools=None) to extract a structured critique anyway.
    """

    subprovider = "anthropic"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.forced_call_seen = False

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        tools: list[Mapping[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append({"tools": tools, "system": system})
        if tools is None:
            # The forced-final turn (tools withheld) — emit text.
            self.forced_call_seen = True
            return _ToolResponse(
                content=json.dumps({
                    "scores": {"specificity": 0.4},
                    "overall_score": 0.4,
                    "confidence": 0.3,
                    "revision_delta": "Forced final after cap.",
                }),
                tool_calls=[],
                usage=_Usage(prompt_tokens=50, completion_tokens=25),
                finish_reason="stop",
            )
        # Tool round — keep asking.
        return _ToolResponse(
            content="Need more data.",
            tool_calls=[
                _ToolCall(
                    id=f"toolu_{len(self.calls)}",
                    name="mnemosyne_trust_query",
                    arguments={"peer_did": "did:key:zX"},
                )
            ],
            usage=_Usage(prompt_tokens=100, completion_tokens=20),
            finish_reason="tool_calls",
        )


class _MnemosyneTrustQueryStub:
    """Recording test-double for the L-211 mnemosyne_trust_query tool.

    Returns the contract-frozen ``{weight, hops}`` shape per MN-3 Q13
    (the production tool's happy-path return shape).  The critic's
    loop dispatches against this via the deps.tools mapping.
    """

    def __init__(self, *, weight: float = 0.8, hops: int = 2) -> None:
        self.weight = weight
        self.hops = hops
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(args))
        return {"weight": self.weight, "hops": self.hops}


@pytest.mark.asyncio
async def test_critic_tool_loop_executes_and_records_tool_call():
    """Whitelist non-empty + tool resolved + LLM emits tool_use →
    loop dispatches, captures result, returns final critique with
    ``tool_calls`` populated on the AnalystMethodResult."""
    analyzed = _analyzed_row()
    llm = _ToolLoopLLMHandler()
    trust_stub = _MnemosyneTrustQueryStub(weight=0.8, hops=2)
    deps = CriticDeps(
        llm=llm,
        tools={"mnemosyne_trust_query": trust_stub},
        tools_whitelist=["mnemosyne_trust_query"],
        max_tool_rounds=3,
    )
    result = await run_method(
        [analyzed],
        {
            "rubric": _rubric_block(),
            "analyzed_model": "vllm",  # heterogeneity guard distinct
            "judge_model": "anthropic",
        },
        deps,
    )
    # Final critique landed (structured, not unstructured fallback).
    critique = result.finding
    assert isinstance(critique, CritiquePayload)
    assert "unstructured" not in critique.tags
    assert critique.overall_score == 0.78
    assert critique.confidence == 0.82
    assert critique.scores == {
        "specificity": 0.85,
        "evidence_density": 0.7,
        "calibration": 0.75,
    }
    # Tool was actually called once with the expected args.
    assert len(trust_stub.calls) == 1
    assert trust_stub.calls[0] == {
        "peer_did": "did:key:z6MkSamplePeerDIDForTest",
        "scope": "general",
    }
    # tool_calls field on the result envelope captures the invocation
    # — this is the surface the runtime threads into the trace's
    # analyst_traces.tool_calls JSONB column.
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc["name"] == "mnemosyne_trust_query"
    assert tc["round"] == 1
    assert tc["tool_use_id"] == "toolu_round1"
    assert tc["args"]["peer_did"] == "did:key:z6MkSamplePeerDIDForTest"
    assert tc["result"] == {"weight": 0.8, "hops": 2}
    assert tc["ok"] is True
    # Aggregate usage = sum of both rounds (120+30) + (200+70).
    assert result.usage["prompt_tokens"] == 320
    assert result.usage["completion_tokens"] == 100
    # The LLM saw the tool definitions on round 1.
    first_call = llm.calls[0]
    assert first_call["tools"] is not None
    assert any(t["name"] == "mnemosyne_trust_query" for t in first_call["tools"])
    # Phase trace shows the tool-loop completion entry.
    kinds = [step.get("kind") for step in result.intermediate_steps]
    assert "tools_resolved" in kinds
    assert "tool_call" in kinds
    assert "tool_loop_complete" in kinds


@pytest.mark.asyncio
async def test_critic_empty_tools_whitelist_falls_back_to_single_turn():
    """No whitelist + single-turn LLM (no tool_calls field on response)
    → the kind takes the legacy path; tool_calls field on the result
    stays empty."""
    analyzed = _analyzed_row()
    llm = _StubLLMHandler(subprovider="judge_X")
    deps = CriticDeps(
        llm=llm,
        tools={},
        tools_whitelist=[],
    )
    result = await run_method(
        [analyzed],
        {
            "rubric": _rubric_block(),
            "analyzed_model": "model_under_test",
            "judge_model": "judge_X",
        },
        deps,
    )
    assert isinstance(result.finding, CritiquePayload)
    # No tool calls on the result.
    assert result.tool_calls == []
    # Only one chat_complete call (the single-turn fallback).
    assert len(llm.calls) == 1
    # And the LLM was NOT offered tools (legacy path).
    assert llm.calls[0]["system"]  # system prompt landed
    # The phase trace shouldn't include tool_loop_complete (single-turn
    # path emits the plain 'llm_call' entry).
    kinds = [step.get("kind") for step in result.intermediate_steps]
    assert "llm_call" in kinds
    assert "tool_loop_complete" not in kinds
    assert "tools_resolved" not in kinds


@pytest.mark.asyncio
async def test_critic_unresolved_tool_in_whitelist_is_skipped_silently():
    """A whitelist name with no entry in deps.tools → skipped with
    warning; loop proceeds with the remaining (or zero) tools.

    Here the only whitelisted tool isn't in deps.tools, so the kind
    falls back to the single-turn path (no usable tools)."""
    analyzed = _analyzed_row()
    llm = _StubLLMHandler(subprovider="judge_X")
    deps = CriticDeps(
        llm=llm,
        tools={},  # empty → name resolves to nothing
        tools_whitelist=["nonexistent_tool"],
    )
    result = await run_method(
        [analyzed],
        {
            "rubric": _rubric_block(),
            "analyzed_model": "model_under_test",
            "judge_model": "judge_X",
        },
        deps,
    )
    assert isinstance(result.finding, CritiquePayload)
    assert result.tool_calls == []
    # Single-turn fallback path took over.
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_critic_tool_loop_caps_at_max_rounds_and_forces_final():
    """When the LLM keeps requesting tools past ``max_tool_rounds``,
    the loop forces a final synthesis turn with tools withheld so the
    operator always gets a structured critique."""
    analyzed = _analyzed_row()
    llm = _ForeverToolLoopLLMHandler()
    trust_stub = _MnemosyneTrustQueryStub()
    deps = CriticDeps(
        llm=llm,
        tools={"mnemosyne_trust_query": trust_stub},
        tools_whitelist=["mnemosyne_trust_query"],
        max_tool_rounds=2,
    )
    result = await run_method(
        [analyzed],
        {
            "rubric": _rubric_block(),
            "analyzed_model": "vllm",
            "judge_model": "anthropic",
        },
        deps,
    )
    # Critique came back structured (from the forced-final turn).
    critique = result.finding
    assert isinstance(critique, CritiquePayload)
    assert "unstructured" not in critique.tags
    # Forced-final synthesis call landed.
    assert llm.forced_call_seen is True
    # Tool was called once per round + once for the forced-final
    # would have been 0 (tools withheld), so total = max_tool_rounds.
    assert len(trust_stub.calls) == 2
    # Phase trace includes the forced_final marker.
    kinds = [step.get("kind") for step in result.intermediate_steps]
    assert "forced_final" in kinds


@pytest.mark.asyncio
async def test_critic_tool_callable_exception_surfaces_as_is_error():
    """When a tool callable raises, the loop folds the error into a
    tool_result(is_error=True) block so the LLM can recover; the
    tool_calls log entry records ok=False."""
    analyzed = _analyzed_row()

    class _RaisingTool:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def __call__(self, args: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(args)
            raise RuntimeError("simulated tool transport failure")

    llm = _ToolLoopLLMHandler()
    deps = CriticDeps(
        llm=llm,
        tools={"mnemosyne_trust_query": _RaisingTool()},
        tools_whitelist=["mnemosyne_trust_query"],
        max_tool_rounds=3,
    )
    result = await run_method(
        [analyzed],
        {
            "rubric": _rubric_block(),
            "analyzed_model": "vllm",
            "judge_model": "anthropic",
        },
        deps,
    )
    # Loop continued past the tool error; tool_calls log records ok=False.
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["ok"] is False
    assert "tool_failed" in result.tool_calls[0]["result"]["error"]
    # And we still got a structured critique back (from round 2).
    critique = result.finding
    assert isinstance(critique, CritiquePayload)


def test_critic_runner_wraps_tools_into_deps():
    """CriticRunner accepts tools + whitelist + max_tool_rounds and
    threads them into the underlying CriticDeps bundle."""
    llm = _StubLLMHandler()
    trust_stub = _MnemosyneTrustQueryStub()
    from legba.data.analysts.critic import MAX_TOOL_ROUNDS as DEFAULT_ROUNDS
    runner = CriticRunner(
        llm,
        tools={"mnemosyne_trust_query": trust_stub},
        tools_whitelist=["mnemosyne_trust_query"],
    )
    assert runner._deps.tools_whitelist == ["mnemosyne_trust_query"]
    assert "mnemosyne_trust_query" in runner._deps.tools
    assert runner._deps.max_tool_rounds == DEFAULT_ROUNDS
