# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reference builder's DATE LADDER — six rungs, one precedence, no guesses.

THE DEFECT (wave O, lane o2). The extractor read JSON-LD, a ``<meta>`` table
and ``<time datetime>``, all of them in ISO only. That is what a newsroom CMS
emits, and the top-ups measured what an OFFICIAL publisher emits instead:
``bundestag.de`` ships ``<meta name="date" content="25.09.2026">`` (right key,
refused value), ``radio.gov.pk`` ships nothing at all but a visible
``September 26, 2026``, and ``opcw.org`` ships a visible ``9 July 2026`` with
an HTTP ``Last-Modified`` that tracks its CDN. So a parliament, a state
broadcaster and an IGO were inadmissible to a reference while an ordinary
newspaper was not.

THE FIXTURES ARE REAL PAGES, captured 2026-09-26 and committed so no test ever
touches the network. Each one is the served HTML with the BODIES of its
``<script>`` (except ``ld+json``), ``<style>`` and ``<svg>`` elements emptied
and its comments dropped — every one of those is discarded by ``html_to_text``
before the extractor sees it, so the extracted text and every ``<meta>``,
``<time>`` and ``ld+json`` the ladder reads are byte-identical to what the host
served. The captured response headers are pinned beside each fixture below.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from legba.data.analysts.deterministic_handlers._reference_dates import (
    DATELINE_SCAN_CHARS,
    DATE_SOURCE_LAST_MODIFIED,
    DATE_SOURCE_NONE,
    DATE_SOURCE_PRECEDENCE,
    dateline_date,
    extract_publish_date,
    is_official_host,
    page_language,
    resolve_publish_date,
)
from legba.data.analysts.deterministic_handlers._reference_page import (
    coerce_page_text,
    html_to_text,
)

FIXTURES = Path(__file__).parent / "fixtures" / "reference_dates"

#: ``(fixture, url, captured response headers)`` for each real page.
BUNDESTAG = (
    "bundestag_hib_kurzmeldung.html",
    "https://www.bundestag.de/presse/hib/kurzmeldungen-1217778",
    {"content-type": "text/html;charset=UTF-8", "content-language": "de"},
)
RADIO_PK = (
    "radio_gov_pk_news.html",
    "https://www.radio.gov.pk/26-09-2026/"
    "president-pm-condemn-terrorist-attack-in-di-khan",
    {"content-type": "text/html; charset=UTF-8"},
)
OPCW_ARTICLE = (
    "opcw_news_article.html",
    "https://www.opcw.org/media-centre/news/2026/07/"
    "executive-council-reinstates-rights-and-privileges-syria-under-chemical",
    {"content-language": "en", "last-modified": "Sat, 26 Sep 2026 15:33:20 GMT"},
)
OPCW_INDEX = (
    "opcw_news_index.html",
    "https://www.opcw.org/media-centre/news",
    {"content-language": "en", "last-modified": "Sat, 26 Sep 2026 15:38:33 GMT"},
)
UN_CHARTER = (
    "un_charter_full_text.html",
    "https://www.un.org/en/about-us/un-charter/full-text",
    {"last-modified": "Sat, 26 Sep 2026 15:49:47 GMT"},
)


def _resolve(case: tuple[str, str, dict[str, str]]):
    """Run the whole ladder over one captured page, exactly as a fetch does."""
    name, url, headers = case
    raw = (FIXTURES / name).read_text(encoding="utf-8")
    text, ldjson = coerce_page_text(raw)
    return resolve_publish_date(
        raw, ldjson, url=url, text=text, headers=headers
    )


def _text_of(case: tuple[str, str, dict[str, str]]) -> str:
    return html_to_text((FIXTURES / case[0]).read_text(encoding="utf-8"))[0]


# ---------------------------------------------------------------------------
# The precedence — the module's whole contract
# ---------------------------------------------------------------------------


def test_the_ladder_is_six_rungs_strongest_first():
    assert DATE_SOURCE_PRECEDENCE == (
        "json-ld", "meta:", "time-datetime", "url-path", "dateline:",
        "http_last_modified",
    )


def test_every_rung_yields_to_the_one_above_it():
    """One page carrying ALL SIX, each with a different date. The strongest
    wins and every weaker one is recorded as dissent — never discarded."""
    raw = (
        '<html lang="en"><head>'
        '<script type="application/ld+json">{"datePublished":"2026-01-01"}</script>'
        '<meta property="article:published_time" content="2026-02-02">'
        "</head><body><main>"
        '<time datetime="2026-03-03">x</time>'
        "<p>Published 2026-05-05 by the desk.</p>"
        "</main></body></html>"
    )
    dated = resolve_publish_date(
        raw, ['{"datePublished":"2026-01-01"}'],
        url="https://www.state.gov/2026/04/04/a-release",
        text="Published 2026-05-05 by the desk.",
        headers={"last-modified": "Sun, 06 Jun 2026 00:00:00 GMT"},
    )
    assert dated.value == "2026-01-01"
    assert dated.source == "json-ld"
    assert dated.disagreements == (
        "meta:article:published_time=2026-02-02",
        "time-datetime=2026-03-03",
        "url-path=2026-04-04",
        "dateline:en=2026-05-05",
        "http_last_modified=2026-06-06",
    )


def test_a_rung_that_agrees_is_not_recorded_as_a_disagreement():
    raw = (
        '<html lang="en"><head>'
        '<meta property="article:published_time" content="2026-02-02">'
        "</head><body><p>2026-02-02</p></body></html>"
    )
    dated = resolve_publish_date(raw, [], text="2026-02-02")
    assert (dated.value, dated.source) == ("2026-02-02", "meta:article:published_time")
    assert dated.disagreements == ()


def test_meta_keys_are_walked_in_KEY_order_not_document_order():
    """THE REGRESSION. The table always said ``article:modified_time`` is last
    and the reader always walked the page's tags in DOCUMENT order, so a CMS
    that emits the modified time first dated the page by its last edit."""
    raw = (
        '<meta property="article:modified_time" content="2026-09-20">'
        '<meta property="article:published_time" content="2026-09-04">'
    )
    assert extract_publish_date(raw, []) == (
        "2026-09-04", "meta:article:published_time",
    )


# ---------------------------------------------------------------------------
# The three original mechanisms, unchanged
# ---------------------------------------------------------------------------


def test_the_three_original_mechanisms_answer_exactly_as_they_did():
    ld = '{"datePublished":"2026-09-04T08:00:00Z"}'
    assert extract_publish_date(f"<script>{ld}</script>", [ld]) == (
        "2026-09-04", "json-ld",
    )
    meta = '<meta property="article:published_time" content="2026-09-05T10:00:00Z">'
    assert extract_publish_date(meta, []) == (
        "2026-09-05", "meta:article:published_time",
    )
    tag = '<time datetime="2026-09-06T11:00:00Z">Sept 6</time>'
    assert extract_publish_date(tag, []) == ("2026-09-06", "time-datetime")


def test_a_page_with_nothing_is_still_UNDATED_and_says_so():
    dated = resolve_publish_date("<html><body><p>No date here.</p></body></html>", [])
    assert dated.value is None
    assert dated.source == DATE_SOURCE_NONE
    assert dated.disagreements == ()


# ---------------------------------------------------------------------------
# The masthead — the one thing the prose reader must never let back in
# ---------------------------------------------------------------------------


def test_a_masthead_is_still_not_a_date_even_now_that_prose_is_read():
    """R1's exact page shape, and it REACHES the text: ``<header>`` is a block
    tag, not a dropped one. Two independent fences refuse it — the abbreviated
    month is outside the closed format set, and the weekday marks it a
    masthead — so the date the model read off this page stays unreachable."""
    page = (
        "<html><body><header>Wednesday, Sep 16, 2026</header>"
        "<p>Israel Katz has been named the new Defence Minister.</p>"
        "</body></html>"
    )
    assert extract_publish_date(page, []) == (None, DATE_SOURCE_NONE)
    text, ldjson = coerce_page_text(page)
    assert "Sep 16" in text  # the chrome is NOT dropped; the fences are why
    assert extract_publish_date(page, ldjson, text=text) == (None, DATE_SOURCE_NONE)


def test_a_weekday_in_front_of_a_date_makes_it_a_masthead_not_a_dateline():
    """Spelt out in full, so the closed-format-set fence is not what is being
    measured — only the weekday rule is."""
    assert dateline_date("Wednesday, September 16, 2026", "en") is None
    assert dateline_date("September 16, 2026", "en") == "2026-09-16"
    assert dateline_date("Mittwoch, 16. September 2026", "de") is None


def test_a_clock_after_a_date_makes_it_a_masthead_too():
    """``radio.gov.pk``'s site clock, verbatim. It sits two lines above that
    page's real dateline and on any page but a same-day one it would carry a
    DIFFERENT date — which would collapse the whole rung under the
    one-distinct-date rule if it were not discarded first."""
    clock = "Saturday, 26 September 2026, 08:44:17 pm"
    assert dateline_date(clock, "en") is None
    assert dateline_date(f"{clock}\nPresident condemns attack\nJuly 9, 2026",
                         "en") == "2026-07-09"


def test_a_weekday_a_sentence_earlier_does_not_reach_the_date():
    assert dateline_date(
        "On Saturday the council met in closed session; the decision it "
        "adopted was published 4 September 2026 by the registry.", "en",
    ) == "2026-09-04"


def test_an_abbreviated_month_is_not_in_the_closed_format_set():
    """"Sep 16, 2026" is the masthead shape. Full month names only."""
    assert dateline_date("Filed Sep 16, 2026 in Jerusalem.", "en") is None
    assert dateline_date("Filed September 16, 2026 in Jerusalem.", "en") == "2026-09-16"


def test_two_different_dates_in_the_window_are_not_a_dateline():
    assert dateline_date("Published 4 September 2026. Updated 5 September 2026.",
                         "en") is None
    assert dateline_date("Published 4 September 2026. Reprinted 4 September 2026.",
                         "en") == "2026-09-04"


def test_a_dateline_past_the_scan_window_is_not_read():
    tail = "September 26, 2026"
    assert dateline_date("x" * DATELINE_SCAN_CHARS + " " + tail, "en") is None
    assert dateline_date("x" * (DATELINE_SCAN_CHARS - 40) + " " + tail, "en") == (
        "2026-09-26"
    )


def test_a_bare_year_and_a_relative_phrase_are_not_dates():
    assert dateline_date("Copyright 2026. Published 3 hours ago.", "en") is None


# ---------------------------------------------------------------------------
# The numeric-date ambiguity rule
# ---------------------------------------------------------------------------


def test_an_ambiguous_dotted_date_reads_as_NO_date_on_an_english_page():
    """``03/04/2026`` is 3 April to half the world and 4 March to the other."""
    assert dateline_date("Published 03/04/2026 by the desk.", "en") is None


def test_the_page_language_disambiguates_a_dotted_date():
    assert dateline_date("Veröffentlicht 03.04.2026 vom Referat.", "de") == (
        "2026-04-03"
    )


def test_a_first_component_over_twelve_is_unambiguous_in_any_language():
    assert dateline_date("Published 25.09.2026 by the desk.", "en") == "2026-09-25"


def test_an_impossible_dotted_date_is_no_date_rather_than_a_wrong_one():
    assert dateline_date("Published 31.02.2026 by the desk.", "de") is None


# ---------------------------------------------------------------------------
# Rung 6 — Last-Modified, official-class hosts only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", [
    "www.state.gov", "radio.gov.pk", "mfa.gov.il", "www.economie.gouv.fr",
    "www.mod.go.jp", "nato.int", "www.opcw.org", "press.un.org",
    "ec.europa.eu", "gob.mx",
])
def test_official_hosts_are_recognised_on_label_boundaries(host):
    assert is_official_host(host)


@pytest.mark.parametrize("host", [
    "www.bbc.com", "notgov.example", "www.bundestag.de", "theguardian.com",
    "fakegov.com", "ungov.org", "", "reuters.com",
])
def test_everything_else_fails_SAFE_out_of_the_official_class(host):
    assert not is_official_host(host)


def test_last_modified_is_refused_for_a_host_outside_the_official_class():
    headers = {"last-modified": "Fri, 25 Sep 2026 10:00:00 GMT"}
    assert resolve_publish_date(
        "<html><body><p>x</p></body></html>", [],
        url="https://www.bbc.com/news/x", headers=headers,
    ).source == DATE_SOURCE_NONE
    assert resolve_publish_date(
        "<html><body><p>x</p></body></html>", [],
        url="https://www.state.gov/news/x", headers=headers,
    ) .as_tuple() == ("2026-09-25", DATE_SOURCE_LAST_MODIFIED)


def test_an_unparseable_last_modified_is_no_date_rather_than_a_crash():
    assert resolve_publish_date(
        "<html><body><p>x</p></body></html>", [],
        url="https://www.state.gov/news/x",
        headers={"last-modified": "whenever"},
    ).source == DATE_SOURCE_NONE


# ---------------------------------------------------------------------------
# The language hint
# ---------------------------------------------------------------------------


def test_the_language_hint_comes_from_the_page_then_the_header():
    assert page_language('<html xml:lang="de" lang="de" dir="ltr">') == "de"
    assert page_language("<html>", {"Content-Language": "fr"}) == "fr"
    assert page_language("<html>") == ""


def test_the_language_hint_chooses_the_month_vocabulary():
    assert dateline_date("Veröffentlicht am 25. September 2026.", "de") == (
        "2026-09-25"
    )
    assert dateline_date("Publié le 25 septembre 2026.", "fr") == "2026-09-25"
    assert dateline_date("Publicado el 25 de septiembre de 2026.", "es") is None
    # English stays available as the fallback vocabulary on every page.
    assert dateline_date("Published 25 September 2026.", "de") == "2026-09-25"


# ---------------------------------------------------------------------------
# THE REAL PAGES. Captured 2026-09-26; no test here touches the network.
# ---------------------------------------------------------------------------


def test_bundestag_is_dated_by_the_meta_key_whose_VALUE_was_being_refused():
    """``<meta name="date" content="25.09.2026">``. The key was always in the
    table; only ``YYYY-MM-DD`` was ever parsed out of it, so the German federal
    parliament's press releases were undated and therefore inadmissible."""
    dated = _resolve(BUNDESTAG)
    assert dated.value == "2026-09-25"
    assert dated.source == "meta:date"
    assert dated.disagreements == ()
    assert '<meta name="date" content="25.09.2026"/>' in (
        (FIXTURES / BUNDESTAG[0]).read_text(encoding="utf-8")
    )


def test_radio_pakistan_is_dated_by_its_visible_dateline_and_nothing_else():
    """No JSON-LD, no dated ``<meta>``, no ``<time>``, and a ``DD-MM-YYYY`` URL
    that ``url_embedded_date`` does not read. The article's own
    ``September 26, 2026`` is the only date the page has."""
    raw = (FIXTURES / RADIO_PK[0]).read_text(encoding="utf-8")
    assert "ld+json" not in raw
    assert "<time" not in raw.lower()
    dated = _resolve(RADIO_PK)
    assert dated.value == "2026-09-26"
    assert dated.source == "dateline:en"


def test_opcw_prefers_its_dateline_over_a_last_modified_that_is_79_days_late():
    """The whole reason rung 6 is last. The page's dateline says 9 July 2026;
    its ``Last-Modified`` is whenever the CDN last refreshed the object. The
    dateline wins and the header rides the row as recorded dissent."""
    dated = _resolve(OPCW_ARTICLE)
    assert (dated.value, dated.source) == ("2026-07-09", "dateline:en")
    assert dated.disagreements == ("http_last_modified=2026-09-26",)


def test_the_opcw_index_is_dated_by_its_time_elements_not_by_its_listing():
    """An index page lists many articles, so its opening carries many dates —
    the dateline rung refuses it outright — and a ``<time datetime>`` above it
    answers first anyway."""
    dated = _resolve(OPCW_INDEX)
    assert dated.source == "time-datetime"
    assert dateline_date(_text_of(OPCW_INDEX), "en") is None


def test_the_un_charter_is_dated_by_its_header_because_nothing_else_answers():
    """A static official document: no JSON-LD, no publish ``<meta>``, no
    ``<time>``, no dated URL, and a text whose opening carries several dates so
    the dateline rung refuses it. The weakest rung is the only one left, and it
    NAMES itself on the row rather than passing as a publication date."""
    raw = (FIXTURES / UN_CHARTER[0]).read_text(encoding="utf-8")
    assert "ld+json" not in raw
    dated = _resolve(UN_CHARTER)
    assert (dated.value, dated.source) == ("2026-09-26", DATE_SOURCE_LAST_MODIFIED)


# ---------------------------------------------------------------------------
# Back-compatibility of the two-value shape every caller still uses
# ---------------------------------------------------------------------------


def test_extract_publish_date_is_the_ladders_two_value_shape():
    raw = '<meta name="pubdate" content="2026-09-07">'
    assert extract_publish_date(raw, []) == resolve_publish_date(
        raw, []
    ).as_tuple()


def test_a_caller_that_passes_no_text_simply_does_not_get_the_dateline_rung():
    raw = "<html><body><p>Published 4 September 2026.</p></body></html>"
    assert extract_publish_date(raw, []) == (None, DATE_SOURCE_NONE)
    text, ldjson = coerce_page_text(raw)
    assert extract_publish_date(raw, ldjson, text=text) == (
        "2026-09-04", "dateline:en",
    )
