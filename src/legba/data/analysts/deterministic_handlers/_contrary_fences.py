# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE FOUR FENCES on ``contradicts`` — written against three live false rows.

WHY THIS MODULE EXISTS, IN NUMBERS. The contrary-evidence pass's FIRST live run
(2026-09-25 22:26:54–22:37:44Z, run ``abfd43d6``, $0) contended 60 claims: 60
web_search + 142 web_fetch, 57 ``none_found``, and **3 ``contradicts``, all
three false**. Every one of them is a page the free rung returns when the real
answer is absent:

  1. ``energy_security`` / ``country_g20_in`` — the claim was about India's
     energy-security pressure; the "counter-evidence" was
     ``en.wikipedia.org/wiki/Strait_of_Hormuz``, published (by its own
     metadata) **2018-12-10**, quoting a sentence about Gulf shipping that
     names no party to the claim. Subject overlap with the matched sentence:
     **2 tokens** (``route``, ``supply``) — enough for R2's floor, and about
     nothing.
  2. ``escalation`` / ``country_g20_kr`` — the claim was about a DMZ land-mine
     explosion in this window; the counter page was
     ``en.wikipedia.org/wiki/Korean_Demilitarized_Zone``, **2007-05-17**,
     quoting the DEFINITION of the DMZ.
  3. ``military_posture`` / ``country_g20_ru`` (the uncalibrated negation leg) —
     one page was ``en.wikipedia.org/wiki/Russia`` (384 chars extracted), the
     other an FPRI article dated **2026-08-04** quoting "In mid-May, Belarus and
     Russia announced…" — a different window, and a lone page.

Search health during that run says the rest: SearXNG reported
``unresponsive_engines`` five times and the free rung's hits were dominated by
wikipedia.org, merriam-webster.com and generic corporate pages. An encyclopedia
article is what a search engine hands back when it has nothing; it is not
reporting, it carries no publication date for the claim's window, and treating
it as opposition is how a retrieval pass manufactures a disagreement for the
fleet to write up. That is the failure R2 shipped zero pairs rather than risk.

THE FOUR FENCES, AND WHAT EACH ONE COSTS A PAGE
------------------------------------------------
Each fence is stated as the narrowest rule that kills its live row, and each
writes its own number onto the record so the fence is measurable rather than
merely asserted.

  **F1 — HOST CLASS.** A page from a reference / encyclopedia / dictionary host
  can never carry ``contradicts``. It is recorded in ``refs`` with
  ``host_class: reference`` and COUNTS TOWARD NOTHING: its own ref stance drops
  to ``none_found``. Kills rows 1 and 2, and the ``Russia`` ref of row 3.

  **F2 — DATED, INSIDE THE CLAIM'S WINDOW.** An undated page cannot contradict,
  and a dated page must fall inside the claim's own evidence window widened by
  the pass's ``contention_ttl_hours``. A counter page from another year is
  ``qualifies`` AT MOST. Kills row 3's FPRI page (2026-08 against a 2026-09
  window) and would have killed rows 1 and 2 independently (2018, 2007).

  **F3 — THE SUBJECT IN THE MATCHED SENTENCE.** The polarity match must occur in
  a sentence that also carries the claim's subject tokens, and the OVERLAP COUNT
  goes on the row beside ``query_novel_tokens``. The floor is the caller's — R2's
  two for the calibrated leg, three for the uncalibrated one — so this fence
  re-states an existing rule in a column somebody can average, and refuses a
  page that falls under it. Row 1 passes it at exactly 2, which is why F1 and F2
  exist and why this fence is not the one doing the work alone.

  **F4 — TWO INDEPENDENT PAGES.** One admissible page ⇒ ``qualifies``;
  ``contradicts`` needs TWO pages from two independent outlets, each past F1–F3.
  The outlet notion is 7d's, imported rather than re-spelled
  (:func:`~legba.data.analysts.source_independence.independence_of`): two
  mastheads running one dispatch are one source, and a second opinion about
  document identity is exactly the drift this codebase keeps paying for. Rows
  record ``independent_pages``.

WHAT THESE FENCES ARE NOT. They do not judge the claim, they do not judge the
page, and they add no model to the path. Each one answers a question about
ADMISSIBILITY — is this a reporting page, does it have a date, is it about this
subject, is there a second one — and every answer is a number on the row. A
stance is still a statement about the retrieval and never a verdict.

NOTHING HERE IS RE-IMPLEMENTED. The date gate is the reference builder's
(:func:`~._reference_fences.date_gate` over
:func:`~._reference_page.parse_iso_date`, with ``_url_canon.url_embedded_date``
as the dated-URL leg); the claim's window is the external audit's
(:func:`~legba.data.provenance.external_span_check.evidence_window_bounds`); the
subject tokens are R2's ``subject_tokens_of``; the independence count is 7d's.
This module contributes one list — the reference hosts — and the wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from ..._url_canon import url_embedded_date
from ...provenance.external_span_check import evidence_window_bounds
from ..claim_contradiction import subject_tokens_of
from ..source_independence import independence_of
from ._reference_fences import REJECT_NO_DATE, REJECT_OUT_OF_WINDOW, date_gate
from ._reference_page import host_of, parse_iso_date

# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------

#: A host this platform already knows as a SOURCE keeps the source-class
#: taxonomy's own word for it (``SourceClass``: reporting / analysis / official
#: / state_media). Two vocabularies for one idea is how two surfaces come to
#: disagree about what a page is, so this module adds exactly two words to that
#: list and neither of them is a synonym of an existing one.
HOST_CLASS_REFERENCE = "reference"

#: Not a known source and not a reference host. It PASSES F1 — most of the open
#: web is unknown, and refusing the unknown would refuse the whole point of a
#: contrary pass — and meets F2, F3 and F4 like anything else.
HOST_CLASS_UNKNOWN = "unknown"

#: The closed ``host_class`` vocabulary, for a receipt that reports zeros rather
#: than omitting a key.
HOST_CLASSES: tuple[str, ...] = (
    HOST_CLASS_REFERENCE, "reporting", "analysis", "official", "state_media",
    HOST_CLASS_UNKNOWN,
)

#: REFERENCE / ENCYCLOPEDIA / DICTIONARY hosts, matched by suffix so
#: ``en.wikipedia.org`` and ``simple.wikipedia.org`` both land.
#:
#: THE REASON, stated once and carried by the constant rather than by a comment
#: at a call site: a page here is not reporting. It has no publication date for
#: any claim's window — the dates rows 1 and 2 carried (2018-12-10, 2007-05-17)
#: are article-creation metadata, not a statement about the world in September
#: 2026 — and, decisively, it is WHAT THE FREE RUNG RETURNS WHEN THE REAL ANSWER
#: IS ABSENT. Three of three false contradictions in the first live run came
#: from this list, and the run's own search health (5 × ``unresponsive_engines``,
#: hits dominated by wikipedia.org and merriam-webster.com) is why.
#:
#: Mirrors are included deliberately: ``wikiwand.com`` and ``dbpedia.org`` serve
#: the same text under another masthead, and a fence a mirror walks around is
#: not a fence.
REFERENCE_HOST_SUFFIXES: frozenset[str] = frozenset({
    # Wikimedia and its mirrors / siblings
    "wikipedia.org", "wikimedia.org", "wikidata.org", "wiktionary.org",
    "wikiquote.org", "wikisource.org", "wikivoyage.org", "wikibooks.org",
    "wikinews.org", "wikiwand.com", "dbpedia.org", "everipedia.org",
    # encyclopedias
    "britannica.com", "encyclopedia.com", "infoplease.com", "citizendium.org",
    "scholarpedia.org", "newworldencyclopedia.org",
    # dictionaries and glossaries
    "merriam-webster.com", "dictionary.com", "thefreedictionary.com",
    "collinsdictionary.com", "vocabulary.com", "wordnik.com",
    "oxfordreference.com", "lexico.com", "etymonline.com",
    # explainer/glossary sites that answer a definitional query the same way
    "investopedia.com", "thoughtco.com", "howstuffworks.com",
})

#: WHICH FENCE FIRED — a closed vocabulary, on the ref, so a week of rows
#: stratifies by the rule rather than by prose.
FENCE_HOST_CLASS = "host_class"
FENCE_PAGE_DATE = "page_date"
FENCE_SUBJECT_OVERLAP = "subject_overlap"
FENCE_INDEPENDENT_PAGES = "independent_pages"

FENCES: tuple[str, ...] = (
    FENCE_HOST_CLASS, FENCE_PAGE_DATE, FENCE_SUBJECT_OVERLAP,
    FENCE_INDEPENDENT_PAGES,
)

#: F4's bar. Two is not a tuning parameter dressed as a constant: one page is
#: one retrieval, and the whole lesson of the first live run is that one page
#: off a degraded free rung is not evidence of a disagreement.
MIN_INDEPENDENT_PAGES = 2

#: ``date_source`` values — WHERE the page's date came from, never invented.
DATE_SOURCE_PAGE = "page"
DATE_SOURCE_URL = "url"
DATE_SOURCE_NONE = "none"


# ---------------------------------------------------------------------------
# F1 — the host class
# ---------------------------------------------------------------------------


def _suffix_match(host: str, suffixes: frozenset[str]) -> bool:
    if not host:
        return False
    if host in suffixes:
        return True
    return any(host.endswith("." + suffix) for suffix in suffixes)


def is_reference_host(url: str) -> bool:
    """True for an encyclopedia / dictionary / glossary host. See the constant."""
    return _suffix_match(host_of(url), REFERENCE_HOST_SUFFIXES)


def host_class_of(url: str, catalog: Mapping[str, str] | None = None) -> str:
    """The ``host_class`` for one fetched page's URL.

    ``catalog`` is ``{host: source_class}`` off the live ``source_descriptors``
    heads (see ``_contrary_store.host_class_catalog``). A host this platform
    already ingests keeps the class IT was registered under — the source-class
    taxonomy, not a second vocabulary invented here — and everything else is
    ``reference`` or ``unknown``.

    The reference list wins over the catalog on purpose. If a wikipedia feed
    were ever registered as a source, its pages would still not be reporting.
    """
    host = host_of(url)
    if not host:
        return HOST_CLASS_UNKNOWN
    if _suffix_match(host, REFERENCE_HOST_SUFFIXES):
        return HOST_CLASS_REFERENCE
    for known, klass in (catalog or {}).items():
        if host == known or host.endswith("." + known):
            text = str(klass or "").strip()
            if text in HOST_CLASSES:
                return text
    return HOST_CLASS_UNKNOWN


# ---------------------------------------------------------------------------
# F2 — the date and the window
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimWindow:
    """The days a counter page may be published in and still contradict.

    UNMEASURED is not out-of-window. A read whose evidence window carries no
    parsable bound yields ``ClaimWindow()``, and F2 then enforces only its other
    half — a page must be DATED — exactly as
    :func:`~legba.data.provenance.external_span_check.evidence_window_bounds`
    declines to run the audit's gate rather than demoting every claim on a read
    with no datable head.
    """

    start: date | None = None
    end: date | None = None

    @property
    def measured(self) -> bool:
        return self.start is not None and self.end is not None


def claim_window(claim: Any, *, ttl_hours: int) -> ClaimWindow:
    """The claim's admissible counter-evidence window, widened by the TTL.

    The read's own evidence window is the audit's (``read_evidence_window``,
    plus the read's ``produced_at`` as the upper bound — a read composed at
    12:00Z may rest on a source published at 11:55Z). It is widened on BOTH
    sides by ``contention_ttl_hours``, which is the record's own shelf life and
    therefore the only defensible grain here: a contention that is live for a
    week may rest on a page from that week.
    """
    try:
        hours = max(0, int(ttl_hours))
    except (TypeError, ValueError):
        hours = 0
    bounds = evidence_window_bounds(
        getattr(claim, "read_evidence_window", None) or {},
        produced_at=getattr(claim, "produced_at", None) or None,
    )
    if not bounds.measured or bounds.start is None or bounds.end is None:
        return ClaimWindow()
    slack = timedelta(hours=hours)
    return ClaimWindow(
        start=(bounds.start - slack).astimezone(timezone.utc).date(),
        end=(bounds.end + slack).astimezone(timezone.utc).date(),
    )


def page_date_of(url: str, published_at: Any) -> tuple[date | None, str]:
    """``(date, source)`` for one page. A date this code cannot find is not a date.

    Order is most-authoritative-first and both legs are the tree's own: the
    page's discovered metadata date (trafilatura's resolution at fetch time,
    parsed by the reference builder's :func:`parse_iso_date`), then a date the
    URL itself embeds (``_url_canon.url_embedded_date``, the reference
    builder's third mechanism). Nothing reads visible prose, a masthead or a
    "published 3 hours ago" string — the R2 lesson that a masthead date turned a
    November-2024 appointment into an in-window leadership transition.
    """
    parsed = parse_iso_date(str(published_at) if published_at else None)
    if parsed is not None:
        return parsed, DATE_SOURCE_PAGE
    embedded = url_embedded_date(str(url or ""))
    if embedded is not None:
        return embedded, DATE_SOURCE_URL
    return None, DATE_SOURCE_NONE


def date_admits(
    page_published_at: date | None, window: ClaimWindow,
) -> tuple[bool, str]:
    """``(admitted, reason)`` — the reference builder's gate, on this window.

    An UNMEASURED window still refuses an undated page: "no date" is a property
    of the page, not of the claim, and it is the half of F2 that holds
    regardless of what the read's stamp says.
    """
    if page_published_at is None:
        return False, REJECT_NO_DATE
    if not window.measured:
        return True, ""
    assert window.start is not None and window.end is not None
    return date_gate(page_published_at.isoformat(), window.start, window.end)


# ---------------------------------------------------------------------------
# F3 — the subject in the matched sentence
# ---------------------------------------------------------------------------


def subject_overlap_of(claim_text: str, sentence: str) -> int:
    """How many of the claim's subject tokens the MATCHED SENTENCE carries.

    R2's ``subject_tokens_of`` on both sides — scaffold words are not subjects
    there and are not subjects here. The number goes on the row beside
    ``query_novel_tokens``: counter-query quality and counter-page relevance are
    the two halves of "was this retrieval about the claim at all", and both
    should be things to average rather than anxieties to assert.
    """
    if not claim_text or not sentence:
        return 0
    return len(subject_tokens_of(claim_text) & subject_tokens_of(sentence))


# ---------------------------------------------------------------------------
# F1 + F2 + F3 for ONE page
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PageVerdict:
    """What the per-page fences found. Numbers, then the one that fired.

    ``fence`` is ``""`` when the page is admissible. A REJECTING fence
    (``host_class``, ``subject_overlap``) takes the page's own stance to
    ``none_found``: it settled nothing. A DEMOTING fence (``page_date``) leaves
    the page stance-bearing but caps it at ``qualifies``, because a real
    counter-report from another year genuinely narrows the picture without
    being about this window.
    """

    host_class: str = HOST_CLASS_UNKNOWN
    page_published_at: date | None = None
    date_source: str = DATE_SOURCE_NONE
    date_reason: str = ""
    subject_overlap: int = 0
    fence: str = ""
    demoted: bool = False

    @property
    def rejected(self) -> bool:
        return self.fence in (FENCE_HOST_CLASS, FENCE_SUBJECT_OVERLAP)

    @property
    def admissible(self) -> bool:
        """Past F1–F3 with nothing capped — the only page F4 may count."""
        return not self.fence


def judge_page(
    claim_text: str,
    *,
    url: str,
    published_at: Any,
    sentence: str,
    window: ClaimWindow,
    min_overlap: int,
    catalog: Mapping[str, str] | None = None,
) -> PageVerdict:
    """F1, F2 and F3 over ONE fetched page's decisive sentence.

    Ordered by WHAT THE FAILURE MEANS rather than by cost, the ``apply_fences``
    convention: a reference host is not a dating failure and must not be counted
    as one, and an off-subject match is not a window failure.
    """
    host_class = host_class_of(url, catalog)
    page_date, date_source = page_date_of(url, published_at)
    overlap = subject_overlap_of(claim_text, sentence)
    if host_class == HOST_CLASS_REFERENCE:
        return PageVerdict(
            host_class=host_class, page_published_at=page_date,
            date_source=date_source, subject_overlap=overlap,
            fence=FENCE_HOST_CLASS,
        )
    if overlap < max(0, int(min_overlap)):
        return PageVerdict(
            host_class=host_class, page_published_at=page_date,
            date_source=date_source, subject_overlap=overlap,
            fence=FENCE_SUBJECT_OVERLAP,
        )
    admitted, reason = date_admits(page_date, window)
    if not admitted:
        return PageVerdict(
            host_class=host_class, page_published_at=page_date,
            date_source=date_source, date_reason=reason,
            subject_overlap=overlap, fence=FENCE_PAGE_DATE, demoted=True,
        )
    return PageVerdict(
        host_class=host_class, page_published_at=page_date,
        date_source=date_source, subject_overlap=overlap,
    )


# ---------------------------------------------------------------------------
# F4 — two independent pages
# ---------------------------------------------------------------------------


def independent_pages(pages: Sequence[Mapping[str, Any]]) -> int:
    """How many INDEPENDENT outlets these pages come from. 7d's count, reused.

    ``pages`` entries carry ``url`` plus the page text (``text``, or ``quote``
    when only the decisive sentence survived). The mapping onto 7d's unit is
    exact rather than analogous: the unit of independence there is the OUTLET,
    and for a page off the open web the outlet is its host. Two pages from one
    host are one source before any content test runs; two hosts running one
    dispatch fold by the same near-verbatim test dedupe tier 4 and the
    composition floor already use. Nothing about document identity is decided
    a second time here.

    ``source_id`` is the host and ``signal_id`` the page hash because
    :func:`independence_of` counts only citations carrying both — the filter IS
    its definition of the unit, and a page with no host cannot be an outlet.
    """
    citations: list[dict[str, Any]] = []
    for page in pages or ():
        host = host_of(str(page.get("url") or ""))
        if not host:
            continue
        body = str(page.get("text") or page.get("quote") or "")
        citations.append({
            "source_id": host,
            "signal_id": str(page.get("sha256") or page.get("url") or host),
            # No headline exists for a fetched page, and inventing one from the
            # URL slug would put a fabricated string into an identity test.
            # The body leg decides; the headline leg self-skips on "".
            "title": "",
            "source_text": body,
        })
    return independence_of(citations).independent


def contradiction_admitted(independent: int) -> bool:
    """F4's whole rule: ``contradicts`` needs two independent pages."""
    return int(independent or 0) >= MIN_INDEPENDENT_PAGES


def as_iso(value: date | datetime | None) -> str | None:
    """``YYYY-MM-DD`` for a record, or ``None``. Never a fabricated today."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return None


__all__ = [
    "DATE_SOURCE_NONE",
    "DATE_SOURCE_PAGE",
    "DATE_SOURCE_URL",
    "FENCE_HOST_CLASS",
    "FENCE_INDEPENDENT_PAGES",
    "FENCE_PAGE_DATE",
    "FENCE_SUBJECT_OVERLAP",
    "FENCES",
    "HOST_CLASSES",
    "HOST_CLASS_REFERENCE",
    "HOST_CLASS_UNKNOWN",
    "MIN_INDEPENDENT_PAGES",
    "REFERENCE_HOST_SUFFIXES",
    "REJECT_NO_DATE",
    "REJECT_OUT_OF_WINDOW",
    "ClaimWindow",
    "PageVerdict",
    "as_iso",
    "claim_window",
    "contradiction_admitted",
    "date_admits",
    "host_class_of",
    "independent_pages",
    "is_reference_host",
    "judge_page",
    "page_date_of",
    "subject_overlap_of",
]
