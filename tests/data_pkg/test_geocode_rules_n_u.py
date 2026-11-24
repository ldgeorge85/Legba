# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lane N-U — the person-name guard keeps country names inside mislabelled
institution / place spans; Britain / Great Britain; the US state Georgia; and
the (flagged-off) demonym evidence tier.

Live (roll 74, 7-day re-tag): 56 of 1,038 changes dropped a country the title
plainly names because the NER typed "US House", "Panama Canal", "Government of
Liberia", "Democratic Republic of Congo", "McDonald's Australia" as person.
"""

from __future__ import annotations

import pytest

from legba.data.filters import _geocode_rules as rules
from legba.data.filters._geocode_rules import (
    first_demonym_iso2,
    is_personal_name_span,
    mask_person_names,
)
from legba.data.filters.geocode import (
    _COUNTRY_NAME_INDEX,
    _DEMONYM_INDEX,
    GeocodeConfig,
    GeocodeHandler,
    country_iso2s_in_text,
    extract_country_iso2_from_text,
)
from legba.data.sources._contract import Signal


def _person(*names: str) -> list[dict[str, str]]:
    return [{"class": "person", "text": n} for n in names]


# --- the person-name guard: mislabelled institutions / places keep their country


@pytest.mark.parametrize(("title", "span", "iso"), [
    ("US House blocks bid to impeach Defence Secretary Pete Hegseth", "US House", "US"),
    ("Panama Canal turns the taps back on", "Panama Canal", "PA"),
    ("Liberia: Govt Threatens to Demolish Abandoned Buildings", "Government of Liberia", "LR"),
    ("Alphamin directs tin from the Democratic Republic of Congo to a new smelter",
     "Democratic Republic of Congo", "CD"),
    ("Mona Vale home bought by McDonald's Australia boss", "McDonald's Australia", "AU"),
    ("Babcock and Saab Denmark sign strategic agreement", "Saab Denmark", "DK"),
    ("Bezzecchi wins Qatar MotoGP", "Qatar MotoGP", "QA"),
    ("10 Police, 18 Militants Killed in Pakistan's Northwest", "Pakistan's Northwest", "PK"),
    ("Rubio warns staff as U.S. Republicans look to 2028", "U.S. Republicans", "US"),
    ("Federal Government of Nigeria urges technology transfer",
     "Federal Government of Nigeria", "NG"),
])
def test_an_institution_or_place_labelled_person_keeps_its_country(title, span, iso):
    assert not is_personal_name_span(span, _COUNTRY_NAME_INDEX)
    assert extract_country_iso2_from_text(title, entities=_person(span)) == iso


@pytest.mark.parametrize("span", [
    "Jim Jordan", "Chad Smith", "Israel Adesanya", "Georgia Meloni", "Kathy Ireland",
    "Lisa Monaco", "Cuba Gooding",
])
def test_a_real_person_named_after_a_country_is_still_masked(span):
    assert is_personal_name_span(span, _COUNTRY_NAME_INDEX)
    assert extract_country_iso2_from_text(
        f"{span} speaks to reporters", entities=_person(span)) is None


@pytest.mark.parametrize("span", ["Israel Police", "Jordan Valley", "Georgia Tech", "India Today"])
def test_a_homonym_country_inside_an_institution_is_not_a_person(span):
    assert not is_personal_name_span(span, _COUNTRY_NAME_INDEX)


def test_a_person_span_naming_no_country_is_masked_as_before():
    # Nothing to protect: blanking it cannot cost a country (byte-identical).
    for span in ("Pete Hegseth", "SK Hynix", "GTA VI", "MK"):
        assert is_personal_name_span(span, _COUNTRY_NAME_INDEX), span
    text = "Rockstar delays GTA VI again"
    assert extract_country_iso2_from_text(text, entities=_person("GTA VI")) is None


def test_the_original_jim_jordan_cases_hold():
    ents = _person("Jim Jordan")
    title = "Republicans elect Jim Jordan as House minority leader after midterm defeats"
    assert extract_country_iso2_from_text(title, entities=ents) is None
    assert extract_country_iso2_from_text(
        "Jim Jordan won. Republicans pick Jordan for the last two years.", entities=ents) is None
    assert extract_country_iso2_from_text("Jim Jordan met the King of Jordan", entities=ents) == "JO"
    assert country_iso2s_in_text("Jim Jordan and Iran", entities=ents) == {"IR"}


def test_masking_keeps_offsets_when_a_span_is_kept():
    text = "US House and Jim Jordan"
    out = mask_person_names(text, _person("US House", "Jim Jordan"),
                            country_names=_COUNTRY_NAME_INDEX)
    assert len(out) == len(text) and out.startswith("US House") and "Jordan" not in out


# --- Britain -----------------------------------------------------------------------


@pytest.mark.parametrize("title", [
    "Britain says its bases are safe after US refuelling planes evacuated",
    "Starring Great Britain: how set-jetting spreads tourists",
])
def test_britain_and_great_britain_are_gb(title):
    assert extract_country_iso2_from_text(title) == "GB"


def test_new_britain_is_not_gb():
    assert extract_country_iso2_from_text("Police in New Britain, Connecticut, arrest two") is None


# --- Georgia the US state ------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Freeze Watch for Chatham County, Georgia and Beaufort County, South Carolina",
    "The Army is testing self-driving vehicles at bases in Georgia and Texas",
    "Memory card with uncounted ballots found in Atlanta, Georgia House race",
])
def test_georgia_with_us_state_context_is_us(text):
    assert extract_country_iso2_from_text(text) == "US"
    assert "GE" not in country_iso2s_in_text(text)


@pytest.mark.parametrize("text", [
    "Georgia's parliament passes foreign agents law",
    "Protesters fill Tbilisi as Georgia's ruling party wins",
    "Georgia and Texas? No: Tbilisi says Georgia will not join",   # both cues: default
])
def test_georgia_without_us_state_context_stays_ge(text):
    assert extract_country_iso2_from_text(text) == "GE"


def test_a_us_declared_source_does_not_add_ge_for_the_state():
    h = GeocodeHandler(GeocodeConfig(nominatim_url="http://offline"))
    payload = {"title": "Flood Watch issued for Chatham County, Georgia by NWS Charleston SC",
               "entities": [{"class": "location", "text": "Georgia"}]}
    assert "GE" not in h.subject_country_iso2s(payload)


def test_a_georgia_entity_queries_the_us_with_state_context():
    h = GeocodeHandler(GeocodeConfig(nominatim_url="http://offline"))
    sig = Signal(source_id="s", payload={
        "title": "Storms hit Fulton County, Georgia",
        "entities": [{"class": "country", "text": "Georgia"}]})
    assert h._derive_candidates(sig)[0] == "United States"


# --- demonyms: low-precedence, title/lead only, flagged off ---------------------------


def test_the_demonym_rule_ships_off():
    assert rules.DEMONYM_LEAD_EVIDENCE is False


@pytest.mark.parametrize(("text", "iso"), [
    ("Russian strikes hit Kharkiv overnight", "RU"),
    ("North Korean troops seen near the border", "KP"),
    ("Iranian officials deny the report", "IR"),
    ("Chinese coast guard rams a supply boat", "CN"),
])
def test_a_capitalized_demonym_names_its_country(text, iso):
    assert first_demonym_iso2(text, _DEMONYM_INDEX, language="en") == iso


@pytest.mark.parametrize("text", [
    "Recipe: the perfect Russian dressing",
    "Chinese New Year parade draws crowds",
    "American Airlines cancels flights",
    "Georgian townhouse sells for a record",       # excluded: the era / architecture
    "a chinese takeaway",                          # lower-case and a non-place phrase
    "Le président russe",                          # not English
])
def test_a_non_place_demonym_is_not_evidence(text):
    assert first_demonym_iso2(text, _DEMONYM_INDEX, language="en" if "russe" not in text
                              else "fr") is None


@pytest.fixture()
def demonyms_on(monkeypatch):
    monkeypatch.setattr(rules, "DEMONYM_LEAD_EVIDENCE", True)


def _cands(payload, **kw):
    h = GeocodeHandler(GeocodeConfig(nominatim_url="http://offline"))
    return h._derive_candidates(Signal(source_id="s", payload=payload), **kw)


def test_off_the_demonym_adds_nothing():
    assert _cands({"title": "Russian strikes hit a power plant"}) == []


def test_on_a_named_country_outranks_the_demonym(demonyms_on):
    cands = _cands({"title": "Russian strikes hit a power plant in Ukraine"})
    assert cands[0] == "Ukraine" and cands.index("Russian Federation") > 0


def test_on_a_demonym_only_tags_what_named_nothing(demonyms_on):
    assert _cands({"title": "Russian strikes hit a power plant"})[0] == "Russian Federation"


def test_on_a_declared_source_never_reads_a_demonym(demonyms_on):
    # subject_only (a declared country) stops before the demonym tier.
    assert _cands({"title": "Russian strikes hit a power plant"}, subject_only=True) == []


def test_on_a_demonym_deep_in_the_body_is_not_read(demonyms_on):
    body = ("Officials met on Tuesday. " * 30) + "A Russian delegation attended."
    assert _cands({"title": "Officials meet", "summary": body}) == []
