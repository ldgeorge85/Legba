# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for :func:`canonicalize_entity` (Phase C entity resolution).

Covers the exact data-quality-audit cases the canonicalization pass exists to
fix:

  * the {US, U.S., USA, United States, America, ...} fragmentation → ONE
    canonical ``("United States", "country")``;
  * a country name is NEVER typed ``person`` (the "United States as a person"
    mistype);
  * NWS forecast offices are NEVER typed ``person`` (18 were);
  * HTML-entity garbage (``&#039;`` / ``&apos;`` / ``&amp;``) + trailing
    possessives (``'s`` / `` 's`` / ``’s``) are stripped;
  * the function is idempotent (a fixed point in one application).

Pure unit tests — no DB, no SLM, no network (the helper is deterministic).
"""

from __future__ import annotations

import pytest

from legba.data.analysts.deterministic_handlers._entity_canon import (
    COUNTRY_CLASS,
    DEFAULT_CLASS,
    ORGANIZATION_CLASS,
    canonicalize_entity,
)
from legba.data.vocabulary import ENTITY_CLASSES


# ---------------------------------------------------------------------------
# Canonical-class strings are members of the closed taxonomy — never invented.
# ---------------------------------------------------------------------------


def test_canonical_classes_are_taxonomy_members():
    assert COUNTRY_CLASS == "country"
    assert ORGANIZATION_CLASS == "organization"
    assert DEFAULT_CLASS == "entity"
    for cls in (COUNTRY_CLASS, ORGANIZATION_CLASS, DEFAULT_CLASS):
        assert cls in ENTITY_CLASSES


# ---------------------------------------------------------------------------
# US-variants → ONE canonical (country), regardless of incoming class
# ---------------------------------------------------------------------------


_US_VARIANTS = [
    "US", "U.S.", "U.S", "USA", "U.S.A.", "U.S.A",
    "United States", "United States of America", "America",
    "the united states",
]


@pytest.mark.parametrize("surface", _US_VARIANTS)
@pytest.mark.parametrize("incoming", ["person", "country", "location", "entity"])
def test_us_variants_converge_to_one_country(surface, incoming):
    name, cls = canonicalize_entity(surface, incoming)
    assert name == "United States"
    assert cls == "country"


def test_us_variants_all_share_one_dedup_key():
    keys = {canonicalize_entity(s, "person") for s in _US_VARIANTS}
    # Every variant collapses to the SAME (name, class) — one profile row.
    assert keys == {("United States", "country")}


@pytest.mark.parametrize(
    "surface,expected",
    [
        ("UK", "United Kingdom"),
        ("U.K.", "United Kingdom"),
        ("Britain", "United Kingdom"),
        ("Great Britain", "United Kingdom"),
        ("United Kingdom", "United Kingdom"),
        ("UAE", "United Arab Emirates"),
        ("United Arab Emirates", "United Arab Emirates"),
    ],
)
def test_other_country_aliases(surface, expected):
    name, cls = canonicalize_entity(surface, "person")
    assert name == expected
    assert cls == "country"


def test_eu_is_organization_not_person_not_country():
    name, cls = canonicalize_entity("EU", "person")
    assert name == "European Union"
    assert cls == ORGANIZATION_CLASS


# ---------------------------------------------------------------------------
# A country name is NEVER typed person (the audit mistype)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "country", ["Brazil", "India", "Germany", "Japan", "Nigeria", "Russia"]
)
def test_country_name_never_person(country):
    name, cls = canonicalize_entity(country, "person")
    assert cls == "country", (country, cls)
    assert cls != "person"
    assert name == country  # bare country names pass through unchanged


# ---------------------------------------------------------------------------
# NWS forecast offices are NEVER typed person (18 were)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "office",
    [
        "NWS St Louis",
        "NWS Mobile AL",
        "NWS Chicago",
        "National Weather Service",
        "National Weather Service Chicago",
    ],
)
def test_nws_office_never_person(office):
    name, cls = canonicalize_entity(office, "person")
    assert cls == ORGANIZATION_CLASS, (office, cls)
    assert cls != "person"
    # The surface form itself is preserved (only the type is corrected).
    assert name == office


# ---------------------------------------------------------------------------
# HTML-entity + possessive stripping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "surface,expected",
    [
        ("Cape Verde&#039;s", "Cape Verde"),       # numeric entity + possessive
        ("Donald Trump's", "Donald Trump"),         # straight-quote possessive
        ("Donald Trump 's", "Donald Trump"),        # NER-spaced possessive
        ("Donald Trump’s", "Donald Trump"),    # curly-quote possessive
        ("AT&amp;T", "AT&T"),                        # named entity unescape
        ('"United Nations"', "United Nations"),     # surrounding quotes
        ("  spaced   out  ", "spaced out"),          # whitespace collapse
    ],
)
def test_strip_html_and_possessive(surface, expected):
    name, _cls = canonicalize_entity(surface, "organization")
    assert name == expected


def test_apos_garbage_strips_to_empty():
    # "&apos;" → "'" → stripped away → empty; caller's MIN_NAME_LEN drops it.
    name, _cls = canonicalize_entity("&apos;", "entity")
    assert name == ""


def test_trump_administration_stays_person_phrase():
    # An org-ish phrase that is NOT in the curated patterns passes through
    # unchanged (conservative — we do not over-merge).
    name, cls = canonicalize_entity("The Trump administration", "person")
    assert name == "The Trump administration"
    assert cls == "person"


# ---------------------------------------------------------------------------
# Idempotency: canonicalize(canonicalize(x)) == canonicalize(x)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "surface,cls",
    [
        ("US", "person"),
        ("Cape Verde&#039;s", "country"),
        ("Donald Trump 's", "person"),
        ("NWS St Louis", "person"),
        ("EU", "person"),
        ("Evian", "location"),
        ("Acme Inc", "organization"),
        ('"United Nations"', "organization"),
    ],
)
def test_idempotent(surface, cls):
    n1, c1 = canonicalize_entity(surface, cls)
    n2, c2 = canonicalize_entity(n1, c1)
    assert (n1, c1) == (n2, c2)


# ---------------------------------------------------------------------------
# Pass-through: unknown names keep their class, only stripped
# ---------------------------------------------------------------------------


def test_unknown_name_passes_through():
    name, cls = canonicalize_entity("Evian", "location")
    assert name == "Evian"
    assert cls == "location"


def test_empty_input_returns_empty_with_class():
    name, cls = canonicalize_entity("", "person")
    assert name == ""
    assert cls == "person"
    name, cls = canonicalize_entity("   ", "entity")
    assert name == ""
    assert cls == "entity"


# ---------------------------------------------------------------------------
# DQ-H4: demonym collapse + junk reject
# ---------------------------------------------------------------------------

from legba.data.analysts.deterministic_handlers._entity_canon import (  # noqa: E402
    is_demonym,
    is_junk_entity,
)


@pytest.mark.parametrize("demonym,country", [
    ("Iranian", "Iran"),
    ("iranian", "Iran"),
    ("Israeli", "Israel"),
    ("American", "United States"),
    ("Russian", "Russia"),
    ("Pakistani", "Pakistan"),
    ("British", "United Kingdom"),
])
def test_demonym_collapses_to_country(demonym, country):
    name, _cls = canonicalize_entity(demonym, "person")
    assert name == country
    assert is_demonym(demonym) is True


def test_non_demonym_surname_not_collapsed():
    """A surname that superficially looks demonym-ish ('Meloni') must NOT be
    collapsed — the curated map avoids the suffix-regex over-match."""
    name, _cls = canonicalize_entity("Meloni", "person")
    assert name == "Meloni"
    assert is_demonym("Meloni") is False


def test_junk_token_dropped():
    name, _cls = canonicalize_entity("TV", "organization")
    assert name == ""
    assert is_junk_entity("TV") is True
    assert is_junk_entity("tv") is True


def test_short_legit_entities_survive():
    """US/UK/EU must NOT be treated as junk (they alias to full country names)."""
    assert is_junk_entity("US") is False
    assert is_junk_entity("UK") is False
    us, _ = canonicalize_entity("US", "organization")
    assert us == "United States"
