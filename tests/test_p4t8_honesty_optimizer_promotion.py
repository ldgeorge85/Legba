# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P4-T8 honesty contract #3 — auto-promote requires a positive, non-degenerate
eval delta.

``optimizer.should_auto_promote`` (L455-543) must return ``(False, ...)`` whenever
the eval delta is:

  * ABSENT — a ``None`` faithfulness mean (honest-null, never faked to 0.0), OR
  * DEGENERATE — ``eval_degenerate=True`` (an under-sampled / judge-unavailable
    measured eval), OR
  * NON-POSITIVE — ``candidate_score <= parent_score``,

even with a FULLY-eligible promotion history. Only a positive, finite,
strictly-improving, non-degenerate delta on the ``auto_with_threshold`` policy
with enough prior manual promotions may promote. The tests FAIL if an
auto-promote fires without that.

These pin the P4-T6-hardened gate (the measurement gates run BEFORE any policy
branch). Pure unit tests (no live DB — a fake conn stands in for the
prior-promotions count).

Selectable via ``pytest -k p4t8_honesty``.
"""
from __future__ import annotations

import asyncio
import math

import pytest

from legba.data.analysts.optimizer import should_auto_promote


class _FakeConn:
    """Stands in for the prior-promotions COUNT query — ``fetchrow`` returns
    ``{"n": <priors>}``."""

    def __init__(self, row):
        self._row = row

    async def fetchrow(self, query, *args):  # noqa: ANN001
        return self._row


def _run(coro):
    # Fresh loop per call — robust when prior tests created/closed loops.
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# NON-POSITIVE delta — cannot promote even with a full history
# ---------------------------------------------------------------------------


def test_no_auto_promote_on_nonpositive_delta_even_with_full_history():
    """candidate == parent (the zero-delta / no-training-data path) and
    candidate < parent both reject with ``score_did_not_improve`` — a fully
    eligible 99-prior history cannot promote a non-improving candidate."""
    for cand, parent in ((0.5, 0.5), (0.4, 0.5)):
        ok, reason = _run(should_auto_promote(
            _FakeConn({"n": 99}),
            analyzed_analyst_id="leadership_transition",
            candidate_score=cand,
            parent_score=parent,
            promotion_policy="auto_with_threshold",
        ))
        assert ok is False
        assert reason == "score_did_not_improve"


# ---------------------------------------------------------------------------
# POLICY gates — a positive delta is necessary but not sufficient
# ---------------------------------------------------------------------------


def test_no_auto_promote_when_human_gated():
    ok, reason = _run(should_auto_promote(
        _FakeConn({"n": 99}),
        analyzed_analyst_id="leadership_transition",
        candidate_score=0.7,
        parent_score=0.5,
        promotion_policy="human_gated",
    ))
    assert ok is False
    assert reason == "human_gated"


def test_no_auto_promote_on_unknown_policy():
    ok, reason = _run(should_auto_promote(
        _FakeConn({"n": 99}),
        analyzed_analyst_id="leadership_transition",
        candidate_score=0.7,
        parent_score=0.5,
        promotion_policy="whatever",
    ))
    assert ok is False
    assert reason == "unknown_policy:whatever"


def test_no_auto_promote_without_sufficient_history():
    ok, reason = _run(should_auto_promote(
        _FakeConn({"n": 4}),
        analyzed_analyst_id="leadership_transition",
        candidate_score=0.7,
        parent_score=0.5,
        promotion_policy="auto_with_threshold",
    ))
    assert ok is False
    assert reason.startswith("insufficient_history")


def test_auto_promote_only_on_positive_delta_and_history():
    """The SINGLE positive path: a strictly-improving finite delta, the
    ``auto_with_threshold`` policy, and >= 5 prior manual promotions."""
    ok, reason = _run(should_auto_promote(
        _FakeConn({"n": 5}),
        analyzed_analyst_id="leadership_transition",
        candidate_score=0.7,
        parent_score=0.5,
        promotion_policy="auto_with_threshold",
    ))
    assert ok is True
    assert reason == "auto_promoted_after_5_priors"


# ---------------------------------------------------------------------------
# ABSENT / DEGENERATE delta — the honest-null cannot promote
# ---------------------------------------------------------------------------


def test_no_auto_promote_on_absent_score():
    """An honest-null measured delta (a ``None`` faithfulness mean, never faked
    to 0.0) is rejected BEFORE the policy branch — for candidate None AND parent
    None — even with a full history."""
    for cand, parent in ((None, 0.5), (0.5, None), (None, None)):
        ok, reason = _run(should_auto_promote(
            _FakeConn({"n": 99}),
            analyzed_analyst_id="leadership_transition",
            candidate_score=cand,
            parent_score=parent,
            promotion_policy="auto_with_threshold",
        ))
        assert ok is False
        assert reason == "degenerate_or_absent_delta"


def test_no_auto_promote_on_degenerate_eval_even_with_scores():
    """The real degeneracy signal — ``eval_degenerate=True`` (an under-sampled /
    judge-unavailable measured eval) — rejects regardless of any score value
    that happens to be carried, even a large positive one or a NaN."""
    for cand in (0.9, float("nan")):
        ok, reason = _run(should_auto_promote(
            _FakeConn({"n": 99}),
            analyzed_analyst_id="leadership_transition",
            candidate_score=cand,
            parent_score=0.5,
            promotion_policy="auto_with_threshold",
            eval_degenerate=True,
        ))
        assert ok is False
        assert reason == "degenerate_or_absent_delta"


def test_no_auto_promote_on_raw_nonfinite_score_defense_in_depth():
    """A NaN/inf score must NEVER auto-promote (adversarial-verify contract 4).

    Closed by the ``math.isfinite`` measurement gate in ``should_auto_promote``
    (a NaN comparison is always False, so without the guard a NaN candidate would
    slip the monotonicity floor and promote on a full history). Not
    producer-reachable — faithfulness is [0,1] — but the promotion gate must
    never rest on a comparison that lies."""
    for cand in (float("nan"), float("inf")):
        ok, reason = _run(should_auto_promote(
            _FakeConn({"n": 99}),
            analyzed_analyst_id="leadership_transition",
            candidate_score=cand,
            parent_score=0.5,
            promotion_policy="auto_with_threshold",
            eval_degenerate=False,
        ))
        assert ok is False, "a non-finite score auto-promoted"
        assert reason == "non_finite_score"
    # Sanity: prove NaN is genuinely non-finite here (guard against a false pass).
    assert not math.isfinite(float("nan"))
