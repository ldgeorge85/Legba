# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M-1 — the OPERATOR correctness axis, one shared definition.

Guards the arithmetic and the honesty rules that the 2026-08-02 engine review
found written in prose in ``labels_api`` and reimplemented inline in SQL: the
3-valued weighting, ``unresolvable`` excluded from BOTH sides, the tiny-n
display, and the standing "never pooled into calibration" segregation.
"""
from __future__ import annotations

import pytest

from legba.data import correctness_axis as ca


# ---------------------------------------------------------------------------
# The weighting
# ---------------------------------------------------------------------------


def test_weights_are_the_documented_three_and_unresolvable_has_none():
    assert ca.WEIGHTS == {
        "correct": 1.0,
        "partially_correct": 0.5,
        "incorrect": 0.0,
    }
    # `unresolvable` deliberately carries NO weight — that absence is what makes
    # it drop out of the denominator as well as the numerator.
    assert ca.LABEL_UNRESOLVABLE not in ca.WEIGHTS
    assert set(ca.VOCABULARY) == set(ca.WEIGHTS) | {ca.LABEL_UNRESOLVABLE}


def test_the_live_gold_set_scores_0_625():
    """The 8 real 2026-W30/W31 verdicts (read from the live table 2026-08-03)."""
    live = [
        "incorrect",           # economic_coercion / country_watch_ml
        "partially_correct",   # energy_security / country_g20_cn
        "correct",             # escalation / country_g20_ru
        "correct",             # internal_stability / country_watch_ua
        "partially_correct",   # leadership_transition / country_g20_tr
        "partially_correct",   # military_posture / country_g20_au
        "partially_correct",   # narrative_coordination / country_watch_cd
        "correct",             # narrative_coordination / country_watch_ml
    ]
    rec = ca.score(live, min_labels=ca.MIN_FLEET_LABELS)
    assert rec["correctness"] == pytest.approx(0.625)
    assert rec["n_scored"] == 8
    assert rec["n_labels"] == 8
    # n=8 is nowhere near the floor — the number is reported, never called
    # measured.
    assert rec["sufficient"] is False
    assert "below the 30 floor" in rec["status"]


def test_unresolvable_leaves_both_numerator_and_denominator():
    rec = ca.score(["correct", "unresolvable", "unresolvable"])
    # 1.0 over ONE scored verdict — the two unresolvables neither raise nor
    # lower it, and neither is silently dropped from the record.
    assert rec["correctness"] == 1.0
    assert rec["n_scored"] == 1
    assert rec["n_labels"] == 3
    assert rec["n_unresolvable"] == 2
    assert rec["mix"]["unresolvable"] == 2


def test_all_unresolvable_is_honest_null_never_zero():
    rec = ca.score(["unresolvable", "unresolvable"])
    assert rec["correctness"] is None
    assert rec["n_scored"] == 0
    assert rec["status"] == "all verdicts unresolvable — nothing scorable"


def test_empty_population_is_honest_null():
    rec = ca.score([])
    assert rec["correctness"] is None
    assert rec["n_labels"] == 0
    assert rec["status"] == "no operator verdicts"


def test_all_incorrect_is_a_real_zero_not_a_null():
    rec = ca.score(["incorrect", "incorrect"])
    assert rec["correctness"] == 0.0
    assert rec["n_scored"] == 2


def test_unknown_label_counts_but_never_scores():
    """A vocabulary outside the CHECK can only arrive if the constraint is
    loosened; it must not silently become a 0.0 (which would read as wrong)."""
    rec = ca.score(["correct", "banana"])
    assert rec["correctness"] == 1.0   # the banana did NOT drag it to 0.5
    assert rec["n_scored"] == 1
    assert rec["n_labels"] == 2
    assert rec["mix"]["banana"] == 1


# ---------------------------------------------------------------------------
# Tiny-n honesty
# ---------------------------------------------------------------------------


def test_below_floor_reports_the_number_and_names_the_n():
    rec = ca.score(["correct"], min_labels=10)
    assert rec["correctness"] == 1.0      # REPORTED, not hidden
    assert rec["sufficient"] is False
    assert "n=1 scored verdict," in rec["status"]
    assert "below the 10 floor" in rec["status"]


def test_at_floor_is_sufficient():
    rec = ca.score(["correct"] * 10, min_labels=10)
    assert rec["sufficient"] is True
    assert rec["status"] == "scored (n=10)"


def test_describe_never_emits_a_bare_ratio():
    line = ca.describe(ca.score(["correct", "partially_correct", "incorrect"]))
    assert "correctness 0.50" in line
    assert "n=3 scored" in line
    assert "1 correct" in line and "1 partial" in line and "1 incorrect" in line
    assert "below the" in line


def test_describe_of_an_unmeasured_record_says_so():
    assert ca.describe(ca.score([])) == (
        "correctness unmeasured — no operator verdicts"
    )


def test_describe_names_excluded_unresolvables():
    line = ca.describe(ca.score(["correct", "unresolvable"]))
    assert "1 unresolvable (excluded)" in line


# ---------------------------------------------------------------------------
# Grouping / the fleet aggregate
# ---------------------------------------------------------------------------


def _rows(pairs):
    return [{"unit_analyst_id": u, "label": lab} for u, lab in pairs]


def test_fleet_pools_verdicts_not_unit_means():
    """A mean of per-unit means would let a single verdict outweigh a
    fully-labelled unit — with n=1 on most units that is exactly backwards."""
    rows = _rows(
        [("a", "incorrect")]
        + [("b", "correct")] * 9
    )
    by_unit, fleet = ca.score_by_unit(rows)
    assert by_unit["a"]["correctness"] == 0.0
    assert by_unit["b"]["correctness"] == 1.0
    # Mean of means would be 0.5; pooling the ten verdicts gives 0.9.
    assert fleet["correctness"] == pytest.approx(0.9)
    assert fleet["n_scored"] == 10


def test_fleet_carries_the_higher_floor():
    by_unit, fleet = ca.score_by_unit(_rows([("a", "correct")] * 12))
    assert by_unit["a"]["min_labels"] == ca.MIN_UNIT_LABELS
    assert by_unit["a"]["sufficient"] is True          # 12 >= 10
    assert fleet["min_labels"] == ca.MIN_FLEET_LABELS
    assert fleet["sufficient"] is False               # 12 < 30


def test_units_are_sorted_so_the_payload_is_stable():
    by_unit, _ = ca.score_by_unit(_rows([("z", "correct"), ("a", "correct")]))
    assert list(by_unit) == ["a", "z"]


# ---------------------------------------------------------------------------
# The public key projection + the never-pooled boundary
# ---------------------------------------------------------------------------


def test_as_payload_uses_the_segregated_key_names():
    payload = ca.as_payload(ca.score(["correct", "incorrect"]))
    assert payload["correctness_operator"] == 0.5
    assert payload["n_operator_labels"] == 2
    assert payload["n_operator_scored"] == 2
    assert payload["operator_sufficient"] is False
    assert payload["operator_mix"]["correct"] == 1
    # It must NOT collide with either the faithfulness keys or the deterministic
    # source-overlap axis' keys — that separation IS the never-pool rule.
    assert "faithfulness" not in payload
    assert "correctness_vs_reference" not in payload


def test_assert_not_pooled_catches_a_leak():
    with pytest.raises(AssertionError, match="never be pooled"):
        ca.assert_not_pooled(
            {"brier": 0.2, "correctness_operator": 0.6}, what="the Brier plane"
        )


def test_assert_not_pooled_passes_a_clean_aggregate():
    ca.assert_not_pooled(
        {"brier": 0.2, "faithfulness": 0.9}, what="the Brier plane"
    )


def test_every_axis_key_is_actually_emitted_by_as_payload():
    """AXIS_KEYS is the leak detector's whitelist — if a key is added to the
    payload without being added there, the detector stops detecting it."""
    payload = ca.as_payload(ca.score(["correct"]))
    for key in ca.AXIS_KEYS:
        assert key in payload, f"{key} is in AXIS_KEYS but not in as_payload()"
