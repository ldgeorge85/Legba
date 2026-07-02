# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""resolve_promoted_system_prompt — closes the optimizer loop (#37 stage D).

A GEPA candidate an operator flips to ``promotion_gate='promoted'`` becomes
the analyst's live system prompt. This resolver is what inference reads; it
must be fully best-effort (never break a run on a lookup hiccup).
"""
from __future__ import annotations

import asyncio
import dataclasses

import pytest

from legba.data.analysts.optimizer import (
    resolve_promoted_system_prompt,
    should_auto_promote,
)
from legba.runtime.dapr_workflow.gepa import (
    OptimizerWorkflowInput,
    _delta_gates_ok,
    _honest_null_eval,
    _pair_faithfulness,
)


class _FakeConn:
    def __init__(self, row, *, raise_exc=False):
        self._row = row
        self._raise = raise_exc

    async def fetchrow(self, query, *args):
        if self._raise:
            raise RuntimeError("db boom")
        return self._row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, row, *, raise_exc=False):
        self._conn = _FakeConn(row, raise_exc=raise_exc)

    def acquire(self):
        return _FakeAcquire(self._conn)


def _run(coro):
    # Fresh loop per call — robust when prior tests have created/closed loops
    # (e.g. the dspy bridge tests), unlike get_event_loop().run_until_complete.
    return asyncio.run(coro)


def test_returns_promoted_champion_text():
    pool = _FakePool({"text": "EVOLVED PROMPT: be sharper."})
    out = _run(resolve_promoted_system_prompt(pool, "country_assessor", default="BASE"))
    assert out == "EVOLVED PROMPT: be sharper."


def test_no_promoted_row_returns_default():
    pool = _FakePool(None)
    out = _run(resolve_promoted_system_prompt(pool, "country_assessor", default="BASE"))
    assert out == "BASE"


def test_empty_text_returns_default():
    pool = _FakePool({"text": None})
    out = _run(resolve_promoted_system_prompt(pool, "country_assessor", default="BASE"))
    assert out == "BASE"


def test_none_pool_returns_default():
    out = _run(resolve_promoted_system_prompt(None, "country_assessor", default="BASE"))
    assert out == "BASE"


def test_db_error_returns_default_never_raises():
    pool = _FakePool(None, raise_exc=True)
    out = _run(resolve_promoted_system_prompt(pool, "country_assessor", default="BASE"))
    assert out == "BASE"


# ===========================================================================
# P4-T6 — the MEASURED bounded-unit GEPA return. A candidate can NEVER promote
# (auto OR via a hand-edited promotion_gate='promoted') without a positive,
# non-degenerate, judge-scored, sufficiently-paired MEASURED faithfulness delta.
# These tests falsify a violation of that contract.
# ===========================================================================


class _RaisingConn:
    """A conn whose DB is never meant to be reached — asserts that a
    ``should_auto_promote`` MEASUREMENT gate short-circuits BEFORE any policy /
    history query (the degeneracy checks must precede the DB)."""

    async def fetchrow(self, query, *args):  # noqa: ANN001
        raise AssertionError(
            "should_auto_promote consulted the DB past a measurement gate"
        )


def test_should_auto_promote_absent_delta_never_promotes():
    """faithfulness_delta=None (candidate_score None / degenerate) → rejected
    BEFORE the policy branch and WITHOUT touching the DB."""
    ok, reason = _run(should_auto_promote(
        _RaisingConn(),
        analyzed_analyst_id="leadership_transition",
        candidate_score=None,
        parent_score=0.5,
        promotion_policy="auto_with_threshold",
        eval_degenerate=True,
        judge_available=True,
        n_paired=12, min_paired=8, min_delta=0.03,
    ))
    assert ok is False
    assert reason == "degenerate_or_absent_delta"


def test_should_auto_promote_insufficient_paired_sample_never_promotes():
    ok, reason = _run(should_auto_promote(
        _RaisingConn(),
        analyzed_analyst_id="leadership_transition",
        candidate_score=0.7, parent_score=0.5,
        promotion_policy="auto_with_threshold",
        eval_degenerate=False, judge_available=True,
        n_paired=3, min_paired=8, min_delta=0.03,
    ))
    assert ok is False
    assert reason == "insufficient_paired_sample:3<8"


def test_should_auto_promote_judge_unavailable_never_promotes():
    """A POSITIVE delta but judge_available=false → rejected (the before/after
    must share the SAME llm.verify.slm_8b yardstick)."""
    ok, reason = _run(should_auto_promote(
        _RaisingConn(),
        analyzed_analyst_id="leadership_transition",
        candidate_score=0.9, parent_score=0.5,
        promotion_policy="auto_with_threshold",
        eval_degenerate=False, judge_available=False,
        n_paired=12, min_paired=8, min_delta=0.03,
    ))
    assert ok is False
    assert reason == "faithfulness_judge_unavailable"


def test_should_auto_promote_delta_below_margin_never_promotes():
    ok, reason = _run(should_auto_promote(
        _RaisingConn(),
        analyzed_analyst_id="leadership_transition",
        candidate_score=0.51, parent_score=0.50,  # +0.01 < the 0.03 margin
        promotion_policy="auto_with_threshold",
        eval_degenerate=False, judge_available=True,
        n_paired=12, min_paired=8, min_delta=0.03,
    ))
    assert ok is False
    assert reason == "delta_below_margin"


def test_should_auto_promote_legacy_call_preserves_score_floor():
    """The legacy 3-arg convention (no measured-delta params) is unchanged: the
    conditional min_paired/min_delta gates skip and the score-monotonicity floor
    still fires — country_optimizer's promotion semantics are byte-preserved."""
    ok, reason = _run(should_auto_promote(
        _RaisingConn(),
        analyzed_analyst_id="country_assessor",
        candidate_score=0.4, parent_score=0.5,   # candidate did NOT improve
        promotion_policy="auto_with_threshold",
    ))
    assert ok is False
    assert reason == "score_did_not_improve"


# --- the DB-edit-proof resolve_promoted_system_prompt promotable guard ----------


class _GuardAwareConn:
    """Models the P4-T6 WHERE guard: a MEASURED candidate (carries a data.eval
    block) resolves ONLY when ``data.eval.promotable='true'``; a legacy candidate
    (no eval block) is unaffected. Also asserts the guard clause is IN the SQL —
    if a future edit drops it, ``has_eval and not promotable`` would leak the
    evolved text and this fake would return it, failing the guard test."""

    def __init__(self, text, *, has_eval, promotable):
        self._text = text
        self._has_eval = has_eval
        self._promotable = promotable

    async def fetchrow(self, query, *args):  # noqa: ANN001
        assert "promotable" in query, "P4-T6 promotable guard missing from SQL"
        if self._has_eval and not self._promotable:
            return None  # the DB filters the non-promotable measured candidate
        return {"text": self._text}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _GuardAwarePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


def test_promoted_measured_candidate_needs_promotable_true():
    """A hand-flipped ``promotion_gate='promoted'`` on a DEGENERATE measured
    candidate (data.eval present, promotable=false) resolves to the BASELINE —
    the DB edit cannot reach inference without a real positive measured delta."""
    pool = _GuardAwarePool(
        _GuardAwareConn("EVOLVED PROMPT", has_eval=True, promotable=False)
    )
    out = _run(resolve_promoted_system_prompt(
        pool, "leadership_transition", default="BASE",
    ))
    assert out == "BASE"


def test_promoted_measured_candidate_promotable_true_resolves():
    pool = _GuardAwarePool(
        _GuardAwareConn("EVOLVED PROMPT", has_eval=True, promotable=True)
    )
    out = _run(resolve_promoted_system_prompt(
        pool, "leadership_transition", default="BASE",
    ))
    assert out == "EVOLVED PROMPT"


def test_promoted_legacy_candidate_unaffected_by_guard():
    """A critique_proxy / legacy candidate (no data.eval) still resolves — the
    frozen monolith's human-promotion path is byte-unchanged."""
    pool = _GuardAwarePool(
        _GuardAwareConn("EVOLVED PROMPT", has_eval=False, promotable=False)
    )
    out = _run(resolve_promoted_system_prompt(
        pool, "country_assessor", default="BASE",
    ))
    assert out == "EVOLVED PROMPT"


# --- the paired-faithfulness MEASURE math (honest-null vs promotable boundary) --


def test_delta_gates_ok_positive_nondegenerate_passes():
    ok, reason = _delta_gates_ok(
        0.6, 0.5, eval_degenerate=False, judge_available=True,
        n_paired=10, min_paired=8, min_delta=0.03,
    )
    assert ok is True and reason == "delta_ok"


def test_delta_gates_ok_none_candidate_is_absent_delta():
    ok, reason = _delta_gates_ok(
        None, 0.5, eval_degenerate=False, judge_available=True,
        n_paired=10, min_paired=8, min_delta=0.03,
    )
    assert ok is False and reason == "degenerate_or_absent_delta"


def test_pair_faithfulness_below_min_paired_is_honest_null():
    """< min_paired rows scored in BOTH arms → HONEST-NULL: means + delta are
    None (NEVER 0.0-faked), degenerate=True, promotable=False."""
    parent = {"a": 0.5, "b": 0.5, "c": 0.5}
    cand = {"a": 0.9, "b": 0.9}  # only 2 paired < min_paired 8
    rec = _pair_faithfulness(
        parent, cand, min_paired=8, min_delta=0.03,
        judge_model="llm.verify.slm_8b", n_labels=1,
    )
    assert rec["degenerate"] is True
    assert rec["faithfulness_delta"] is None
    assert rec["candidate_faithfulness_mean"] is None
    assert rec["promotable"] is False
    assert rec["correctness_vs_reference"]["status"] == "insufficient_sample"
    assert rec["correctness_vs_reference"]["brier"] is None


def test_pair_faithfulness_positive_delta_is_promotable():
    parent = {f"f{i}": 0.40 for i in range(10)}
    cand = {f"f{i}": 0.60 for i in range(10)}
    rec = _pair_faithfulness(
        parent, cand, min_paired=8, min_delta=0.03, judge_model="j", n_labels=1,
    )
    assert rec["degenerate"] is False
    assert rec["n_paired"] == 10
    assert rec["faithfulness_delta"] == pytest.approx(0.20)
    assert rec["candidate_faithfulness_mean"] == pytest.approx(0.60)
    assert rec["promotable"] is True
    # A positive FAITHFULNESS delta never fakes a correctness Brier.
    assert rec["correctness_vs_reference"]["brier"] is None


def test_pair_faithfulness_delta_below_margin_not_promotable():
    parent = {f"f{i}": 0.50 for i in range(10)}
    cand = {f"f{i}": 0.51 for i in range(10)}  # +0.01 < 0.03
    rec = _pair_faithfulness(
        parent, cand, min_paired=8, min_delta=0.03, judge_model="j", n_labels=1,
    )
    assert rec["degenerate"] is False
    assert rec["faithfulness_delta"] == pytest.approx(0.01)
    assert rec["promotable"] is False


def test_honest_null_eval_shape():
    rec = _honest_null_eval(
        min_paired=8, min_delta=0.03, judge_model="llm.verify.slm_8b",
        judge_available=False, degenerate_reason="faithfulness_judge_unavailable",
        n_labels=1,
    )
    assert rec["faithfulness_delta"] is None
    assert rec["candidate_faithfulness_mean"] is None
    assert rec["degenerate"] is True
    assert rec["promotable"] is False
    assert rec["fitness_metric"] == "faithfulness"


def test_optimizer_workflow_input_roundtrips_without_new_fields():
    """A pre-P4-T6 serialized input (no fitness_metric etc.) still deserializes —
    workflow.py rehydrates via ``OptimizerWorkflowInput(**wf_input)`` — and the
    MEASURE stage stays OFF (default critique_proxy), so the frozen monolith's
    result is byte-unchanged."""
    legacy = {
        "analyst_id": "country_assessor",
        "analyst_version": "v0",
        "parent_prompt_module_path": "legba.prompts.country_assessor.v1",
    }
    wf = OptimizerWorkflowInput(**legacy)
    assert wf.fitness_metric == "critique_proxy"
    assert wf.min_paired == 8
    assert wf.min_promote_delta == pytest.approx(0.03)
    assert wf.optimizer_analyst_id == ""
    d = dataclasses.asdict(wf)
    assert d["fitness_metric"] == "critique_proxy"
    assert "min_paired" in d
