# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FIX A — widened entity-canon gate over the VERBATIM live junk catalog.

Pure (no-DB, no-network) unit tests for the 2026-06-26 remediation second-pass
regressions the first pass failed to catch (D6/D13/D7, stop D8 growing). Every
input string here is taken VERBATIM from the live junk catalog so the gate/canon
is tested against exactly what leaked:

  * demonym (``is_demonym`` must map nationality → country) — the curated map
    was too small ("Albanian"/"Belgian"/"Kenyan"/"Liberian"/"Bangladeshi" all
    missed);
  * money / currency tokens (``is_junk_entity`` must be True) — "S$2,500",
    "US$ 525 million", "$3.2bn";
  * age / time tokens — "51 - year - old", "2,600 - year - old",
    "24 - year - old", "centuries";
  * possessive fragments — "Abu Dhabi 's" (canon strips to the referent);
  * sports / event noise — "World Cup", "Group F";
  * mis-typed-as-person → organization / location — "Robertson Quay",
    "Falkland Islands Legislative Assembly", "CITIC Tower", "Yerevan", "Earth".
"""

from __future__ import annotations

import pytest

from legba.data._entity_canon import (
    COUNTRY_CLASS,
    LOCATION_CLASS,
    ORGANIZATION_CLASS,
    canonicalize_entity,
    is_demonym,
    is_junk_entity,
    is_org_surface,
    is_place_surface,
)
from legba.data.vocabulary import ENTITY_CLASSES


# ---------------------------------------------------------------------------
# D14 / proposed_edge_governance — COMPREHENSIVE demonym → country map
# ---------------------------------------------------------------------------

# (verbatim catalog) — these all returned False before the widening.
_CATALOG_DEMONYMS = {
    "Albanian": "Albania",
    "Belgian": "Belgium",
    "Kenyan": "Kenya",
    "Liberian": "Liberia",
    "Bangladeshi": "Bangladesh",
}


@pytest.mark.parametrize("demonym,country", sorted(_CATALOG_DEMONYMS.items()))
def test_catalog_demonym_is_recognised(demonym, country):
    assert is_demonym(demonym) is True


@pytest.mark.parametrize("demonym,country", sorted(_CATALOG_DEMONYMS.items()))
def test_catalog_demonym_collapses_to_country_class(demonym, country):
    # NER often emits a demonym typed 'person'; the canon must collapse it to
    # the COUNTRY surface + COUNTRY_CLASS so it stops being a distinct node.
    name, cls = canonicalize_entity(demonym, "person")
    assert name == country
    assert cls == COUNTRY_CLASS


def test_wide_demonym_coverage_resolves_to_country():
    # A broad cross-section beyond the verbatim catalog — every demonym value
    # must land on a recognised country (no demonym left typed as a non-country).
    for d in (
        "Austrian", "Dutch", "Swedish", "Norwegian", "Portuguese", "Greek",
        "Emirati", "Kazakh", "Vietnamese", "Thai", "Filipino", "Ethiopian",
        "Ghanaian", "Moroccan", "Colombian", "Peruvian", "Chilean", "Cuban",
    ):
        name, cls = canonicalize_entity(d, "person")
        assert cls == COUNTRY_CLASS, f"{d!r} -> {(name, cls)!r}"


def test_multi_country_adjectives_are_not_demonyms():
    # Conservative boundary: an adjective with NO single country must NOT be
    # mis-collapsed (would corrupt the graph). Surnames likewise.
    for word in ("Asian", "European", "African", "Arab", "Latin", "Meloni"):
        assert is_demonym(word) is False


# ---------------------------------------------------------------------------
# D6/D13 — money / currency tokens are junk
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token", ["S$2,500", "US$ 525 million", "$3.2bn"])
def test_money_tokens_are_junk(token):
    assert is_junk_entity(token) is True


def test_money_compound_prefix_variants_are_junk():
    for token in ("C$1,000", "HK$3.5 billion", "A$200", "R$45 million",
                  "USD 525 million", "Rs. 1,000 crore"):
        assert is_junk_entity(token) is True, token


# ---------------------------------------------------------------------------
# D6/D13 — age / time tokens are junk
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "token",
    ["51 - year - old", "2,600 - year - old", "24 - year - old", "centuries"],
)
def test_age_time_tokens_are_junk(token):
    assert is_junk_entity(token) is True


def test_age_time_variants_are_junk():
    for token in ("24-year-old", "decades", "a century", "millennia"):
        assert is_junk_entity(token) is True, token


# ---------------------------------------------------------------------------
# D6/D13 — possessive fragment collapses to its referent (NOT junk-dropped)
# ---------------------------------------------------------------------------

def test_possessive_fragment_strips_to_referent():
    # "Abu Dhabi 's" must collapse to the real place "Abu Dhabi" — dropping it
    # would lose the referent. The trailing possessive is peeled by the strip.
    name, cls = canonicalize_entity("Abu Dhabi 's", "person")
    assert name == "Abu Dhabi"
    # And it is NOT reported as junk (the referent survives).
    assert is_junk_entity("Abu Dhabi 's") is False
    # The recovered referent is typed as a place (a known city), never person.
    assert cls == LOCATION_CLASS


# ---------------------------------------------------------------------------
# Sports / event noise is junk (D6/D13 — World-Cup feed scaffolding)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token", ["World Cup", "Group F"])
def test_sports_event_noise_is_junk(token):
    assert is_junk_entity(token) is True


def test_sports_noise_shapes_and_literals():
    for token in ("Group A", "Group H", "Round of 16", "Champions League",
                  "Quarter-finals", "Olympics"):
        assert is_junk_entity(token) is True, token


def test_real_org_containing_sports_word_is_not_junk():
    # Whole-surface literal match only — a real org that merely contains a
    # sports word survives.
    for token in ("World Cup Committee", "Premier League Properties", "FIFA"):
        assert is_junk_entity(token) is False, token


# ---------------------------------------------------------------------------
# D7 — mis-typed-as-person → organization / location (never person)
# ---------------------------------------------------------------------------

def test_robertson_quay_is_location_not_person():
    assert is_place_surface("Robertson Quay") is True
    name, cls = canonicalize_entity("Robertson Quay", "person")
    assert name == "Robertson Quay"
    assert cls == LOCATION_CLASS


def test_citic_tower_is_location_not_person():
    assert is_place_surface("CITIC Tower") is True
    _, cls = canonicalize_entity("CITIC Tower", "person")
    assert cls == LOCATION_CLASS


def test_falkland_islands_legislative_assembly_is_org_not_person():
    assert is_org_surface("Falkland Islands Legislative Assembly") is True
    _, cls = canonicalize_entity("Falkland Islands Legislative Assembly", "person")
    assert cls == ORGANIZATION_CLASS


def test_yerevan_known_city_is_location_not_person():
    assert is_place_surface("Yerevan") is True
    _, cls = canonicalize_entity("Yerevan", "person")
    assert cls == LOCATION_CLASS


def test_earth_is_location_not_person():
    assert is_place_surface("Earth") is True
    _, cls = canonicalize_entity("Earth", "person")
    assert cls == LOCATION_CLASS


def test_institutional_suffixes_type_organization():
    for name in ("Falkland Islands Legislative Assembly",
                 "National Election Committee",
                 "State Audit Agency",
                 "Federal Communications Commission",
                 "European Court of Justice"):
        _, cls = canonicalize_entity(name, "person")
        assert cls == ORGANIZATION_CLASS, name


def test_place_feature_suffixes_type_location():
    for name in ("Robertson Quay", "CITIC Tower", "Falkland Islands",
                 "Sentosa Island", "Marina Bay", "Changi Airport"):
        _, cls = canonicalize_entity(name, "person")
        assert cls == LOCATION_CLASS, name


# ---------------------------------------------------------------------------
# Regression guards — conservative boundaries the widening MUST preserve
# ---------------------------------------------------------------------------

def test_person_with_org_surname_stays_person():
    # The 2-token given-name guard must keep a real person.
    _, cls = canonicalize_entity("Michelle Steel", "person")
    assert cls == "person"


def test_trump_administration_stays_person_phrase():
    # "administration" deliberately excluded from the institutional suffixes —
    # the conservative no-over-merge contract.
    name, cls = canonicalize_entity("The Trump administration", "person")
    assert name == "The Trump administration"
    assert cls == "person"


def test_country_outranks_place_and_org():
    # A country name forced to COUNTRY even when an org/place surface could
    # otherwise fire (priority country > org > location).
    _, cls = canonicalize_entity("United States", "person")
    assert cls == COUNTRY_CLASS


def test_lone_feature_token_is_not_a_place():
    # A bare feature word alone is not a place (would over-type real surnames).
    assert is_place_surface("Tower") is False
    assert is_place_surface("Bridge") is False


def test_widened_classes_are_taxonomy_members():
    for cls in (COUNTRY_CLASS, ORGANIZATION_CLASS, LOCATION_CLASS):
        assert cls in ENTITY_CLASSES
