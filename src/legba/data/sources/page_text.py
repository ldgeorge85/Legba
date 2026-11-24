# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Page-text extraction — ONE implementation for every page the platform reads.

Lane I-2 moved these helpers here, verbatim, out of
``analysts/deterministic_handlers/evidence_archiver.py`` (which imports them
back under the same names), so the three readers of a fetched page run the
SAME extraction and the same wall rejection:

* the evidence archiver (its archived copy, ``payload.archived_text``);
* the fetch-receipt writer (``runtime/fetch_receipts.extract_page_text``: the
  text stored on a page version and hashed for drift);
* the ``page`` source kind (``data/sources/page.py``: the document's
  ``raw_body``) — so a page landed on request carries exactly the text its
  receipt holds, and a later drift check compares like with like.

Trafilatura is imported lazily (heavy, and the registry's slim image never
extracts a page); the challenge rules are imported lazily from
``_challenge_detect`` because that package's ``__init__`` imports the
archiver, which imports this module.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

logger = logging.getLogger("legba.data.analysts.deterministic_handlers.evidence_archiver")

__all__ = [
    "extract_main_text",
    "is_textual",
    "match_wall_phrase",
    "match_wall_pattern",
]

#: Content-type prefixes Trafilatura extraction is attempted for.
_TEXTUAL_CONTENT_TYPES = ("text/html", "application/xhtml", "text/plain", "application/xml", "text/xml")


def _is_textual(content_type: str | None, body: bytes) -> bool:
    """Whether Trafilatura extraction should be attempted on this object."""
    if content_type:
        ct = content_type.split(";", 1)[0].strip().lower()
        if any(ct.startswith(p) for p in _TEXTUAL_CONTENT_TYPES):
            return True
        # A declared non-text type (image/pdf/…) is authoritative — don't sniff.
        return False
    head = body[:512].lstrip().lower()
    return head.startswith((b"<!doctype", b"<html", b"<?xml"))


#: R6b — hosts whose pages carry no article-shaped main-content region for
#: Trafilatura to find. A ``t.me/<channel>/<msg>`` (and the ``/s/<channel>/<msg>``
#: public-preview variant) page is a Telegram embed-WIDGET: nav chrome, a
#: "Download"/"Context"/"Embed" action row, and the ``telegram-widget.js`` loader
#: snippet — never the message prose. Trafilatura's main-text heuristic, given
#: no content region, falls back to that chrome and writes it to
#: ``payload.archived_text`` as if it were the article — measured at ~30.7% of
#: archived telegram rows. The raw HTML bytes are still archived either way;
#: this only withholds the derived-text upgrade for a host that can never
#: produce one.
_NO_CONTENT_REGION_HOSTS: frozenset[str] = frozenset({"t.me", "telegram.me"})


def _skip_text_extraction(url: str, hosts: frozenset[str] | None = None) -> bool:
    """Whether ``url`` is a known no-article-content page (see
    :data:`_NO_CONTENT_REGION_HOSTS`) that Trafilatura extraction must be
    skipped for. Any parse failure degrades to ``False`` (attempt extraction
    — the existing best-effort ``_extract_text`` failure path already handles
    a genuinely bad fetch)."""
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return False
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]  # drop userinfo/port
    return host in (_NO_CONTENT_REGION_HOSTS if hosts is None else hosts)


#: V-E1 — deny list of known JS-wall / bot-check / redirect-interstitial
#: boilerplate. Case-insensitive substring match, gated by :data:`_WALL_MAX_CHARS`
#: (see :func:`_match_wall_pattern`). Seeded HONESTLY from a read-only live-DB
#: audit of ``signals.payload.archived_text`` on 2026-07-31 (never guessed):
#:
#:   * ``"javascript is disabled"`` / ``"enable javascript"`` — 86 confirmed
#:     live rows (lemonde.fr x83, press.un.org x3), all exactly the no-JS
#:     fallback page ("JavaScript is disabled in your browser. Please enable
#:     JavaScript to proceed...") at 286 chars — the EXACT artifact named in
#:     JUDGE_READOUT §5.
#:   * ``"one of your browser extensions seems to be blocking the video
#:     player"`` — 148 confirmed live rows (france24.com), but ONLY when short
#:     (see :data:`_WALL_MAX_CHARS` docstring — the identical string also
#:     occurs as an embedded-video caption inside multi-KB GENUINE articles,
#:     which must never be rejected).
#:   * ``"transferring to the website"`` — 83 confirmed live rows (en.irna.ir),
#:     a Google-redirect interstitial, all exactly 70 chars.
#:   * ``"we are optimizing your request for the best experience"`` — 1
#:     confirmed live row (a ShopShield-style bot-mitigation "please wait"
#:     page reached via a link-shortener).
#:   * ``"error message heading"`` — 2 confirmed live rows: a literal, never-
#:     substituted template placeholder ("ERROR MESSAGE HEADING ERROR MESSAGE
#:     SUBHEADING...") — unambiguous, zero legitimate-prose overlap risk.
#:   * ``"are you a robot"`` / ``"unusual traffic from your computer
#:     network"`` / ``"verify you are human"`` / ``"checking your browser
#:     before accessing"`` — industry-standard bot-challenge phrasing (Google/
#:     Cloudflare); not observed live in this audit, but included defensively
#:     — same zero-overlap-risk reasoning as above, and cheap insurance against
#:     the next walled publisher.
#:
#: DELIBERATELY EXCLUDED (found live, considered, rejected as patterns):
#:   * A GDACS disaster-alert boilerplate paragraph (32 rows) — genuine
#:     low-information template content from a real page, not a wall. This is
#:     exactly what the V-E2 substance-floor marker is FOR (thin-but-real),
#:     not what V-E1 rejection is for (not-real-at-all).
#:   * CGTN's cookie-notice footer ("By continuing to browse our site you
#:     agree to our use of cookies...", 77 rows) — appears on articles from
#:     314 chars up to 7,517 chars; it is decorative chrome CGTN appends to
#:     EVERY page, never the entire body. A blind "accept cookies" pattern
#:     would have false-rejected real, cited news content — this is why every
#:     pattern above was checked against its own live length distribution
#:     before being kept, not lifted uncritically from the spec's example list.
_WALL_DENY_PATTERNS: tuple[str, ...] = (
    "javascript is disabled",
    "enable javascript",
    "one of your browser extensions seems to be blocking the video player",
    "transferring to the website",
    "we are optimizing your request for the best experience",
    "error message heading",
    "are you a robot",
    "unusual traffic from your computer network",
    "verify you are human",
    "checking your browser before accessing",
)

#: V-E1 length gate: a wall/redirect pattern only REJECTS the extraction when
#: the whole cleaned text is this short or shorter. Live-audit-derived, not
#: guessed: the longest confirmed 100%-boilerplate body (no article prose at
#: all — headline-only stubs included) was 499 chars; the SHORTEST confirmed
#: GENUINE article that merely happens to contain one of the deny patterns
#: (an embedded-video caption sentence inside real France24 prose) was 852
#: chars. 500 sits cleanly in that gap — every pattern hit above this length
#: is left untouched (and, if genuinely thin, is still visible via the V-E2
#: ``archived_text_chars`` marker rather than being silently discarded).
_WALL_MAX_CHARS: int = 500


def _match_wall_pattern(text: str) -> str | None:
    """The matched deny-pattern when ``text`` looks like a JS-wall / bot-check
    / redirect-interstitial body rather than real extracted prose, else
    ``None``.

    Both conditions must hold (see :data:`_WALL_DENY_PATTERNS` and
    :data:`_WALL_MAX_CHARS` docstrings for the live-DB audit behind each):

      1. the text is short (``len(text) <= _WALL_MAX_CHARS``) — a pattern
         inside a long article is furniture (an embedded-video caption, a
         cookie-notice footer), not the whole "extraction";
      2. it contains one of the curated patterns (case-insensitive).
    """
    # (a′) THE CHALLENGE TIER RUNS FIRST AND IS NOT LENGTH-GATED. The 500-char
    # cap below was measured against ≤500-char no-JS fallback pages and does
    # not reach a modern interstitial (AP 5 507 B, ToI 12 604 B — FETCH_REVIEW
    # §2). ``detect_challenge_text`` carries its discrimination in conjunctive
    # RULES instead of in a character count.
    from ..analysts.deterministic_handlers._challenge_detect import (  # noqa: PLC0415
        detect_challenge_text,  # lazy: that package's __init__ imports the archiver
    )

    challenge = detect_challenge_text(text)
    if challenge is not None:
        return challenge
    if len(text) > _WALL_MAX_CHARS:
        return None
    lowered = text.lower()
    for pattern in _WALL_DENY_PATTERNS:
        if pattern in lowered:
            return pattern
    return None


def match_wall_phrase(text: str) -> str | None:
    """The challenge tell or the wall deny-pattern in ``text`` at ANY length,
    else ``None``.

    Only for a page the fetch was REDIRECTED to on another host (W11-F259):
    there the length gate's premise — a pattern inside a long article is
    furniture — does not hold, because the page is not the article that was
    asked for. Everywhere else :func:`_match_wall_pattern` (length-gated) rules.
    """
    if not text:
        return None
    hit = _match_wall_pattern(text)
    if hit is not None:
        return hit
    lowered = text.lower()
    return next((p for p in _WALL_DENY_PATTERNS if p in lowered), None)


def _extract_text(body: bytes, encoding: str | None, *, max_chars: int) -> str | None:
    """Trafilatura main-text extraction — bonus only; ``None`` on any failure.

    Lazy import (trafilatura is a base dep, but its import is heavy and every
    other sub-handler would pay it at package import time otherwise). Does
    NOT apply the V-E1 wall-pattern gate — that is a separate, independently
    testable check applied by the caller (:func:`_match_wall_pattern`) so a
    "Trafilatura extracted nothing" outcome and a "Trafilatura extracted a
    wall" outcome stay distinguishable in the counters."""
    try:
        import trafilatura  # noqa: PLC0415 — deliberate lazy import (see docstring)

        html = body.decode(encoding or "utf-8", errors="replace")
        text = trafilatura.extract(html, include_comments=False)
        if text and text.strip():
            return text.strip()[:max_chars]
        return None
    except Exception as exc:  # extraction is best-effort by contract
        logger.debug("evidence_archiver.extract_failed err=%s", exc)
        return None


#: Public names (the archiver keeps its underscored ones).
is_textual = _is_textual
match_wall_pattern = _match_wall_pattern
extract_main_text = _extract_text
