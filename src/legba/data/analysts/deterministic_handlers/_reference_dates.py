# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R2 — WHERE a page's publish date came from, and what each source is worth.

Extracted from :mod:`._reference_page` (wave O, lane o2) when the three-source
extractor became a six-rung LADDER with a precedence order, a disagreement
record and a language-sensitive prose reader. ``_reference_page`` imports these
names back ONE WAY and re-exports them, so every caller and test is unchanged.

WHY THIS WIDENED. The three original mechanisms (JSON-LD, ``<meta>``,
``<time datetime>``, all in ISO) are what NEWSROOM CMSs emit. Official primary
sources frequently emit none of them, and the top-ups measured it:

  * ``bundestag.de`` press releases carry ``<meta name="date"
    content="25.09.2026">`` — the key was already in the table and the VALUE
    was refused, because the parser only ever read ``YYYY-MM-DD``;
  * ``radio.gov.pk`` news items carry no JSON-LD, no dated ``<meta>``, no
    ``<time>`` and a ``DD-MM-YYYY`` URL the URL parser does not read — only the
    visible ``September 26, 2026`` above the article;
  * ``opcw.org`` news carries only a visible ``9 July 2026`` dateline and an
    HTTP ``Last-Modified`` of whenever the CDN last refreshed the object.

So a government, an IGO and a state broadcaster were all INADMISSIBLE to a
reference while an ordinary newspaper was not — the ladder was upside down.

WHAT DID NOT CHANGE, AND MUST NOT. The date is still the PAGE's, never the
model's, and a date this module cannot find is still NOT a date: the caller
gets ``None`` and the date gate drops the development. Every rung names itself
in ``date_source``, which is carried onto the reference row and shown to the
reader, so an accepted item can always be re-argued against the thing that
dated it. Nothing here guesses.

THE MASTHEAD, which is the one thing this module exists to keep out. R1 run 2
shipped a FALSE development — "Israel Katz… has been named the new Defence
Minister", span verbatim, URL really fetched — because the page declared no
date and the model read the site's masthead ("Wednesday, Sep 16, 2026"). The
article was from November 2024. Reading VISIBLE PROSE at all is therefore a
reversal, and it is fenced five ways:

  1. the dateline is read from the EXTRACTED TEXT, so everything
     ``html_to_text`` discards — ``head``, ``nav``, ``footer``, ``aside``,
     ``script``, ``form``, ``button`` — is gone before the rung is handed
     anything. NOTE what this does NOT cover: ``<header>`` is a BLOCK tag, not
     a dropped one, because its content is frequently the headline. A masthead
     therefore does reach the text, and fence 4 is what deals with it.
  2. only the first :data:`DATELINE_SCAN_CHARS` characters are scanned;
  3. only a CLOSED set of unambiguous formats is read, with full month names
     in a closed set of languages chosen by the page's own ``lang`` hint —
     never ``03/04/2026``, never ``Sep 16, 2026``, never a bare year, never
     "3 hours ago";
  4. A DATE CARRYING A WEEKDAY OR A CLOCK IS A MASTHEAD AND IS DISCARDED. This
     is the R1 item's own shape ("Wednesday, Sep 16, 2026") and it is also
     ``radio.gov.pk``'s site clock ("Saturday, 26 September 2026, 08:44:17
     pm") sitting two lines above that page's real dateline ("September 26,
     2026"). The rule is not a heuristic dressed up: a masthead names the
     weekday because it is telling a reader what day it is NOW, and a dateline
     written to date a document does not.
  5. what survives all of that must be exactly ONE DISTINCT date. Two
     different dates in the opening is what an index page, an archive listing
     or a two-dateline page looks like, and that is not a dateline.

And it sits second-from-bottom on the ladder, so it only ever answers for a
page that declared nothing machine-readable at all.

THE PRECEDENCE, strongest first. Every rung is evaluated; the STRONGEST that
answers wins, and every other rung that answered with a DIFFERENT date is
recorded in :attr:`PageDate.disagreements` rather than discarded, because a
page whose metadata contradicts its own dateline is a fact a reader needs.

  1. ``json-ld``            — ``datePublished`` / ``dateCreated`` / ``uploadDate``
  2. ``meta:<key>``         — the publish-time ``<meta>`` tags, in KEY order
  3. ``time-datetime``      — ``<time datetime="…">``
  4. ``url-path``           — the date the URL itself embeds
  5. ``dateline:<lang>``    — one unambiguous visible dateline (see above)
  6. ``http_last_modified`` — the HTTP header, OFFICIAL-CLASS HOSTS ONLY

Rung 6 is last and fenced to government / IGO hosts because a
``Last-Modified`` is a FILE mtime: on a news CDN it tracks the cache, not the
publication, and the ``opcw.org`` fixture in the tests shows it landing 79 days
after the dateline on the very same page. On an official publisher's static
document server it is frequently the only date there is, which is why it is
admitted at all — as the weakest rung, named in the row, never silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from email.utils import parsedate_to_datetime
from typing import Iterable, Mapping
from urllib.parse import urlsplit

from ..._url_canon import url_embedded_date

# ---------------------------------------------------------------------------
# The ``date_source`` vocabulary
# ---------------------------------------------------------------------------

#: No rung answered. The date gate rejects the development.
DATE_SOURCE_NONE = "none"
DATE_SOURCE_JSON_LD = "json-ld"
DATE_SOURCE_TIME_ELEMENT = "time-datetime"
DATE_SOURCE_URL_PATH = "url-path"
DATE_SOURCE_LAST_MODIFIED = "http_last_modified"

#: ``meta:<key>`` and ``dateline:<lang>`` are minted per page from the key /
#: language that actually answered, so the row names the exact tag or
#: vocabulary a reader would have to go and look at.
DATE_SOURCE_META_PREFIX = "meta:"
DATE_SOURCE_DATELINE_PREFIX = "dateline:"

#: The ladder, strongest first. The order is the module's whole contract and
#: the tests pin it; a rung added here is added to the docstring above too.
DATE_SOURCE_PRECEDENCE: tuple[str, ...] = (
    DATE_SOURCE_JSON_LD,
    DATE_SOURCE_META_PREFIX,
    DATE_SOURCE_TIME_ELEMENT,
    DATE_SOURCE_URL_PATH,
    DATE_SOURCE_DATELINE_PREFIX,
    DATE_SOURCE_LAST_MODIFIED,
)

_LD_KEYS = ("datePublished", "dateCreated", "uploadDate")

#: ``<meta>`` names/properties that carry a PUBLISH time, IN PRECEDENCE ORDER.
#:
#: The order is now load-bearing and it was not before: the previous reader
#: walked the page's ``<meta>`` tags in DOCUMENT order and returned the first
#: whose key was in this set, so a CMS that emits ``article:modified_time``
#: above ``article:published_time`` dated the page by its last edit while this
#: table said otherwise. The keys are walked in THIS order now.
#:
#: ``article:modified_time`` stays last and deliberately: a modified time is an
#: upper bound on the publish time, so a page carrying only that is dated no
#: EARLIER than the value — which still keeps a November-2024 article out of a
#: September-2026 window.
_META_KEYS: tuple[str, ...] = (
    "article:published_time", "og:published_time", "publishdate", "pubdate",
    "date", "dc.date", "dc.date.issued", "dcterms.date", "dcterms.created",
    "sailthru.date", "parsely-pub-date", "cxenseparse:recs:publishtime",
    "citation_publication_date", "article:modified_time",
)

# ---------------------------------------------------------------------------
# The closed format set
# ---------------------------------------------------------------------------

#: ``YYYY-MM-DD`` anywhere in a value. The original (and still the only)
#: machine-readable shape; an ISO timestamp is matched by its date half.
_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

#: ``D.M.YYYY`` / ``D/M/YYYY``. Read DAY-FIRST and only when that reading is
#: UNAMBIGUOUS — see :func:`_dotted_date`.
_DOTTED_RE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./]((?:19|20)\d{2})(?!\d)")

#: Full month names, per language. A closed set, chosen by the page's ``lang``
#: hint with English always available as the fallback vocabulary: an official
#: page that declares no language is overwhelmingly English, and a page that
#: declares one this table does not carry reads as undated rather than as
#: mis-dated. Abbreviations are deliberately ABSENT — "Sep 16, 2026" is the
#: masthead shape from the R1 failure, and a full month name is what a dateline
#: written for a reader actually uses.
_MONTHS: dict[str, tuple[str, ...]] = {
    "en": ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"),
    "de": ("januar", "februar", "märz", "april", "mai", "juni", "juli",
           "august", "september", "oktober", "november", "dezember"),
    "fr": ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"),
    "es": ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
           "agosto", "septiembre", "octubre", "noviembre", "diciembre"),
}

#: Weekday names, per language — fence 4's vocabulary. Abbreviations are
#: included here (unlike the month table) because DISCARDING on a weekday is
#: the safe direction: a missed weekday re-admits a masthead, an over-eager one
#: only costs a dateline the page can lose.
_WEEKDAYS: dict[str, tuple[str, ...]] = {
    "en": ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
           "sunday", "mon", "tue", "tues", "wed", "thu", "thur", "thurs",
           "fri", "sat", "sun"),
    "de": ("montag", "dienstag", "mittwoch", "donnerstag", "freitag",
           "samstag", "sonnabend", "sonntag"),
    "fr": ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi",
           "dimanche"),
    "es": ("lunes", "martes", "miércoles", "miercoles", "jueves", "viernes",
           "sábado", "sabado", "domingo"),
}

#: How far either side of a candidate date fence 4 looks for a weekday or a
#: clock. Wide enough for "Saturday, 26 September 2026, 08:44:17 pm" and narrow
#: enough that a weekday in the previous sentence does not reach it.
_MASTHEAD_CONTEXT_CHARS = 24

_CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}")

#: Languages whose conventional numeric date is DAY-first, so ``05.04.2026``
#: reads as 5 April. A dotted date on a page in any other language is accepted
#: only when its first component cannot be a month (see :func:`_dotted_date`).
_DAY_FIRST_LANGS = frozenset({
    "de", "fr", "es", "it", "pt", "nl", "pl", "cs", "sk", "da", "fi", "sv",
    "nb", "no", "ru", "uk", "tr", "ro", "hu", "el", "bg", "hr", "sl", "et",
    "lv", "lt", "sr", "id", "vi", "he", "ar", "fa", "ur", "hi",
})

#: How far into the extracted TEXT a dateline may sit. An article's own date
#: line is printed with the headline; 1,200 characters is the headline plus
#: standfirst plus the first paragraph on every page the top-ups measured, and
#: short enough that a related-items rail never reaches it.
DATELINE_SCAN_CHARS = 1200

# ---------------------------------------------------------------------------
# The official-host class — rung 6's fence
# ---------------------------------------------------------------------------

#: TLDs that are controlled registries for government / military / treaty
#: organisations.
_OFFICIAL_TLDS = frozenset({"gov", "mil", "int"})

#: Second-level labels that national registries reserve for government:
#: ``gov.uk``, ``gov.pk``, ``gouv.fr``, ``gob.mx``, ``go.jp``, ``govt.nz``.
_OFFICIAL_LABELS = frozenset({"gov", "gouv", "gob", "govt", "go", "mil"})

#: IGOs that publish under a generic TLD. Deliberately its own short list and
#: NOT ``_reference_fences.TIER1_SUFFIXES``: this answers a different question
#: — "does this host's web server plausibly stamp a document's own mtime?" —
#: and importing the tier table here would make the two modules circular.
_OFFICIAL_IGO_SUFFIXES = frozenset({
    "un.org", "ohchr.org", "unhcr.org", "unrwa.org", "iaea.org", "opcw.org",
    "imf.org", "worldbank.org", "oecd.org", "wto.org", "bis.org",
    "icrc.org", "ifrc.org", "iea.org", "opec.org", "fatf-gafi.org",
    "icj-cij.org", "europa.eu",
})


def is_official_host(host: str) -> bool:
    """Is ``host`` a government / military / IGO publisher? Rung 6's gate.

    Label-boundary matching throughout, so ``radio.gov.pk`` and ``state.gov``
    qualify and ``notgov.example`` never does. Fails SAFE: a host this rule
    does not recognise simply does not get its ``Last-Modified`` read.
    """
    host = (host or "").strip().lower().removeprefix("www.")
    if not host:
        return False
    labels = host.split(".")
    if labels[-1] in _OFFICIAL_TLDS:
        return True
    if len(labels) >= 2 and labels[-2] in _OFFICIAL_LABELS:
        return True
    for suffix in _OFFICIAL_IGO_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


# ---------------------------------------------------------------------------
# Parsing one value in the closed format set
# ---------------------------------------------------------------------------


def parse_iso_date(value: str | None) -> date | None:
    """``YYYY-MM-DD`` → ``date``, or ``None``. Never raises.

    The GATE's parser, unchanged: it reads the one machine-readable shape and
    nothing else, so a value that reached the row by some other rung has
    already been normalised to ISO by the time the gate sees it.
    """
    if not value:
        return None
    match = _ISO_RE.search(str(value))
    if not match:
        return None
    try:
        return date(
            int(match.group(1)), int(match.group(2)), int(match.group(3))
        )
    except ValueError:
        return None


def _iso(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _dotted_date(value: str, *, day_first: bool) -> str | None:
    """``25.09.2026`` → ``2026-09-25``, or ``None`` when it is AMBIGUOUS.

    A dotted or slashed pair is read day-first when the page's language says
    so, and otherwise only when the first component cannot be a month (>12) or
    the two components are equal. ``03/04/2026`` on an English page is 3 April
    to half the world and 4 March to the other half, and a reference is not the
    place to pick one — it reads as no date.
    """
    match = _DOTTED_RE.search(value)
    if not match:
        return None
    first, second, year = (
        int(match.group(1)), int(match.group(2)), int(match.group(3))
    )
    if day_first or first > 12 or first == second:
        return _iso(year, second, first)
    return None


def _month_res(lang: str) -> Iterable[tuple[str, re.Pattern[str]]]:
    """``(lang, pattern)`` for each month vocabulary this page may use."""
    langs = ["en"] if lang in ("", "en") else [lang, "en"]
    for code in langs:
        names = _MONTHS.get(code)
        if not names:
            continue
        alternation = "|".join(names)
        yield code, re.compile(
            # ``D Month YYYY`` and ``Month D, YYYY`` in ONE pattern, so a
            # single scan cannot read the same characters as two dates.
            rf"(?<!\d)(?:(\d{{1,2}})\.?\s+({alternation})|({alternation})\s+"
            rf"(\d{{1,2}}))(?:st|nd|rd|th)?\,?\s+((?:19|20)\d{{2}})(?!\d)",
            re.IGNORECASE | re.UNICODE,
        )


def _named_month_dates(text: str, lang: str) -> set[str]:
    """Every ``D Month YYYY`` / ``Month D, YYYY`` date in ``text``, as ISO."""
    out: set[str] = set()
    for code, pattern in _month_res(lang):
        names = _MONTHS[code]
        for match in pattern.finditer(text):
            if is_masthead_context(text, match.start(), match.end(), lang):
                continue
            day = match.group(1) or match.group(4)
            name = (match.group(2) or match.group(3) or "").lower()
            if name not in names:
                continue
            iso = _iso(int(match.group(5)), names.index(name) + 1, int(day))
            if iso:
                out.add(iso)
    return out


def _weekday_res(lang: str) -> Iterable[re.Pattern[str]]:
    langs = ["en"] if lang in ("", "en") else [lang, "en"]
    for code in langs:
        names = _WEEKDAYS.get(code)
        if names:
            yield re.compile(
                rf"\b(?:{'|'.join(names)})\b\.?[,\s]*$",
                re.IGNORECASE | re.UNICODE,
            )


def is_masthead_context(text: str, start: int, end: int, lang: str) -> bool:
    """Fence 4 — is the date at ``text[start:end]`` a site masthead?

    True when a WEEKDAY NAME runs up to it or a CLOCK follows it within
    :data:`_MASTHEAD_CONTEXT_CHARS`. See the module banner: a masthead names
    the weekday because it is reporting what day it is now, and it is the exact
    shape of the R1 item that made this whole subsystem necessary.
    """
    before = text[max(0, start - _MASTHEAD_CONTEXT_CHARS):start]
    if any(pattern.search(before) for pattern in _weekday_res(lang)):
        return True
    after = text[end:end + _MASTHEAD_CONTEXT_CHARS]
    return bool(_CLOCK_RE.search(after))


# ---------------------------------------------------------------------------
# The page's own language hint
# ---------------------------------------------------------------------------

_HTML_LANG_RE = re.compile(
    r"<html[^>]*\blang\s*=\s*[\"\']([A-Za-z]{2})", re.IGNORECASE
)
_CONTENT_LANG_RE = re.compile(
    r"<meta[^>]*\bhttp-equiv\s*=\s*[\"\']content-language[\"\'][^>]*\bcontent"
    r"\s*=\s*[\"\']\s*([A-Za-z]{2})", re.IGNORECASE,
)


def page_language(raw: str, headers: Mapping[str, str] | None = None) -> str:
    """The page's two-letter language hint, lowercased, or ``""``.

    ``<html lang>`` first (the page's own declaration about itself), then the
    ``content-language`` meta, then the HTTP header. Used ONLY to choose a
    month vocabulary and a numeric-date convention — never to accept or reject
    a page.
    """
    match = _HTML_LANG_RE.search(raw or "") or _CONTENT_LANG_RE.search(raw or "")
    if match:
        return match.group(1).lower()
    for key, value in (headers or {}).items():
        if key.lower() == "content-language" and value:
            head = str(value).strip()[:2]
            if head.isalpha():
                return head.lower()
    return ""


# ---------------------------------------------------------------------------
# The rungs
# ---------------------------------------------------------------------------


def _ld_date(blocks: list[str]) -> str | None:
    for block in blocks:
        for key in _LD_KEYS:
            match = re.search(rf'"{key}"\s*:\s*"([^"]+)"', block)
            if match:
                found = _ISO_RE.search(match.group(1))
                if found:
                    return found.group(0)
    return None


_META_TAG_RE = re.compile(r"<meta[^>]+>", re.IGNORECASE)
_META_NAME_RE = re.compile(
    r'(?:property|name|itemprop)\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE
)
_META_CONTENT_RE = re.compile(
    r'content\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE
)


def _meta_contents(raw: str) -> dict[str, str]:
    """``{lowercased meta key: content}`` — FIRST occurrence of each key wins."""
    out: dict[str, str] = {}
    for match in _META_TAG_RE.finditer(raw or ""):
        tag = match.group(0)
        name = _META_NAME_RE.search(tag)
        content = _META_CONTENT_RE.search(tag)
        if not name or not content:
            continue
        out.setdefault(name.group(1).strip().lower(), content.group(1))
    return out


def _meta_date(raw: str, *, day_first: bool) -> tuple[str, str] | None:
    """``("meta:<key>", iso)`` off the publish-time metas, in KEY order.

    Returned SOURCE-FIRST, like every other rung in
    :func:`resolve_publish_date`'s ladder, because that is the shape the
    ladder appends.
    """
    contents = _meta_contents(raw)
    for key in _META_KEYS:
        value = contents.get(key)
        if not value:
            continue
        found = _ISO_RE.search(value)
        if found:
            return f"{DATE_SOURCE_META_PREFIX}{key}", found.group(0)
        # bundestag.de's ``<meta name="date" content="25.09.2026">``: the key
        # was always admissible and the VALUE was the thing being refused.
        dotted = _dotted_date(value, day_first=day_first)
        if dotted:
            return f"{DATE_SOURCE_META_PREFIX}{key}", dotted
    return None


_TIME_RE = re.compile(
    r'<time[^>]+datetime\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE
)


def _time_element_date(raw: str) -> str | None:
    match = _TIME_RE.search(raw or "")
    if not match:
        return None
    found = _ISO_RE.search(match.group(1))
    return found.group(0) if found else None


def _url_path_date(url: str) -> str | None:
    found = url_embedded_date(url or "")
    return found.isoformat() if found is not None else None


def dateline_date(text: str, lang: str) -> str | None:
    """The ONE unambiguous visible date in the opening of ``text``, or ``None``.

    Every fence this rung has is here: the scan window, the closed format set,
    the page's language vocabulary, and the rule that two DIFFERENT dates in
    the window mean there is no dateline. See the module banner for why a
    prose reader is admitted at all and what it must never do.
    """
    window = (text or "")[:DATELINE_SCAN_CHARS]
    if not window:
        return None
    found: set[str] = set()
    for match in _ISO_RE.finditer(window):
        if is_masthead_context(window, match.start(), match.end(), lang):
            continue
        iso = _iso(
            int(match.group(1)), int(match.group(2)), int(match.group(3))
        )
        if iso:
            found.add(iso)
    for match in _DOTTED_RE.finditer(window):
        if is_masthead_context(window, match.start(), match.end(), lang):
            continue
        dotted = _dotted_date(
            match.group(0), day_first=lang in _DAY_FIRST_LANGS
        )
        if dotted:
            found.add(dotted)
    found |= _named_month_dates(window, lang)
    return found.pop() if len(found) == 1 else None


def _last_modified_date(
    headers: Mapping[str, str] | None, host: str,
) -> str | None:
    """The HTTP ``Last-Modified``, for an official-class host only."""
    if not headers or not is_official_host(host):
        return None
    for key, value in headers.items():
        if key.lower() != "last-modified" or not value:
            continue
        try:
            return parsedate_to_datetime(str(value)).date().isoformat()
        except (TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PageDate:
    """One page's publish date, the rung that produced it, and what dissented.

    ``disagreements`` holds ``"<source>=<YYYY-MM-DD>"`` for every OTHER rung
    that answered with a different date, strongest first. It is a RECORD, not a
    rejection: the strongest rung still wins and the development is still
    admissible, because a page whose CDN mtime is three months after its
    dateline is an ordinary page — but the reader is told, and a later audit
    can find every row where the page argued with itself.
    """

    value: str | None = None
    source: str = DATE_SOURCE_NONE
    disagreements: tuple[str, ...] = ()

    def as_tuple(self) -> tuple[str | None, str]:
        """The pre-lane-o2 return shape, for callers that want only the date."""
        return self.value, self.source


def resolve_publish_date(
    raw: str,
    ldjson: list[str],
    *,
    url: str = "",
    text: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> PageDate:
    """Walk the whole ladder over ONE page and return the strongest answer.

    Every rung is evaluated even after one has answered, because the
    disagreement record is the point: a silent first-match-wins cannot tell a
    reader that the metadata and the dateline contradict each other.

    ``text`` is the EXTRACTED text (``coerce_page_text``'s first element). A
    caller that does not pass it simply does not get the dateline rung — which
    is the honest degradation, since the dateline's first fence is that the
    chrome has already been stripped out of what it reads.
    """
    lang = page_language(raw, headers)
    day_first = lang in _DAY_FIRST_LANGS
    host = ""
    try:
        host = (urlsplit((url or "").strip()).hostname or "").lower()
    except ValueError:
        host = ""

    rungs: list[tuple[str, str | None]] = [
        (DATE_SOURCE_JSON_LD, _ld_date(ldjson or [])),
    ]
    meta = _meta_date(raw or "", day_first=day_first)
    rungs.append(meta if meta else (DATE_SOURCE_META_PREFIX, None))
    rungs.append((DATE_SOURCE_TIME_ELEMENT, _time_element_date(raw or "")))
    rungs.append((DATE_SOURCE_URL_PATH, _url_path_date(url)))
    rungs.append((
        f"{DATE_SOURCE_DATELINE_PREFIX}{lang or 'en'}",
        dateline_date(text or "", lang),
    ))
    rungs.append((DATE_SOURCE_LAST_MODIFIED, _last_modified_date(headers, host)))

    answered = [(source, value) for source, value in rungs if value]
    if not answered:
        return PageDate()
    source, value = answered[0]
    return PageDate(
        value=value,
        source=source,
        disagreements=tuple(
            f"{other}={found}" for other, found in answered[1:]
            if found != value
        ),
    )


def extract_publish_date(
    raw: str,
    ldjson: list[str],
    *,
    url: str = "",
    text: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[str | None, str]:
    """``(YYYY-MM-DD | None, date_source)`` — :func:`resolve_publish_date`'s
    two-value shape, kept for every caller that does not need the dissent."""
    return resolve_publish_date(
        raw, ldjson, url=url, text=text, headers=headers
    ).as_tuple()


__all__ = [
    "DATELINE_SCAN_CHARS",
    "DATE_SOURCE_DATELINE_PREFIX",
    "DATE_SOURCE_JSON_LD",
    "DATE_SOURCE_LAST_MODIFIED",
    "DATE_SOURCE_META_PREFIX",
    "DATE_SOURCE_NONE",
    "DATE_SOURCE_PRECEDENCE",
    "DATE_SOURCE_TIME_ELEMENT",
    "DATE_SOURCE_URL_PATH",
    "PageDate",
    "dateline_date",
    "extract_publish_date",
    "is_masthead_context",
    "is_official_host",
    "page_language",
    "parse_iso_date",
    "resolve_publish_date",
]
