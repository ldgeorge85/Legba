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

from legba.data.analysts.optimizer import resolve_promoted_system_prompt
import legba.runtime.dapr_workflow.gepa as gepa_mod
from legba.runtime.dapr_workflow.gepa import (
    OptimizerWorkflowInput,
    _candidate_faithfulness_for_finding,
    _delta_gates_ok,
    _honest_null_eval,
    _pair_faithfulness,
    _resolve_verify_component_id,
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
# (via a hand-edited promotion_gate='promoted') without a positive,
# non-degenerate, judge-scored, sufficiently-paired MEASURED faithfulness delta.
# These tests falsify a violation of that contract. The LIVE gate is
# ``_delta_gates_ok`` (there is no auto-promotion path); the honesty suite
# ``tests/test_p4t8_honesty_optimizer_promotion.py`` pins its full contract.
# ===========================================================================


def test_delta_gates_ok_rejects_nonfinite_score():
    """The LIVE gate rejects a raw NaN/inf so it can never stamp promotable — the
    ``math.isfinite`` guard the C3 fix had landed only on the dead
    ``should_auto_promote`` now lives on the gate that ACTUALLY runs."""
    for cand in (float("nan"), float("inf")):
        ok, reason = _delta_gates_ok(
            cand, 0.5, eval_degenerate=False, judge_available=True,
            n_paired=12, min_paired=8, min_delta=0.03,
        )
        assert ok is False
        assert reason == "non_finite_score"


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


# --- H2: a judge-error (floor-fallback) candidate row cannot inflate the delta --


class _FakeResp:
    def __init__(self, content):
        self.content = content


class _FakeSynth:
    """A candidate synthesizer whose generation is fixed — the guard under test
    is downstream (verify's judge_status), not generation quality."""

    async def chat_complete(self, messages, *, system=None, max_tokens=None,
                            temperature=None):
        return _FakeResp('{"title": "Cand", "body": "Iran raised output [1]."}')


class _FakeReport:
    """Minimal stand-in for verify.FaithfulnessReport."""

    def __init__(self, judge_status, score, reason=None):
        self.judge_status = judge_status
        self.faithfulness_score = score
        self.judge_unavailable_reason = reason


def _patch_candidate_arm(monkeypatch, report):
    """Wire the fakes _candidate_faithfulness_for_finding needs to reach (and
    return the result of) the verify judge — a fixed slice fetch + a fixed
    verify report — so the ONLY variable is the report's judge_status."""
    async def _fake_fetch(pool, signal_ids):
        return {
            sid: {
                "id": sid, "title": f"Sig {sid}", "source_url": "http://x",
                "produced_at": None, "data": {"summary": "raised output"},
            }
            for sid in signal_ids
        }

    async def _fake_verify(*, body, citations, judge_llm=None,
                           finding_confidence=None):
        return report

    monkeypatch.setattr(gepa_mod, "_fetch_signal_render_rows", _fake_fetch)
    monkeypatch.setattr(
        "legba.data.provenance.verify.verify_finding_faithfulness", _fake_verify
    )


def test_candidate_floor_fallback_row_is_dropped(monkeypatch):
    """H2 — when the candidate arm's judge ERRORED and verify soft-failed to the
    citation floor (``judge_status != 'llm'``, e.g. a 1.0 floor for a fully-cited
    body), the row is DROPPED (returns None → UNPAIRED). A judge error therefore
    cannot enter — let alone inflate — the paired delta."""
    _patch_candidate_arm(
        monkeypatch, _FakeReport("deterministic", 1.0, reason="judge_error")
    )
    row = {"finding_id": "f1", "data": None, "derived_from": ["sig-1"]}
    out = _run(_candidate_faithfulness_for_finding(
        row=row, candidate_text="SYS", synth=_FakeSynth(), judge=object(),
        pool=object(),
    ))
    assert out is None, "a floor-fallback (judge_error) row was NOT dropped"


def test_candidate_llm_judged_row_is_scored(monkeypatch):
    """The companion: a genuinely LLM-judged row (``judge_status == 'llm'``) IS
    scored and paired — the guard drops ONLY floor fallbacks."""
    _patch_candidate_arm(monkeypatch, _FakeReport("llm", 0.8))
    row = {"finding_id": "f1", "data": None, "derived_from": ["sig-1"]}
    out = _run(_candidate_faithfulness_for_finding(
        row=row, candidate_text="SYS", synth=_FakeSynth(), judge=object(),
        pool=object(),
    ))
    assert out == pytest.approx(0.8)


def test_judge_error_row_cannot_inflate_computed_delta(monkeypatch):
    """End-to-end proof: with a judge-error row DROPPED (returns None), the fid is
    absent from ``candidate_scores`` and the pairing math computes the delta over
    ONLY the real judge-scored pairs — the inflated 1.0 floor never enters. We
    contrast against the counterfactual where the same 1.0 HAD leaked in."""
    # Parent: 10 real judge-scored rows at 0.5.
    parent = {f"f{i}": 0.50 for i in range(10)}
    parent_reg = {f"f{i}": "llm" for i in range(10)}
    # Candidate arm: rows f0..f8 judged at 0.50 (no improvement); f9's judge
    # ERRORED → dropped (absent), NOT carried as a 1.0 floor.
    candidate_dropped = {f"f{i}": 0.50 for i in range(9)}
    rec = _pair_faithfulness(
        parent, candidate_dropped, min_paired=8, min_delta=0.03,
        judge_model="j", n_labels=1, parent_regimes=parent_reg,
        candidate_regime="llm",
    )
    assert rec["degenerate"] is False
    assert rec["n_paired"] == 9
    assert rec["faithfulness_delta"] == pytest.approx(0.0)
    assert rec["promotable"] is False

    # Counterfactual (the OLD bug): the floored 1.0 leaks into f9 → a spurious
    # positive delta that WOULD have stamped promotable. This proves the drop is
    # what prevents the inflation.
    leaked = {**candidate_dropped, "f9": 1.0}
    rec_bug = _pair_faithfulness(
        parent, leaked, min_paired=8, min_delta=0.03, judge_model="j",
        n_labels=1, parent_regimes=parent_reg, candidate_regime="llm",
    )
    assert rec_bug["faithfulness_delta"] > 0.03
    assert rec_bug["promotable"] is True  # the defect the drop prevents


def test_pair_excludes_mixed_regime_parent(monkeypatch):
    """A parent row whose LIVE verify FLOORED (regime 'deterministic', e.g. a 1.0
    floor from a judge error) is EXCLUDED from a judge-scored candidate pair — a
    judge-scored candidate must never pair against a floor-scored parent (the
    mixed-judge-regime spurious-delta class)."""
    parent = {f"f{i}": 0.50 for i in range(9)}
    parent["fp"] = 1.0  # a FLOORED parent (judge errored on the live pass)
    parent_reg = {f"f{i}": "llm" for i in range(9)}
    parent_reg["fp"] = "deterministic"
    candidate = {f"f{i}": 0.60 for i in range(9)}
    candidate["fp"] = 0.60  # candidate judged fp fine, but parent fp is floored
    rec = _pair_faithfulness(
        parent, candidate, min_paired=8, min_delta=0.03, judge_model="j",
        n_labels=1, parent_regimes=parent_reg, candidate_regime="llm",
    )
    assert rec["n_paired"] == 9, "the floored parent row was NOT excluded"
    assert rec["n_mixed_regime_excluded"] == 1
    # The pair is measured over the 9 same-regime rows only: 0.60 vs 0.50.
    assert rec["parent_faithfulness_mean"] == pytest.approx(0.50)
    assert rec["candidate_faithfulness_mean"] == pytest.approx(0.60)


def test_pair_faithfulness_stamps_judge_regime_on_both_arms():
    """Every admitted pair names the judge regime on BOTH arms (all 'llm' after
    mixed-regime exclusion) — the eval record is honest about the yardstick."""
    parent = {f"f{i}": 0.40 for i in range(10)}
    cand = {f"f{i}": 0.60 for i in range(10)}
    reg = {f"f{i}": "llm" for i in range(10)}
    rec = _pair_faithfulness(
        parent, cand, min_paired=8, min_delta=0.03, judge_model="j", n_labels=1,
        parent_regimes=reg, candidate_regime="llm",
    )
    assert rec["parent_judge_regime"] == "llm"
    assert rec["candidate_judge_regime"] == "llm"
    assert rec["n_mixed_regime_excluded"] == 0


# --- eval records name the TRUE judge (not the retired hardcode) -----------------


def test_resolve_verify_component_id_names_true_judge(monkeypatch):
    """The eval record's ``judge_model`` is the analyzed unit's ACTUAL
    ``method.llm.verify`` component — since 1ed2187 the core reasoning model
    (``llm.primary.openai_compat``), NOT the retired hardcoded
    ``llm.verify.slm_8b``."""
    async def _fake_typed(aid):
        return {"method": {"llm": {"verify": {
            "factory_kind": "stack_ref", "raw": "llm.primary.openai_compat",
        }}}}

    monkeypatch.setattr(gepa_mod, "_get_descriptor_typed", _fake_typed)
    out = _run(_resolve_verify_component_id("leadership_transition"))
    assert out == "llm.primary.openai_compat"
    assert out != "llm.verify.slm_8b"


def test_resolve_verify_component_id_none_on_unresolved(monkeypatch):
    """Descriptor fetch failure → None (the caller records an honest
    ``unresolved_verify_component`` marker, never a wrong hardcode)."""
    async def _fake_typed(aid):
        return None

    monkeypatch.setattr(gepa_mod, "_get_descriptor_typed", _fake_typed)
    assert _run(_resolve_verify_component_id("x")) is None


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
