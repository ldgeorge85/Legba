# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The geocoder's person-name guard and Congo disambiguation.

Live misses (11-10/11-11): 0bbbf4c0 "Republicans elect Jim Jordan ..." filed
under JO; d3437c51 / 0bf738d4 / 757bedb3 / fe03b154 "Ebola cases in DR Congo
pass 11,000" filed under CG (Brazzaville).
"""

from __future__ import annotations

import pytest

from legba.data.filters._geocode_rules import (
    AMBIGUOUS_NAMES,
    disambiguate,
    mask_person_names,
)
from legba.data.filters.geocode import (
    GeocodeConfig,
    GeocodeHandler,
    _COUNTRY_NAME_INDEX,
    country_iso2s_in_text,
    extract_country_iso2_from_text,
)
from legba.data.sources._contract import Signal

_JORDAN_ENTS = [{"class": "person", "text": "Jim Jordan"}]


# --- person-name guard ---------------------------------------------------------


def test_jim_jordan_is_not_the_country_0bbbf4c0():
    title = "Republicans elect Jim Jordan as House minority leader after midterm defeats"
    assert extract_country_iso2_from_text(title) == "JO"            # the old answer
    assert extract_country_iso2_from_text(title, entities=_JORDAN_ENTS) is None


def test_a_bare_surname_repeat_is_masked_too():
    text = "Jim Jordan won. Republicans pick Jordan for the last two years."
    assert extract_country_iso2_from_text(text, entities=_JORDAN_ENTS) is None


def test_the_country_after_a_locative_survives_the_guard():
    text = "Jim Jordan met the King of Jordan"
    assert extract_country_iso2_from_text(text, entities=_JORDAN_ENTS) == "JO"


def test_masking_keeps_offsets_and_other_countries():
    text = "Jim Jordan says Iran is the threat"
    masked = mask_person_names(text, _JORDAN_ENTS, country_names=_COUNTRY_NAME_INDEX)
    assert len(masked) == len(text) and "Jordan" not in masked
    assert extract_country_iso2_from_text(text, entities=_JORDAN_ENTS) == "IR"


def test_no_person_entities_is_byte_identical():
    text = "Jordan reopens border crossing"
    assert mask_person_names(text, [], country_names=_COUNTRY_NAME_INDEX) is text
    assert extract_country_iso2_from_text(text, entities=[{"class": "location", "text": "x"}]) == "JO"


def test_the_set_sweep_applies_the_guard():
    assert country_iso2s_in_text("Jim Jordan and Iran", entities=_JORDAN_ENTS) == {"IR"}


# --- Congo -------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "iso"), [
    ("Ebola cases in DR Congo pass 11,000, with 5,387 deaths", "CD"),   # 757bedb3
    ("DRC Ebola toll rises", "CD"),
    ("Fighting near Goma as Congo army retreats", "CD"),
    ("Congo-Kinshasa signs deal", "CD"),
    ("Republic of the Congo holds vote", "CG"),
    ("Sassou Nguesso wins Congo election", "CG"),
    ("Congo basin rainforest summit", "CG"),          # no cue → today's answer
])
def test_congo_resolves_by_its_own_form_or_co_occurring_places(text, iso):
    assert extract_country_iso2_from_text(text) == iso


def test_the_cue_may_come_from_the_rest_of_the_signal():
    assert extract_country_iso2_from_text(
        "Ebola spreads in Congo", context="Ebola spreads in Congo. Officials in Ituri said",
    ) == "CD"


def test_cues_for_both_keep_the_default():
    assert AMBIGUOUS_NAMES["congo"].resolve("Kinshasa and Brazzaville talks") == "CG"
    assert disambiguate("Iran", "IR", "Kinshasa") == "IR"


class _Backend:
    name = "stub"

    def __init__(self):
        self.queries: list[str] = []

    async def geocode(self, query):
        self.queries.append(query)
        return None

    async def reachable(self):
        return True


def _handler():
    return GeocodeHandler(GeocodeConfig(), backend=_Backend())


def test_the_drc_query_is_a_name_the_backend_resolves():
    h = _handler()
    sig = Signal(source_id="source.aljazeera.world", payload={
        "title": "Ebola cases in DR Congo pass 11,000, with 5,387 deaths",
    })
    cands = h._derive_candidates(sig)
    assert cands[0] == "Democratic Republic of the Congo"
    assert "Congo" not in cands


def test_a_congo_location_entity_is_disambiguated_too():
    h = _handler()
    sig = Signal(source_id="s", payload={
        "title": "Ebola outbreak: Congo cases climb",
        "summary": "Health teams in North Kivu",
        "entities": [{"class": "country", "text": "Congo"}],
    })
    assert h._derive_candidates(sig)[0] == "Democratic Republic of the Congo"


def test_jim_jordan_signal_never_queries_jordan():
    h = _handler()
    sig = Signal(source_id="source.guardian.world", payload={
        "title": "Republicans elect Jim Jordan as House minority leader after midterm defeats",
        "entities": _JORDAN_ENTS,
    })
    assert "Jordan" not in h._derive_candidates(sig)
