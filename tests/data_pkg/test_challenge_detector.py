# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FETCH_REVIEW (a′) — the false-absence defect: a block must read as a block.

THE DEFECT. ``evidence_archiver``'s wall detector already carried bot-challenge
phrasing, but ``_WALL_MAX_CHARS = 500`` gated every pattern. That cap was
honestly derived — a 2026-07-31 live-DB audit measured the longest confirmed
100%-boilerplate body at 499 chars and the shortest genuine article containing
a deny pattern at 852 — and it is simply out of range for a modern challenge
page. The review's probes measured AP's Cloudflare interstitial at **5 507 B**
and Times of Israel's at **12 604 B**. Both sailed past the cap, so a challenge
landed as a thin/failed extraction and read downstream as *the web had
nothing*: the same false-absence failure the search plane's degradation reads
exist to prevent.

THE FIXTURES. The four probe bodies are reconstructed here from the tells and
the byte counts FETCH_REVIEW §2 published (the live probe bodies live in that
session's scratchpad, never in the repo), padded to the measured sizes so the
length dimension of the defect is exercised for real and not assumed away:

  * ``www.reuters.com``        401, 774 B   — DataDome (``var dd={'rt'…``)
  * ``apnews.com``             403, 5 507 B — Cloudflare "Just a moment..."
  * ``www.timesofisrael.com``  403, 12 604 B — "Cloudflare capcha page"
  * ``www.haaretz.com``        200, real HTML — NOT a challenge (a paywall,
    which is a licence/money question and explicitly out of scope)

THE NON-REGRESSION half matters as much: a genuine article must never become a
"wall" because the cap was raised. The legacy short-page patterns keep their
500-char gate; only the challenge tier is un-gated, and it carries its
discrimination in conjunctive rules instead.
"""
from __future__ import annotations

import pytest

from legba.data.analysts.deterministic_handlers._challenge_detect import (
    BLOCKED_BY_CHALLENGE,
    challenge_from_status,
    detect_challenge_page,
    detect_challenge_text,
)
from legba.data.analysts.deterministic_handlers.evidence_archiver import (
    _WALL_MAX_CHARS,
    _match_wall_pattern,
)
from legba.data.research_evidence import (
    detect_challenge_page as public_detect_challenge_page,
)


def _pad(html: str, target_bytes: int) -> bytes:
    """Pad ``html`` with inert markup to the measured wire size."""
    body = html.encode("utf-8")
    if len(body) >= target_bytes:
        return body
    filler = b"<!-- " + b"0" * (target_bytes - len(body) - 9) + b" -->"
    return body.replace(b"</body>", filler + b"</body>")


# --- the four probe bodies -------------------------------------------------

REUTERS_401 = _pad(
    "<html><head><title>reuters.com</title></head><body>"
    "<p>Please enable JS and disable any ad blocker</p>"
    "<script>var dd={'rt':'i','cid':'AHrlqAAAAA','hsh':'2211','t':'fe'}</script>"
    "</body></html>",
    774,
)

AP_403 = _pad(
    "<html><head><title>Just a moment...</title>"
    "<meta http-equiv=\"refresh\" content=\"390\"></head><body>"
    "<div class=\"main-wrapper\"><h1>apnews.com</h1>"
    "<p id=\"WwAlD3\">Verifying you are human. This may take a few seconds.</p>"
    "<p>apnews.com needs to review the security of your connection before "
    "proceeding.</p></div>"
    "<script src=\"/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1\">"
    "</script></body></html>",
    5_507,
)

TOI_403 = _pad(
    "<html><head><title>Cloudflare capcha page</title></head><body>"
    "<div id=\"cf-wrapper\"><h1>Please enable cookies.</h1>"
    "<p>Please stand by, while we are checking your browser...</p></div>"
    "<script>window._cf_chl_opt={cvId:'3'};</script>"
    "</body></html>",
    12_604,
)

HAARETZ_200 = (
    "<html><head><title>Haaretz | Israel News, the Middle East and the "
    "Jewish World</title></head><body><main><article>"
    "<h1>Israeli forces withdraw from the northern corridor</h1>"
    "<p>The withdrawal began on Tuesday and was completed overnight, two "
    "officers said, describing a phased handover to the civil administration "
    "that had been negotiated over the preceding fortnight.</p>"
    "<p>Subscribe to read the full article.</p>"
    "</article></main></body></html>"
).encode("utf-8")


# --- a genuine article, and a nastier one ----------------------------------

GENUINE_ARTICLE_TEXT = (
    "Fuel deliveries into Bamako fell by four fifths this week as JNIM "
    "roadblocks held on the three southern corridors, according to two "
    "haulage operators reached by telephone in the capital. The national "
    "fuel importers' association said reserves would last eleven days at "
    "current rationing levels, and that the government had begun releasing "
    "strategic stocks to hospitals and to the two power stations that still "
    "run on imported diesel. " * 6
)

#: The adversarial case: a REAL article that talks about Cloudflare and happens
#: to contain the words "just a moment". Conjunctive rules are what keep this
#: from being classified as a block; a bare "cloudflare" token would not.
ARTICLE_ABOUT_CLOUDFLARE = (
    "Cloudflare said the outage began at 06:21 UTC and lasted just under an "
    "hour. \"Wait just a moment,\" the engineer recalled being told, before "
    "the dashboards came back. The company's status page attributed the fault "
    "to a configuration push. " * 8
)


# ---------------------------------------------------------------------------
# The four probes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "body", "size"),
    [
        ("reuters", REUTERS_401, 774),
        ("apnews", AP_403, 5_507),
        ("timesofisrael", TOI_403, 12_604),
    ],
)
def test_the_three_blocked_probes_are_classified_as_challenges(name, body, size):
    assert len(body) == size, f"{name} fixture drifted from the measured size"
    # …and every one of them is FAR past the old 500-char wall gate.
    assert len(body) > _WALL_MAX_CHARS
    tell = detect_challenge_page(body, "text/html")
    assert tell is not None, f"{name} ({size} B) was not classified as a challenge"


def test_haaretz_is_not_a_challenge_it_is_a_paywall():
    """The review is explicit: Haaretz serves us 200 with our own bot UA.
    There is no edge block, and no fetcher change touches a subscription."""
    assert detect_challenge_page(HAARETZ_200, "text/html") is None


def test_the_public_re_export_is_the_same_verdict():
    """``research_evidence.detect_challenge_page`` is the seam the tools use;
    it must not be able to drift from the archiver's own detector."""
    for body in (REUTERS_401, AP_403, TOI_403, HAARETZ_200):
        assert public_detect_challenge_page(body, "text/html") == (
            detect_challenge_page(body, "text/html")
        )


# ---------------------------------------------------------------------------
# The 500-char wall, raised for the challenge tier only
# ---------------------------------------------------------------------------


def test_a_13kb_interstitial_now_matches_the_wall_gate():
    """THE (a′) REGRESSION. ``_match_wall_pattern`` is what stood between a
    challenge body and ``text_extract_rejected_boilerplate``; a 12.6 kB
    interstitial used to walk straight past it."""
    extracted = (
        "Cloudflare capcha page Please enable cookies. Please stand by, while "
        "we are checking your browser... " + ("filler prose. " * 900)
    )
    assert len(extracted) > _WALL_MAX_CHARS * 20
    assert _match_wall_pattern(extracted) is not None


def test_the_legacy_short_wall_patterns_keep_their_cap():
    """A no-JS fallback page under the cap still matches (no coverage lost)…"""
    assert _match_wall_pattern(
        "JavaScript is disabled in your browser. Please enable JavaScript to "
        "proceed and reload the page."
    ) is not None


def test_a_long_genuine_article_containing_a_legacy_pattern_is_still_safe():
    """…and the France24 case the 500-char cap was measured FOR is unchanged:
    a real article whose prose happens to contain an embedded-video caption
    must not be rejected."""
    text = (
        GENUINE_ARTICLE_TEXT
        + " One of your browser extensions seems to be blocking the video "
        "player. "
        + GENUINE_ARTICLE_TEXT
    )
    assert len(text) > _WALL_MAX_CHARS
    assert _match_wall_pattern(text) is None


def test_a_genuine_article_is_never_a_challenge():
    assert detect_challenge_text(GENUINE_ARTICLE_TEXT) is None
    assert _match_wall_pattern(GENUINE_ARTICLE_TEXT) is None


def test_an_article_about_cloudflare_is_not_a_block():
    """Conjunctive rules, not a bare vendor name: an outage story mentioning
    Cloudflare AND the phrase "just a moment" must still read as an article."""
    assert detect_challenge_text(ARTICLE_ABOUT_CLOUDFLARE) is None
    assert _match_wall_pattern(ARTICLE_ABOUT_CLOUDFLARE) is None


# ---------------------------------------------------------------------------
# Tiers, types and statuses
# ---------------------------------------------------------------------------


def test_extracted_text_tier_catches_what_survives_extraction():
    """Trafilatura keeps the prose and drops the script — the text tier has to
    stand on its own for the cases where the raw tells are gone."""
    assert detect_challenge_text(
        "Just a moment... Enable JavaScript and cookies to continue"
    ) is not None


def test_raw_tier_catches_what_never_survives_extraction():
    """``var dd={'rt'…`` is inside a <script>; no extractor will ever hand it
    to the text tier, which is why the raw body is scanned at all."""
    script_only = b"<html><body><script>var dd={'rt':'i','cid':'x'}</script></body></html>"
    assert detect_challenge_text(script_only.decode()) is None
    assert detect_challenge_page(script_only, "text/html") is not None


def test_non_textual_bodies_are_never_scanned():
    assert detect_challenge_page(b"\x89PNG\r\n\x1a\n", "image/png") is None
    assert detect_challenge_page(b"%PDF-1.4", "application/pdf") is None


def test_empty_and_missing_bodies_are_not_challenges():
    assert detect_challenge_page(None) is None
    assert detect_challenge_page(b"") is None
    assert detect_challenge_text("") is None


@pytest.mark.parametrize("code", [401, 403, 429])
def test_edge_refusal_statuses_are_blocks(code):
    assert challenge_from_status(code) == f"http_{code}"


@pytest.mark.parametrize("code", [200, 301, 404, 500, 503, None])
def test_everything_else_is_not_a_block(code):
    assert challenge_from_status(code) is None


def test_402_is_a_paywall_not_a_challenge():
    """FETCH_REVIEW §3(e): a paywall is a money and licence decision, and
    nothing in this lane routes around one. It must not be counted as a block
    the fetcher could fix."""
    assert challenge_from_status(402) is None


def test_the_carry_has_exactly_one_name():
    """Archiver counter, tool counter, sidecar ``last_error`` prefix and the
    probe script's class column all use this string."""
    assert BLOCKED_BY_CHALLENGE == "blocked_by_challenge"
