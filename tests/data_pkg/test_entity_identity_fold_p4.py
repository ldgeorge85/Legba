# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the DQ Phase-4 entity de-fragmentation canon changes.

Covers ``identity_fold`` (the class-agnostic dedup key that the merge migration
clusters on and the resolver caches on) plus the paired canon fixes that make it
stable: leading-article strip, zero-width removal, partial-tag residue handling,
region-adjective + guarded de-pluralization collapse, the ``palestine``
gazetteer add, and the extended org/place gazetteers.

Pure — no DB, no SLM, no network.
"""

from __future__ import annotations

import pytest

from legba.data._entity_canon import (
    _REGION_ADJECTIVE_MAP,
    canonicalize_entity,
    identity_fold,
    is_junk_entity,
)

# ---------------------------------------------------------------------------
# The required fold groups — every surface form of one referent folds equal.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "group",
    [
        # article strip + partial-tag residue: all fold to United Kingdom
        ["the United Kingdom", "United Kingdom", "the United Kingdom.</p"],
        # region-adjective + guarded de-pluralization: all fold to Africa
        ["African", "Africans", "Africa"],
        # demonym + plural -> United States
        ["Americans", "American", "United States", "US", "U.S.", "USA"],
        # residue-shaped variants of Iran fold onto Iran
        ["Iran</p", "/>Iranian", "Iran", "Iranian", "Iranians"],
        # zero-width variants fold onto the clean surface
        ["​World Cup", "World Cup", "‌World Cup", "world cup"],
        # bare name vs demonym -> Palestine
        ["Palestine", "Palestinian"],
        # curly/straight/spaced possessive + article all fold together
        ["the Strait of Hormuz", "Strait of Hormuz", "The Strait of Hormuz"],
    ],
)
def test_fold_group_collapses_to_one_key(group):
    keys = {identity_fold(s) for s in group}
    assert len(keys) == 1, f"{group} folded to {keys}"
    # and the key is a non-empty, alphanumeric-only, article-free token
    (key,) = keys
    assert key and key.isalnum()


def test_identity_fold_is_idempotent():
    for s in [
        "the United Kingdom", "African", "Africans", "Americans", "Iran</p",
        "/>Iranian", "​World Cup", "Palestine", "The Costa Rican",
        "Meloni", "Georgia", "US", "UN", "WHO", "the Strait of Hormuz",
    ]:
        assert identity_fold(s) == identity_fold(identity_fold(s)), s


def test_distinct_referents_do_not_fold():
    # Different real referents keep different fold keys.
    assert identity_fold("Iran") != identity_fold("Iraq")
    assert identity_fold("Meloni") != identity_fold("Macron")
    assert identity_fold("United States") != identity_fold("United Kingdom")


# ---------------------------------------------------------------------------
# "Iran</p" folds onto Iran AND is junk-shaped (so the write path drops it while
# the migration re-points the historical row) — the paired residue behavior.
# ---------------------------------------------------------------------------


def test_iran_residue_folds_but_is_junk_shaped():
    assert identity_fold("Iran</p") == identity_fold("Iran")
    assert is_junk_entity("Iran</p") is True          # partial-tag residue rejected
    assert is_junk_entity("/>Iranian") is True
    assert is_junk_entity("the Middle East.</p") is True
    assert is_junk_entity("Iran") is False


@pytest.mark.parametrize(
    "residue",
    ["Iran</p", "Tore Godal</strong", "St. James Parish</b", "/>Sharjah",
     "the U.S. Department of State < a", "<img src=x>", "foo&nbsp;bar"],
)
def test_partial_tag_residue_is_junk(residue):
    assert is_junk_entity(residue) is True, residue


# ---------------------------------------------------------------------------
# palestine types country (pycountry gap fix)
# ---------------------------------------------------------------------------


def test_palestine_types_country():
    for incoming in ("person", "entity", "location", "country"):
        name, cls = canonicalize_entity("Palestine", incoming)
        assert name == "Palestine"
        assert cls == "country", (incoming, cls)
    # the demonym resolves to the same bare form (folds together)
    assert canonicalize_entity("Palestinian", "person")[0] == "Palestine"


# ---------------------------------------------------------------------------
# region-adjective collapse -> continent (LOCATION), NOT country
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "adjective,continent",
    [("African", "Africa"), ("Africans", "Africa"), ("European", "Europe"),
     ("Asian", "Asia"), ("North American", "North America")],
)
def test_region_adjective_collapses_to_continent_location(adjective, continent):
    name, cls = canonicalize_entity(adjective, "person")
    assert name == continent
    assert cls == "location", (adjective, cls)


def test_region_map_excludes_ambiguous_adjectives():
    # Deliberately NOT collapsed (ambiguous multi-region adjectives).
    for ambiguous in ("arab", "latin", "western", "eastern", "scandinavian"):
        assert ambiguous not in _REGION_ADJECTIVE_MAP


def test_guarded_depluralization_never_stems_arbitrary_names():
    # A plural that is NOT a demonym/region singular is left untouched.
    for name in ("Analysts", "Forces", "Systems", "Prices"):
        out, _ = canonicalize_entity(name, "entity")
        assert out == name, out


# ---------------------------------------------------------------------------
# short orgs / abbreviations survive (never junk-dropped by the length rule)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("legit", ["US", "UK", "EU", "UN", "WHO", "NATO"])
def test_short_orgs_survive(legit):
    assert is_junk_entity(legit) is False, legit
    name, _ = canonicalize_entity(legit, "organization")
    assert name and name != ""


def test_us_un_type_correctly():
    assert canonicalize_entity("US", "person") == ("United States", "country")
    assert canonicalize_entity("UN", "person") == ("United Nations", "organization")


# ---------------------------------------------------------------------------
# "The Trump administration" + surnames are NOT collapsed (no over-merge)
# ---------------------------------------------------------------------------


def test_trump_administration_and_surnames_preserved():
    assert canonicalize_entity("The Trump administration", "person") == (
        "The Trump administration", "person")
    assert canonicalize_entity("Meloni", "person") == ("Meloni", "person")


# ---------------------------------------------------------------------------
# extended org / place gazetteers (person-contamination fix)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "org",
    ["225th Separate Assault Regiment", "United Cajun Navy", "Republican Guard",
     "Islamic Revolutionary Guard Corps"],
)
def test_military_org_surfaces_type_organization(org):
    _name, cls = canonicalize_entity(org, "person")
    assert cls == "organization", (org, cls)


@pytest.mark.parametrize(
    "place",
    ["Temple of Apollo", "Mount Erciyes", "Fort Bragg", "Blue Mosque"],
)
def test_place_head_surfaces_type_location(place):
    _name, cls = canonicalize_entity(place, "person")
    assert cls == "location", (place, cls)


# ---------------------------------------------------------------------------
# extended quantifier reject
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "junk", ["an Estimated $ 1.8 Billion", "estimated 40,000", "an estimated 12"]
)
def test_estimated_quantifier_rejected(junk):
    assert is_junk_entity(junk) is True, junk


# ---------------------------------------------------------------------------
# articles / stopwords are junk (DQ P4 §E — "the" must never be a fold survivor)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "word",
    ["the", "The", "THE", "a", "an", "and", "or", "but", "of", "for",
     "from", "with", "into", "onto"],
)
def test_articles_and_stopwords_are_junk(word):
    assert is_junk_entity(word) is True, word


def test_stopword_folds_are_content_junk_not_survivors():
    # A bare "the" folds to a stable key but is content-junk (len 3 dodges the
    # length<=2 rule) — so the generator junks it and never elects it a survivor.
    from legba.data._entity_canon import _strip_name
    assert identity_fold("the") == identity_fold("The")
    for w in ("the", "and", "from"):
        assert is_junk_entity(w) is True
        # content-junk shape: a CONTENT rule fired (not just length<=2).
        assert len(_strip_name(w)) > 2 or w in ("a", "an", "of")


def test_world_cup_stays_junk_sports_gate():
    # E: confirm the sports gate still holds after the stopword add.
    assert is_junk_entity("World Cup") is True
    assert is_junk_entity("​World Cup") is True  # zero-width variant too


def test_legit_short_forms_still_survive_stopword_gate():
    # The stopword add must not swallow real short forms exempted FIRST.
    for legit in ("US", "UK", "EU", "UN", "WHO", "NATO"):
        assert is_junk_entity(legit) is False, legit


# ---------------------------------------------------------------------------
# B0-7 (2026-07-10) — markdown / URL residue gate + fold, and the overlong cap.
# Telegram payload.text carried raw markdown ("[**title**](url)") into
# /extract; NER minted 115 junk profiles wearing the syntax (live exemplar:
# "Ayatollah Ali Khamenei**](https://f24.my"). Paired behavior mirrors the
# HTML-residue class: the write path REJECTS the residue span, while the fold
# re-points a historical junk row onto its clean survivor at the next merge.
# ---------------------------------------------------------------------------


def test_b07_live_exemplar_is_junk_and_folds_onto_clean_survivor():
    exemplar = "Ayatollah Ali Khamenei**](https://f24.my"
    assert is_junk_entity(exemplar) is True
    assert identity_fold(exemplar) == identity_fold("Ayatollah Ali Khamenei")
    assert is_junk_entity("Ayatollah Ali Khamenei") is False


@pytest.mark.parametrize(
    "residue",
    [
        "Ayatollah Ali Khamenei**](https://f24.my",   # the live exemplar
        "see https://f24.my/C2Ba.g",                   # URL scheme anywhere
        "http://example.com",                          # bare http URL
        "Khamenei](https://x.y)",                      # link-syntax residue
        "Ali **Khamenei",                              # bold residue
        "Reuters | World News",                        # pipe / table residue
        "Iran\nbegins funeral",                        # embedded newline
        "Iran\rbegins funeral",                        # carriage return
    ],
)
def test_b07_markdown_url_residue_is_junk(residue):
    assert is_junk_entity(residue) is True, residue


def test_b07_overlong_cap_rejects_over_120_chars():
    # Cap raised 80→120 by the adversarial review: genuine treaty/UN-body FULL
    # names run long (UNRWA official name = 82 chars, the Hague cultural-
    # property convention = 91, longest live legit = 118) while the live junk
    # band starts at 153 (153/183/688-char swallowed sentences).
    assert is_junk_entity("A" + "b" * 120) is True   # 121 chars — junk band
    assert is_junk_entity("A" + "b" * 78) is False   # 79 chars
    # Genuine long referents the 80-cap would have silently frozen out
    # platform-wide (live rows, caught by the review) must PASS the gate:
    assert is_junk_entity(
        "the United Nations Relief and Works Agency for Palestine "
        "Refugees in the Near East") is False        # 82 chars — UNRWA
    assert is_junk_entity(
        "the Hague Convention for the Protection of Cultural Property "
        "in the Event of Armed Conflict") is False   # 91 chars — treaty
    # The longest real country official name stays comfortably under the cap.
    assert is_junk_entity(
        "United Kingdom of Great Britain and Northern Ireland") is False


@pytest.mark.parametrize(
    "legit", ["O'Brien", "AT&T", "Ali Khamenei", "Ayatollah Ali Khamenei"]
)
def test_b07_legitimate_names_still_pass_the_gate(legit):
    assert is_junk_entity(legit) is False, legit


def test_b07_full_markdown_link_wrapper_folds_to_inner_text():
    # "[**Ali Khamenei**](https://x.y)" folds onto the clean referent key.
    wrapped = "[**Ali Khamenei**](https://x.y)"
    assert identity_fold(wrapped) == identity_fold("Ali Khamenei")
    assert identity_fold(wrapped) == "alikhamenei"
    # ... and stays junk-shaped at write time (never minted forward).
    assert is_junk_entity(wrapped) is True


def test_b07_markdown_fold_is_idempotent():
    for s in [
        "[**Ali Khamenei**](https://x.y)",
        "Ayatollah Ali Khamenei**](https://f24.my",
        "Ali **Khamenei",
    ]:
        assert identity_fold(s) == identity_fold(identity_fold(s)), s
