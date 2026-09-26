# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Anti-bot CHALLENGE / interstitial detection — the false-absence defect.

``evidence_archiver._match_wall_pattern`` already carried bot-challenge
phrasing (``are you a robot``, ``checking your browser before accessing``, …)
but gated EVERY pattern behind ``_WALL_MAX_CHARS = 500``. That cap was tuned on
the ≤500-char no-JS fallback pages the 2026-07-31 live audit actually found
(Le Monde x83 at 286 chars, irna x83 at 70 chars) and it does not reach a
modern challenge page: the FETCH_REVIEW probes measured AP's Cloudflare
interstitial at **5 507 B** and Times of Israel's at **12 604 B**. Both sail
past the cap, so today a challenge lands as a thin/failed extraction — i.e. it
reads downstream as *the web had nothing*.

That is a false absence, and it is a worse defect than the 403 itself: a desk
cannot tell "this publisher blocked us" from "nothing was published". This
module is the fix. It classifies a block as a block, at three tiers, and the
callers carry the verdict downstream under ONE name, ``blocked_by_challenge``:

  * :func:`detect_challenge_text` — over Trafilatura's extracted text, with
    **no length cap at all**. This is the (a′) widening: the discrimination
    comes from the RULES, not from a character count.
  * :func:`detect_challenge_page` — over the RAW response body, because the
    sharpest tells never survive extraction (DataDome's ``var dd={'rt'…``
    script, Cloudflare's ``/cdn-cgi/challenge-platform`` include). Scanning is
    bounded to the head of the body (:data:`_SCAN_BYTES`) as a COST bound —
    it is not a discriminator, and must not be read as one.
  * :func:`challenge_from_status` — HTTP 401/403/429. A status-only verdict,
    used when the body was never read (``raise_for_status`` inside a streaming
    fetch discards it). "A publisher refused us" is the honest reading of
    those three, whatever the body would have said.

**Rules are conjunctions.** Each entry in :data:`_CHALLENGE_RULES` is a tuple
of lowercase substrings that must ALL be present. A single-element rule is a
string with no plausible overlap with real prose (``cloudflare capcha page`` is
Times of Israel's own misspelled ``<title>``); anything that could appear in a
genuine article ("just a moment") is only ever a conjunct, and never paired
with a bare vendor name — an outage story about Cloudflare that also says "wait
just a moment" is a real article, and there is a test asserting it stays one.
The discrimination lives in the rules rather than in a length gate we already
know does not work; a residual false positive remains possible and costs the
derived-text upgrade, never the archived bytes.

What a detection does NOT do: it never discards archived bytes (the CAS write
is unchanged, and the bytes ARE the evidence that we were blocked), it never
touches the robots decision, and it never touches
``research_evidence.depth_for_license`` — the licence gate is the operator's.
"""
from __future__ import annotations

from typing import Iterable

#: The ONE name this verdict travels under: the archiver counter, the
#: ``web_evidence`` tool counter, the sidecar ``last_error`` prefix, and the
#: probe script's class column all use it, so a reader never has to map
#: vocabularies between layers.
BLOCKED_BY_CHALLENGE = "blocked_by_challenge"

#: HTTP statuses that ARE a block, with or without a readable body. 402
#: (payment required) is deliberately absent — that is a PAYWALL, a money and
#: licence decision, and FETCH_REVIEW §3(e) forbids routing around it.
BLOCKED_STATUSES: frozenset[int] = frozenset({401, 403, 429})

#: Cost bound on the raw-body scan. A challenge page is ENTIRELY inside its
#: first few kB; scanning the tail of a 1.4 MB publisher homepage buys nothing.
#: This is not a discriminator (see the module docstring) — the rules are.
_SCAN_BYTES = 262_144

#: Tells that survive Trafilatura extraction AND appear in raw HTML. Each
#: tuple is a CONJUNCTION (every substring must be present, case-insensitive).
_CHALLENGE_RULES: tuple[tuple[str, ...], ...] = (
    # Times of Israel's own <title>, misspelling included. Zero prose overlap.
    ("cloudflare capcha page",),
    # Cloudflare "Just a moment..." — the AP interstitial. "just a moment" is
    # ordinary English, so it is only ever a conjunct, and never with a bare
    # vendor name: an outage story about Cloudflare that also says "wait just a
    # moment" is a REAL article (there is a test for exactly that). The second
    # conjunct always has to be challenge-specific.
    ("just a moment", "enable javascript"),
    ("just a moment", "enable cookies"),
    ("just a moment", "review the security of your connection"),
    # Cloudflare managed-challenge / error-1020 phrasing.
    ("attention required! | cloudflare",),
    ("checking if the site connection is secure",),
    ("verifying you are human",),
    ("please stand by, while we are checking your browser",),
    ("ddos protection by cloudflare",),
    ("this process is automatic. your browser will redirect",),
    # DataDome — the Reuters 401 body ("Please enable JS and disable any ad
    # blocker"). Reuters is closed by ROBOTS (FETCH_REVIEW §2) and is never
    # fetched; the tell is kept because DataDome fronts other publishers too.
    ("enable js and disable any ad blocker",),
)

#: Tells that exist only in the markup/script and can never survive main-text
#: extraction — applied by :func:`detect_challenge_page` only.
_CHALLENGE_RAW_RULES: tuple[tuple[str, ...], ...] = (
    ("<title>just a moment",),
    ("/cdn-cgi/challenge-platform",),
    ("__cf_chl",),
    ("cf-browser-verification",),
    ("var dd={'rt'",),           # DataDome's inline config object
    ("var dd={", "captcha-delivery.com"),
    ("geo.captcha-delivery.com",),
    ("_incapsula_resource",),    # Imperva
    ("px-captcha",),             # PerimeterX
)


def _first_match(lowered: str, rules: Iterable[tuple[str, ...]]) -> str | None:
    """The first rule every conjunct of which is present in ``lowered``.

    Returned as a human-readable ``"a"+"b"`` label so the counter, the log line
    and the sidecar ``last_error`` all name the SAME tell.
    """
    for rule in rules:
        if all(token in lowered for token in rule):
            return "+".join(rule)
    return None


def detect_challenge_text(text: str) -> str | None:
    """The challenge tell in Trafilatura-extracted ``text``, else ``None``.

    **No length cap** — that is the entire point of this function (see the
    module docstring). ``_WALL_MAX_CHARS`` still gates the legacy short-page
    wall list in ``evidence_archiver``; it never gates this one.
    """
    if not text:
        return None
    return _first_match(text.lower(), _CHALLENGE_RULES)


def detect_challenge_page(
    body: bytes | None, content_type: str | None = None,
    encoding: str | None = None,
) -> str | None:
    """The challenge tell in a RAW response body, else ``None``.

    Applies both rule sets: the extraction-surviving prose tells and the
    markup/script-only ones. Non-textual bodies (an image, a PDF) are never
    scanned. Decoding is lossy on purpose (``errors="replace"``) — a challenge
    page's tells are ASCII and a decode failure must not silently mean
    "not blocked".
    """
    if not body:
        return None
    ctype = (content_type or "").lower()
    if ctype and not (
        ctype.startswith("text/")
        or "html" in ctype
        or "xml" in ctype
        or "json" in ctype
    ):
        return None
    head = body[:_SCAN_BYTES]
    try:
        lowered = head.decode(encoding or "utf-8", errors="replace").lower()
    except (LookupError, UnicodeDecodeError):
        lowered = head.decode("utf-8", errors="replace").lower()
    return (
        _first_match(lowered, _CHALLENGE_RULES)
        or _first_match(lowered, _CHALLENGE_RAW_RULES)
    )


def challenge_from_status(status_code: int | None) -> str | None:
    """``"http_<code>"`` when ``status_code`` is itself a block, else ``None``.

    Used where the body is unavailable: both fetch helpers call
    ``raise_for_status()`` inside the streaming context manager, so a 403's
    body is discarded before any caller can see it. The status alone is enough
    to stop reporting the outcome as an empty web.
    """
    if status_code is None:
        return None
    if int(status_code) in BLOCKED_STATUSES:
        return f"http_{int(status_code)}"
    return None


__all__ = [
    "BLOCKED_BY_CHALLENGE",
    "BLOCKED_STATUSES",
    "challenge_from_status",
    "detect_challenge_page",
    "detect_challenge_text",
]
