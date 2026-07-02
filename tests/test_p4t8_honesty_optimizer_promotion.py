# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P4-T8 honesty contract #3 — a candidate promotes ONLY on a positive,
non-degenerate, judge-scored, sufficiently-paired MEASURED delta.

This suite pins the LIVE promotion gate, ``gepa._delta_gates_ok`` — the function
that stamps ``data.eval.promotable`` at candidate write time
(``optimizer.run_method``) and that ``optimizer.resolve_promoted_system_prompt``
(the wired inference path) consults before admitting an operator-promoted prompt.
Promotion is human-gated end to end; there is NO auto-promotion path (the former
``should_auto_promote`` ``auto_with_threshold`` helper had ZERO production call
sites and was removed — so the honesty suite tests the gate that ACTUALLY runs,
not dead code).

``_delta_gates_ok`` must return ``(False, ...)`` — i.e. NOT promotable — whenever
the eval delta is:

  * ABSENT — a ``None`` faithfulness mean (honest-null, never faked to 0.0), OR
  * DEGENERATE — ``eval_degenerate=True`` (under-sampled / judge-unavailable), OR
  * NON-FINITE — a raw NaN/inf score (a comparison that lies), OR
  * JUDGE-UNAVAILABLE — the before/after didn't share the LLM-judge yardstick, OR
  * UNDER-PAIRED — ``n_paired < min_paired``, OR
  * SUB-MARGIN — the delta is below ``min_delta``.

Only a positive, finite, judge-scored, sufficiently-paired, non-degenerate delta
is promotable. Pure unit tests (no DB, no LLM — the gate is pure arithmetic).

Selectable via ``pytest -k p4t8_honesty``.
"""
from __future__ import annotations

import math

from legba.runtime.dapr_workflow.gepa import _delta_gates_ok

# The production defaults the unit_optimizer eval carries.
_MIN_PAIRED = 8
_MIN_DELTA = 0.03


def _gate(cand, parent, **kw):
    """Call the LIVE gate with the promotable-stamp defaults, overridable."""
    return _delta_gates_ok(
        cand,
        parent,
        eval_degenerate=kw.get("eval_degenerate", False),
        judge_available=kw.get("judge_available", True),
        n_paired=kw.get("n_paired", 12),
        min_paired=kw.get("min_paired", _MIN_PAIRED),
        min_delta=kw.get("min_delta", _MIN_DELTA),
    )


# ---------------------------------------------------------------------------
# NON-POSITIVE / SUB-MARGIN delta — cannot promote
# ---------------------------------------------------------------------------


def test_no_promote_on_nonpositive_delta():
    """candidate == parent and candidate < parent both fail the margin gate — a
    non-improving candidate is never promotable."""
    for cand, parent in ((0.5, 0.5), (0.4, 0.5)):
        ok, reason = _gate(cand, parent)
        assert ok is False
        assert reason == "delta_below_margin"


def test_no_promote_on_sub_margin_positive_delta():
    """A POSITIVE but tiny delta (+0.01 < the 0.03 margin) is not promotable."""
    ok, reason = _gate(0.51, 0.50)
    assert ok is False
    assert reason == "delta_below_margin"


# ---------------------------------------------------------------------------
# ABSENT / DEGENERATE delta — the honest-null cannot promote
# ---------------------------------------------------------------------------


def test_no_promote_on_absent_score():
    """An honest-null measured delta (a ``None`` faithfulness mean, never faked
    to 0.0) is rejected — for candidate None AND parent None."""
    for cand, parent in ((None, 0.5), (0.5, None), (None, None)):
        ok, reason = _gate(cand, parent)
        assert ok is False
        assert reason == "degenerate_or_absent_delta"


def test_no_promote_on_degenerate_eval_even_with_scores():
    """``eval_degenerate=True`` rejects regardless of any score carried — even a
    large positive one, or a NaN."""
    for cand in (0.9, float("nan")):
        ok, reason = _gate(cand, 0.5, eval_degenerate=True)
        assert ok is False
        assert reason == "degenerate_or_absent_delta"


# ---------------------------------------------------------------------------
# NON-FINITE score — the isfinite guard on the LIVE gate (review H2/C3)
# ---------------------------------------------------------------------------


def test_no_promote_on_raw_nonfinite_score():
    """A NaN/inf score must NEVER stamp promotable. NaN comparisons are all
    False, so WITHOUT the ``math.isfinite`` guard a NaN candidate would slip the
    margin check and read as promotable. The guard lives on the LIVE gate
    ``_delta_gates_ok`` (the C3 fix had landed only on the dead
    ``should_auto_promote``)."""
    for cand in (float("nan"), float("inf")):
        ok, reason = _gate(cand, 0.5, eval_degenerate=False)
        assert ok is False, "a non-finite score was stamped promotable"
        assert reason == "non_finite_score"
    ok, reason = _gate(0.9, float("-inf"), eval_degenerate=False)
    assert ok is False
    assert reason == "non_finite_score"
    # Sanity: prove NaN is genuinely non-finite (guard against a false pass).
    assert not math.isfinite(float("nan"))


# ---------------------------------------------------------------------------
# JUDGE-UNAVAILABLE / UNDER-PAIRED — necessary conditions
# ---------------------------------------------------------------------------


def test_no_promote_when_judge_unavailable():
    """A POSITIVE delta but ``judge_available=False`` → not promotable (the
    before/after must share the SAME LLM-judge yardstick)."""
    ok, reason = _gate(0.9, 0.5, judge_available=False)
    assert ok is False
    assert reason == "faithfulness_judge_unavailable"


def test_no_promote_on_insufficient_paired_sample():
    ok, reason = _gate(0.9, 0.5, n_paired=3, min_paired=8)
    assert ok is False
    assert reason == "insufficient_paired_sample:3<8"


# ---------------------------------------------------------------------------
# The SINGLE positive path
# ---------------------------------------------------------------------------


def test_promote_only_on_positive_finite_judged_paired_delta():
    """A strictly-improving, finite, judge-scored, sufficiently-paired delta is
    the ONLY promotable case."""
    ok, reason = _gate(0.70, 0.50)
    assert ok is True
    assert reason == "delta_ok"
