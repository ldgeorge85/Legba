# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DQ M4/M5/M6 (2026-07-06 live audit) — entity write-path hardening.

Pure (no-DB, no-network) unit tests over the VERBATIM live cases the three
paired fixes exist to close:

  * **M4** — the entity pre-lookup was keyed on exact ``lower(canonical_name)``,
    blind to a leading article and to a keeper's ``merged_aliases``, so
    ingestion re-spawned competing rows ("the Strait of Hormuz" vs keeper
    "Strait of Hormuz"). :func:`lookup_key` is the article/case/whitespace
    normalization the alias/article-aware pre-lookup now applies to BOTH the
    incoming surface and the DB-side ``canonical_name`` / ``merged_aliases`` (it
    mirrors the SQL ``regexp_replace`` byte-for-byte), so the variant folds onto
    the keeper.
  * **M5** — junk classes the entity path never rejected: number+unit quantity
    ("188,000 barrels", "770 bln won", "four million euros"), a possessive
    KINSHIP referring expression ("Donald Trump's son"), and bare temporal
    surfaces ("last week", "the 21st century", "Today").
  * **M6** — the 29.5% default-``entity`` bucket: a curated geographic REGION is
    relabeled LOCATION ("the West Bank"), a sports TEAM / supranational-org
    acronym ORGANIZATION ("Minnesota Twins", "OPEC+") — conservatively, so a
    real person/org is NEVER mis-relabeled.
"""

from __future__ import annotations

import pytest

from legba.data._entity_canon import (
    canonicalize_entity,
    is_junk_entity,
    is_known_org_surface,
    is_region_surface,
    is_sports_team_surface,
    lookup_key,
)
from legba.data.analysts.deterministic_handlers.entity_resolution import (
    _fallback_class_compatible,
)


# ===========================================================================
# M4 — alias/article-aware lookup key
# ===========================================================================


@pytest.mark.parametrize(
    "variant,keeper",
    [
        ("the Strait of Hormuz", "Strait of Hormuz"),
        ("The White House", "White House"),
        ("the Middle East", "Middle East"),
        ("A Memorandum of Understanding", "Memorandum of Understanding"),
        ("  the   Axis  of   Resistance ", "Axis of Resistance"),  # ws + article
        ("THE Gaza Strip", "gaza strip"),                          # case + article
    ],
)
def test_article_case_ws_variants_fold_to_one_lookup_key(variant, keeper):
    assert lookup_key(variant) == lookup_key(keeper)
    # And the key itself is article-free / lower / single-spaced.
    assert lookup_key(variant) == lookup_key(variant).strip().lower()
    assert not lookup_key(variant).startswith(("the ", "a ", "an "))


def test_lookup_key_matches_sql_normalization_shape():
    # The Python key must equal what the pre-lookup's regexp_replace produces:
    # lower(btrim(x)) -> strip '^(the|a|an)\s+' -> collapse '\s+' to ' '.
    assert lookup_key("The   Coast Guard") == "coast guard"
    assert lookup_key("a week") == "week"
    assert lookup_key("an Embassy") == "embassy"


def test_lookup_key_guards_are_conservative():
    # The article strip requires a FOLLOWING token (mirrors the SQL
    # '^(the|a|an)\\s+'), so a bare "the" is left intact — never blanked into a
    # spurious empty key that would match everything.
    assert lookup_key("the") == "the"
    assert lookup_key("") == ""
    # "theater" must NOT be article-stripped (no space after "the").
    assert lookup_key("Theater") == "theater"
    # Distinct referents do NOT collide.
    assert lookup_key("North Korea") != lookup_key("South Korea")


# ===========================================================================
# M5 — number+unit / possessive-kinship / temporal junk
# ===========================================================================


@pytest.mark.parametrize(
    "junk",
    [
        # number + unit (the documented live misses)
        "188,000 barrels",
        "770 bln won",
        "four million euros",
        "10 million barrels",
        "70 bln euros",
        "seven tons",
        "115 acres",
        "1.34 million hectares",
        "55.3 million bpd",
        "20 M barrels",
        # possessive-kinship
        "Donald Trump's son",
        "Netanyahu's wife",
        "Putin’s daughter",
        # count-noun quantity requires a DIGIT (adversarial #4)
        "300 seats",
        "45 votes",
        # temporal / duration (modified phrases + clearly-temporal bare tokens)
        "last week",
        "next month",
        "the 21st century",
        "past 24 hours",
        "The past 100 days",
        "a century",
        "centuries",
        "yesterday",
        "morning",
        "midnight",
        "a year",
        "the day",
        "the third day",
        "2026",
    ],
)
def test_m5_junk_rejected(junk):
    assert is_junk_entity(junk) is True, junk


@pytest.mark.parametrize(
    "keep",
    [
        # a nominal token anywhere keeps a quantity-ish surface
        "Boeing 737",
        "Group of 20",
        "five US senators",
        "Boat People",
        # a real title with a generic possessive is NOT kinship-junk
        "The Battle for the World's Children",
        # real referents that superficially resemble a junk class
        "Seven Sisters",         # not "<num> <unit>" (sisters not a unit)
        "Sunday",                # a weekday, not in the temporal set
        "Independence Day",      # a named holiday, not bare "day"
        "United States",         # country (exempted)
        # adversarial #4 — number-WORD + count noun is a PLACE, not junk
        "Five Points",
        "Four Points",
        # adversarial #2 — brand-ambiguous BARE temporal tokens are kept
        "Today",                 # NBC show
        "Noon",                  # retailer
        "Century",               # Century Aluminum
        "Week",                  # The Week (bare)
        "Day",                   # The Day (bare)
    ],
)
def test_m5_conservative_keeps_real_entities(keep):
    assert is_junk_entity(keep) is False, keep


def test_m5_entity_path_drops_via_canonicalize_then_gate():
    # The write path canonicalizes then junk-gates; a possessive-kinship surface
    # survives canonicalization (no trailing 's to strip) but the gate drops it.
    name, _cls = canonicalize_entity("Donald Trump's son", "person")
    assert name == "Donald Trump's son"
    assert is_junk_entity(name) is True


# ===========================================================================
# M6 — conservative class relabel (regions -> location, teams/orgs -> org)
# ===========================================================================


@pytest.mark.parametrize(
    "region",
    ["the West Bank", "West Bank", "Gaza Strip", "the Middle East",
     "Horn of Africa", "Donbas", "Crimea", "West Coast"],
)
def test_m6_regions_typed_location(region):
    # A non-person NER class relabels to the region (the person-guard is #3).
    name, cls = canonicalize_entity(region, "entity")
    assert cls == "location", (region, cls)
    assert is_region_surface(region) is True


def test_m6_west_bank_region_beats_bank_org_suffix():
    # "the West Bank" ends in the org-suffix token "bank"; the region gazetteer
    # must win so it types location, NOT organization.
    _name, cls = canonicalize_entity("the West Bank", "organization")
    assert cls == "location"


@pytest.mark.parametrize(
    "team", ["Minnesota Twins", "Boston Celtics", "Manchester United",
             "Green Bay Packers"],
)
def test_m6_sports_teams_typed_org(team):
    name, cls = canonicalize_entity(team, "person")
    assert cls == "organization", (team, cls)
    assert is_sports_team_surface(team) is True


@pytest.mark.parametrize("org", ["OPEC", "OPEC+", "NATO", "ASEAN", "Interpol"])
def test_m6_known_org_acronyms_typed_org(org):
    name, cls = canonicalize_entity(org, "person")
    assert cls == "organization", (org, cls)
    assert is_known_org_surface(org) is True


@pytest.mark.parametrize(
    "not_relabeled,expected_cls",
    [
        ("Vladimir Putin", "person"),      # a real person stays person
        ("Giorgia Meloni", "person"),
        ("Bank of England", "organization"),  # a genuine org stays org
        ("Evian", "location"),             # an unknown place keeps its class
        ("The Trump administration", "person"),  # curated no-over-merge case
    ],
)
def test_m6_conservative_no_mis_relabel(not_relabeled, expected_cls):
    _name, cls = canonicalize_entity(not_relabeled, expected_cls)
    assert cls == expected_cls, (not_relabeled, cls)


def test_m6_region_and_team_helpers_reject_non_members():
    assert is_region_surface("Vladimir Putin") is False
    assert is_region_surface("Bank of England") is False
    assert is_sports_team_surface("Minnesota") is False   # bare city, not a team
    assert is_sports_team_surface("twins") is False       # bare nickname
    assert is_known_org_surface("Iran") is False


# ===========================================================================
# Adversarial #3 — region gazetteer must NOT downgrade a confident PERSON
# ===========================================================================


@pytest.mark.parametrize("surname", ["Golan", "Levant", "Sinai", "Anatolia"])
def test_region_token_typed_person_stays_person(surname):
    # These region tokens are also real surnames (Menahem Golan, Oscar Levant);
    # a mention NER typed 'person' must NEVER be relabeled to location.
    _name, cls = canonicalize_entity(surname, "person")
    assert cls == "person", (surname, cls)


@pytest.mark.parametrize("region", ["Golan", "Levant", "Sinai", "Anatolia"])
def test_region_token_non_person_still_types_location(region):
    # A non-person mention of the SAME token still resolves to the region.
    _name, cls = canonicalize_entity(region, "location")
    assert cls == "location", (region, cls)
    _name, cls2 = canonicalize_entity(region, "entity")
    assert cls2 == "location", (region, cls2)


# ===========================================================================
# Adversarial #1 — fallback class-compatibility gate (never merge distinct
# referents that merely normalize the same; never mutate a keeper's class)
# ===========================================================================


@pytest.mark.parametrize(
    "stored_cls,cls,compatible",
    [
        ("organization", "organization", True),   # same class -> adopt
        ("organization", "corporation", True),     # org sub-type -> adopt
        ("corporation", "organization", True),
        ("location", "organization", False),       # "Atlantic" ocean vs magazine
        ("organization", "location", False),
        ("location", "person", False),             # person fallback never adopts location
        ("person", "location", False),
        ("country", "person", False),
        ("entity", "organization", False),
    ],
)
def test_fallback_class_compatibility(stored_cls, cls, compatible):
    assert _fallback_class_compatible(stored_cls, cls) is compatible, (stored_cls, cls)


def test_atlantic_org_incompatible_with_ocean_location():
    # The BLOCKER counterexample: "the Atlantic" (organization) normalizes to the
    # same key as the "Atlantic" ocean (location), but the classes are
    # incompatible, so the fallback keeps them DISTINCT (no merge, no class
    # mutation of the ocean row).
    assert _fallback_class_compatible("location", "organization") is False
    assert _fallback_class_compatible("location", "location") is True
