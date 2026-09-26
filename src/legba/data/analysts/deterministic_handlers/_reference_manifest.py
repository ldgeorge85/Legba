# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R2/D6 — THE PAGE MANIFEST, and the three rules that keep the budget on pages.

WHAT THE 06:43Z AUSTRALIA BUILD ACTUALLY DID, read out of ``analyst_traces``
(run ``14e81d75``, 44 tool calls, 71 model rounds, 306 s, 479 k tokens, $0):

  * 36 searches and 8 admitted fetches, and **one usable page** at the end of it
    (``pages_archived: 1``).
  * Two of those eight fetches went to
    ``https://theguardian.com/australia-news/2026/sep/17/imf-downgrades-...``.
    The search result the model had been SHOWN was
    ``https://www.theguardian.com/australia-news/…`` — the same page. Dropping
    four characters turns it into a **404 of 202 bytes**. Re-fetched with the
    ``www.`` the page is 6,538 characters, dated ``2026-09-17`` in JSON-LD, and
    squarely in the window. The single best in-window page of the run was lost
    to a missing subdomain, twice.
  * Four more fetches went to ``reuters.com`` and ``bloomberg.com``, both
    MEASURED unfetchable to this lane (FETCH_REVIEW §2). They were dropped from
    the search RESULTS and then fetched anyway, from URLs the model wrote out of
    its own head — the drop was a discovery fence and there was no fetch fence
    behind it.
  * One went to ``abs.gov.au`` (dated 2026-08-26, outside the window) and one to
    ``thebulletin.net.au`` (dated 2020-06-22). The one usable page the run
    archived was the 2020 one.
  * The model wrote a real, span-carrying NOTE at round 66 — and the loop threw
    it away, because it arrived in the same turn as a tool call and the
    gathering branch never looks at the turn's text.
  * At round 58, 230 s in, the model emitted the **complete final JSON** as a
    plain-text turn. It did not contain the words REFERENCE COMPLETE, so the
    loop replied "Noted. Continue." and bought thirteen more calls that changed
    nothing.

So the yield defect was never "the model would not take notes". It was that the
loop spent its fetch budget on pages it could not read, lost the one page it
could, discarded the one note it got, and then asked the model to commit "from
the notes you have" — which was nothing.

THE ANSWER IN ONE SENTENCE: the loop keeps its OWN record of every page it
read, and the commit turn is built from that record rather than from the model's
cooperation. Everything in this module serves that sentence:

  * :class:`ManifestEntry` — one page, recorded by the LOOP at fetch time: url,
    outlet, title, the page's own machine-readable date, whether that date is
    inside the window, the archive digest, and the first
    :data:`MANIFEST_EXCERPT_CHARS` characters of the archived text. No model
    cooperation is involved and none is possible; the entry exists whether or
    not a NOTE ever arrives.
  * :func:`commit_prompt` — the commit turn, rendered FROM the manifest, with
    the excerpt the model may quote from and the window verdict printed against
    each page. A span copied out of an excerpt is a prefix of the archived text,
    so it verifies by construction.
  * :func:`clean_query` — ``site:`` operators and date literals out of queries.
    The allowlist filters RESULTS; date discipline is the DATE GATE's job. A
    model that pre-filters with ``site:reuters.com`` is spending a call to
    narrow a search onto a host whose results are dropped anyway.
  * the FETCH-FIRST rule and the SEARCH/FETCH ratio guard — spans live in pages,
    and 36:8 is not a research strategy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from ._reference_page import ArchivedPage, parse_iso_date

# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------

#: Characters of each page kept on the manifest as the QUOTABLE EXCERPT. The
#: model sees the whole page (``page_chars_to_model``, 7,000) at fetch time and
#: then the payload is evicted; this is what comes back at commit time. 1,400
#: reaches the lead and the first substantive paragraphs of a news page, which
#: is where a decisive sentence lives, and twelve pages of it is ~5 k tokens
#: against a 1.2 M ceiling.
MANIFEST_EXCERPT_CHARS = 1400

#: Hard ceiling on the whole rendered manifest. A build that read forty pages
#: must not turn its commit turn into a second corpus: in-window pages are
#: rendered first and with excerpts, and the rest degrade to one line each.
MANIFEST_TOTAL_CHARS = 26_000

#: The outcome name for a commit that carried NO development while the manifest
#: held at least one in-window page with usable text. It is NOT ``no_commit``:
#: no_commit says the build produced nothing to fence, and that sends its reader
#: to the plane and the budget. This says the plane worked, the pages were
#: there, they were put in front of the model, and the model still wrote an
#: empty reference — which is a MODEL failure and must be visible as one.
EMPTY_COMMIT_WITH_MATERIAL = "empty_commit_with_material"


@dataclass
class ManifestEntry:
    """One page this run read, recorded by the loop and not by the model."""

    url: str
    host: str
    title: str = ""
    publish_date: str | None = None
    date_source: str = "none"
    chars: int = 0
    archive_sha256: str = ""
    excerpt: str = ""

    def in_window(self, window_start: date | None, window_end: date | None) -> bool:
        """Is the PAGE's own machine-readable date inside the window?

        ``None`` for either bound means "the caller did not state a window", and
        an unstated window admits nothing rather than everything: the date gate
        would reject the development anyway, and a manifest that said IN WINDOW
        where the gate says out of it would be the one lie this block cannot
        afford.
        """
        parsed = parse_iso_date(self.publish_date)
        if parsed is None or window_start is None or window_end is None:
            return False
        return window_start <= parsed <= window_end

    def as_record(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "host": self.host,
            "title": self.title,
            "publish_date": self.publish_date,
            "date_source": self.date_source,
            "chars": self.chars,
            "archive_sha256": self.archive_sha256,
        }

    def carry_line(self) -> str:
        """One line for the next attempt's carry — a LEAD, never evidence."""
        return (
            f"PAGE: | URL: {self.url} | OUTLET: {self.host} | "
            f"DATE: {self.publish_date or 'UNDATED'} | TITLE: {self.title[:120]}"
        )


def record_page(
    page: ArchivedPage, text: str, *, title: str = ""
) -> ManifestEntry:
    """Build the manifest entry for ONE fetched page. Called by the loop.

    Deliberately takes the ARCHIVED TEXT rather than reading it back: the entry
    is made at fetch time, inside the same call that archived the bytes, so a
    page whose body the licence rule forbids storing still gets an excerpt (it
    is held in memory for exactly as long as this run) and still reaches the
    commit turn.
    """
    excerpt = " ".join((text or "")[: MANIFEST_EXCERPT_CHARS * 2].split())
    return ManifestEntry(
        url=page.final_url or page.url,
        host=page.host,
        title=str(title or "").strip()[:200],
        publish_date=page.publish_date,
        date_source=page.date_source,
        chars=page.chars,
        archive_sha256=page.archive_sha256,
        excerpt=excerpt[:MANIFEST_EXCERPT_CHARS],
    )


def in_window_entries(
    entries: Sequence[ManifestEntry],
    window_start: date | None,
    window_end: date | None,
) -> list[ManifestEntry]:
    """The pages a development could legally rest on. The rest are context."""
    return [
        e for e in entries
        if e.excerpt and e.in_window(window_start, window_end)
    ]


def has_material(
    entries: Sequence[ManifestEntry],
    window_start: date | None,
    window_end: date | None,
) -> bool:
    """Was there at least one in-window page with usable text to build from?

    This is the whole test behind :data:`EMPTY_COMMIT_WITH_MATERIAL`. It is
    asked of the LOOP's record, never of the model's, which is the point.
    """
    return bool(in_window_entries(entries, window_start, window_end))


def render_manifest(
    entries: Sequence[ManifestEntry],
    window_start: date | None = None,
    window_end: date | None = None,
    *,
    total_chars: int = MANIFEST_TOTAL_CHARS,
) -> str:
    """The pages this run read, as the block the commit turn is built from.

    In-window pages first, with their excerpts, because they are the only ones
    a development may rest on. Out-of-window and undated pages follow as one
    line each with the verdict printed, so the model can see that the page was
    read and why it cannot be used — an omission would read as "the harness lost
    it" and invite a citation from memory.
    """
    if not entries:
        return "(no page was successfully read)"
    window = ""
    if window_start and window_end:
        window = f" (window {window_start.isoformat()} -> {window_end.isoformat()})"
    usable = in_window_entries(entries, window_start, window_end)
    # By IDENTITY, not by value: two pages that happen to compare equal (an
    # outlet serving the same lead twice) must not both vanish from the tail.
    usable_ids = {id(e) for e in usable}
    rest = [e for e in entries if id(e) not in usable_ids]

    lines: list[str] = []
    used = 0
    for index, entry in enumerate(usable, 1):
        head = (
            f"[{index}] {entry.url}\n"
            f"    outlet: {entry.host} | published: {entry.publish_date} "
            f"({entry.date_source}) | IN WINDOW{window}\n"
            + (f"    title: {entry.title}\n" if entry.title else "")
        )
        body = f"    TEXT YOU MAY QUOTE FROM:\n    \"{entry.excerpt}\"\n"
        if used + len(head) + len(body) > total_chars:
            lines.append(head + "    (excerpt omitted — manifest at its size cap)\n")
            used += len(head)
            continue
        lines.append(head + body)
        used += len(head) + len(body)
    if not usable:
        lines.append(
            "NO PAGE THIS RUN READ CARRIES A MACHINE-READABLE PUBLISH DATE "
            f"INSIDE THE WINDOW{window}.\n"
        )
    for entry in rest:
        verdict = (
            "UNDATED — cannot carry a development"
            if not parse_iso_date(entry.publish_date)
            else f"published {entry.publish_date} — OUTSIDE THE WINDOW"
        )
        lines.append(f"  - {entry.url} [{entry.host}] {verdict}\n")
    return "".join(lines).rstrip()


# ---------------------------------------------------------------------------
# The commit turn
# ---------------------------------------------------------------------------

_COMMIT_RULES = """BUILD YOUR DEVELOPMENTS ONLY FROM THE PAGES BELOW. They are the pages this run actually fetched and archived; there is no other evidence and there will be no further tool call.

* Every `source_url` must be one of the URLs below, copied character for character.
* Every `decisive_span` must be copied VERBATIM out of that page's quoted text below — every character, every comma, every accent. A span that is not an exact substring of the archived page is UNVERIFIED and the development is DROPPED.
* A page marked OUTSIDE THE WINDOW or UNDATED cannot carry a development. Omit it. Do not date it from its content and do not argue with the verdict — the date gate reads the page's own metadata and will reject it.
* Where a page below IS in window and carries a decisive sentence, WRITE THE DEVELOPMENT. A reference with no development makes every claim graded against it read `silent`, which teaches the instrument nothing; a thin reference built from two real pages is worth more than an empty one."""


def commit_prompt(
    entries: Sequence[ManifestEntry],
    lead: str,
    *,
    window_start: date | None = None,
    window_end: date | None = None,
    forced: bool = False,
) -> str:
    """The commit turn's user message, built FROM THE MANIFEST.

    ``forced=True`` is D3(b): the loop hit a wall rather than the model
    finishing. The 06:43Z build's forced turn said "commit the reference now
    from the notes you have" to a model that had no notes, and got an empty
    reference back in fifteen seconds. It now says: here are the pages, here is
    their text, build from these.
    """
    parts = [
        f"{lead} Emit the final JSON object now, exactly as the output "
        "contract specifies, and nothing else."
    ]
    if forced:
        parts.append(
            "The tools are CLOSED for the rest of this build — there will be no "
            "further search and no further fetch, so a development you were "
            "still chasing does not go in, and a thin dimension is recorded as "
            "thin rather than guessed at. Do not ask for more time and do not "
            "call a tool; emit the JSON."
        )
    parts.append(_COMMIT_RULES)
    parts.append(
        "THE PAGES THIS RUN READ:\n"
        + render_manifest(entries, window_start, window_end)
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Query hygiene — the model does not pre-filter; the fences do
# ---------------------------------------------------------------------------

#: A ``site:``/``inurl:``/``intitle:`` operator. The 06:43Z build spent six
#: searches on ``site:reuters.com``, ``site:treasury.gov.au`` and ``site:gov.au``
#: — narrowing a search onto hosts whose results the allowlist would have kept
#: anyway, or (reuters) dropped anyway. The operator is stripped, never refused:
#: refusing spends the round and returns nothing, stripping spends the round and
#: returns the thirty results the query would have found.
_SITE_OPERATOR_RE = re.compile(r"\b(?:site|inurl|intitle|inbody):\S+", re.I)

#: A date LITERAL in a query — ``2026-09-05``, ``"2026-09"``. Eight of the
#: 06:43Z queries carried one and every one of them returned nothing usable: a
#: news page does not contain its own ISO date as searchable text. The window is
#: the DATE GATE's job, enforced on the page's metadata after the fetch.
_DATE_LITERAL_RE = re.compile(r"\"?\b\d{4}-\d{2}(?:-\d{2})?\b\"?")

#: Outlet names that steer a query at a host this lane is MEASURED unable to
#: read (see ``_reference_fences.UNFETCHABLE_SUFFIXES``). Thirteen of the
#: 06:43Z queries ended in one of these words. A result from those hosts is
#: dropped from discovery, so the word can only ever narrow the answer onto
#: pages that will not be shown.
_UNFETCHABLE_OUTLET_RE = re.compile(
    r"\b(?:reuters|bloomberg|ap\s*news|apnews|associated\s+press|"
    r"financial\s+times|ft\.com|haaretz|times\s+of\s+israel)\b",
    re.I,
)

#: Quoted-phrase stacks. ``"2026-09" "Australia" "Reuters"`` is three exact-match
#: constraints ANDed together; it is how a metasearch is made to return nothing.
_QUOTED_RE = re.compile(r"\"([^\"]{1,80})\"")


def clean_query(query: str) -> tuple[str, list[str]]:
    """``(query the pack is actually asked, reasons it was rewritten)``.

    Never raises and never returns empty when it was given something: if the
    rewrite would strip a query to nothing, the ORIGINAL is sent. A hygiene rule
    that can turn a real query into no query is a worse defect than the habit it
    was written to correct.
    """
    original = str(query or "").strip()
    if not original:
        return "", []
    cleaned = original
    reasons: list[str] = []
    if _SITE_OPERATOR_RE.search(cleaned):
        cleaned = _SITE_OPERATOR_RE.sub(" ", cleaned)
        reasons.append(
            "a site:/inurl: operator was removed — discovery is ALREADY "
            "filtered to reference-grade hosts, so pre-filtering only narrows "
            "what you are shown"
        )
    if _DATE_LITERAL_RE.search(cleaned):
        cleaned = _DATE_LITERAL_RE.sub(" ", cleaned)
        reasons.append(
            "a date literal was removed — a page does not carry its own ISO "
            "date as searchable text, and the window is enforced on the page's "
            "metadata after you fetch it, not on the query"
        )
    if _UNFETCHABLE_OUTLET_RE.search(cleaned):
        cleaned = _UNFETCHABLE_OUTLET_RE.sub(" ", cleaned)
        reasons.append(
            "an outlet this lane cannot read was removed from the query — its "
            "results are dropped from discovery, so naming it can only narrow "
            "the answer onto pages you will never be shown"
        )
    if len(_QUOTED_RE.findall(cleaned)) >= 2:
        cleaned = _QUOTED_RE.sub(r"\1", cleaned)
        reasons.append(
            "stacked exact-match quotes were unquoted — several quoted phrases "
            "ANDed together is how a search is made to return nothing"
        )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return original, []
    if cleaned == original:
        return original, []
    return cleaned, reasons


# ---------------------------------------------------------------------------
# Query patterns, per dimension — event-oriented, outlet-agnostic
# ---------------------------------------------------------------------------

#: Three to five patterns per dimension, given to the model in the stage-1
#: instruction. ``{country}`` is the only substitution. They name EVENTS a
#: fortnight can contain, not outlets and not dates, because that is what a
#: metasearch indexes and what a reference records.
QUERY_PATTERNS: dict[str, tuple[str, ...]] = {
    "leadership_transition": (
        "{country} cabinet reshuffle minister appointed",
        "{country} prime minister resignation no-confidence vote",
        "{country} election result coalition talks",
        "{country} central bank governor appointed replaced",
    ),
    "energy_security": (
        "{country} gas supply pipeline outage",
        "{country} electricity grid blackout capacity",
        "{country} LNG contract export terminal",
        "{country} fuel price cap subsidy decision",
    ),
    "escalation": (
        "{country} border incident troops clash",
        "{country} strike attack casualties",
        "{country} airspace violation intercepted",
        "{country} summons ambassador protest diplomatic",
    ),
    "narrative_coordination": (
        "{country} foreign ministry statement condemns",
        "{country} joint statement allies communique",
        "{country} disinformation campaign attributed",
        "{country} state media coordinated messaging",
    ),
    "internal_stability": (
        "{country} protest thousands police",
        "{country} strike union industrial action",
        "{country} court ruling government challenge",
        "{country} arrests crackdown opposition",
    ),
    "military_posture": (
        "{country} defence spending budget announcement",
        "{country} military exercise deployment",
        "{country} arms purchase procurement contract",
        "{country} troops deployed reinforcements",
    ),
    "economic_coercion": (
        "{country} tariffs imposed imports",
        "{country} sanctions export ban trade",
        "{country} investment screening blocked deal",
        "{country} IMF review forecast downgrade",
    ),
    "proliferation_watch": (
        "{country} IAEA inspection enrichment",
        "{country} missile test launch",
        "{country} nuclear programme agreement",
        "{country} export controls dual-use technology",
    ),
}

#: Patterns for a dimension this module does not know by name.
_GENERIC_PATTERNS: tuple[str, ...] = (
    "{country} {dimension} announcement",
    "{country} {dimension} decision this month",
    "{country} {dimension} report",
)


def query_guidance(country: str, dimensions: Sequence[str]) -> str:
    """The query block the stage-1 instruction carries, for THESE dimensions."""
    lines = [
        "Write queries the way an index works: the WORDS THAT WOULD APPEAR IN "
        "THE HEADLINE. Never a `site:` operator, never a date string, never an "
        "outlet name — discovery is already filtered to reference-grade hosts "
        "for you, the window is enforced on each page's own metadata AFTER you "
        "fetch it, and naming an outlet only narrows what you are shown. A "
        "query the harness has to rewrite costs you the round.",
        "",
        "Patterns that work, per dimension:",
    ]
    for dimension in dimensions:
        patterns = QUERY_PATTERNS.get(dimension) or tuple(
            p.format(country="{country}", dimension=dimension.replace("_", " "))
            for p in _GENERIC_PATTERNS
        )
        rendered = "; ".join(
            f"`{p.format(country=country)}`" for p in patterns
        )
        lines.append(f"* `{dimension}` — {rendered}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The fetch-first rule and the search/fetch ratio guard
# ---------------------------------------------------------------------------

#: Searches in a row without a fetch before the rule is injected. TWO, down from
#: four: the 06:43Z build ran nine searches between its second and third fetch
#: and eleven between its third and fourth.
SEARCH_RUN_LIMIT = 2

#: Tool calls after which the RATIO is judged at all. Below it a build is still
#: opening the subject and a lopsided ratio means nothing.
RATIO_GUARD_AFTER_CALLS = 12

#: Searches per fetch above which the build is searching instead of reading.
#: 3.0 — the 06:43Z build ran at 4.5:1 and archived one page.
RATIO_GUARD_FACTOR = 3.0

FETCH_FIRST_RULE = (
    "STOP SEARCHING. A search snippet is NOT evidence, cannot carry a span and "
    "cannot be dated — every development in your reference has to come out of a "
    "page you FETCHED. Your next call must be `fetch_page` on one of the URLs "
    "you have already been shown, copied EXACTLY as it appeared in the `url` "
    "field (including any `www.`; a URL you retype loses the page). If not one "
    "of the results you have seen is worth fetching, say so in a plain-text "
    "NOTE naming the dimension you are still missing, and search once more for "
    "THAT — but do not run another search before you have done one or the other."
)


def ratio_breached(*, searches: int, fetches: int, tool_calls: int) -> bool:
    """Is this build searching instead of reading?

    Judged on the RAW counts the plane recorded, not on the model's account of
    them, and only after :data:`RATIO_GUARD_AFTER_CALLS` calls have been spent.
    """
    if tool_calls < RATIO_GUARD_AFTER_CALLS:
        return False
    return searches > RATIO_GUARD_FACTOR * max(fetches, 0)


def ratio_nudge(*, searches: int, fetches: int) -> str:
    return (
        f"You have run {searches} searches and {fetches} fetches. Spans live in "
        f"PAGES: a build that searches more than {RATIO_GUARD_FACTOR:g} times "
        "per fetch ends with nothing to quote. " + FETCH_FIRST_RULE
    )


# ---------------------------------------------------------------------------
# The gathering-phase nudges that used to live in the loop
# ---------------------------------------------------------------------------


def opened_prompt(opened: Sequence[str], calls_left: int) -> str:
    return (
        "Most of your tool budget is spent and these dimensions still carry "
        "fewer than two reference-grade sources, so they are now OPEN to wider "
        "sources (regional and national press): " + ", ".join(opened) + ".\n"
        f"You have about {max(calls_left, 0)} tool calls left. Spend them "
        "THERE — an empty dimension is worse than one anchored at a lower tier, "
        "and everything you anchor there will be marked as resting on one. "
        "Still prefer an official or wire source where one exists."
    )


def pushback_prompt(
    noted: int,
    target: int,
    dimensions: Sequence[str],
    notes: Sequence[str],
    calls_left: int,
    opened: Sequence[str],
) -> str:
    blob = "\n".join(notes)
    thin = [d for d in dimensions if blob.count(d) < 2]
    lines = [
        f"Not yet. Your NOTEs carry {noted} developments; the target is "
        f"{target}-35 with at least 2 per dimension.",
        f"Thin or empty dimensions: {', '.join(thin) or 'none'}.",
        f"You have about {max(calls_left, 0)} tool calls left. Keep "
        "gathering, then say REFERENCE COMPLETE again.",
    ]
    if opened:
        lines.append(
            "These dimensions have fewer than two reference-grade sources and "
            "are now OPEN to wider sources (regional and national press): "
            + ", ".join(opened)
            + ". Everything you anchor there will be marked as resting on a "
            "lower-tier source, so still prefer an official or wire source "
            "where one exists."
        )
    return "\n".join(lines)


def manifest_records(entries: Iterable[ManifestEntry]) -> list[dict[str, Any]]:
    """The manifest as receipt-shaped dicts — excerpts dropped, counts kept."""
    return [entry.as_record() for entry in entries]


def carry_lines(
    entries: Sequence[ManifestEntry],
    window_start: date | None = None,
    window_end: date | None = None,
) -> str:
    """The pages a failed build leaves for the next attempt, newest date first.

    Only pages with TEXT: a page that served nothing is not a lead, it is the
    same dead end the next build would rediscover. The window verdict is not
    carried, because the next build's window will have moved.
    """
    lines = [e.carry_line() for e in entries if e.excerpt]
    return "\n".join(lines)


@dataclass
class QueryLedger:
    """What the hygiene rules actually did, for the receipt."""

    rewritten: list[dict[str, Any]] = field(default_factory=list)

    def record(self, original: str, cleaned: str, reasons: Sequence[str]) -> None:
        self.rewritten.append({
            "original": original[:200],
            "sent": cleaned[:200],
            "reasons": list(reasons),
        })

    def as_record(self) -> dict[str, Any]:
        return {
            "queries_rewritten": len(self.rewritten),
            "examples": self.rewritten[:5],
        }


def apply_query_ledger(
    ledger: QueryLedger | None, query: str
) -> tuple[str, list[str]]:
    """Clean ``query`` and, when it changed, record it on ``ledger``."""
    cleaned, reasons = clean_query(query)
    if reasons and ledger is not None:
        ledger.record(query, cleaned, reasons)
    return cleaned, reasons


def offered_key(url: str) -> str:
    """The loose identity a fetch URL is repaired against. See ``url_key``."""
    from ._reference_page import url_key

    return url_key(url)


def repair_url(url: str, offered: Mapping[str, str]) -> tuple[str, str]:
    """``(url to fetch, note)`` — the EXACT URL the model was shown, if any.

    THE ``www.`` DEFECT, in code. The 06:43Z build was shown
    ``https://www.theguardian.com/australia-news/…`` and asked for
    ``https://theguardian.com/australia-news/…``; the second is a 404 and the
    first is the run's only in-window page. The loop knows every URL it put in
    front of the model, and the loose key (host without ``www.`` + path) matches
    the two — so the repair is exact, recorded, and costs nothing.

    It never INVENTS a target: a URL that matches no offered result is fetched
    exactly as written, because carried notes and re-fetches legitimately name
    pages no search in THIS run returned.
    """
    exact = (url or "").strip()
    if not exact or exact in offered.values():
        return exact, ""
    match = offered.get(offered_key(exact))
    if not match or match == exact:
        return exact, ""
    return match, (
        f"URL REPAIRED: you asked for {exact} ; the result you were shown was "
        f"{match} and that is what was fetched. Copy the `url` field exactly "
        "next time — a retyped URL usually 404s."
    )


__all__ = [
    "EMPTY_COMMIT_WITH_MATERIAL",
    "FETCH_FIRST_RULE",
    "MANIFEST_EXCERPT_CHARS",
    "MANIFEST_TOTAL_CHARS",
    "QUERY_PATTERNS",
    "RATIO_GUARD_AFTER_CALLS",
    "RATIO_GUARD_FACTOR",
    "SEARCH_RUN_LIMIT",
    "ManifestEntry",
    "QueryLedger",
    "apply_query_ledger",
    "carry_lines",
    "clean_query",
    "commit_prompt",
    "has_material",
    "in_window_entries",
    "manifest_records",
    "opened_prompt",
    "pushback_prompt",
    "query_guidance",
    "ratio_breached",
    "ratio_nudge",
    "record_page",
    "render_manifest",
    "repair_url",
]
