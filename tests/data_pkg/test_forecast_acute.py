# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R1-T1.3 (#92) — the pre-registered acute-binary forecast pilot.

Pure-logic coverage (no DB) for the pieces that earn the word "forecast":

  * the Poisson tail that turns an expected event count into P(>=1 event);
  * the strictly-forward weekly window (no look-ahead leakage at issue time);
  * the SEGREGATED Brier + Brier-skill-score the calibration handler reports in
    its OWN keys (never pooled into the headline calibration Brier);
  * the contract that the pilot's resolution label is EXOGENOUS (so the world,
    not the system, grades it).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from legba.data.analysts.deterministic_handlers import forecast_acute as fa
from legba.data.analysts.deterministic_handlers import calibration_tracking as cal


# ---------------------------------------------------------------------------
# Poisson tail
# ---------------------------------------------------------------------------


def test_poisson_tail_p_basic():
    assert fa.poisson_tail_p(0.0) == 0.0
    assert fa.poisson_tail_p(-5.0) == 0.0           # clamps negatives
    assert math.isclose(fa.poisson_tail_p(1.0), 1 - math.exp(-1), rel_tol=1e-9)
    assert 0.99 < fa.poisson_tail_p(10.0) <= 1.0    # saturates, never exceeds 1
    assert fa.poisson_tail_p(0.5) < fa.poisson_tail_p(2.0)  # monotone in lambda


# ---------------------------------------------------------------------------
# Forward window (no leakage)
# ---------------------------------------------------------------------------


def test_next_window_is_strictly_forward_monday():
    wed = datetime(2026, 6, 24, 15, 0, tzinfo=timezone.utc)  # a Wednesday
    start, end = fa._next_window(wed)
    assert start.weekday() == 0          # Monday
    assert start > wed                   # entirely in the future → no look-ahead
    assert start.hour == 0 and start.minute == 0 and start.second == 0
    assert (end - start) == timedelta(days=7)


def test_next_window_rolls_a_monday_forward():
    mon = datetime(2026, 6, 22, 0, 0, tzinfo=timezone.utc)  # a Monday
    start, _ = fa._next_window(mon)
    assert start == mon + timedelta(days=7)  # strictly the NEXT Monday


# ---------------------------------------------------------------------------
# Segregated pilot metrics + Brier skill score
# ---------------------------------------------------------------------------


def _calls(n: int, *, p_when_event: float, p_when_none: float, p_base: float):
    """Alternating event/no-event calls with a fixed climatology base rate."""
    rows = []
    for i in range(n):
        o = i % 2
        rows.append({
            "claimed_confidence": p_when_event if o else p_when_none,
            "p_base": p_base,
            "outcome": o,
        })
    return rows


def test_metrics_empty():
    m = cal._forecast_acute_metrics([], min_sample=30)
    assert m["brier_forecast_acute"] is None
    assert m["forecast_acute_sample_size"] == 0
    assert m["forecast_acute_ready"] is False


def test_metrics_beats_climatology_positive_bss():
    # A model that tracks outcomes (0.9 on events, 0.1 on non-events) crushes a
    # flat 0.5 climatology → BSS > 0 and the at-sample headline is populated.
    rows = _calls(40, p_when_event=0.9, p_when_none=0.1, p_base=0.5)
    m = cal._forecast_acute_metrics(rows, min_sample=30)
    assert m["forecast_acute_ready"] is True
    assert m["brier_forecast_acute"] is not None
    assert m["brier_climatology"] is not None
    assert m["brier_skill_score"] > 0.0


def test_metrics_accumulating_below_sample():
    rows = _calls(10, p_when_event=0.9, p_when_none=0.1, p_base=0.5)
    m = cal._forecast_acute_metrics(rows, min_sample=30)
    assert m["forecast_acute_ready"] is False
    # Headline withheld below sample; raw value still available as a diagnostic.
    assert m["brier_forecast_acute"] is None
    assert m["brier_forecast_acute_raw"] is not None
    assert "accumulating" in m["forecast_acute_status"]


def test_metrics_degenerate_geography_dominated():
    # Calls that are only ever 0/1 certainties (a seismic country certain, a
    # non-seismic one impossible) are NOT probabilistic forecasting — the guard
    # must flag degeneracy and withhold the skill claim even though BSS could be
    # large (climatology-vs-geography is trivial skill, not forecasting).
    rows = []
    for i in range(40):
        o = 1 if i < 8 else 0           # base rate 0.2 — non-degenerate sample
        rows.append({"claimed_confidence": float(o), "p_base": 0.2, "outcome": o})
    m = cal._forecast_acute_metrics(rows, min_sample=30)
    assert m["forecast_acute_degenerate"] is True
    assert m["forecast_acute_probabilistic_share"] < 0.2
    # Even though the model nails every call (BSS would be high), the headline
    # finding must NOT carry the earned-forecast tag.
    finding = cal._build_finding(
        brier=None, sample_size=0, reliability_bins=[], per_analyst={},
        rolling=[], drift_z=None, drift_threshold=2.0, resolution_sources={},
        self_consistency_only=False, brier_exogenous=None,
        brier_self_consistency=None, brier_pooled=None, exogenous_sample_size=0,
        self_consistency_fraction=0.0, insufficient_exogenous=True,
        forecast_acute=m, warnings=[], target_id=None,
    )
    assert "forecast_skill_positive" not in finding.tags
    assert "forecast_pilot_degenerate" in finding.tags


def test_metrics_genuine_skill_sets_earned_tag():
    # A genuinely probabilistic, at-sample, climatology-beating pilot DOES earn
    # the tag — the guard is not a blanket suppressor.
    rows = [
        {"claimed_confidence": 0.7 if i % 2 else 0.3, "p_base": 0.5, "outcome": i % 2}
        for i in range(40)
    ]
    m = cal._forecast_acute_metrics(rows, min_sample=30)
    assert m["forecast_acute_degenerate"] is False
    assert m["brier_skill_score"] > 0.0
    finding = cal._build_finding(
        brier=None, sample_size=0, reliability_bins=[], per_analyst={},
        rolling=[], drift_z=None, drift_threshold=2.0, resolution_sources={},
        self_consistency_only=False, brier_exogenous=None,
        brier_self_consistency=None, brier_pooled=None, exogenous_sample_size=0,
        self_consistency_fraction=0.0, insufficient_exogenous=True,
        forecast_acute=m, warnings=[], target_id=None,
    )
    assert "forecast_skill_positive" in finding.tags


def test_metrics_bss_none_when_climatology_perfect():
    # All outcomes 0 and p_base 0 → climatology Brier is 0 → BSS undefined (None),
    # never a divide-by-zero.
    rows = [{"claimed_confidence": 0.0, "p_base": 0.0, "outcome": 0} for _ in range(30)]
    m = cal._forecast_acute_metrics(rows, min_sample=30)
    assert m["brier_skill_score"] is None


# ---------------------------------------------------------------------------
# Exogeneity contract — the pilot must count as world-graded, not self-graded
# ---------------------------------------------------------------------------


def test_resolved_by_label_is_exogenous():
    # The pilot's resolution label must be treated as EXOGENOUS by the calibration
    # honesty split (else its outcomes could never count as real calibration data)
    # AND must NOT collide with the legacy CI-coverage label.
    assert cal._is_exogenous({"resolved_by": fa.RESOLVED_BY}) is True
    assert fa.RESOLVED_BY not in cal._SELF_CONSISTENCY_SOURCES
    assert fa.RESOLVED_BY != cal._FORECAST_RESOLVER_SOURCE


# ---------------------------------------------------------------------------
# D9 — forecast honesty: epsilon-clamp, climatology-shrink, degeneracy abstain
# ---------------------------------------------------------------------------


def test_clamp_p_never_asserts_0_or_1():
    # A saturated tail (λ huge → p≈1) is pulled back to 1-eps; a dead one (λ=0 →
    # p=0) up to eps. The forecast keeps a hedge in BOTH directions.
    assert fa.clamp_p(1.0) == 1.0 - fa.P_EPSILON
    assert fa.clamp_p(0.0) == fa.P_EPSILON
    assert fa.clamp_p(fa.poisson_tail_p(50.0)) == 1.0 - fa.P_EPSILON
    # An interior probability is untouched.
    assert abs(fa.clamp_p(0.5) - 0.5) < 1e-12
    # Custom epsilon honored.
    assert fa.clamp_p(1.0, epsilon=0.05) == 0.95
    assert fa.clamp_p(0.0, epsilon=0.05) == 0.05


def test_derate_lambda_shrinks_toward_climatology():
    # A high recent λ (saturating) blended 50/50 with a low climatology λ lands
    # halfway — so the issued p is pulled OFF saturation.
    lam = fa.derate_lambda(10.0, 0.0, w=0.5)
    assert abs(lam - 5.0) < 1e-12
    # w=0 ⇒ pure recent; w=1 ⇒ pure climatology.
    assert fa.derate_lambda(10.0, 2.0, w=0.0) == 10.0
    assert fa.derate_lambda(10.0, 2.0, w=1.0) == 2.0
    # The default weight is the documented 0.5.
    assert fa.CLIMATOLOGY_SHRINK_W == 0.5
    assert abs(fa.derate_lambda(8.0, 0.0) - 4.0) < 1e-12
    # Negative inputs floored; weight clamped into [0,1].
    assert fa.derate_lambda(-3.0, -1.0, w=0.5) == 0.0
    assert fa.derate_lambda(4.0, 0.0, w=2.0) == 0.0   # w clamped to 1 → all clim


def test_lambda_from_p_is_poisson_tail_inverse():
    # _lambda_from_p(poisson_tail_p(λ)) round-trips λ for a regular value.
    lam = 1.3
    p = fa.poisson_tail_p(lam)
    assert abs(fa._lambda_from_p(p) - lam) < 1e-9
    assert fa._lambda_from_p(0.0) == 0.0


def test_derate_lambda_desaturates_a_saturated_country():
    # A country whose recent λ saturates p to ~1.0, with a modest climatology
    # base rate, ends up with a NON-degenerate issued p after shrink + clamp.
    p_base = 0.3
    lam_recent = 12.0                       # poisson_tail_p ≈ 1.0 (saturated)
    assert fa.poisson_tail_p(lam_recent) > 0.99
    lam_clim = fa._lambda_from_p(p_base)
    lam_eff = fa.derate_lambda(lam_recent, lam_clim, w=0.5)
    p = fa.clamp_p(fa.poisson_tail_p(lam_eff))
    # No longer a 1.0 certainty.
    assert p < 1.0 - fa.P_EPSILON + 1e-12
    assert p <= 1.0 - fa.P_EPSILON
    assert p > fa.P_EPSILON


def test_p_vector_degenerate_abstain():
    # An all-{0,1} certainty vector (geography-dominated) is degenerate → abstain.
    deg = [0.0, 1.0, 0.0, 1.0, 0.0]
    assert fa.p_vector_is_degenerate(deg) is True
    # Near-{0,1} after clamping is STILL degenerate (no genuine uncertainty).
    near = [fa.P_EPSILON, 1.0 - fa.P_EPSILON] * 5
    assert fa.p_vector_is_degenerate(near) is True
    # An empty batch abstains (nothing to issue).
    assert fa.p_vector_is_degenerate([]) is True


def test_p_vector_non_degenerate_issues():
    # A vector with enough genuinely-uncertain calls is NOT degenerate → issue.
    mixed = [0.0, 1.0, 0.4, 0.55, 0.6]   # 3/5 = 60% uncertain ≥ 20% threshold
    assert fa.p_vector_is_degenerate(mixed) is False
    # Exactly at the threshold share is NOT degenerate (>= passes).
    at_thresh = [0.5, 0.0, 0.0, 0.0, 0.0]   # 1/5 = 20% uncertain
    assert fa.p_vector_is_degenerate(at_thresh) is False
    # A stricter required share flips the same vector to degenerate.
    assert fa.p_vector_is_degenerate(at_thresh, min_uncertain_share=0.5) is True


def test_class_k_definition_frozen():
    # The forecaster does not get to invent its outcome vocabulary; the class is
    # the upstream-severity-stamped hazard catalogs.
    assert fa.EVENT_CLASS == "hazard_severe"
    # GLOBAL catalogs only. NWS active-alerts is deliberately EXCLUDED — it is
    # US-only (breaks cross-country comparability) and its volume trivialises the
    # weekly call (the first live seeding saturated US p to 1.0). See
    # forecast_acute.HAZARD_SEVERE_SOURCES.
    assert "source.usgs.earthquakes_m45" in fa.HAZARD_SEVERE_SOURCES
    assert "source.nasa.eonet_events" in fa.HAZARD_SEVERE_SOURCES
    assert "source.nws.active_alerts" not in fa.HAZARD_SEVERE_SOURCES
