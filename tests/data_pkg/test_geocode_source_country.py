# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The geocoder must not override the country a source DECLARES.

Live 11-13, five new official sources: 5 of 17 new signals mis-tagged —
647b9356 / cc81c011 (BIS export-control notices) FR off the Federal Register
citation "91 FR 60505"; 4872a035 (the US "Declaration of Freedom in Space")
FR off an NER location "Space" the backend put in Paris; ffe8ebeb / e8314cbe
(BMVg) BA / EE off image-hash hex in the RSS ``<img src>``; plus b0bc4417 GI
("GI Breuer" = Generalinspekteur) and c16f3717 BD (hex again).
"""

from __future__ import annotations

import pycountry
import pytest

from legba.data.filters import _geo_precedence as prec
from legba.data.filters._contract import FilterContext
from legba.data.filters._geocode_rules import (
    abbreviation_collisions,
    blank_markup,
    is_common_word_name,
    is_non_place_surface,
)
from legba.data.filters.geocode import (
    GeocodeConfig,
    GeocodeHandler,
    GeocodeResult,
    _COUNTRY_NAME_INDEX,
    country_iso2s_in_text,
    extract_country_iso2_from_text,
    geocoder_query_name,
)
from legba.data.sources._contract import Signal

_IMG_BA = ('<img alt="Der Bau von Kasernen" height="113" src="https://www.bmvg.de/resource/'
           'image/6165386/landscape_ratio16x9/200/113/1dc97adfe3a3d413d3d72afbf6e9dead/'
           '32CBAA36A2A7FDD56BA9105E76AFE320/gebaeude.jpg" width="200" />')
_IMG_EE = ('<img alt="Weitere Beschaffung" src="https://www.bmvg.de/resource/image/6172082/'
           'f3fb7d5d89a28222b9c510f209bd72c1/347A1EE0B560FD4657ED5AD80FE130DB/server.jpg" />')
_IMG_BD = ('<img src="https://www.bmvg.de/resource/image/146926/6173c7e0d6d87b2eb59a5d583356b092/'
           '6E10B4FC4BD8FBD13990904CAF2A8407/mehrere-zeitungen.jpg" />')
_BIS_BODY = ("The Department of Commerce, in accordance with the Paperwork Reduction Act "
             "of 1995 (PRA), invites the general public to comment. The rule "
             "\"Section 232 National Security Adjustments of Imports\" "
             "(September 24, 2026, 91 FR 60505).")


class _Backend:
    """Country names resolve; a few places resolve the way Nominatim did."""

    name = "stub"
    PLACES = {"Space": "FR", "Berlin": "DE", "Tehran": "IR"}

    def __init__(self):
        self.queries: list[str] = []

    async def geocode(self, query):
        self.queries.append(query)
        iso = _COUNTRY_NAME_INDEX.get(query.lower()) or self.PLACES.get(query)
        if iso is None:
            try:
                iso = pycountry.countries.lookup(query).alpha_2
            except LookupError:
                return None
        return GeocodeResult(country=query, country_iso2=iso, country_iso3=None,
                             region=None, municipality=None, address=None,
                             lat=1.0, lon=2.0, precision="country", source="stub")

    async def reachable(self):
        return True


def _ctx(source_class="official", scope_geo=("US",), languages=("en",)):
    return FilterContext(target_id="s", scope_geo=list(scope_geo),
                         scope_languages=list(languages), source_class=source_class)


async def _run(payload, *, source_id="source.x", ctx=None, url=None):
    backend = _Backend()
    h = GeocodeHandler(GeocodeConfig(precision="country"), backend=backend)
    out = await h.transform(Signal(source_id=source_id, payload=payload,
                                   canonical_url=url), ctx or _ctx())
    return out.payload.get("geo"), backend


# --- token rules ------------------------------------------------------------


def test_federal_register_citation_is_not_france():
    assert extract_country_iso2_from_text(_BIS_BODY, language="en") is None
    assert "FR" in abbreviation_collisions("en")


def test_generalinspekteur_is_not_gibraltar_in_german():
    title = "Dunkelfeldstudie: Statement von Minister Pistorius und GI Breuer"
    assert extract_country_iso2_from_text(title, language="de") is None
    # The rule is the source LANGUAGE's: English copy keeps its own table.
    assert "GI" not in abbreviation_collisions("en")


@pytest.mark.parametrize("markup", [_IMG_BA, _IMG_EE, _IMG_BD])
def test_image_hash_hex_never_geocodes(markup):
    assert country_iso2s_in_text(markup + " Mit neuen Regelungen", language="de") == set()


def test_a_digit_bounds_a_code():
    assert country_iso2s_in_text("an MQ-9 drone and GLP-1 drugs, hash 56BA9105E") == set()
    assert country_iso2s_in_text("talks in US, then GBR") == {"US", "GB"}


def test_sdn_list_is_not_sudan_and_spanish_ee_uu_is_the_us():
    assert country_iso2s_in_text("placed on the SDN List", language="en") == set()
    assert extract_country_iso2_from_text("EE. UU. vuelve a la carga", language="es") == "US"


def test_markup_is_lowercased_not_erased_so_a_slug_name_still_counts():
    text = '<a href="https://www.theguardian.com/australia-news/2026/nov/1">x</a>'
    assert len(blank_markup(text)) == len(text)
    assert country_iso2s_in_text(text) == {"AU"}


def test_common_word_names_need_a_capital():
    assert is_common_word_name("réunion", "fr") and not is_common_word_name("Réunion", "fr")
    assert extract_country_iso2_from_text("une réunion des ministres", language="fr") is None
    assert extract_country_iso2_from_text("font-family: georgia, serif") is None
    assert extract_country_iso2_from_text("Talks in Georgia") == "GE"


def test_new_jersey_is_not_jersey():
    assert extract_country_iso2_from_text("Northern Ocean County in southern New Jersey") is None


def test_all_caps_copy_yields_no_bare_codes_but_names_still_count():
    body = "SEVERE THUNDERSTORM WATCH 704 REMAINS VALID UNTIL 11 PM EST IN BO AND GD COUNTIES"
    assert country_iso2s_in_text(body) == set()
    assert extract_country_iso2_from_text("FLOODING REPORTED ACROSS MEXICO AND IN GD") == "MX"


def test_non_place_location_surface():
    assert is_non_place_surface("Space", "en") and is_non_place_surface("Weltraum", "de")
    assert not is_non_place_surface("Berlin", "de")


def test_query_names_use_common_names_not_inverted_formal_names():
    # Live cache 11-13: "korea, republic of" -> Singapore; "taiwan, province
    # of china" -> China.
    assert geocoder_query_name("KR") == "South Korea"
    assert geocoder_query_name("TW") == "Taiwan"
    assert geocoder_query_name("CD") == "Democratic Republic of the Congo"


# --- the five live mis-tags (plus GI / BD) under the official precedence ------


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"title": "Agency Information Collection Activities; Comment Request",
     "summary": _BIS_BODY, "raw_body": _BIS_BODY, "language": "en"},          # 647b9356
    {"title": "Agency Information Collection Activities; Section 232",
     "raw_body": _BIS_BODY.replace("60505", "60360"), "language": "en"},      # cc81c011
    {"title": "The American Declaration of Freedom in Space", "raw_body": "",
     "language": "en",
     "entities": [{"class": "person", "text": "American Declaration of Freedom"},
                  {"class": "location", "text": "Space"}]},                   # 4872a035
])
async def test_us_official_items_are_us(payload):
    geo, backend = await _run(payload)
    assert prec.promoted_geo_codes(geo) == ["US"]
    assert geo["derivation"] == {"US": ["declared_by_source"]}
    assert "France" not in backend.queries and "Space" not in backend.queries


@pytest.mark.asyncio
@pytest.mark.parametrize("title,body", [
    ("Der Bau von Kasernen wird schneller und unkomplizierter", _IMG_BA),     # ffe8ebeb
    ("Weitere Beschaffung von IT-Dienstleistungen", _IMG_EE),                  # e8314cbe
    ("Statement Boris Pistorius und Carsten Breuer", _IMG_BD),                 # c16f3717
    ("Dunkelfeldstudie: Statement von Minister Pistorius und GI Breuer", ""),  # b0bc4417
])
async def test_german_defence_ministry_items_are_de(title, body):
    payload = {"title": title, "summary": body + "<p>Mit neuen Regelungen</p>",
               "raw_body": body, "language": "de"}
    geo, _ = await _run(payload, ctx=_ctx(scope_geo=("DE",), languages=("de",)))
    assert prec.promoted_geo_codes(geo) == ["DE"]


# --- precedence ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_issuer_adds_the_subject_beside_its_declared_country():
    """The sanctions-designation case: issuer US, subject IR -> both."""
    payload = {"title": "Treasury Sanctions Iran-Based Oil Network",
               "raw_body": "OFAC designated six entities in Iran and the United Arab "
                           "Emirates.", "language": "en"}
    geo, _ = await _run(payload)
    assert geo["country_iso2"] == "IR"                       # the subject is primary
    assert set(prec.promoted_geo_codes(geo)) == {"IR", "US", "AE"}
    assert geo["derivation"]["US"] == ["declared_by_source"]
    assert geo["derivation"]["IR"] == ["named_in_text"]


@pytest.mark.asyncio
async def test_issuer_ignores_deep_body_mentions():
    payload = {"title": "Money Market Operations", "language": "en",
               "raw_body": "x " * 300 + "a footnote about Japan"}
    geo, _ = await _run(payload, ctx=_ctx(scope_geo=("IN",)))
    assert prec.promoted_geo_codes(geo) == ["IN"]


@pytest.mark.asyncio
async def test_general_news_source_is_unchanged_text_only():
    payload = {"title": "Treasury Sanctions Iran-Based Oil Network", "language": "en"}
    geo, _ = await _run(payload, ctx=_ctx(source_class="reporting"))
    assert prec.promoted_geo_codes(geo) == ["IR"]
    assert geo["derivation"] == {"IR": ["named_in_text"]}
    # ...and with nothing in-body, a reporting source gets NO declared tag
    # (the baseline's publisher-origin fallback stays in charge).
    geo, _ = await _run({"title": "Weather today", "language": "en"},
                        ctx=_ctx(source_class="reporting"))
    assert geo is None


@pytest.mark.asyncio
async def test_official_without_a_declared_country_keeps_the_ladder():
    geo, _ = await _run({"title": "Cholera in Sudan", "language": "en"},
                        ctx=_ctx(scope_geo=()))
    assert prec.promoted_geo_codes(geo) == ["SD"]
    assert prec.issuer_declared_countries(_ctx(scope_geo=())) == []
    assert prec.issuer_declared_countries(_ctx(source_class="state_media")) == []


@pytest.mark.asyncio
async def test_geometry_basis_and_no_text_additions():
    payload = {"title": "Flood Warning by NWS San Juan PR for Puerto Rico and Mexico",
               "geometry": {"type": "Point", "coordinates": [-66.1, 18.4]}}
    geo, _ = await _run(payload)
    assert geo["derivation"]["US"] == ["declared_by_source"]
    assert geo["country_iso2"] == "PR" and geo["derivation"]["PR"] == ["geometry"]
    assert "MX" not in prec.promoted_geo_codes(geo)


@pytest.mark.asyncio
async def test_publisher_tld_basis_is_marked():
    geo, _ = await _run({"title": "Local council meets", "language": "en"},
                        ctx=_ctx(source_class="reporting"), url="https://example.com.br/x")
    assert geo["derivation"] == {"BR": ["publisher_fallback"]}


# --- provenance helpers ----------------------------------------------------------


def test_build_block_and_promote_order():
    block = prec.build_geo_block({"country_iso2": "IR", "lat": 1.0, "lon": 2.0}, "named_in_text",
                                 declared=["US"], named=["IR", "AE"])
    assert block["countries"] == ["IR", "US", "AE"]
    assert prec.promoted_geo_codes(block) == ["IR", "US", "AE"]
    assert prec.build_geo_block(None, None, declared=["DE"])["source"] == "declared"
    assert prec.build_geo_block(None, None) is None
    # A legacy single-country block promotes exactly as before.
    assert prec.promoted_geo_codes({"country_iso2": "FR"}) == ["FR"]


def test_geo_derivation_folds_the_baseline_fallback():
    payload = {"geo_fallback": {"basis": "publisher_origin", "iso2": ["IL"]}}
    assert prec.geo_derivation(payload) == {"IL": ["publisher_fallback"]}
    payload = {"geo": {"country_iso2": "IR", "derivation": {"IR": ["named_in_text"],
                                                            "US": ["declared_by_source"]}}}
    assert prec.geo_derivation(payload) == {"IR": ["named_in_text"],
                                           "US": ["declared_by_source"]}
    assert prec.geo_derivation({"geo": {"country_iso2": "FR"}}) == {"FR": ["named_in_text"]}


@pytest.mark.asyncio
async def test_ofac_general_license_letters_are_not_greenland_or_estonia():
    """OFAC GL W -> {GL}, GLs EE and FF -> {EE}."""
    for title, body in (
        ("Publication of Iran-Related Web General License W",
         "OFAC is publishing an Iran-related general license (GL): GL W."),
        ("Publication of Iran-Related Web General Licenses EE and FF",
         "OFAC is publishing Iran-related general licenses (GLs) EE and FF."),
    ):
        geo, _ = await _run({"title": title, "raw_body": body, "summary": body,
                             "language": "en"})
        assert set(prec.promoted_geo_codes(geo)) == {"US", "IR"}
