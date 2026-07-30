# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-4 equivalence proof for the ONE ordinal-map builder.

``verify.py`` carried SIX hand-rolled copies of the same per-ordinal traversal
(five ordinal MAP builders + the periphery ordinal SET). They were collapsed onto
:func:`legba.data.provenance.verify._build_ordinal_map`.

This module keeps the PRE-refactor bodies verbatim as REFERENCE implementations
(``_ref_*`` below — copied byte-for-byte from the 068d515 parent of the collapse)
and asserts, over a fixture matrix, that the shipped builder returns a result
EQUAL to its reference for every input. The reference copies are the proof
artifact: if a future edit changes a shipped builder's semantics, this suite
fails against the frozen original rather than silently re-baselining.

Fixture matrix deliberately covers the sharp edges the collapse could have
blunted:
  * full-width 【N】 citation-marker variants (the core-plane bracket defect)
  * MISSING ordinals (no ``ordinal`` field, no digit in the marker, legacy
    ``[[ref:<uuid>]]``) — must be skipped, never fabricated
  * DUPLICATE ordinals where the LATER entry is SKIPPED by the projector — the
    earlier value must SURVIVE (the ``continue`` semantics, not a ``None`` write)
  * non-list ``citations``, non-Mapping entries, bool/str/float ordinals
"""

from __future__ import annotations

import itertools
from typing import Any, Mapping

import pytest

from legba.data.provenance import verify as V


# ---------------------------------------------------------------------------
# REFERENCE implementations — the verbatim PRE-refactor bodies (068d515).
# Do NOT "clean these up": their value is being frozen originals.
# ---------------------------------------------------------------------------
def _ref_resolved_citation_ordinals(citations: Any) -> set[int]:
    out: set[int] = set()
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if isinstance(entry, Mapping):
            n = V._citation_ordinal(entry)
            if n is not None:
                out.add(n)
    return out


def _ref_ordinal_evidence_map(citations: Any) -> dict[int, str]:
    out: dict[int, str] = {}
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if not isinstance(entry, Mapping):
            continue
        n = V._citation_ordinal(entry)
        if n is None:
            continue
        text = entry.get("evidence_text")
        if not (isinstance(text, str) and text):
            text = entry.get("title")
        if not (isinstance(text, str) and text):
            text = str(n)
        out[n] = str(text)
    return out


def _ref_ordinal_effconf_map(citations: Any) -> dict[int, float]:
    out: dict[int, float] = {}
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if not isinstance(entry, Mapping):
            continue
        n = V._citation_ordinal(entry)
        if n is None:
            continue
        eff = entry.get("effective_confidence")
        if eff is None:
            continue
        try:
            out[n] = float(eff)
        except (TypeError, ValueError):
            continue
    return out


def _ref_ordinal_derived_map(citations: Any) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if not isinstance(entry, Mapping):
            continue
        n = V._citation_ordinal(entry)
        if n is None:
            continue
        df = entry.get("derived_from")
        if isinstance(df, (list, tuple)):
            out[n] = {str(x) for x in df if x is not None and str(x)}
    return out


def _ref_ordinal_source_map(citations: Any) -> dict[int, str]:
    out: dict[int, str] = {}
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if not isinstance(entry, Mapping):
            continue
        n = V._citation_ordinal(entry)
        if n is None:
            continue
        src = entry.get("source")
        if not (isinstance(src, str) and src):
            src = entry.get("analyst_id")
        if isinstance(src, str) and src:
            out[n] = src
    return out


def _ref_periphery_ordinals(citations: Any) -> set[int]:
    out: set[int] = set()
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("tier") != V._PERIPHERY_TIER:
            continue
        n = V._citation_ordinal(entry)
        if n is not None:
            out.add(n)
    return out


#: (shipped, reference) pairs — every collapsed site.
_PAIRS = [
    ("_resolved_citation_ordinals", V._resolved_citation_ordinals, _ref_resolved_citation_ordinals),
    ("_ordinal_evidence_map", V._ordinal_evidence_map, _ref_ordinal_evidence_map),
    ("_ordinal_effconf_map", V._ordinal_effconf_map, _ref_ordinal_effconf_map),
    ("_ordinal_derived_map", V._ordinal_derived_map, _ref_ordinal_derived_map),
    ("_ordinal_source_map", V._ordinal_source_map, _ref_ordinal_source_map),
    ("_periphery_ordinals", V._periphery_ordinals, _ref_periphery_ordinals),
]

_PERIPHERY = V._PERIPHERY_TIER


# ---------------------------------------------------------------------------
# Fixture matrix
# ---------------------------------------------------------------------------
_CITATION_FIXTURES: dict[str, Any] = {
    # --- degenerate containers ------------------------------------------
    "none": None,
    "empty_list": [],
    "empty_tuple": (),
    "not_a_sequence_dict": {"ordinal": 1},
    "not_a_sequence_str": "[[ref:1]]",
    "not_a_sequence_int": 7,
    "entries_not_mappings": [None, 3, "x", ["ordinal", 1]],
    # --- ordinary, well-formed -------------------------------------------
    "plain_two": [
        {
            "ordinal": 1,
            "evidence_text": "Kyiv reported shelling.",
            "title": "Shelling",
            "effective_confidence": 0.72,
            "derived_from": ["sig-a", "sig-b"],
            "source": "escalation",
        },
        {
            "ordinal": 2,
            "evidence_text": "Port closed.",
            "title": "Port",
            "effective_confidence": 0.41,
            "derived_from": ["sig-c"],
            "source": "military_posture",
        },
    ],
    # --- marker-derived ordinals (no explicit field) ----------------------
    "marker_only": [
        {"marker": "[[ref:3]]", "title": "T3", "source": "escalation"},
        {"marker": "[[ref:11]]", "evidence_text": "E11", "analyst_id": "a11"},
    ],
    # --- FULL-WIDTH 【N】 bracket variants (core-plane defect) ------------
    # The composition marker regex matches ASCII [[ref:N]] only; a full-width
    # variant carries NO resolvable digit ordinal via the marker path, so such
    # an entry resolves ONLY through an explicit ``ordinal`` field. Both the
    # with-field and without-field cases are pinned.
    "fullwidth_marker_no_field": [
        {"marker": "【1】", "title": "FW1", "source": "escalation"},
        {"marker": "【【ref:2】】", "title": "FW2", "source": "escalation"},
        {"marker": "[[ref:2]]", "title": "ASCII2", "source": "escalation"},
    ],
    "fullwidth_marker_with_field": [
        {"ordinal": 5, "marker": "【5】", "title": "FW5", "source": "escalation"},
        {"ordinal": 6, "marker": "【【ref:6】】", "evidence_text": "FW6"},
    ],
    "fullwidth_mixed_tiers": [
        {"ordinal": 1, "marker": "【1】", "tier": _PERIPHERY, "title": "p1"},
        {"ordinal": 2, "marker": "[[ref:2]]", "title": "b2"},
        {"marker": "【3】", "tier": _PERIPHERY, "title": "unresolvable"},
    ],
    # --- MISSING ordinals -------------------------------------------------
    "missing_ordinals": [
        {"title": "no ordinal at all"},
        {"marker": "[[ref:not-a-number]]", "title": "bad marker"},
        {"marker": "[[ref:1f2e3d4c-0000-0000-0000-000000000000]]", "title": "legacy uuid"},
        {"ordinal": None, "title": "explicit none"},
        {"ordinal": "abc", "title": "unparseable str"},
        {"ordinal": 9, "title": "the only resolvable one"},
    ],
    "all_missing": [
        {"title": "a"},
        {"marker": "[[ref:zzz]]"},
        {"ordinal": []},
    ],
    # --- ordinal FIELD type coercions ------------------------------------
    "ordinal_type_variants": [
        {"ordinal": True, "title": "bool-true"},
        {"ordinal": False, "title": "bool-false"},
        {"ordinal": "4", "title": "str-4"},
        {"ordinal": 5.9, "title": "float-5.9"},
        {"ordinal": 6, "marker": "[[ref:99]]", "title": "field-beats-marker"},
    ],
    # --- DUPLICATE ordinals: later entry WINS ----------------------------
    "dup_later_wins": [
        {
            "ordinal": 1,
            "evidence_text": "first",
            "effective_confidence": 0.1,
            "derived_from": ["x"],
            "source": "first_src",
        },
        {
            "ordinal": 1,
            "evidence_text": "second",
            "effective_confidence": 0.9,
            "derived_from": ["y"],
            "source": "second_src",
        },
    ],
    # --- DUPLICATE ordinals: later entry SKIPPED, earlier must SURVIVE ---
    # This is the semantics the sentinel exists to preserve. A later duplicate
    # that the projector skips must NOT clear the earlier value.
    "dup_later_skipped": [
        {
            "ordinal": 1,
            "evidence_text": "keep me",
            "effective_confidence": 0.55,
            "derived_from": ["keep-a"],
            "source": "keep_src",
        },
        # no effective_confidence, no derived_from, no source/analyst_id
        {"ordinal": 1, "title": "bare duplicate"},
    ],
    "dup_later_skipped_badtypes": [
        {
            "ordinal": 2,
            "effective_confidence": 0.33,
            "derived_from": ["d"],
            "source": "s",
        },
        {
            "ordinal": 2,
            "effective_confidence": "not-a-float",
            "derived_from": "not-a-list",
            "source": "",
            "analyst_id": "",
        },
    ],
    "dup_three_way": [
        {"ordinal": 1, "effective_confidence": 0.1, "source": "s1"},
        {"ordinal": 1, "title": "bare"},
        {"ordinal": 1, "effective_confidence": 0.3},
    ],
    # --- effective_confidence edge values --------------------------------
    "effconf_variants": [
        {"ordinal": 1, "effective_confidence": 0},
        {"ordinal": 2, "effective_confidence": "0.5"},
        {"ordinal": 3, "effective_confidence": True},
        {"ordinal": 4, "effective_confidence": None},
        {"ordinal": 5, "effective_confidence": float("inf")},
        {"ordinal": 6, "effective_confidence": [1]},
        {"ordinal": 7, "effective_confidence": 0.0},
    ],
    # --- evidence fallback chain -----------------------------------------
    "evidence_fallback": [
        {"ordinal": 1, "evidence_text": "E", "title": "T"},
        {"ordinal": 2, "evidence_text": "", "title": "T2"},
        {"ordinal": 3, "evidence_text": None, "title": ""},
        {"ordinal": 4, "title": 12345},
        {"ordinal": 5},
    ],
    # --- derived_from shapes ---------------------------------------------
    "derived_variants": [
        {"ordinal": 1, "derived_from": ["a", "b", "a"]},
        {"ordinal": 2, "derived_from": ("c",)},
        {"ordinal": 3, "derived_from": []},
        {"ordinal": 4, "derived_from": [None, "", "d", 5]},
        {"ordinal": 5, "derived_from": "not-a-list"},
        {"ordinal": 6, "derived_from": None},
        {"ordinal": 7},
    ],
    # --- source / analyst_id fallback ------------------------------------
    "source_fallback": [
        {"ordinal": 1, "source": "src", "analyst_id": "ana"},
        {"ordinal": 2, "source": "", "analyst_id": "ana2"},
        {"ordinal": 3, "source": None, "analyst_id": "ana3"},
        {"ordinal": 4, "source": 99, "analyst_id": "ana4"},
        {"ordinal": 5, "analyst_id": ""},
        {"ordinal": 6, "source": "only"},
        {"ordinal": 7},
    ],
    # --- periphery tiering -----------------------------------------------
    "tiers_mixed": [
        {"ordinal": 1, "tier": _PERIPHERY, "title": "p"},
        {"ordinal": 2, "tier": "basis", "title": "b"},
        {"ordinal": 3, "title": "untiered"},
        {"ordinal": 4, "tier": _PERIPHERY, "source": "s"},
    ],
    "tiers_none": [
        {"ordinal": 1, "title": "a"},
        {"ordinal": 2, "title": "b"},
    ],
    "tiers_all": [
        {"ordinal": 1, "tier": _PERIPHERY},
        {"ordinal": 2, "tier": _PERIPHERY},
    ],
    "tier_dup_ordinal_split": [
        {"ordinal": 1, "tier": _PERIPHERY},
        {"ordinal": 1, "tier": "basis"},
    ],
    # --- mixed junk + real ------------------------------------------------
    "junk_and_real": [
        None,
        {"ordinal": 1, "evidence_text": "real", "source": "s", "derived_from": ["z"]},
        "string entry",
        {"no": "ordinal"},
        {"ordinal": 2, "tier": _PERIPHERY},
        42,
    ],
}


@pytest.mark.parametrize("fixture_name", sorted(_CITATION_FIXTURES))
@pytest.mark.parametrize("builder_name", [p[0] for p in _PAIRS])
def test_ordinal_builder_matches_reference(builder_name: str, fixture_name: str) -> None:
    """Every collapsed builder == its frozen pre-refactor reference."""
    shipped, reference = next((s, r) for n, s, r in _PAIRS if n == builder_name)
    citations = _CITATION_FIXTURES[fixture_name]
    expected = reference(citations)
    actual = shipped(citations)
    assert actual == expected, (
        f"{builder_name} diverged from its pre-refactor reference on "
        f"fixture {fixture_name!r}: {actual!r} != {expected!r}"
    )
    assert type(actual) is type(expected)


@pytest.mark.parametrize("fixture_name", sorted(_CITATION_FIXTURES))
def test_builders_do_not_mutate_input(fixture_name: str) -> None:
    """No collapsed builder mutates the citation list it is handed."""
    import copy

    citations = _CITATION_FIXTURES[fixture_name]
    before = copy.deepcopy(citations)
    for _name, shipped, _ref in _PAIRS:
        shipped(citations)
    assert citations == before


def test_skip_sentinel_preserves_earlier_value_on_duplicate() -> None:
    """The load-bearing sentinel contract, asserted directly.

    A projector that SKIPS a later duplicate must leave the earlier value in
    place — writing ``None`` instead would be an observable behavior change.
    """
    cits = [
        {"ordinal": 1, "effective_confidence": 0.55, "source": "keep", "derived_from": ["k"]},
        {"ordinal": 1},
    ]
    assert V._ordinal_effconf_map(cits) == {1: 0.55}
    assert V._ordinal_source_map(cits) == {1: "keep"}
    assert V._ordinal_derived_map(cits) == {1: {"k"}}


def test_skip_sentinel_distinct_from_none_value() -> None:
    """``None`` is a storable projected value; only the sentinel skips."""
    cits = [{"ordinal": 1}, {"ordinal": 2}]
    assert V._build_ordinal_map(cits, lambda _e, _n: None) == {1: None, 2: None}
    assert V._build_ordinal_map(cits, lambda _e, _n: V._ORDINAL_SKIP) == {}


def test_build_ordinal_map_projector_receives_resolved_ordinal() -> None:
    """The projector is handed the RESOLVED ordinal, not the raw field."""
    seen: list[tuple[Any, int]] = []

    def _spy(entry: Mapping[str, Any], n: int) -> Any:
        seen.append((entry.get("ordinal"), n))
        return n

    V._build_ordinal_map([{"ordinal": "4"}, {"marker": "[[ref:7]]"}], _spy)
    assert seen == [("4", 4), (None, 7)]


def test_no_hand_rolled_ordinal_loops_remain() -> None:
    """Drift guard: the collapsed traversal has exactly ONE definition.

    Scans the shipped module source for the hand-rolled skeleton the collapse
    removed. A re-introduced copy fails here rather than drifting silently.
    """
    import inspect

    src = inspect.getsource(V)
    # The traversal body appears once, inside _build_ordinal_map.
    assert src.count("n = _citation_ordinal(entry)") == 1, (
        "a hand-rolled per-ordinal traversal was re-introduced; route it "
        "through _build_ordinal_map instead"
    )


def test_full_width_markers_never_fabricate_an_ordinal() -> None:
    """A full-width 【N】 marker carries no ASCII [[ref:N]] digit.

    Pinned explicitly (not just via the reference comparison) because this is
    the documented core-plane bracket defect: such an entry must resolve to NO
    ordinal unless it also carries an explicit ``ordinal`` field — it must never
    be guessed at from the full-width digits.
    """
    assert V._resolved_citation_ordinals([{"marker": "【1】"}]) == set()
    assert V._resolved_citation_ordinals([{"marker": "【【ref:2】】"}]) == set()
    assert V._resolved_citation_ordinals([{"marker": "[[ref:2]]"}]) == {2}
    assert V._resolved_citation_ordinals([{"ordinal": 1, "marker": "【1】"}]) == {1}


def test_cross_builder_ordinal_domains_agree() -> None:
    """Every map's key set is a subset of the resolved-ordinal set.

    The shared traversal makes this structural; asserted so a projector that
    starts inventing keys is caught.
    """
    for fixture in _CITATION_FIXTURES.values():
        resolved = V._resolved_citation_ordinals(fixture)
        for builder in (
            V._ordinal_evidence_map,
            V._ordinal_effconf_map,
            V._ordinal_derived_map,
            V._ordinal_source_map,
        ):
            assert set(builder(fixture)).issubset(resolved), builder.__name__
        assert V._periphery_ordinals(fixture).issubset(resolved)


def test_evidence_map_is_total_over_resolved_ordinals() -> None:
    """Evidence NEVER skips: it degrades to the ordinal string, so its key set
    equals the resolved-ordinal set exactly (the no-fabrication contract)."""
    for fixture in _CITATION_FIXTURES.values():
        assert set(V._ordinal_evidence_map(fixture)) == V._resolved_citation_ordinals(fixture)


@pytest.mark.parametrize(
    "a,b", list(itertools.combinations(sorted(_CITATION_FIXTURES), 2))[:60]
)
def test_concatenated_fixtures_match_reference(a: str, b: str) -> None:
    """Concatenating two fixtures still matches the references.

    Exercises cross-fixture duplicate ordinals (last-wins vs skip-preserves)
    that no single fixture produces on its own.
    """
    fa, fb = _CITATION_FIXTURES[a], _CITATION_FIXTURES[b]
    if not isinstance(fa, (list, tuple)) or not isinstance(fb, (list, tuple)):
        pytest.skip("not concatenable")
    combined = list(fa) + list(fb)
    for name, shipped, reference in _PAIRS:
        assert shipped(combined) == reference(combined), name
