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

import hashlib
import re
from datetime import date
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

__all__ = [
    "canonical_url",
    "canonical_url_hash",
    "levenshtein_distance",
    "normalize_wire_title",
    "normalized_levenshtein",
    "stored_canonical_url_hash",
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
# URL dedupe surface is defeated (measured: 258 population signals → 245
# distinct URLs; two of 71 sampled pairs were one Dawn article at
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


# ---------------------------------------------------------------------------
# Valueless IDENTIFIER query keys
# ---------------------------------------------------------------------------
#
# ``parse_qsl(keep_blank_values=False)`` drops a key with no value, which is
# right for a flag (``?amp``, ``?orig_query``) and catastrophic for the SPIP
# CMS, which addresses every article as ``/spip.php?article<N>``: all 1,503
# lefaso.net signals (1,398 distinct articles) canonicalised to
# ``https://lefaso.net/spip.php`` and tier 1 aliased them into one. MEASURED
# 2026-11-13 over every stored ``canonical_url`` carrying a query (42,696
# distinct): exactly four hosts carry a valueless key at all — lefaso.net
# (``article<N>``, 1,400 URLs collapsed into ONE group), sawatdeenetwork.com
# (``234-christianpfc``, 1 URL), eia.gov (``id``, 1 URL) and redwatch.org.au
# (``orig_query``, 1 URL). The rule: a valueless key that carries a DIGIT is
# the item's identifier (``article149046``, SPIP ``p123`` / ``breve42``,
# ``234-christianpfc``) and is KEPT, bare, in sorted position; a digit-free
# valueless key (a flag / tracking marker: ``id``, ``orig_query``, ``amp``)
# is dropped as before. On the live set the canonical form changes for
# lefaso.net (1,400 URLs) and sawatdeenetwork.com (1 URL) only.

_IDENTIFIER_KEY_RE = re.compile(r"^[A-Za-z_\-]*\d[\w\-.]*$")


def _valueless_identifier_keys(query: str) -> list[str]:
    """The query's valueless keys (``k`` or ``k=``) that read as identifiers."""
    if not query:
        return []
    out = []
    for k, v in parse_qsl(query, keep_blank_values=True):
        if k and v == "" and _IDENTIFIER_KEY_RE.match(k) and k not in out:
            out.append(k)
    return out


def _merge_identifier_keys(pairs: list[tuple[str, str]], id_keys: list[str]) -> str:
    """The sorted query with each identifier key written bare (``article123``)."""
    items = [(k, v) for k, v in pairs] + [(k, None) for k in id_keys]
    items.sort(key=lambda kv: (kv[0], kv[1] or ""))
    return "&".join(
        urlencode([(k, v)]) if v is not None else quote_plus(k) for k, v in items
    )


def canonical_url(url: str) -> str:
    """Canonicalize a URL for identity hashing.

    Rules:

      * Lowercase scheme + host.
      * Strip a leading ``www.`` label (CW-7).
      * Strip fragment (``#...``).
      * Sort query params lexicographically by name; preserve duplicate keys
        via stable order on (name, value).
      * Drop empty query params — except a valueless IDENTIFIER key
        (``spip.php?article123``), kept bare; see above.
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

    try:
        # ``.port`` parses lazily and raises on a malformed port
        # ("host:443:8443", "host:99999"). Before N-S that escaped this
        # function — a stored row carrying one made every tier-1 scan of its
        # tenant raise. Same answer as an unsplittable URL: identity is the
        # stripped string.
        port = parts.port
    except ValueError:
        return url.strip()
    scheme = (parts.scheme or "").lower()
    netloc = strip_www(parts.hostname or "")
    if port is not None:
        default = (scheme == "http" and port == 80) or (
            scheme == "https" and port == 443
        )
        if not default:
            netloc = f"{netloc}:{port}"
    if parts.username or parts.password:
        # Preserve userinfo only if present (rare for normal URLs).
        user = parts.username or ""
        pw = f":{parts.password}" if parts.password else ""
        netloc = f"{user}{pw}@{netloc}" if user or pw else netloc

    # Sort query: parse, drop empty-value pairs, sort by (key, value) — except
    # a valueless IDENTIFIER key (N-U, below), kept bare.
    query_pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if k
    ]
    query_pairs.sort(key=lambda kv: (kv[0], kv[1]))
    query = urlencode(query_pairs, doseq=False)
    id_keys = _valueless_identifier_keys(parts.query)
    if id_keys:
        query = _merge_identifier_keys(query_pairs, id_keys)

    path = _strip_article_slug(parts.path or "")

    return urlunsplit((scheme, netloc, path, query, ""))


def canonical_url_hash(url: str) -> str:
    """SHA-256 hex of :func:`canonical_url` — the tier-1 document-identity key.

    The ONE place the key is minted, so the probe side (a new signal's URL in
    ``filters.ingest_dedupe``) and the stored side (``signals.canonical_url_hash``,
    migration 0247, written on every insert path and by
    ``scripts/backfill_signal_url_hash.py``) can never hash differently.

    A stored key is only as current as the rules above: if :func:`canonical_url`
    ever changes what it returns, stored keys go stale silently (a missed
    duplicate, never a false one — but missed). Any change to the rules must
    re-run the backfill script with ``--recompute``; its ``--verify`` mode
    counts stale rows.
    """
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


def stored_canonical_url_hash(stored: str | None) -> str | None:
    """The value ``signals.canonical_url_hash`` carries for a stored
    ``canonical_url`` column value.

    ``None`` exactly when the column is NULL or ``''`` — the rows tier 1 has
    never looked at (its stored-side predicate was always ``canonical_url IS
    NOT NULL AND canonical_url <> ''``), so they need no key and never sit in
    the un-hashed fallback set. Every other value gets a key, even one whose
    canonical form is empty (``"   "``): that key is the hash of ``""``, which
    no probe can produce (a probe with an empty canonical form never looks
    up), so the row is correctly unmatchable instead of being re-scanned
    forever as "not yet hashed".
    """
    if stored is None or stored == "":
        return None
    return canonical_url_hash(stored)


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


# ---------------------------------------------------------------------------
# NEAR-VERBATIM distance (moved here 2026-09-23 from
# ``filters.dedupe._normalized_levenshtein``, which keeps the old private name
# as an alias so its two call sites and its tests resolve unchanged)
# ---------------------------------------------------------------------------
#
# WHY IT MOVED. The independence count at the composition floor
# (``analysts.source_independence``) has to ask the SAME question the tier-4
# dedupe filter asks — "is this the same dispatch under two mastheads" — and it
# lives in ``data.analysts``, which has no import edge to ``data.filters`` and
# should not grow one for a pure string function. This module is already the
# place the tree keeps document IDENTITY agreements (see the banner), imports
# nothing but the standard library, and is already imported by both sides.
# Copying the DP into a second file would have made two near-duplicate
# implementations of the near-duplicate test, which is its own joke.


def levenshtein_distance(a: str, b: str) -> int:
    """The exact Levenshtein (unit-cost insert/delete/substitute) distance.

    N-W (2026-11-13): bit-parallel (Myers 1999 as restated for global edit
    distance by Hyyrö 2001) instead of the O(n*m) cell-by-cell DP. The loop
    lag monitor named the DP as the innermost frame of every 2-plus-second
    block in ``event_clustering`` — about 0.3 ms per title pair, tens of
    thousands of pairs per tick, all on the runtime's event loop. The
    bit-parallel form does one handful of integer operations per character
    of the longer string (Python integers are arbitrary-width, so the
    pattern length is not bounded by a machine word) and returns the SAME
    integer the DP returned; ``tests/data_pkg/test_levenshtein_n_w.py``
    pins that against the DP kept verbatim as a fixture.
    """
    if a == b:
        return 0
    if len(a) > len(b):
        a, b = b, a             # the shorter string is the bit pattern
    m = len(a)
    if m == 0:
        return len(b)
    peq: dict[str, int] = {}
    bit = 1
    for ch in a:
        peq[ch] = peq.get(ch, 0) | bit
        bit <<= 1
    mask = (1 << m) - 1
    last = 1 << (m - 1)
    vp, vn, score = mask, 0, m
    for ch in b:
        x = peq.get(ch, 0) | vn
        d0 = ((((x & vp) + vp) ^ vp) | x) & mask
        hn = vp & d0
        hp = (vn | ~(d0 | vp)) & mask
        if hp & last:
            score += 1
        elif hn & last:
            score -= 1
        x = ((hp << 1) | 1) & mask
        vn = x & d0
        vp = ((hn << 1) | ~(d0 | x)) & mask
    return score


def normalized_levenshtein(a: str, b: str) -> float:
    """Return Levenshtein distance / max(len(a), len(b)).

    Pure Python, no third-party dependency (``rapidfuzz`` is not pinned);
    the distance itself is :func:`levenshtein_distance`'s bit-parallel
    walk (N-W, 2026-11-13), exact and roughly an order of magnitude faster
    than the DP it replaced on headline-length titles.
    """
    if a == b:
        return 0.0
    if not a or not b:
        return 1.0
    return levenshtein_distance(a, b) / float(max(len(a), len(b)))
