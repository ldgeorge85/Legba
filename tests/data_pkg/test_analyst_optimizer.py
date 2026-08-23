# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-176 optimizer analyst-kind tests.

Covers:

  * Kind identity — KIND_NAME, OUTPUT_KIND, build_prompt_module() returns
    None (Wave B exempt).
  * READ_SLICE — joins analyst_traces + analyst_critiques, filters by
    analyzed_analyst_id, returns empty on no target.
  * run_method happy path — dispatches the durable workflow (via
    in-process stub), receives a candidate, builds the
    :class:`PromptModuleCandidatePayload` shape correctly, populates
    derived_from with trace + critique UUIDs.
  * eval_score_delta correctness — candidate − parent on the same
    holdout.
  * Promotion policy LABEL round-trips onto the candidate row (the LIVE
    promotion gate ``_delta_gates_ok`` is covered by
    ``tests/test_optimizer_promotion.py`` + the ``p4t8_honesty`` suite).
  * ``min_traces_required`` enforcement — under-trained analyst yields
    a "skipped_validation" candidate with zero delta.
  * Self-improvement loop boundedness — naive fallback respects
    ``max_generations``.
  * Bug surface — write path's ``_select_output_payload`` correctly
    pivots to the candidate when ``OUTPUT_KIND ==
    PROMPT_MODULE_CANDIDATE``.

Tests use the in-process workflow client (no docker dependency).  The
GEPA-loop determinism tests live in
``tests/runtime/test_optimizer_gepa_loop.py``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts.optimizer import (
    DEFAULT_MAX_GENERATIONS,
    DEFAULT_MIN_TRACES_REQUIRED,
    HANDLER_VERSION,
    KIND_NAME,
    OUTPUT_KIND,
    OptimizerDeps,
    SCHEMA_VERSION,
    _collect_derived_from,
    _collect_derived_trace_ids,
    _dispatch_workflow,
    _resolve_analyzed_identity,
    _shape_training_set,
    build_prompt_module,
    run_method,
)
from legba.data.provenance.kinds import OutputKind, spec_for_kind
from legba.data.provenance.models import PromptModuleCandidatePayload
from legba.runtime.dapr_workflow.gepa import (
    InProcessWorkflowClient,
    OptimizerWorkflowInput,
    OptimizerWorkflowResult,
    StubWorkflowHandle,
)


# ---------------------------------------------------------------------------
# Stub workflow client — returns a canned result without an LLM call
# ---------------------------------------------------------------------------


class _StubTemporalClient:
    """Test double for the durable-workflow client.

    Records the workflow input + workflow_id it was called with, returns
    a canned result.  Allows tests to assert the kind passed the right
    arguments through to the workflow dispatch.
    """

    def __init__(self, canned_result: OptimizerWorkflowResult | None = None) -> None:
        self._canned = canned_result or OptimizerWorkflowResult(
            candidate_prompt_module_text="You are a careful analyst. Be concise.",
            training_set_size=10,
            eval_score=0.72,
            eval_score_delta=0.15,
            gepa_generation=3,
            diagnostics={"method": "stub_dspy_gepa", "baseline_score": 0.57},
        )
        self.calls: list[dict[str, Any]] = []

    async def start_optimizer_workflow(
        self,
        workflow_input: OptimizerWorkflowInput,
        *,
        workflow_id: str,
    ) -> StubWorkflowHandle:
        self.calls.append({
            "workflow_input": workflow_input,
            "workflow_id": workflow_id,
        })
        return StubWorkflowHandle(
            id=workflow_id,
            result_run_id=f"stub_run::{workflow_id}",
            _result=self._canned,
        )


class _HangingHandle:
    """A workflow handle whose result() never returns — models the GEPA
    compile() hang that left the optimizer leg dormant ~4 days (DQ-C4)."""

    def __init__(self, workflow_id: str) -> None:
        self.id = workflow_id
        self.result_run_id = f"hang_run::{workflow_id}"

    async def result(self) -> OptimizerWorkflowResult:
        await asyncio.sleep(3600)  # would hang run_method forever w/o the bound
        raise AssertionError("unreachable")


class _HangingTemporalClient:
    """Returns a handle that never resolves."""

    async def start_optimizer_workflow(
        self, workflow_input: OptimizerWorkflowInput, *, workflow_id: str,
    ) -> _HangingHandle:
        return _HangingHandle(workflow_id)


@pytest.mark.asyncio
async def test_dispatch_workflow_timeout_yields_observable_result(monkeypatch) -> None:
    """A hung workflow must NOT hang run_method — it yields a timeout result.

    DQ-C4 silent-death class: compile() hangs → result() never returns →
    run_method never completes → NO analyst_trace is written, so the leg looks
    dormant. The dispatch bound turns that silence into an observable
    'workflow_timeout' outcome so the actor records a trace.
    """
    monkeypatch.setenv("LEGBA_OPTIMIZER_DISPATCH_TIMEOUT_S", "0.3")
    wf_in = OptimizerWorkflowInput(
        analyst_id="country_assessor",
        analyst_version="v0",
        parent_prompt_module_path="legba.prompts.country_assessor.v1",
        training_set=[{"run_id": "x", "input": "ctx", "gold": "g",
                       "critique_score": 0.5}],
    )
    result, meta = await _dispatch_workflow(
        _HangingTemporalClient(), wf_in, workflow_id="optimizer.country_assessor.deadbeef",
    )
    assert meta["timed_out"] is True
    assert result.diagnostics["method"] == "workflow_timeout"
    assert result.eval_score == 0.0
    assert "workflow_timeout" in result.candidate_prompt_module_text
    assert result.training_set_size == 1


# ---------------------------------------------------------------------------
# Synthetic trace/critique row builder
# ---------------------------------------------------------------------------


#: K-3 — pin the parent prompt to a module that actually imports.
#:
#: ``optimizer.py`` used to derive the parent path by convention as
#: ``legba.prompts.{analyzed_analyst_id}.v1``, and the synthetic analyst id
#: below (``inline_target.india_energy``) yielded
#: ``legba.prompts.inline_target.india_energy.v1`` — a module that has never
#: existed. The tests that drive the full loop still passed, because
#: ``_load_parent_prompt_text`` returned a ``<<missing prompt module>>`` marker
#: at ``logger.debug`` and the optimizer went on to score and mutate *that
#: marker*: every "the loop produced a candidate" assertion was being made
#: against a placeholder. That path raises now, which is what surfaced it.
#:
#: Passed explicitly rather than by renaming the synthetic analyst to a real
#: one, because a real analyst id also changes which descriptor the parent-text
#: fork looks up — these tests are about the loop's control flow, not about
#: descriptor resolution. ``predictor.v1`` is the cheapest real prompt package
#: to build (~0.01s).
_REAL_PARENT_PROMPT = {"parent_prompt_module_path": "legba.prompts.predictor.v1"}

#: The analyzed analyst's KIND — what the corrected convention builds from.
#:
#: Prompt PACKAGES are named by kind (``legba/prompts/inline_target/v1.py``),
#: not by analyst id, so the synthetic ``inline_target.india_energy`` /
#: ``inline_target.brazil`` units below resolve to the REAL
#: ``legba.prompts.inline_target.v1``. In production the actor plumbs this key
#: from the analyzed analyst's head descriptor
#: (``dapr_actors._inject_optimizer_prompt_options``); a run that can supply
#: NEITHER a declared path nor a kind now emits a loud no-op instead of
#: inventing a module path.
_ANALYZED_KIND = {"analyzed_analyst_kind": "inline_target"}


def _trace_critique_row(
    *,
    analyst_id: str = "inline_target.india_energy",
    analyst_version: str = "v" + "a" * 16,
    critique_score: float | None = 0.6,
    prompt_text: str = "Analyze Brazil energy signals from the last week.",
    output_text: str = "Itaipu upgrade was the most significant event.",
    run_id: UUID | None = None,
    critique_id: UUID | None = None,
    output_row_refs: list[UUID] | None = None,
    produced_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a row as :func:`read_traces_and_critiques` would project.

    ``output_row_refs`` mirrors ``analyst_traces.output_row_refs`` — the
    substrate-row UUIDs the analyzed run produced. When unset it defaults to a
    single fresh produced-row id so the D10 ``derived_from`` resolution has a
    real lineage root to surface; pass ``[]`` to model a trace that produced no
    substrate rows.
    """
    return {
        "run_id": run_id or uuid4(),
        "analyzed_analyst_id": analyst_id,
        "analyzed_analyst_version": analyst_version,
        "input": prompt_text,
        "gold": output_text,
        "trace_status": "success",
        "output_row_refs": (
            output_row_refs if output_row_refs is not None else [uuid4()]
        ),
        "critique_score": critique_score,
        "critique_scores": {} if critique_score is None else {"helpfulness": critique_score},
        "critique_revision_delta": None,
        "critique_id": critique_id or (uuid4() if critique_score is not None else None),
        "run_started_at": (
            produced_at or datetime.now(tz=timezone.utc) - timedelta(hours=1)
        ),
    }


# ---------------------------------------------------------------------------
# Kind identity
# ---------------------------------------------------------------------------


def test_kind_name_matches_taxonomy() -> None:
    assert KIND_NAME == "optimizer"


def test_handler_and_schema_versions_set() -> None:
    assert HANDLER_VERSION
    assert SCHEMA_VERSION.startswith("legba/analyst.optimizer/")


def test_output_kind_is_prompt_module_candidate() -> None:
    assert OUTPUT_KIND == OutputKind.PROMPT_MODULE_CANDIDATE


def test_build_prompt_module_returns_none() -> None:
    """Wave B prereq: the optimizer is exempt — it COMPILES prompts."""
    assert build_prompt_module() is None


def test_output_kind_spec_registered() -> None:
    """OutputKind.PROMPT_MODULE_CANDIDATE must round-trip through the registry."""
    spec = spec_for_kind(OutputKind.PROMPT_MODULE_CANDIDATE)
    assert spec.kind == OutputKind.PROMPT_MODULE_CANDIDATE
    assert spec.table == "analyst_outputs"
    assert spec.payload_model is PromptModuleCandidatePayload
    assert "prompt_module_candidate" in spec.schema_uri


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


def test_resolve_analyzed_identity_from_options() -> None:
    options = {"analyzed_analyst_id": "inline_target.x", "analyzed_analyst_version": "v1"}
    aid, aver = _resolve_analyzed_identity([], options)
    assert aid == "inline_target.x"
    assert aver == "v1"


def test_resolve_analyzed_identity_from_inputs() -> None:
    inputs = [_trace_critique_row(analyst_id="inline_target.x", analyst_version="v2")]
    aid, aver = _resolve_analyzed_identity(inputs, {})
    assert aid == "inline_target.x"
    assert aver == "v2"


def test_resolve_analyzed_identity_options_wins() -> None:
    inputs = [_trace_critique_row(analyst_id="inline_target.x")]
    aid, _ = _resolve_analyzed_identity(
        inputs, {"analyzed_analyst_id": "inline_target.y"},
    )
    assert aid == "inline_target.y"


def test_resolve_analyzed_identity_empty() -> None:
    aid, aver = _resolve_analyzed_identity([], {})
    assert aid is None
    assert aver is None


def test_shape_training_set_strips_excess_text() -> None:
    big_input = "x" * 20000
    row = _trace_critique_row(prompt_text=big_input)
    shaped = _shape_training_set([row])
    assert len(shaped) == 1
    assert len(shaped[0]["input"]) <= 8000


def test_shape_training_set_normalizes_score_to_float() -> None:
    row = _trace_critique_row(critique_score=0.42)
    shaped = _shape_training_set([row])
    assert shaped[0]["critique_score"] == pytest.approx(0.42)


def test_shape_training_set_handles_missing_critique() -> None:
    row = _trace_critique_row(critique_score=None)
    shaped = _shape_training_set([row])
    assert shaped[0]["critique_score"] is None


def test_collect_derived_from_uses_output_row_refs_not_trace_ids() -> None:
    """D10: derived_from carries the PRODUCED substrate-row ids, NOT trace ids.

    A trace run_id / critique_id points at analyst_traces / analyst_critiques —
    tables NOT in the derived_from lineage catalog — so putting them in
    derived_from creates 100%-dangling edges. The lineage roots are the rows the
    analyzed run produced (analyst_traces.output_row_refs).
    """
    trace_id = uuid4()
    critique_id = uuid4()
    produced = [uuid4(), uuid4()]
    row = _trace_critique_row(
        run_id=trace_id, critique_id=critique_id, output_row_refs=produced,
    )
    refs = _collect_derived_from([row])
    assert set(refs) == set(produced)
    # The trace + critique ids MUST NOT leak into derived_from (the D10 bug).
    assert trace_id not in refs
    assert critique_id not in refs


def test_collect_derived_from_empty_when_no_produced_rows() -> None:
    """A trace that produced no substrate rows contributes no lineage edge."""
    row = _trace_critique_row(output_row_refs=[])
    assert _collect_derived_from([row]) == []


def test_collect_derived_from_dedupes_shared_produced_rows() -> None:
    """Two traces sharing a produced row yield ONE derived_from edge (ordered)."""
    shared = uuid4()
    only_b = uuid4()
    rows = [
        _trace_critique_row(output_row_refs=[shared]),
        _trace_critique_row(output_row_refs=[shared, only_b]),
    ]
    refs = _collect_derived_from(rows)
    assert refs == [shared, only_b]


def test_collect_derived_from_skips_malformed_output_refs() -> None:
    """Unparseable produced-row ids are dropped, not raised on."""
    good = uuid4()
    row = _trace_critique_row(output_row_refs=[good])
    row["output_row_refs"] = ["not-a-uuid", str(good), None]
    refs = _collect_derived_from([row])
    assert refs == [good]


def test_collect_derived_trace_ids_picks_up_traces_and_critiques() -> None:
    """The trace + critique ids live ONLY in the data.derived_trace_ids audit
    slot — recorded for debugging, NEVER in derived_from (D10)."""
    trace_id = uuid4()
    critique_id = uuid4()
    row = _trace_critique_row(run_id=trace_id, critique_id=critique_id)
    refs = _collect_derived_trace_ids([row])
    assert trace_id in refs
    assert critique_id in refs


def test_collect_derived_trace_ids_skips_malformed_ids() -> None:
    row = _trace_critique_row()
    row["run_id"] = "not-a-uuid"
    row["critique_id"] = None
    assert _collect_derived_trace_ids([row]) == []


# ---------------------------------------------------------------------------
# run_method — happy path + edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_method_dispatches_workflow_with_correct_shape() -> None:
    """The kind hands the durable workflow a training-set REFERENCE (not the
    inlined rows — the 4 MB gRPC-cap pass-by-reference fix, #86), plus the
    analyst identity + the GEPA hyperparams from deps. Verifying the wire shape
    so we catch param drift."""
    stub_client = _StubTemporalClient()
    deps = OptimizerDeps(temporal_client=stub_client, max_generations=4)

    inputs = [
        _trace_critique_row(analyst_id="inline_target.brazil")
        for _ in range(7)
    ]
    options = {
        "analyst_id": "optimizer.brazil",
        "run_id": uuid4(),
        **_ANALYZED_KIND,
    }

    result = await run_method(inputs, options, deps)

    assert len(stub_client.calls) == 1
    call = stub_client.calls[0]
    wf_input: OptimizerWorkflowInput = call["workflow_input"]
    assert wf_input.analyst_id == "inline_target.brazil"
    # The CONVENTION path, derived from the analyzed analyst's KIND. Deriving
    # it from the analyst ID (the old behaviour) produced
    # `legba.prompts.inline_target.brazil.v1`, which has never existed —
    # prompt packages are named by kind.
    assert wf_input.parent_prompt_module_path == "legba.prompts.inline_target.v1"
    assert wf_input.max_generations == 4
    # _StubTemporalClient models the DURABLE (Dapr) client, so the 7 joined rows
    # pass BY REFERENCE (the 4 MB gRPC-cap fix, #86): the inline training_set is
    # intentionally empty and the ref re-fetches the identical rows in-activity.
    # (The inline path is exercised by test_optimizer_payload_by_reference.py.)
    assert wf_input.training_set == []
    assert wf_input.training_set_ref is not None
    assert wf_input.training_set_ref.analyzed_analyst_id == "inline_target.brazil"
    assert wf_input.training_set_ref.until_ts
    # The candidate payload landed under finding.data["candidate"].
    candidate_dict = result.finding.data["candidate"]
    candidate = PromptModuleCandidatePayload(**candidate_dict)
    assert candidate.analyst_id == "inline_target.brazil"
    assert candidate.eval_score == pytest.approx(0.72)
    assert candidate.eval_score_delta == pytest.approx(0.15)
    assert candidate.gepa_generation == 3
    # Workflow handle ids were propagated. Separator is '.' not '::' — a '::'
    # in the Dapr workflow instance id collides with the durabletask
    # activity-actor id scheme and orphans the activity result (see optimizer.py).
    assert candidate.temporal_workflow_id.startswith("optimizer.inline_target.brazil.")
    assert candidate.temporal_run_id.startswith("stub_run::")


@pytest.mark.asyncio
async def test_run_method_surfaces_workflow_token_usage() -> None:
    """G5 — the optimizer must report its REAL token usage so the actor
    records spend against country_optimizer's per-day cap. Previously
    run_method returned usage={} so the optimizer was exempt from its OWN
    cap; the workflow now stamps observed usage into diagnostics['usage']
    and run_method lifts it onto the method result."""
    canned = OptimizerWorkflowResult(
        candidate_prompt_module_text="Be concise; cite evidence.",
        training_set_size=10,
        eval_score=0.7,
        eval_score_delta=0.1,
        gepa_generation=2,
        diagnostics={
            "method": "dspy_gepa",
            "usage": {"prompt_tokens": 4321, "completion_tokens": 765,
                      "total_tokens": 5086},
        },
    )
    stub_client = _StubTemporalClient(canned_result=canned)
    deps = OptimizerDeps(temporal_client=stub_client)
    inputs = [_trace_critique_row() for _ in range(6)]

    result = await run_method(inputs, {"analyst_id": "optimizer.x", **_ANALYZED_KIND}, deps)

    # The cap accrues on prompt+completion+reasoning — all must propagate.
    assert result.usage, "optimizer must NOT report empty usage on a real run"
    assert result.usage["prompt_tokens"] == 4321
    assert result.usage["completion_tokens"] == 765
    assert result.usage["total_tokens"] == 5086


@pytest.mark.asyncio
async def test_run_method_usage_zero_when_no_llm_calls() -> None:
    """A workflow that made no LLM calls (naive / empty path) stamps a
    zero-usage dict; run_method passes the zeros through truthfully —
    no fabricated spend, but a well-formed dict (not {})."""
    canned = OptimizerWorkflowResult(
        candidate_prompt_module_text="parent unchanged",
        training_set_size=3,
        eval_score=0.4,
        eval_score_delta=0.0,
        gepa_generation=0,
        diagnostics={
            "method": "naive_best_of_n",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                      "total_tokens": 0},
        },
    )
    stub_client = _StubTemporalClient(canned_result=canned)
    deps = OptimizerDeps(temporal_client=stub_client)
    inputs = [_trace_critique_row() for _ in range(6)]

    result = await run_method(inputs, {"analyst_id": "optimizer.x", **_ANALYZED_KIND}, deps)
    assert result.usage["prompt_tokens"] == 0
    assert result.usage["total_tokens"] == 0


@pytest.mark.asyncio
async def test_run_method_usage_defaults_when_diagnostics_lack_usage() -> None:
    """A diagnostics blob missing 'usage' (older workflow result) yields a
    zeroed dict rather than raising — the run still produced a candidate."""
    canned = OptimizerWorkflowResult(
        candidate_prompt_module_text="x",
        training_set_size=5,
        eval_score=0.6,
        eval_score_delta=0.05,
        gepa_generation=1,
        diagnostics={"method": "stub_no_usage_key"},
    )
    stub_client = _StubTemporalClient(canned_result=canned)
    deps = OptimizerDeps(temporal_client=stub_client)
    inputs = [_trace_critique_row() for _ in range(6)]

    result = await run_method(inputs, {"analyst_id": "optimizer.x", **_ANALYZED_KIND}, deps)
    assert result.usage == {
        "prompt_tokens": 0, "completion_tokens": 0,
        "reasoning_tokens": 0, "total_tokens": 0,
    }


@pytest.mark.asyncio
async def test_run_method_no_analyzed_analyst_short_circuits() -> None:
    """No analyzed_analyst_id resolvable → noop result, no workflow."""
    stub_client = _StubTemporalClient()
    deps = OptimizerDeps(temporal_client=stub_client)
    # Bare options + empty inputs — nothing to resolve.
    result = await run_method([], {"analyst_id": "optimizer.x", **_ANALYZED_KIND}, deps)
    assert stub_client.calls == [], "should not dispatch a workflow with no target"
    assert "noop" in result.finding.tags
    assert result.derived_from == []


@pytest.mark.asyncio
async def test_run_method_derived_from_is_produced_rows_traces_in_data() -> None:
    """D10 end-to-end: the candidate's derived_from carries the analyzed runs'
    PRODUCED substrate-row ids; the trace + critique ids land only under
    data.derived_trace_ids (the audit slot), never in derived_from."""
    stub_client = _StubTemporalClient()
    deps = OptimizerDeps(temporal_client=stub_client)
    trace_ids = [uuid4() for _ in range(3)]
    critique_ids = [uuid4() for _ in range(3)]
    produced_ids = [uuid4() for _ in range(3)]
    inputs = [
        _trace_critique_row(run_id=t, critique_id=c, output_row_refs=[p])
        for t, c, p in zip(trace_ids, critique_ids, produced_ids)
    ]
    result = await run_method(inputs, {"analyst_id": "optimizer.x", **_ANALYZED_KIND}, deps)
    derived = set(result.derived_from)
    # derived_from = produced substrate rows (real lineage roots).
    assert set(produced_ids).issubset(derived)
    # trace / critique ids are EXCLUDED from derived_from (the D10 fix).
    assert derived.isdisjoint(set(trace_ids))
    assert derived.isdisjoint(set(critique_ids))
    # ...but recorded in the candidate's data.derived_trace_ids audit slot.
    candidate = PromptModuleCandidatePayload(**result.finding.data["candidate"])
    audit = set(candidate.data["derived_trace_ids"])
    assert {str(t) for t in trace_ids}.issubset(audit)
    assert {str(c) for c in critique_ids}.issubset(audit)
    # And the produced rows are NOT in the trace-id audit slot.
    assert audit.isdisjoint({str(p) for p in produced_ids})


@pytest.mark.asyncio
async def test_run_method_promotion_policy_round_trips() -> None:
    """The descriptor's promotion policy flows onto the candidate row."""
    stub_client = _StubTemporalClient()
    deps = OptimizerDeps(temporal_client=stub_client)
    inputs = [_trace_critique_row()]
    options = {
        "analyst_id": "optimizer.x",
        "promotion_policy": "auto_with_threshold",
        **_ANALYZED_KIND,
    }
    result = await run_method(inputs, options, deps)
    candidate = PromptModuleCandidatePayload(**result.finding.data["candidate"])
    assert candidate.promotion_gate == "auto_with_threshold"


@pytest.mark.asyncio
async def test_run_method_eval_score_delta_is_candidate_minus_parent() -> None:
    """delta = candidate − parent on the workflow's reported scores.

    The kind module trusts the workflow's reported eval_score_delta and
    surfaces it on the payload verbatim — this confirms there's no
    accidental sign flip or substitution.
    """
    canned = OptimizerWorkflowResult(
        candidate_prompt_module_text="X",
        training_set_size=5,
        eval_score=0.83,
        eval_score_delta=0.21,
        gepa_generation=2,
        diagnostics={},
    )
    stub_client = _StubTemporalClient(canned_result=canned)
    deps = OptimizerDeps(temporal_client=stub_client)
    inputs = [_trace_critique_row()]
    result = await run_method(inputs, {"analyst_id": "optimizer.x", **_ANALYZED_KIND}, deps)
    candidate = PromptModuleCandidatePayload(**result.finding.data["candidate"])
    assert candidate.eval_score == pytest.approx(0.83)
    assert candidate.eval_score_delta == pytest.approx(0.21)


@pytest.mark.asyncio
async def test_run_method_propagates_real_method_not_hardcoded() -> None:
    """finding.data['method'] reflects the workflow's REAL method.

    Regression for the §3.8 label bug: the kind used to hardcode
    ``'dspy_gepa'`` regardless of the actual path. A worker-less / dspy-
    unavailable deploy runs the naive fallback, so the recorded method MUST
    come from ``workflow_result.diagnostics['method']``.
    """
    canned = OptimizerWorkflowResult(
        candidate_prompt_module_text="cand",
        training_set_size=5,
        eval_score=0.6,
        eval_score_delta=0.05,
        gepa_generation=1,
        parent_prompt_module_text="PARENT BASELINE TEXT",
        diagnostics={"method": "naive_best_of_n", "baseline_score": 0.55},
    )
    stub_client = _StubTemporalClient(canned_result=canned)
    deps = OptimizerDeps(temporal_client=stub_client)
    result = await run_method(
        [_trace_critique_row()], {"analyst_id": "optimizer.x", **_ANALYZED_KIND}, deps,
    )
    # Both the finding envelope AND the candidate's data bag carry the REAL
    # method — never the hardcoded 'dspy_gepa'.
    assert result.finding.data["method"] == "naive_best_of_n"
    candidate = PromptModuleCandidatePayload(**result.finding.data["candidate"])
    assert candidate.data["method"] == "naive_best_of_n"


@pytest.mark.asyncio
async def test_run_method_snapshots_parent_prompt_text() -> None:
    """The candidate carries the parent text snapshot for the diff route.

    The operator diff route reads ``parent_prompt_module_text`` from the
    persisted row (no dspy import), so the kind MUST copy the workflow's
    captured parent text onto the payload.
    """
    canned = OptimizerWorkflowResult(
        candidate_prompt_module_text="the candidate prompt body",
        training_set_size=5,
        eval_score=0.6,
        eval_score_delta=0.05,
        gepa_generation=1,
        parent_prompt_module_text="the PARENT prompt body the delta was vs",
        diagnostics={"method": "dspy_gepa"},
    )
    stub_client = _StubTemporalClient(canned_result=canned)
    deps = OptimizerDeps(temporal_client=stub_client)
    result = await run_method(
        [_trace_critique_row()], {"analyst_id": "optimizer.x", **_ANALYZED_KIND}, deps,
    )
    candidate = PromptModuleCandidatePayload(**result.finding.data["candidate"])
    assert candidate.parent_prompt_module_text == (
        "the PARENT prompt body the delta was vs"
    )


# ---------------------------------------------------------------------------
# In-process Temporal path — min_traces_required enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_process_client_enforces_min_traces_required() -> None:
    """Under-trained analyst → zero-delta "skipped_validation" candidate.

    Exercises the InProcessWorkflowClient + gepa-loop pipeline end-to-
    end, not just the kind module — confirms min_traces_required gates
    the GEPA loop entry.
    """
    client = InProcessWorkflowClient()
    deps = OptimizerDeps(
        temporal_client=client,
        min_traces_required=10,   # require 10, we'll supply 3
    )
    inputs = [_trace_critique_row() for _ in range(3)]
    result = await run_method(inputs, {"analyst_id": "optimizer.x", **_ANALYZED_KIND}, deps)
    candidate = PromptModuleCandidatePayload(**result.finding.data["candidate"])
    # Validation failed → text starts with the skip marker, delta is 0.
    assert candidate.candidate_prompt_module_text.startswith("<<skipped:")
    assert candidate.eval_score_delta == pytest.approx(0.0)
    assert candidate.eval_score == pytest.approx(0.0)


@pytest.mark.skip(reason="optimizer plane mothballed 2026-08-21 (RUST-4)")
@pytest.mark.asyncio
async def test_in_process_client_runs_loop_when_enough_traces() -> None:
    client = InProcessWorkflowClient()
    deps = OptimizerDeps(
        temporal_client=client,
        min_traces_required=2,
        max_generations=2,
    )
    inputs = [
        _trace_critique_row(critique_score=0.5 + (i % 3) * 0.1) for i in range(5)
    ]
    result = await run_method(
        inputs, {"analyst_id": "optimizer.x", **_REAL_PARENT_PROMPT}, deps,
    )
    candidate = PromptModuleCandidatePayload(**result.finding.data["candidate"])
    # Loop produced *something* — the candidate text is non-empty and
    # the loop ran for between 0 and max_generations generations.
    assert candidate.candidate_prompt_module_text
    assert 0 <= candidate.gepa_generation <= deps.max_generations
    # Score is bounded.
    assert 0.0 <= candidate.eval_score <= 1.0


@pytest.mark.skip(reason="optimizer plane mothballed 2026-08-21 (RUST-4)")
@pytest.mark.asyncio
async def test_naive_fallback_respects_max_generations_bound() -> None:
    """The naive fallback shouldn't try more mutations than max_generations.

    With dspy.settings.lm = None (test env) we always hit the naive
    fallback; max_generations=1 should keep the per_generation_scores
    diagnostics list at parent + 1 mutation.
    """
    try:
        import dspy
    except ModuleNotFoundError:
        # dspy absent → lm is definitionally unconfigured, so run_method
        # always lands on the naive fallback path (what this test asserts).
        dspy = None  # type: ignore[assignment]
    if dspy is not None and getattr(dspy.settings, "lm", None) is not None:
        pytest.skip("dspy.settings.lm is configured; this test exercises the fallback")
    client = InProcessWorkflowClient()
    deps = OptimizerDeps(
        temporal_client=client,
        min_traces_required=1,
        max_generations=1,
    )
    inputs = [_trace_critique_row() for _ in range(3)]
    result = await run_method(
        inputs, {"analyst_id": "optimizer.x", **_REAL_PARENT_PROMPT}, deps,
    )
    candidate = PromptModuleCandidatePayload(**result.finding.data["candidate"])
    diagnostics = candidate.data.get("diagnostics") or {}
    # Either the dspy.GEPA path ran (rare in test env) OR the naive
    # fallback respected the bound.
    if diagnostics.get("method") == "naive_best_of_n":
        scores = diagnostics.get("per_generation_scores") or []
        # parent (gen 0) + max_generations mutations = 2 entries.
        assert len(scores) == 2


# NOTE — promotion-gate coverage moved to the LIVE gate. The former
# ``should_auto_promote`` auto-promotion policy was removed (it had ZERO
# production call sites); the human-gated promotion path now runs through
# ``gepa._delta_gates_ok`` (the write-time promotable stamp) +
# ``resolve_promoted_system_prompt``. Those contracts are pinned by
# ``tests/test_p4t8_honesty_optimizer_promotion.py`` and
# ``tests/test_optimizer_promotion.py``.


# ---------------------------------------------------------------------------
# Discovery — the optimizer kind shows up in the host's discover layer
# ---------------------------------------------------------------------------


def test_optimizer_discoverable_via_kind_registry() -> None:
    """The host's discover_analyst_kinds picks up the optimizer."""
    from legba.data.analysts import discover_analyst_kinds
    registry = discover_analyst_kinds()
    assert "optimizer" in registry
    handler = registry["optimizer"]
    assert handler.output_kind == OutputKind.PROMPT_MODULE_CANDIDATE
    assert handler.read_slice is not None
    assert callable(handler.read_slice)
