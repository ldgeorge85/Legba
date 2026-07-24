# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""E4 eval harness — pairwise + B-cubed scoring (:mod:`legba.data._entity_eval`).

Pure metrics (no DB, no LLM). These are the yardstick the entity_researcher's
adjudicator is measured against before its merges are trusted. The headline
scenario models the KNOWN DAMAGE: the correct de-fragmentation must score 1.0,
and the dangerous over-merge (fusing Ali Khamenei with his son Mojtaba) must be
PENALIZED on precision — that is the whole point of the metric.
"""

from __future__ import annotations

import math

import pytest

from legba.data._entity_eval import (
    bcubed,
    clusters_to_pairs,
    pairwise_prf,
)


def test_pairwise_perfect():
    gold = [("a", "b"), ("b", "c")]
    pred = [("a", "b"), ("c", "b")]  # order within a pair is irrelevant
    s = pairwise_prf(pred, gold)
    assert (s.precision, s.recall, s.f1) == (1.0, 1.0, 1.0)
    assert (s.tp, s.fp, s.fn) == (2, 0, 0)


def test_pairwise_false_positive_hurts_precision():
    gold = [("a", "b")]
    pred = [("a", "b"), ("a", "z")]  # 'a~z' is a wrong merge
    s = pairwise_prf(pred, gold)
    assert s.tp == 1 and s.fp == 1 and s.fn == 0
    assert s.precision == 0.5 and s.recall == 1.0


def test_pairwise_missed_merge_hurts_recall():
    gold = [("a", "b"), ("c", "d")]
    pred = [("a", "b")]
    s = pairwise_prf(pred, gold)
    assert s.tp == 1 and s.fp == 0 and s.fn == 1
    assert s.precision == 1.0 and s.recall == 0.5


def test_pairwise_self_pairs_and_dupes_ignored():
    s = pairwise_prf([("a", "a"), ("a", "b"), ("b", "a")], [("a", "b")])
    assert (s.tp, s.fp, s.fn) == (1, 0, 0)


def test_pairwise_empty_both_is_unit():
    s = pairwise_prf([], [])
    assert s.precision == 1.0 and s.recall == 1.0 and s.f1 == 1.0


def test_clusters_to_pairs():
    pairs = clusters_to_pairs({"a": 1, "b": 1, "c": 1, "d": 2})
    assert pairs == {frozenset({"a", "b"}), frozenset({"a", "c"}),
                     frozenset({"b", "c"})}  # cluster {a,b,c} -> 3 pairs, d singleton


def test_bcubed_identical_is_perfect():
    g = {"a": "x", "b": "x", "c": "y"}
    s = bcubed(dict(g), dict(g))
    assert math.isclose(s.f1, 1.0)
    assert s.n_elements == 3


def test_bcubed_over_merge_penalizes_precision():
    # gold: {a,b} together, c alone. pred fuses all three.
    gold = {"a": "1", "b": "1", "c": "2"}
    pred = {"a": "Z", "b": "Z", "c": "Z"}
    s = bcubed(pred, gold)
    # recall perfect (every gold-mate is together), precision < 1 (c dragged in)
    assert math.isclose(s.recall, 1.0)
    assert s.precision < 1.0


def test_bcubed_under_merge_penalizes_recall():
    gold = {"a": "1", "b": "1"}
    pred = {"a": "X", "b": "Y"}  # split the true pair
    s = bcubed(pred, gold)
    assert math.isclose(s.precision, 1.0)
    assert s.recall < 1.0


# ---------------------------------------------------------------------------
# The known-damage scenario — the metric must reward the correct fold and
# punish the father/son over-merge.
# ---------------------------------------------------------------------------

# GOLD truth over a slice of the real damage:
#  - Ali Khamenei has 3 surface variants (one cluster),
#  - Mojtaba Khamenei is a DISTINCT person (his own cluster),
#  - SNSC folds onto Supreme National Security Council (one cluster),
#  - Axis of Resistance stands alone.
_GOLD = {
    "ali_1": "ali", "ali_2": "ali", "ali_3": "ali",
    "mojtaba": "mojtaba",
    "snsc": "council", "council_full": "council",
    "axis": "axis",
}


def test_known_damage_correct_fold_scores_perfect():
    # E4 folds the 3 Ali surfaces + the 2 council surfaces, leaves Mojtaba/Axis.
    pred = {
        "ali_1": "A", "ali_2": "A", "ali_3": "A",
        "mojtaba": "M",
        "snsc": "C", "council_full": "C",
        "axis": "X",
    }
    s = bcubed(pred, _GOLD)
    assert math.isclose(s.f1, 1.0), s
    ps = pairwise_prf(clusters_to_pairs(pred), clusters_to_pairs(_GOLD))
    assert math.isclose(ps.f1, 1.0), ps


def test_known_damage_father_son_overmerge_is_penalized():
    # The DANGEROUS error: fuse Ali + his son Mojtaba into one cluster.
    bad = {
        "ali_1": "A", "ali_2": "A", "ali_3": "A", "mojtaba": "A",  # <-- wrong
        "snsc": "C", "council_full": "C",
        "axis": "X",
    }
    good = {
        "ali_1": "A", "ali_2": "A", "ali_3": "A", "mojtaba": "M",
        "snsc": "C", "council_full": "C", "axis": "X",
    }
    bad_b = bcubed(bad, _GOLD)
    good_b = bcubed(good, _GOLD)
    # The over-merge must score strictly worse on precision AND F1.
    assert bad_b.precision < good_b.precision
    assert bad_b.f1 < good_b.f1
    # And pairwise must show the one false-positive merge (mojtaba~each Ali = 3).
    ps = pairwise_prf(clusters_to_pairs(bad), clusters_to_pairs(_GOLD))
    assert ps.fp == 3 and ps.fn == 0
