# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure (no-DB) unit tests for the DQ M1/M2/M3 fact-write gates (2026-07-06 audit).

The live audit found the ingestion relation-extractor still laundering three
classes the earlier D6/P5 gates were not scoped for:

  * M1 — semantically absurd / relation-direction-inverted membership
    ("NATO member of Turkiye" inverted; "Russia member of <person>" bad object);
  * M2 — bare demonym / relative-temporal-phrase SUBJECTS ("Chinese founded by
    Jin Mingri", "250 years ago founded by …", "December last year …");
  * M3 — nationality-adjective VALUES that become false geographic facts
    ("Kyiv capital of Russian", "US conflict with Iranian").

No database, no network — only the pure gate helpers, the handler's
``_d6_drop_reason`` static method, and the shared canon's ``is_junk_entity``
(now relative-temporal-phrase aware, which the fact gate calls on both
endpoints).
"""

from __future__ import annotations

import pytest

from legba.data._entity_canon import is_demonym, is_junk_entity
from legba.data.filters.fact_extractor import (
    FactExtractorHandler,
    _is_inverted_membership,
    _is_member_part_person_object,
    _normalize_demonym_value,
)
from legba.data.vocabulary import normalize_predicate


def _norm_pred(predicate: str) -> str:
    """Mirror the write-loop predicate normalization."""
    return normalize_predicate(str(predicate).strip().lower())


def _d6(subject: str, predicate: str, value: str) -> str | None:
    """The full D6 gate (predicate normalized as the write loop does)."""
    return FactExtractorHandler._d6_drop_reason(subject, _norm_pred(predicate), value)


# ===========================================================================
# M1 — predicate-argument TYPE + relation-DIRECTION gate
# ===========================================================================


@pytest.mark.parametrize(
    "subject,predicate,value",
    [
        ("NATO", "member of", "Turkiye"),   # org member of country -> inverted
        ("UN", "member of", "Iran"),         # the country is the member of the org
        ("EU", "member of", "France"),
    ],
)
def test_m1_inverted_membership_dropped(subject, predicate, value):
    """An ORGANIZATION is not a 'member of' a COUNTRY — the direction is
    inverted and the triple is dropped with the inverted_membership reason."""
    assert _is_inverted_membership(subject, _norm_pred(predicate), value) is True
    assert _d6(subject, predicate, value) == "inverted_membership"


@pytest.mark.parametrize(
    "subject,predicate,value",
    [
        ("Germany", "member of", "NATO"),           # country -> org: correct
        ("Hungary", "member of", "EU"),
    ],
)
def test_m1_correct_membership_direction_kept(subject, predicate, value):
    """The RIGHT direction (a country is a member of an org) survives the
    inverted-membership + type gates."""
    assert _is_inverted_membership(subject, _norm_pred(predicate), value) is False
    assert _d6(subject, predicate, value) is None


@pytest.mark.parametrize(
    "subject,predicate,value",
    [
        # A recognised org / country / place object is a VALID containment
        # target — NOT treated as a person even when NER mis-types it person.
        ("Nigeria", "member of", "African Union"),   # multi-word org (NER->person)
        ("Sylvia Lim", "member of", "Parliament"),   # institution word
        ("Sciver - Brunt", "member of", "England"),  # country object
    ],
)
def test_m1_membership_org_place_object_not_person(subject, predicate, value):
    """The person-object gate is gazetteer-guarded: a legit org/place/country
    object is never dropped as a 'person object' (no over-reject)."""
    assert _is_member_part_person_object(subject, _norm_pred(predicate), value) is False


@pytest.mark.parametrize(
    "subject,predicate,value",
    [
        ("Russia", "member of", "Emmanuel Macron"),
        ("Russia", "part of", "Emmanuel Macron"),
    ],
)
def test_m1_member_part_person_object_dropped(subject, predicate, value):
    """'member of'/'part of' with a clear PERSON object (object must be an
    org/place) is a mis-extraction. The gazetteer-guarded person-object gate
    fires on it, and the write pipeline DROPS it (the broad NER person heuristic
    means the existing roster gate usually tags it first — either way it is
    rejected, which is what M1 requires)."""
    assert _is_member_part_person_object(subject, _norm_pred(predicate), value) is True
    assert _d6(subject, predicate, value) is not None


def test_m1_member_part_person_gate_is_predicate_scoped():
    """The person-object gate fires ONLY for 'member of'/'part of' — an
    employment/other relation with a person object is not its concern."""
    assert _is_member_part_person_object("Taylor Swift", "employed by", "George Stephanopoulos") is False
    assert _is_member_part_person_object("Russia", "conflict with", "Emmanuel Macron") is False


def test_m1_quantity_object_dropped_as_junk():
    """A quantity/number object under a membership predicate ("Russia member of
    188,000 barrels") is caught by the shared is_junk_entity gate."""
    assert is_junk_entity("188,000 barrels") is True
    assert _d6("Russia", "member of", "188,000 barrels") == "junk_entity"


# ===========================================================================
# M2 — demonym / relative-temporal-phrase SUBJECT
# ===========================================================================


@pytest.mark.parametrize(
    "subject,predicate,value",
    [
        ("Chinese", "founded by", "Jin Mingri"),
        ("Ukrainian", "conflict with", "Russia"),
        ("Afghan", "border with", "Pakistan"),
        ("American", "employed by", "Rodriguez"),
        ("Swiss", "conflict with", "Canada"),
    ],
)
def test_m2_demonym_subject_dropped(subject, predicate, value):
    """A bare national demonym SUBJECT is a nationality adjective, not a named
    entity — dropped with the demonym_subject reason."""
    assert is_demonym(subject) is True
    assert _d6(subject, predicate, value) == "demonym_subject"


@pytest.mark.parametrize(
    "subject",
    [
        "250 years ago", "35 years ago", "last year", "last week",
        "December last year", "the 21st century", "past 24 hours",
        "next month",
    ],
)
def test_m2_relative_temporal_subject_is_junk(subject):
    """A relative-temporal-phrase SUBJECT is caught by is_junk_entity (the
    shared canon is now relative-phrase aware) and dropped by the fact gate."""
    assert is_junk_entity(subject) is True
    assert _d6(subject, "sanctioned by", "Marine Le Pen") == "junk_entity"


@pytest.mark.parametrize(
    "surface",
    ["March", "May", "August", "Norway", "Theresa May", "March on Washington"],
)
def test_m2_bare_month_or_name_not_temporal_junk(surface):
    """CONSERVATIVE: a bare month name or a real name containing a month is NOT
    flagged temporal (no over-reject; a month is junk only combined with a
    temporal modifier / year)."""
    assert is_junk_entity(surface) is False


# ===========================================================================
# M3 — nationality-adjective VALUE normalized to the country/continent lemma
# ===========================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Russian", "Russia"),
        ("Iranian", "Iran"),
        ("American", "United States"),
        ("Chinese", "China"),
        ("African", "Africa"),           # region adjective -> continent
        ("European", "Europe"),
        ("South American", "South America"),
    ],
)
def test_m3_demonym_value_normalized(raw, expected):
    """A nationality / region adjective VALUE collapses to its canonical
    country / continent lemma before the write ("Russian" -> "Russia") — under a
    GEO / relational predicate (here "located in")."""
    assert _normalize_demonym_value("located in", raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["Russia", "Ukraine", "Paris", "Emmanuel Macron", "United Nations",
     "NATO", "Europe"],
)
def test_m3_non_adjective_value_untouched(raw):
    """CONSERVATIVE: only a curated demonym / region adjective is normalized;
    every other value (a country, city, person, org, bare continent) is left
    byte-identical (no broad canonicalization of the write path)."""
    assert _normalize_demonym_value("located in", raw) == raw


# --- F2 (2026-07-06 review): normalization is SCOPED to geo/relational preds ---


@pytest.mark.parametrize(
    "predicate,raw,expected",
    [
        ("capital of", "Russian", "Russia"),       # geo -> normalized
        ("conflict with", "Iranian", "Iran"),
        ("part of", "American", "United States"),
        ("member of", "Chinese", "China"),
        ("sanctioned by", "Russian", "Russia"),
    ],
)
def test_f2_geo_predicate_value_normalized(predicate, raw, expected):
    assert _normalize_demonym_value(predicate, raw) == expected


@pytest.mark.parametrize(
    "predicate,raw",
    [
        ("speaks", "Russian"),           # LANGUAGE — keep the adjective
        ("native language", "Chinese"),
        ("ethnic group", "Russian"),
        ("written in", "Russian"),
        ("language", "French"),
        ("citizenship", "German"),
        # person-agentive relations: the object is a person/org, not the country
        ("founded by", "Russian"),
        ("employed by", "Afghan"),
    ],
)
def test_f2_non_geo_predicate_value_untouched(predicate, raw):
    """A demonym VALUE under a language / ethnicity / agentive predicate is the
    LANGUAGE / PEOPLE, not the country — it must be left byte-identical (an
    unlisted predicate never normalizes, so no silent corruption)."""
    assert _normalize_demonym_value(predicate, raw) == raw


# ===========================================================================
# F1 — the roster gate must NOT delete real IGO / bloc membership facts
# ===========================================================================


@pytest.mark.parametrize(
    "subject,value",
    [
        ("France", "European Union"),
        ("Nigeria", "African Union"),
        ("South Korea", "United Nations"),
        ("Germany", "European Union"),
        ("Brazil", "World Trade Organization"),
        ("Kenya", "East African Community"),     # 'community' org suffix
        ("Fiji", "Pacific Islands Forum"),       # 'forum' org suffix
    ],
)
def test_f1_igo_membership_kept(subject, value):
    """A country 'member of' an IGO / alliance / bloc is a real fact — the
    organization value is exempt from the roster gate (canon org typing is
    authoritative over the noisy NER person heuristic)."""
    from legba.data.filters.fact_extractor import _is_roster_triple
    assert _is_roster_triple(subject, "member of", value) is False
    assert _d6(subject, "member of", value) is None


@pytest.mark.parametrize(
    "subject,value",
    [
        ("Harry Kane", "Jude Bellingham"),   # person -> person: genuine roster
        ("Kylian Mbappe", "Iraq"),           # person -> squad-as-country
    ],
)
def test_f1_genuine_sports_roster_still_dropped(subject, value):
    """The org exemption must NOT reopen the genuine sports-roster noise."""
    from legba.data.filters.fact_extractor import _is_roster_triple
    assert _is_roster_triple(subject, "member of", value) is True
    assert _d6(subject, "member of", value) == "sports_roster_triple"


# ===========================================================================
# F4 — named calendar dates are NOT temporal junk
# ===========================================================================


@pytest.mark.parametrize(
    "surface",
    ["September 11", "October 7", "July 4", "March 2022", "November 2024",
     "Black September", "March on Washington", "May", "August", "December"],
)
def test_f4_named_date_or_bare_month_kept(surface):
    """A named calendar DATE / event ("September 11", "October 7", "March
    2022") and a bare month / month-name are date/event references, NOT
    temporal junk — they survive is_junk_entity."""
    assert is_junk_entity(surface) is False


@pytest.mark.parametrize(
    "surface",
    ["December last year", "last November", "250 years ago", "next month",
     "28 Days Later"],
)
def test_f4_relative_temporal_still_junk(surface):
    """A month/duration carrying a RELATIVE modifier ("December last year",
    "last November") stays temporal junk."""
    assert is_junk_entity(surface) is True
