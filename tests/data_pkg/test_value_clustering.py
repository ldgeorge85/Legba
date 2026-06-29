# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Holes-B Wave 3 — the shared fuzzy value clusterer.

Pure (no-DB) tests of the canon + tight normalized-Levenshtein "same-claim"
grouping that BOTH the contested-claims arbiter and the Holes-A noisy-OR leg
import. The adversarial requirement: typo / spacing / demonym / alias variants
MERGE, but two genuinely different claims (North Korea vs South Korea) do NOT.
"""

from __future__ import annotations

from legba.data.provenance.value_clustering import (
    FUZZY_MERGE_MAX_DISTANCE,
    canonical_value_key,
    cluster_values,
)


def _cluster_of(values: list[str], target: str) -> list[str]:
    """Return the member values of the cluster that ``target`` lands in."""
    clusters = cluster_values(values)
    ti = values.index(target)
    for c in clusters:
        if ti in c.members:
            return [values[i] for i in c.members]
    raise AssertionError("target not clustered")


# ===========================================================================
# canonical_value_key — demonym/alias/spelling-variant folding
# ===========================================================================


def test_canon_collapses_demonym_and_alias():
    # National demonym -> country (shared canon).
    assert canonical_value_key("Russian") == canonical_value_key("Russia")
    # Country alias -> canonical (shared canon).
    assert canonical_value_key("USA") == canonical_value_key("United States")
    assert canonical_value_key("U.S.") == canonical_value_key("United States")


def test_canon_folds_city_spelling_variants():
    # Local value-alias map (NOT the shared write-path canon).
    assert canonical_value_key("Kyiv") == canonical_value_key("Kiev")
    assert canonical_value_key("Peking") == canonical_value_key("Beijing")


def test_canon_empty_value_is_empty_key():
    assert canonical_value_key("") == ""
    assert canonical_value_key("   ") == ""


# ===========================================================================
# cluster_values — MERGE the same claim
# ===========================================================================


def test_merges_kyiv_kiev():
    members = _cluster_of(["Kyiv", "Kiev"], "Kyiv")
    assert set(members) == {"Kyiv", "Kiev"}


def test_merges_russian_russia():
    members = _cluster_of(["Russian", "Russia"], "Russia")
    assert set(members) == {"Russian", "Russia"}


def test_merges_spacing_and_hyphen_variants():
    # "de-escalating" / "de escalating" (spacing) — normlev 0.077, under 0.12.
    assert set(_cluster_of(["de-escalating", "de escalating"], "de-escalating")) == {
        "de-escalating",
        "de escalating",
    }
    # "ceasefire" / "cease-fire" (hyphen) — normlev 0.10, under 0.12.
    assert set(_cluster_of(["ceasefire", "cease-fire"], "ceasefire")) == {
        "ceasefire",
        "cease-fire",
    }


# ===========================================================================
# cluster_values — DO NOT merge genuinely different claims (the safety floor)
# ===========================================================================


def test_does_not_merge_north_south_korea():
    clusters = cluster_values(["North Korea", "South Korea"])
    assert len(clusters) == 2, "North/South Korea must stay distinct claims"


def test_does_not_merge_east_west_germany():
    clusters = cluster_values(["East Germany", "West Germany"])
    assert len(clusters) == 2


def test_threshold_below_closest_nonmerge_pair():
    """The constant must sit below the tightest real false-merge pair
    (North/South Korea ~0.182) and above the typo band (~0.077–0.11)."""
    from legba.data.filters.dedupe import _normalized_levenshtein

    nk = canonical_value_key("North Korea")
    sk = canonical_value_key("South Korea")
    assert _normalized_levenshtein(nk, sk) > FUZZY_MERGE_MAX_DISTANCE
    de1 = canonical_value_key("de-escalating")
    de2 = canonical_value_key("de escalating")
    assert _normalized_levenshtein(de1, de2) <= FUZZY_MERGE_MAX_DISTANCE


def test_mixed_list_yields_expected_distinct_clusters():
    vals = [
        "Russian", "Russia",          # -> 1 cluster
        "Kyiv", "Kiev",               # -> 1 cluster
        "North Korea", "South Korea", # -> 2 clusters
    ]
    clusters = cluster_values(vals)
    assert len(clusters) == 4


def test_clustering_is_order_stable_and_deterministic():
    a = cluster_values(["Russia", "Russian", "North Korea"])
    b = cluster_values(["Russia", "Russian", "North Korea"])
    assert [c.key for c in a] == [c.key for c in b]
    assert [sorted(c.members) for c in a] == [sorted(c.members) for c in b]


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
