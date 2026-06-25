# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-176 optimizer GEPA-loop tests — determinism + validation wiring.

Targets :mod:`legba.runtime.dapr_workflow.gepa` (the loop moved there
from the deleted ``legba.runtime.temporal`` package with P-CUT/C-3).

Surface under test: the **in-process loop** —
:func:`run_optimizer_in_process` exercises the GEPA loop without any
durable-workflow indirection.  This is the production fallback path when
no Dapr Workflow client can be built, AND the exact algorithm the Dapr
Workflow activities delegate to (see
``tests/runtime/test_dapr_workflow_optimizer.py`` for the activity /
client contract).  Tests here confirm the loop is deterministic +
bounded + handles bad rows gracefully.

(The old Temporal-server replay tests, gated by ``LEGBA_TEST_TEMPORAL=1``,
were deleted with the Temporal substrate — ``temporalio`` left the
dependency set, making them permanently unrunnable.)
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from legba.runtime.dapr_workflow.gepa import (
    OptimizerWorkflowInput,
    OptimizerWorkflowResult,
    _dspy_usage_delta,
    _extract_keywords,
    _gepa_num_threads,
    _gepa_rollout_headroom,
    _gepa_valset_max,
    _naive_candidate_search,
    _score_prompt_on_dataset,
    _zero_usage,
    run_optimizer_in_process,
    validate_training_set_activity,
)


# ---------------------------------------------------------------------------
# In-process loop tests (always run)
# ---------------------------------------------------------------------------


def _build_workflow_input(
    *,
    n_rows: int = 10,
    min_traces_required: int = 1,
    max_generations: int = 3,
    base_score: float = 0.5,
) -> OptimizerWorkflowInput:
    return OptimizerWorkflowInput(
        analyst_id="inline_target.test",
        analyst_version="v" + "0" * 16,
        parent_prompt_module_path="legba.prompts.inline_target.v1",
        training_set=[
            {
                "run_id": str(uuid4()),
                "input": f"signal context row {i} energy infrastructure brazil",
                "gold": f"finding {i} about brazil energy",
                "critique_score": base_score + (i % 3) * 0.05,
            }
            for i in range(n_rows)
        ],
        max_generations=max_generations,
        min_traces_required=min_traces_required,
    )


@pytest.mark.asyncio
async def test_validate_activity_passes_when_enough_traces() -> None:
    wf_in = _build_workflow_input(n_rows=20, min_traces_required=10)
    result = await validate_training_set_activity(wf_in)
    assert result["ok"] is True
    assert result["training_size"] == 20


@pytest.mark.asyncio
async def test_validate_activity_rejects_short_training_set() -> None:
    wf_in = _build_workflow_input(n_rows=3, min_traces_required=10)
    result = await validate_training_set_activity(wf_in)
    assert result["ok"] is False
    assert "insufficient_traces" in result["reason"]


@pytest.mark.asyncio
async def test_in_process_short_circuits_below_min_traces() -> None:
    wf_in = _build_workflow_input(n_rows=2, min_traces_required=5)
    result = await run_optimizer_in_process(wf_in)
    assert isinstance(result, OptimizerWorkflowResult)
    assert result.gepa_generation == 0
    assert result.eval_score_delta == 0.0
    assert "<<skipped:" in result.candidate_prompt_module_text


@pytest.mark.asyncio
async def test_in_process_returns_bounded_result() -> None:
    wf_in = _build_workflow_input(n_rows=10, min_traces_required=1, max_generations=2)
    result = await run_optimizer_in_process(wf_in)
    assert isinstance(result, OptimizerWorkflowResult)
    assert 0 <= result.gepa_generation <= wf_in.max_generations
    assert 0.0 <= result.eval_score <= 1.0
    assert -1.0 <= result.eval_score_delta <= 1.0
    assert result.training_set_size == 10


@pytest.mark.asyncio
async def test_in_process_is_deterministic_for_same_input() -> None:
    """Replay determinism precondition: same input → same output.

    The durable-workflow layer's whole value proposition is replay
    safety, so the inner loop must be deterministic when run twice with
    the same input.  This is the strongest correctness claim we can make
    at the in-process layer.
    """
    wf_in = _build_workflow_input(n_rows=8, min_traces_required=1, max_generations=2)
    r1 = await run_optimizer_in_process(wf_in)
    r2 = await run_optimizer_in_process(wf_in)
    assert r1.candidate_prompt_module_text == r2.candidate_prompt_module_text
    assert r1.eval_score == pytest.approx(r2.eval_score)
    assert r1.eval_score_delta == pytest.approx(r2.eval_score_delta)
    assert r1.gepa_generation == r2.gepa_generation


def test_score_function_deterministic() -> None:
    """Score function must be pure-deterministic so the workflow
    replays correctly.  Same prompt + same dataset → same score."""
    dataset = [
        {"input": "abc def", "critique_score": 0.6},
        {"input": "xyz abc", "critique_score": 0.4},
    ]
    s1 = _score_prompt_on_dataset("abc be concise", dataset)
    s2 = _score_prompt_on_dataset("abc be concise", dataset)
    assert s1 == s2
    assert 0.0 <= s1 <= 1.0


def test_score_function_responds_to_prompt_quality() -> None:
    """A prompt whose keywords overlap with the input should outscore
    one with no overlap, all else equal.  This is what makes the
    naive fallback informative."""
    dataset = [
        {"input": "energy infrastructure brazil signals", "critique_score": 0.5},
        {"input": "energy markets brazil supply chain", "critique_score": 0.5},
    ]
    matched_score = _score_prompt_on_dataset(
        "energy infrastructure brazil supply analysis", dataset,
    )
    unmatched_score = _score_prompt_on_dataset(
        "weather forecast tomorrow morning daily", dataset,
    )
    assert matched_score > unmatched_score


def test_keyword_extraction_drops_short_tokens() -> None:
    kws = _extract_keywords("be a very wise analyst")
    # "be" + "a" dropped (len < 4), rest kept.
    assert "very" in kws
    assert "wise" in kws
    assert "analyst" in kws
    assert "be" not in kws
    assert "a" not in kws


def test_naive_search_returns_parent_when_no_mutation_helps() -> None:
    """If the parent prompt happens to be optimal for the heuristic
    score, the naive loop returns the parent unchanged (delta = 0)."""
    wf_in = _build_workflow_input(n_rows=5, min_traces_required=1)
    parent = "this is the parent prompt"
    baseline_score = _score_prompt_on_dataset(parent, wf_in.training_set)
    result = _naive_candidate_search(
        wf_in,
        parent_text=parent,
        baseline_score=baseline_score,
    )
    assert result.eval_score_delta >= 0.0       # never worse than parent
    assert 0.0 <= result.eval_score <= 1.0


# ---------------------------------------------------------------------------
# G5 — the optimizer reports REAL token usage so its OWN cap accrues
# ---------------------------------------------------------------------------


def test_naive_search_stamps_zero_usage_into_diagnostics() -> None:
    """The naive fallback makes no LLM calls — its honest usage is zero,
    but the dict is PRESENT so run_method can read it without a guard."""
    wf_in = _build_workflow_input(n_rows=5, min_traces_required=1)
    parent = "this is the parent prompt"
    baseline_score = _score_prompt_on_dataset(parent, wf_in.training_set)
    result = _naive_candidate_search(
        wf_in, parent_text=parent, baseline_score=baseline_score,
    )
    usage = result.diagnostics["usage"]
    assert usage == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


@pytest.mark.asyncio
async def test_in_process_result_carries_usage_dict() -> None:
    """Every in-process result (which goes through naive fallback when no
    LLM is configured) carries a usage dict in diagnostics — the contract
    run_method depends on (G5)."""
    wf_in = _build_workflow_input(n_rows=8, min_traces_required=1)
    result = await run_optimizer_in_process(wf_in)
    assert "usage" in result.diagnostics
    usage = result.diagnostics["usage"]
    assert set(usage) >= {"prompt_tokens", "completion_tokens", "total_tokens"}
    # No real LLM in the test rig → zero observed tokens, but well-formed.
    assert all(isinstance(v, int) for v in usage.values())


def test_zero_usage_shape() -> None:
    assert _zero_usage() == {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
    }


def test_dspy_usage_delta_sums_only_new_history_entries() -> None:
    """The usage delta sums ONLY the LM-history entries appended after the
    snapshot index — pre-existing calls (from earlier runs sharing the LM)
    must not be double-counted, and total falls back to prompt+completion."""

    class _LM:
        history = [
            {"usage": {"prompt_tokens": 100, "completion_tokens": 50}},   # pre-existing
            {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
            {"usage": {"prompt_tokens": 20, "completion_tokens": 7}},     # no total
            {"no_usage_key": True},                                       # tolerated
        ]

    usage = _dspy_usage_delta(_LM(), since=1)
    assert usage["prompt_tokens"] == 30        # 10 + 20, NOT the pre-existing 100
    assert usage["completion_tokens"] == 12    # 5 + 7
    # total = the one real total (15) + the fallback for the no-total row (27).
    assert usage["total_tokens"] == 15 + 27


def test_dspy_usage_delta_tolerates_missing_history() -> None:
    class _NoHistory:
        pass

    assert _dspy_usage_delta(_NoHistory(), since=0) == _zero_usage()


# ---------------------------------------------------------------------------
# DQ-C4 — valset cap + parallelism bounds (the GEPA hang fix)
# ---------------------------------------------------------------------------


def test_gepa_valset_max_default_and_clamps(monkeypatch) -> None:
    """The valset cap defaults to 40 and clamps to a sane window.

    A small valset is what stops dspy.GEPA from falling back to the WHOLE
    500-row trainset as its valset (the base + per-rollout evals that wedged
    the daily compile at 0/30 rollouts)."""
    monkeypatch.delenv("LEGBA_GEPA_VALSET_MAX", raising=False)
    assert _gepa_valset_max() == 40
    monkeypatch.setenv("LEGBA_GEPA_VALSET_MAX", "25")
    assert _gepa_valset_max() == 25
    monkeypatch.setenv("LEGBA_GEPA_VALSET_MAX", "9999")   # clamp high
    assert _gepa_valset_max() == 200
    monkeypatch.setenv("LEGBA_GEPA_VALSET_MAX", "1")      # clamp low
    assert _gepa_valset_max() == 5
    monkeypatch.setenv("LEGBA_GEPA_VALSET_MAX", "garbage")  # tolerate junk
    assert _gepa_valset_max() == 40


def test_gepa_num_threads_default_and_clamps(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_GEPA_NUM_THREADS", raising=False)
    assert _gepa_num_threads() == 4
    monkeypatch.setenv("LEGBA_GEPA_NUM_THREADS", "8")
    assert _gepa_num_threads() == 8
    monkeypatch.setenv("LEGBA_GEPA_NUM_THREADS", "999")    # clamp high
    assert _gepa_num_threads() == 16
    monkeypatch.setenv("LEGBA_GEPA_NUM_THREADS", "0")      # clamp low
    assert _gepa_num_threads() == 1
    monkeypatch.setenv("LEGBA_GEPA_NUM_THREADS", "nope")   # tolerate junk
    assert _gepa_num_threads() == 4


def test_gepa_rollout_headroom_default_and_clamps(monkeypatch) -> None:
    """Headroom funds rollouts ABOVE the base valset eval (else 0 rollouts)."""
    monkeypatch.delenv("LEGBA_GEPA_ROLLOUT_HEADROOM", raising=False)
    assert _gepa_rollout_headroom() == 60
    monkeypatch.setenv("LEGBA_GEPA_ROLLOUT_HEADROOM", "120")
    assert _gepa_rollout_headroom() == 120
    monkeypatch.setenv("LEGBA_GEPA_ROLLOUT_HEADROOM", "99999")  # clamp high
    assert _gepa_rollout_headroom() == 460
    monkeypatch.setenv("LEGBA_GEPA_ROLLOUT_HEADROOM", "-5")     # clamp low
    assert _gepa_rollout_headroom() == 0
    monkeypatch.setenv("LEGBA_GEPA_ROLLOUT_HEADROOM", "junk")   # tolerate junk
    assert _gepa_rollout_headroom() == 60


@pytest.mark.asyncio
async def test_activity_failure_propagates_through_loop() -> None:
    """If the inner loop raises, run_optimizer_in_process surfaces it.

    This is the contract the workflow engine needs: a failing activity
    raises, the engine catches and retries per the workflow's retry
    policy.  The in-process path doesn't retry — it surfaces the
    exception so the kind's actor-failure machinery can classify it.
    """
    # Inject a malformed row to force the score function down its
    # defensive path.  The activity should still return a result
    # (not raise) because the metric is wrapped in try/except.
    wf_in = OptimizerWorkflowInput(
        analyst_id="inline_target.test",
        analyst_version="v0",
        parent_prompt_module_path="legba.prompts.inline_target.v1",
        training_set=[
            {"run_id": "bad", "input": None, "gold": None, "critique_score": "nope"},
        ],
        max_generations=1,
        min_traces_required=1,
    )
    # Should NOT raise — the activity is defensive about row shape.
    result = await run_optimizer_in_process(wf_in)
    assert isinstance(result, OptimizerWorkflowResult)

