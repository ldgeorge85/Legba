# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document-IDENTITY canonicalisation — URL and wire-headline, stdlib only.

CW-7. Three surfaces have to agree on "is this the same document": the
target-side dedupe handler (:mod:`legba.data.filters.dedupe`), the ingest-side
dedupe engine (:mod:`legba.data.filters.ingest_dedupe`), and the per-source
baseline's backstop ``content_hash`` (:mod:`legba.data.sources.baseline`). They
did not: the baseline hashed the RAW url and the RAW title while the tiers
hashed canonicalised forms, so its fallback key silently disagreed with the
thing it was a fallback for.

This module is where the agreement lives. It imports NOTHING but the standard
library, and it sits at ``legba.data`` level rather than under ``filters/``
for one concrete reason: ``sources.baseline`` needs it, ``filters/__init__``
imports ``filters._contract`` which imports ``sources._contract``, and a
``sources -> filters`` edge closes that loop into a circular import. A pure
identity helper has no business owning a package cycle.

The K-4 R3 sampling is what put these classes on the list. Duplicate documents
that the keys cannot see do not merely waste rows: they double-count every
precision measurement taken off the stream (the round's own worksheet had to
add a same-URL dedupe step because one Guardian live blog was re-ingested as
seven distinct signals matching one question), and they double-flag every
consumer downstream.
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = [
    "canonical_url",
    "normalize_wire_title",
    "strip_www",
    "url_embedded_date",
]


def strip_www(host: str) -> str:
    """Drop a leading ``www.`` label from a hostname.

    ``www`` ONLY — not ``www2``, ``www3`` or ``wwwtest``, which are genuinely
    different origins on plenty of sites. Collapsing those would mint FALSE
    duplicate links, which is destructive in a way a missed duplicate is not.
    Never strips the whole host, and idempotent.
    """
    low = (host or "").lower()
    if not low.startswith("www."):
        return low
    rest = low[4:]
    return rest if rest else low


# ---------------------------------------------------------------------------
# Publisher article-id identity (K-4 R4 F3)
# ---------------------------------------------------------------------------
#
# The slug-rewrite class: many CMSs address an article by a NUMERIC id and
# treat the trailing slug as decoration — Dawn's ``/news/2020547/<slug>``
# serves the same article under every headline the story has ever had, so a
# re-headlined developing story re-ingests as a "new" URL and every canonical-
# URL dedupe surface is defeated (K-4 R4 §9 F3: 258 population signals → 245
# distinct URLs; two of the round's 71 sampled pairs were one Dawn article at
# two slugs). The document identity is the id, not the slug.
#
# The rule is deliberately narrow, and MEASURED before it shipped (2026-08-10,
# the 80,000 newest stored ``canonical_url`` values, 74,251 distinct): a path
# segment that is PURE digits, at least :data:`_ARTICLE_ID_MIN_DIGITS` long
# and not date-shaped, sitting at the final or penultimate position, with at
# most ONE following segment that carries a letter (a slug, not a second id),
# truncates the path after the id segment. On the live sample it fired on
# 19,060 URLs and created exactly 62 NEW collapse groups — every one of them
# the same-article-id re-headline class (Dawn, gdnonline, presstv,
# muswellbrook), ZERO cross-story collapses. The date guard exists because an
# 8-digit ``YYYYMMDD`` segment is a publication DATE shared by that day's
# whole output, and collapsing on it would mint false duplicates — the
# destructive direction this module refuses throughout.

#: Minimum digits for a path segment to read as a publisher article id.
#: 6 keeps years (4), months/days (2) and small ordinals out.
_ARTICLE_ID_MIN_DIGITS = 6

_PURE_DIGITS_RE = re.compile(r"^\d+$")
_HAS_LETTER_RE = re.compile(r"[A-Za-z]")


def _is_dateish_number(seg: str) -> bool:
    """True when a pure-digit segment is plausibly a date, not an article id.

    ``YYYYMMDD`` (8 digits, 19xx/20xx, valid month+day) and ``YYYYMM`` (6
    digits, valid month). A date segment is shared by every article published
    that day — treating it as an id would collapse a whole day's output."""
    if len(seg) == 8 and seg[:2] in ("19", "20"):
        month, day = int(seg[4:6]), int(seg[6:8])
        return 1 <= month <= 12 and 1 <= day <= 31
    if len(seg) == 6 and seg[:2] in ("19", "20"):
        return 1 <= int(seg[4:6]) <= 12
    return False


def _strip_article_slug(path: str) -> str:
    """Truncate a path after its publisher article-id segment (see the rule
    banner above). Returns ``path`` UNCHANGED — byte for byte — whenever the
    rule does not fire, so every URL outside the measured class keeps its
    exact prior identity. Idempotent: a stripped path ends in its id segment,
    where the rule rebuilds the same string."""
    if not path.startswith("/"):
        return path  # relative/degenerate input — identity untouched
    segs = [s for s in path.split("/") if s]
    if not segs:
        return path
    id_index = None
    for i, seg in enumerate(segs):
        if (
            len(seg) >= _ARTICLE_ID_MIN_DIGITS
            and _PURE_DIGITS_RE.match(seg)
            and not _is_dateish_number(seg)
        ):
            id_index = i
    if id_index is None:
        return path
    trailing = segs[id_index + 1:]
    if len(trailing) > 1:
        return path  # deep hierarchy after the id — shape unknown, hands off
    if len(trailing) == 1 and not _HAS_LETTER_RE.search(trailing[0]):
        return path  # a second number is not a slug — hands off
    rebuilt = "/" + "/".join(segs[: id_index + 1])
    # A bare-id path with no slug and no trailing slash is already canonical:
    # return the ORIGINAL bytes so the no-op case stays a no-op.
    return rebuilt if rebuilt != path else path


def canonical_url(url: str) -> str:
    """Canonicalize a URL for identity hashing.

    Rules:

      * Lowercase scheme + host.
      * Strip a leading ``www.`` label (CW-7).
      * Strip fragment (``#...``).
      * Sort query params lexicographically by name; preserve duplicate keys
        via stable order on (name, value).
      * Drop empty query params.
      * Strip default ports (``:80`` http, ``:443`` https).
      * Preserve path case (paths are case-sensitive per RFC 3986).
      * Truncate the path after a publisher ARTICLE-ID segment (K-4 R4 F3),
        so a re-headlined story is one document at every slug it has worn —
        see :func:`_strip_article_slug` for the measured, deliberately narrow
        rule and its date guard.

    Idempotent — running it on an already-canonical URL is a no-op.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()

    scheme = (parts.scheme or "").lower()
    netloc = strip_www(parts.hostname or "")
    if parts.port is not None:
        default = (scheme == "http" and parts.port == 80) or (
            scheme == "https" and parts.port == 443
        )
        if not default:
            netloc = f"{netloc}:{parts.port}"
    if parts.username or parts.password:
        # Preserve userinfo only if present (rare for normal URLs).
        user = parts.username or ""
        pw = f":{parts.password}" if parts.password else ""
        netloc = f"{user}{pw}@{netloc}" if user or pw else netloc

    # Sort query: parse, drop empty-value pairs, sort by (key, value).
    query_pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if k
    ]
    query_pairs.sort(key=lambda kv: (kv[0], kv[1]))
    query = urlencode(query_pairs, doseq=False)

    path = _strip_article_slug(parts.path or "")

    return urlunsplit((scheme, netloc, path, query, ""))


#: Agency wire REVISION markers. A wire story is re-sent as it develops and
#: the revision rides in the title: Yonhap "(LEAD)" / "(2nd LD)", Reuters
#: "UPDATE 1-", AFP "(Update)", Kyodo "URGENT:", PTI "(Eds: ...)". The body
#: often changes with the revision — tier 2 will still see those apart, and
#: correctly — but the very common marker-only re-send was landing as a fresh
#: document because the title differed by five characters.
#:
#: Anchored at the START only. A parenthetical inside a headline is content
#: ("Cabinet clears the bill (with conditions)"), and stripping those would
#: collapse genuinely different stories.
_WIRE_REVISION_RE = re.compile(
    r"""^\s*(?:
        \((?:\d+(?:st|nd|rd|th)\s+)?
          (?:LEAD|LD|LD[\s\-]?\w+|UPDATE(?:\s*\d+)?|URGENT|CORRECTED|
             CORRECTION|REFILE|RECAST|REPEAT|EDS?:[^)]*|ATTN:[^)]*)
        \)
        | (?:UPDATE|URGENT|CORRECTED|CORRECTION|REFILE|RECAST|BREAKING)
          \s*\d*\s*[-:–—]
    )\s*""",
    re.IGNORECASE | re.VERBOSE,
)

#: A re-sent revision can stack markers ("(LEAD) (2nd LD) ..."); bounded so a
#: pathological title cannot spin.
_MAX_REVISION_STRIPS = 4


def normalize_wire_title(title: str) -> str:
    """Strip leading agency revision markers from a headline.

    Returns the title unchanged when nothing matches, and NEVER returns
    empty — a headline that is nothing but markers keeps its original text,
    because an empty identity key would collapse every such story onto one
    another, which is worse than the duplicate it was trying to catch.
    """
    text = str(title or "")
    out = text
    for _ in range(_MAX_REVISION_STRIPS):
        stripped = _WIRE_REVISION_RE.sub("", out, count=1)
        if stripped == out:
            break
        out = stripped
    out = out.strip()
    return out or text


# ---------------------------------------------------------------------------
# URL-embedded publication date (K-4 R4 F4 — the provenance AUDIT read)
# ---------------------------------------------------------------------------
#
# F4's measured defect: a FRESH signal carrying a STALE ``canonical_url`` —
# a Yonhap item whose body was unambiguously 6 Aug 2026 stored
# ``…/AEN20250710005751315``, a 2025-dated article id. A labeller trusting
# the URL grades the match ``temporal_stale``; a K-5 closer citing it cites
# the wrong article. Many publishers embed the publication date in the URL
# (path segments ``/2026/08/06/``, ``2026-08-05`` slugs, ``YYYYMMDD``-prefixed
# article ids), which makes the defect DETECTABLE: parse the date the URL
# claims and compare it against the dates the row actually carries. This
# helper is the parse half; ``scripts/audit_canonical_url_dates.py`` is the
# read-only audit that runs it over the stored stream. It deliberately powers
# an AUDIT, not an ingest-time rewrite — the report routes F4 to the
# data-quality queue, and silently "fixing" provenance on a heuristic would
# be a worse defect than the one it flags.

#: ``/YYYY/MM/DD/`` (or ``/YYYY/M/D/``) as consecutive path segments.
_URL_DATE_SEGMENTS_RE = re.compile(
    r"/((?:19|20)\d{2})/(\d{1,2})/(\d{1,2})(?=/|$)"
)

#: ``YYYY-MM-DD`` embedded anywhere in the path (slug dates).
_URL_DATE_DASHED_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})-(\d{2})-(\d{2})(?!\d)"
)

#: A digit run BEGINNING with a plausible ``YYYYMMDD`` — the wire-agency
#: article-id shape (Yonhap ``AEN20250710005751315``). Anchored at the run's
#: start (``(?<!\d)``) so digits later in an id can never fabricate a date.
_URL_DATE_COMPACT_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])"
)


def url_embedded_date(url: str):
    """The publication date a URL CLAIMS, or ``None``.

    Scans the path only (query strings carry ids, not dates), most-explicit
    pattern first: ``/YYYY/MM/DD/`` segments, then a ``YYYY-MM-DD`` slug
    date, then a ``YYYYMMDD``-prefixed digit run (wire-agency article ids).
    Every candidate is validated through :class:`datetime.date`, so a
    Feb-30-shaped id reads as no date rather than a wrong one. Returns the
    first valid parse; ``None`` for URLs that embed no date — absence of a
    claim is not a claim."""
    if not url:
        return None
    try:
        path = urlsplit(url.strip()).path or ""
    except ValueError:
        return None
    for pattern in (
        _URL_DATE_SEGMENTS_RE,
        _URL_DATE_DASHED_RE,
        _URL_DATE_COMPACT_RE,
    ):
        for m in pattern.finditer(path):
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue  # date-shaped but not a date (Feb 30) — keep looking
    return None
