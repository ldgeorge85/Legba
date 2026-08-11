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

from legba.data._entity_canon import (
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

from legba.data._entity_canon import (  # noqa: E402
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


# ===========================================================================
# W1 (defect D7) — shared canon spine: hardened junk gate, org-surface
# gazetteer. The canon lives at legba.data._entity_canon; the old
# deterministic_handlers re-export shim was DELETED 2026-08-02 (phase 1C)
# once every importer had been repointed at the shared module.
# ===========================================================================

from legba.data._entity_canon import is_org_surface  # noqa: E402
from legba.data._entity_canon import (  # noqa: E402
    _DEMONYM_MAP,
    _JUNK_ENTITIES,
)


def test_internal_maps_are_exposed():
    # _JUNK_ENTITIES + _DEMONYM_MAP are part of the load-bearing public surface
    # (the 0045 migration mirrors them).
    assert "tv" in _JUNK_ENTITIES
    assert _DEMONYM_MAP["chinese"] == "China"
    assert _DEMONYM_MAP["israeli"] == "Israel"


# ---------------------------------------------------------------------------
# Hardened junk gate — each live junk class (verbatim review strings)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "junk",
    [
        # clock-times (NWS forecast spans)
        "6:53PM MDT",
        "10:00PM AKDT",
        "23:00",
        "9 PM",
        "9PM EDT",
        # leading-quantifier
        "More than 450,000",
        "hundreds of thousands",
        "at least 12",
        "up to 3 million",
        "tens of thousands",
        # pure numeric / percent / currency
        "1,200",
        "45%",
        "$3.2bn",
        "3.5 million",
        "12.5",
        # length <= 2
        "F1",
        "Xi",
        "Co",
        # residual HTML
        "...</p><img src=x",
        "<img src=x>",
        "foo&nbsp;bar",
    ],
)
def test_live_junk_classes_rejected(junk):
    assert is_junk_entity(junk) is True, junk


def test_clock_time_canonicalizes_to_empty_only_via_caller_guard():
    # is_junk_entity flags it; canonicalize_entity itself does NOT silently drop
    # predicate-junk (only the literal base set), so the caller wires the gate.
    # We assert the GATE here — the contract W2 consumes.
    assert is_junk_entity("6:53PM MDT") is True


@pytest.mark.parametrize("demonym", ["Chinese", "Israeli", "Russian", "Iranian"])
def test_bare_demonym_routes_through_demonym_map(demonym):
    # A bare demonym endpoint is handled by ROUTING through _DEMONYM_MAP: it
    # collapses to its country (so a standalone demonym node never persists),
    # rather than being junk-DROPPED. Dropping would lose the referent entirely;
    # proposed_edge_governance relies on is_junk_entity()==False here so the
    # collapse (demonym -> country) happens instead of an edge being discarded.
    assert is_demonym(demonym) is True
    assert is_junk_entity(demonym) is False
    name, _cls = canonicalize_entity(demonym, "person")
    assert name == _DEMONYM_MAP[demonym.lower()]


@pytest.mark.parametrize("legit", ["US", "UK", "EU", "UN", "WHO"])
def test_hardened_gate_spares_legit_short_orgs(legit):
    # length<=2 / numeric predicates must NOT eat alias-mapped short orgs.
    assert is_junk_entity(legit) is False


@pytest.mark.parametrize(
    "country", ["Iran", "Japan", "Brazil", "India", "United States"]
)
def test_hardened_gate_spares_country_names(country):
    assert is_junk_entity(country) is False


def test_real_multiword_names_not_junk():
    # Names with letters + an embedded number are NOT pure-numeric junk.
    assert is_junk_entity("Boeing 737") is False
    assert is_junk_entity("Group of 20") is False
    assert is_junk_entity("Donald Trump") is False


# ---------------------------------------------------------------------------
# Demonym collapse (the verbatim cases the task calls out)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "demonym,country",
    [
        ("Chinese", "China"),
        ("Israeli", "Israel"),
        ("Russian", "Russia"),
        ("Iranian", "Iran"),
        ("American", "United States"),
    ],
)
def test_demonym_collapse_w1(demonym, country):
    name, _cls = canonicalize_entity(demonym, "person")
    assert name == country
    assert is_demonym(demonym) is True


# ---------------------------------------------------------------------------
# Org-surface gazetteer (W2's entity resolver consumes this)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "org",
    [
        "Bank of England",
        "Bank of America",
        "Nippon Steel",
        "Hyundai Motor Group",
        "Mitsubishi Heavy Industries",
        "Acme Inc",
        "Goldman Group",
        "Toyota Motor",
        "University of Oxford",
        "Ministry of Finance",
    ],
)
def test_is_org_surface_true(org):
    assert is_org_surface(org) is True, org


@pytest.mark.parametrize(
    "not_org",
    [
        "Michelle Steel",   # a PERSON surnamed Steel — must NOT be org
        "Steel",            # single bare token
        "Bank",             # single bare token
        "Giorgia Meloni",   # a person
        "Vladimir Putin",   # a person
        "Iran",             # a country
    ],
)
def test_is_org_surface_false(not_org):
    assert is_org_surface(not_org) is False, not_org


def test_is_org_surface_empty_input():
    assert is_org_surface("") is False
    assert is_org_surface("   ") is False
    assert is_org_surface(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Idempotency over the W1 surfaces (verbatim live strings)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "surface,cls",
    [
        ("Chinese", "person"),
        ("Israeli", "person"),
        ("Bank of England", "person"),
        ("Nippon Steel", "person"),
        ("Hyundai Motor Group", "organization"),
        ("6:53PM MDT", "entity"),
        ("More than 450,000", "entity"),
    ],
)
def test_idempotent_w1(surface, cls):
    n1, c1 = canonicalize_entity(surface, cls)
    n2, c2 = canonicalize_entity(n1, c1)
    assert (n1, c1) == (n2, c2)
