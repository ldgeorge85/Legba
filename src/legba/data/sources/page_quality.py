# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The page kind's QUALITY GATES — in code, each with a named reason.

A fetched page becomes a document only when it is an article-like
page; otherwise it is REFUSED with one reason from :data:`REFUSALS` (the
fetch receipt still keeps the bytes — they are the evidence of what the
address answered — but no signal is written). A page that passes may still
carry FLAGS (:func:`assess` returns them) recorded on the document.

The gates, in the order they are checked (the first that applies decides):

``unsupported_content_type``
    Anything but HTML/XHTML (a missing content type is sniffed). PDFs and
    other documents are refused here: no PDF text extractor ships in the
    tree. A document family would need one (pypdf or pdfminer.six) in the
    runtime image, a page/size cap, a scanned-PDF (OCR) decision, and a
    record shape that keeps page numbers so a citation can point at a page.
``blocked_by_challenge``
    An anti-bot interstitial: HTTP 401/403/429, or a challenge tell in the
    raw body (``_challenge_detect.detect_challenge_page``) or in the
    extracted text (the archiver's ``_match_wall_pattern`` challenge tier).
    The same rules and the same name the evidence archiver uses.
    A page on a WALL HOST (an ``unblock.``/``captcha.`` subdomain) is refused
    here whatever it says, and so is a redirect that left the requested host
    and landed on a page carrying any wall or challenge tell, at any length
    (www.federalregister.gov answered with a 200 redirect to
    unblock.federalregister.gov's "Request Access" CAPTCHA page, which passed
    every other gate).
``wall_page``
    The archiver's V-E1 short-page wall list (no-JS fallbacks, redirect
    interstitials) — ``_match_wall_pattern`` outside its challenge tier.
``soft_404``
    A 2xx page that says it is not there: its ``<title>``/``<h1>`` reads as
    a not-found page, or the final URL's path is a not-found path, or a
    short text (under :data:`SHORT_PAGE_CHARS`) carries a not-found phrase.
``login_wall``
    A password field on a short page, or a final URL on a sign-in path.
``consent_wall``
    A consent-management interstitial: the final host is a ``consent.``
    host or the path a ``/consent`` path, or a short text that asks to
    accept cookies/consent and says little else.
``paywall``
    HTTP 402; or a page that declares ``isAccessibleForFree: false`` AND
    yields a short text; or a short text carrying a subscribe-to-read
    phrase. A paywall is never routed around. A page
    that declares the paywall but serves its full text is LANDED with the
    flag ``paywall_declared``.
``too_little_text``
    Fewer than ``min_text_chars`` (default :data:`DEFAULT_MIN_TEXT_CHARS`)
    characters after extraction.
``mostly_navigation``
    The content region's anchor text is at least ``max_link_density``
    (default :data:`DEFAULT_MAX_LINK_DENSITY`) of its text: a section front,
    a tag page, a site map — links, not an article.

Thresholds and why:

* :data:`DEFAULT_MIN_TEXT_CHARS` = 400. The live feed teasers this pipeline
  exists to fill run a median of 104–126 characters (2026-07-07 audit), and
  the archiver's longest confirmed all-boilerplate extraction was 499
  characters while its shortest confirmed genuine article was 852. 400 keeps
  a short wire brief (three sentences) and drops headline-plus-chrome stubs;
  the wall and challenge gates above catch the boilerplate that is longer.
* :data:`SHORT_PAGE_CHARS` = 1500. The phrase-based gates (soft 404, login,
  consent, subscribe-to-read) fire only on a text shorter than this, so an
  article that merely MENTIONS a login or a cookie banner is never refused.
  It is the follow step's own ``FULL_BODY_CHARS``: at or above it a body IS
  the article.
* :data:`DEFAULT_MAX_LINK_DENSITY` = 0.5. Measured on the synthetic
  fixtures in ``tests/data_pkg/test_page_kind.py``: an article with a
  related-links box sits under 0.15; a section front sits above 0.7.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .page_meta import PageMeta

__all__ = [
    "DEFAULT_MAX_LINK_DENSITY",
    "DEFAULT_MIN_TEXT_CHARS",
    "REFUSALS",
    "SHORT_PAGE_CHARS",
    "Verdict",
    "assess",
    "content_type_refusal",
    "status_refusal",
    "wall_host",
]

DEFAULT_MIN_TEXT_CHARS = 400
DEFAULT_MAX_LINK_DENSITY = 0.5
SHORT_PAGE_CHARS = 1500

REFUSALS: tuple[str, ...] = (
    "unsupported_content_type", "too_large", "blocked_by_challenge", "wall_page", "soft_404",
    "login_wall", "consent_wall", "paywall", "too_little_text", "mostly_navigation",
    "no_text_extracted",
)

_HTML_TYPES = ("text/html", "application/xhtml")
#: A title/h1 that IS a not-found page whatever the length of the text…
_NOT_FOUND_STRONG = re.compile(
    r"(^\s*404\b|\b404\b.*not found|error 404|404 error|page not found|seite nicht gefunden"
    r"|page introuvable|página no encontrada)", re.I)
#: …and one that reads so only beside a short text ("Missing hikers not found"
#: is a headline).
_NOT_FOUND_TITLE = re.compile(r"(not found|(?:does not|doesn't) exist|no longer available)", re.I)
_NOT_FOUND_PATH = re.compile(r"/(?:404|not-?found|page-?not-?found|error/?404)(?:[/.]|$)", re.I)
_NOT_FOUND_TEXT = ("page not found", "page you requested", "could not be found", "cannot be found",
                   "no longer available", "doesn't exist", "does not exist", "error 404", "404 error")
_LOGIN_PATH = re.compile(r"/(?:login|log-in|signin|sign-in|sign_in|account/login|auth)(?:[/?.]|$)", re.I)
_CONSENT_TOKENS = ("cookie", "consent", "privacy settings", "manage preferences",
                   "we and our partners", "legitimate interest")
_ACCEPT_TOKENS = ("accept all", "accept cookies", "i accept", "agree and continue", "reject all")
_PAYWALL_TEXT = ("subscribe to continue reading", "subscribe to read", "to continue reading, subscribe",
                 "this article is for subscribers", "this content is for subscribers",
                 "subscribers only", "already a subscriber", "create a free account to continue",
                 "to read the full article")


@dataclass
class Verdict:
    refusal: str | None = None
    detail: str | None = None
    flags: list[str] = field(default_factory=list)


def content_type_refusal(content_type: str | None, head: bytes) -> str | None:
    """The refusal detail for a non-HTML body, else ``None``."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct:
        if ct.startswith(_HTML_TYPES):
            return None
        if ct == "application/pdf" or head[:5] == b"%PDF-":
            return ("application/pdf: no PDF text extractor ships in the tree; a document "
                    "family would need one (pypdf or pdfminer.six) in the runtime image, a "
                    "page/size cap, an OCR decision for scanned files, and page-numbered records")
        return f"{ct}: the page kind reads HTML only"
    if head[:5] == b"%PDF-":
        return "application/pdf (sniffed): see the PDF note"
    sniff = head[:512].lstrip().lower()
    if sniff.startswith((b"<!doctype html", b"<html")) or b"<html" in sniff:
        return None
    return "no content type and the body does not start like HTML"


def status_refusal(status: int) -> tuple[str, str] | None:
    """``(reason, detail)`` for a status that IS a verdict on the page."""
    if status in (401, 403, 429):
        return "blocked_by_challenge", f"http_{status}"
    if status == 402:
        return "paywall", "http_402"
    return None


#: Sub-domain labels that name an access wall, never an article host. Only a
#: SUB-domain counts (``unblock.example.gov``), never a registered name itself.
_WALL_HOST_LABELS = ("unblock", "captcha")


def wall_host(url: str | None) -> str | None:
    """The host when ``url`` is on an access-wall sub-domain, else ``None``."""
    host = (urlsplit(url or "").hostname or "").lower()
    labels = host.split(".")
    if len(labels) >= 3 and labels[0] in _WALL_HOST_LABELS:
        return host
    return None


def _cross_host(requested_url: str | None, final_url: str) -> str | None:
    """The final host when the fetch was redirected off the requested host."""
    if not requested_url:
        return None
    want = (urlsplit(requested_url).hostname or "").lower()
    got = (urlsplit(final_url).hostname or "").lower()
    return got if want and got and want != got else None


def _has_any(text: str, needles: tuple[str, ...]) -> str | None:
    return next((n for n in needles if n in text), None)


def assess(
    *, text: str | None, raw: bytes, content_type: str | None, final_url: str, meta: PageMeta,
    min_text_chars: int = DEFAULT_MIN_TEXT_CHARS, max_link_density: float = DEFAULT_MAX_LINK_DENSITY,
    requested_url: str | None = None,
) -> Verdict:
    """The gate verdict for one 2xx HTML page (pure; runs in the worker
    thread beside extraction). ``requested_url`` is the address asked for;
    ``final_url`` where the redirects ended."""
    from ..analysts.deterministic_handlers._challenge_detect import (  # noqa: PLC0415 — lazy
        detect_challenge_page, detect_challenge_text,
    )
    from .page_text import match_wall_pattern, match_wall_phrase  # noqa: PLC0415

    tell = detect_challenge_page(raw, content_type)
    if tell is None and text:
        tell = detect_challenge_text(text)
    if tell is not None:
        return Verdict("blocked_by_challenge", tell)
    walled = wall_host(final_url) or wall_host(meta.canonical)
    if walled is not None:
        return Verdict("blocked_by_challenge", f"wall host: {walled}")
    moved = _cross_host(requested_url, final_url)
    if moved is not None and text and (hit := match_wall_phrase(text)) is not None:
        return Verdict("blocked_by_challenge", f"cross-host redirect to {moved}: {hit}")
    if text:
        wall = match_wall_pattern(text)
        if wall is not None:
            return Verdict("wall_page", wall)
    body = (text or "").strip()
    low = body.lower()
    short = len(body) < SHORT_PAGE_CHARS
    parts = urlsplit(final_url)
    for label, value in (("title", meta.html_title), ("h1", meta.h1)):
        if value and (_NOT_FOUND_STRONG.search(value) or (short and _NOT_FOUND_TITLE.search(value))):
            return Verdict("soft_404", f"{label}: {value[:120]}")
    if _NOT_FOUND_PATH.search(parts.path or ""):
        return Verdict("soft_404", f"path: {parts.path[:120]}")
    if short and (hit := _has_any(low, _NOT_FOUND_TEXT)):
        return Verdict("soft_404", f"text: {hit}")
    if short and (meta.password_field or _LOGIN_PATH.search(parts.path or "")):
        return Verdict("login_wall", "password field" if meta.password_field else f"path: {parts.path[:120]}")
    host = (parts.hostname or "").lower()
    if host.startswith("consent.") or re.search(r"/consent(?:[/?.]|$)", parts.path or "", re.I):
        return Verdict("consent_wall", f"url: {host}{parts.path[:80]}")
    if short and _has_any(low, _ACCEPT_TOKENS) and _has_any(low, _CONSENT_TOKENS):
        return Verdict("consent_wall", "accept-cookies interstitial")
    if short and meta.paywall_declared:
        return Verdict("paywall", "isAccessibleForFree=false and a short text")
    if short and (hit := _has_any(low, _PAYWALL_TEXT)):
        return Verdict("paywall", f"text: {hit}")
    if not body:
        return Verdict("no_text_extracted", "the extractor found no main text")
    if len(body) < min_text_chars:
        return Verdict("too_little_text", f"{len(body)} < {min_text_chars} chars")
    if meta.link_density >= max_link_density:
        return Verdict("mostly_navigation", f"link density {meta.link_density:.2f} >= {max_link_density}")
    flags: list[str] = []
    if meta.paywall_declared:
        flags.append("paywall_declared")
    if meta.canonical_basis == "declared_cross_site_ignored":
        flags.append("cross_site_canonical_ignored")
    return Verdict(None, None, flags)


def summary(verdict: Verdict) -> dict[str, Any]:
    return {"refusal": verdict.refusal, "detail": verdict.detail, "flags": list(verdict.flags)}
