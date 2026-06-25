# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for legba.data.vocabulary.normalize_predicate (Phase B item 5).

The substrate predicate-column normalizer converges the two divergent surface
forms of a relation — the seed driver's CamelCase ("LeaderOf") and the ingest
extractor's lowercase-spaced ("leader of") — onto ONE canonical lowercase-
spaced form so the lower(predicate)/lower(rel_type) dedup + supersession keys
line up across producers. Conservative: unknown predicates pass through.
"""

from __future__ import annotations

import pytest

from legba.data.vocabulary import PREDICATE_CANONICAL, normalize_predicate


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The five pairs called out in the Phase B brief + the rest of the seed
        # adapters' CamelCase rel_types / predicates.
        ("LeaderOf", "leader of"),
        ("MemberOf", "member of"),
        ("LocatedIn", "located in"),
        ("AlliedWith", "allied with"),
        ("HostileTo", "hostile to"),
        ("Sanctions", "sanctions"),
        ("ArmsTransferTo", "arms transfer to"),
        ("InvolvedInConflictEvent", "involved in conflict event"),
        ("PartOf", "part of"),
        ("CoOccursWith", "co occurs with"),
        # Case / whitespace tolerance — same canonical form.
        ("leaderof", "leader of"),
        ("  MemberOf  ", "member of"),
        ("HOSTILETO", "hostile to"),
    ],
)
def test_normalize_predicate_camelcase_to_canonical(raw, expected):
    assert normalize_predicate(raw) == expected


def test_already_canonical_is_idempotent():
    # The ingest form is already canonical — unchanged, and normalizing twice is
    # a fixed point.
    for spaced in ("leader of", "member of", "hostile to", "allied with"):
        assert normalize_predicate(spaced) == spaced
        assert normalize_predicate(normalize_predicate(spaced)) == spaced


@pytest.mark.parametrize(
    "raw",
    [
        "led by",            # ingest phrase not in the map → verbatim
        "ruled by",
        "rivals",
        "located_in",        # SLM corrected_type (underscore) → verbatim
        "SomeNovelPredicate",  # unknown CamelCase → verbatim (conservative)
        "capital",
    ],
)
def test_unknown_predicate_passes_through_unchanged(raw):
    assert normalize_predicate(raw) == raw


def test_empty_and_blank_returned_unchanged():
    assert normalize_predicate("") == ""
    assert normalize_predicate("   ") == "   "


def test_every_canonical_value_is_lowercase_spaced_fixed_point():
    """Every target value in the map is itself canonical (normalizing a target
    is a no-op) — guards against a CamelCase target sneaking into the map."""
    for canonical in set(PREDICATE_CANONICAL.values()):
        assert canonical == canonical.lower()
        assert normalize_predicate(canonical) == canonical
