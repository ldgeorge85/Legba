# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R2 — the bounded tool loop the core plane runs to build ONE reference.

Ported from ``planning/PROGRAM2_2026-09-16/R1/build_core_reference.py``, which
is the measured implementation, with three platform changes and no behavioural
invention:

  * **The transport is the platform's.** Search and fetch go through the
    ``web_access`` ACTION PACK binding (``binding.run_tool``) — the same
    governed door ``desk_reference`` and ``standing_auditor`` use — so the SSRF
    guard, the pack's invocation governor and the per-run cost ledger all apply.
    R1 reached past the pack to the raw egress client because it had no
    ``ToolContext``; a registered analyst has one, so it does not.
  * **The protocol is the shipped one.** ``stack.llm.tool_rounds`` carries the
    native tool-call rounds (``parse_tool_calls`` / ``assistant_tool_turn`` /
    ``tool_result_messages``). That module exists because the JSON-in-prose
    protocol failed live three distinct ways, and one of its measured rules —
    a FORCED call returns ``finish_reason=stop`` with tool calls present — would
    silently drop calls here if this loop keyed on ``finish_reason``. It does
    not; it keys on the structured payload, via that module.
  * **The licence rule binds before the bytes are kept.** A host whose
    ``license_class`` puts it at teaser depth has its page READ (the span is
    checked against it in memory) and its body NOT STORED; the development is
    kept with ``span_source: snippet`` and no archive path. A host at
    ``forbidden`` is never fetched at all.

THE MODEL IS AN INSTRUMENT, NOT A DRIVER. It chooses queries and quotes; it
chooses nothing about what is accepted. Every acceptance rule lives in
``_reference_fences`` and runs AFTER the model has committed, over text the
model cannot reach. That is the same division the correctness grader draws, for
the same reason: a builder whose acceptance rules a model could influence is
not a builder, it is a model's opinion with extra steps.

CONTEXT DISCIPLINE. R1 measured the token shape: **2.57M prompt tokens against
73k completion** over two runs. A tool loop over fetched pages is almost
entirely prompt-side, so the lever is how much page text stays in the
conversation. Tool payloads older than ``EVICT_AFTER_TOOL_MESSAGES`` are
replaced by a stub naming what was there. The model is told this in the
instruction, in terms: its NOTE is the only record that survives.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ...stack.llm.tool_rounds import (
    PROVIDER_OPENAI_COMPAT,
    ToolCall,
    ToolSpec,
    assistant_tool_turn,
    parse_tool_calls,
    provider_for_subprovider,
    render_tools,
    tool_result_messages,
    visible_text,
)
from ._reference_fences import (
    DomainBlocklist,
    NoteGate,
    is_tier12,
    is_unfetchable,
    tier3_dimensions_after_first_pass,
)
from . import _reference_manifest as MANIFEST
from ._reference_page import (
    ArchivedPage,
    ReferenceArchive,
    coerce_page_text,
    host_of,
    now_iso,
    resolve_publish_date,
)

logger = logging.getLogger(__name__)

#: Tool payloads older than this many tool messages are stubbed out. R1's value.
EVICT_AFTER_TOOL_MESSAGES = 4

#: Search results shown per query.
SEARCH_RESULTS_TO_MODEL = 10

#: Developments the model is asked for before it may say REFERENCE COMPLETE.
MIN_NOTED_DEVELOPMENTS = 20

#: How many times the loop pushes back on a premature "REFERENCE COMPLETE".
MAX_GATHER_PUSHBACKS = 2

#: Searches in a row without a fetch before the loop reminds the model that a
#: snippet is not evidence. R1 run 2's nudge; it exists because run 1 spent 61
#: of its 80 calls searching and 17 fetching, and spans live in pages. D6
#: lowered it from four to two: the 06:43Z Australia build ran nine searches
#: between its second and third fetch and archived one page in 44 calls.
SEARCH_RUN_NUDGE = MANIFEST.SEARCH_RUN_LIMIT

#: Hard ceiling on model rounds, independent of the tool budget: a model that
#: neither calls a tool nor commits must still terminate.
MAX_ROUNDS_PER_TOOL_CALL = 4

#: Fraction of the tool budget after which an under-covered dimension is opened
#: to Tier 3. Before this the allowlist holds the bar; after it, an empty
#: dimension is the worse outcome. See the call site.
TIER3_OPEN_AT = 0.5

# ---------------------------------------------------------------------------
# D3 — THE COMMIT WALL (2026-09-17)
# ---------------------------------------------------------------------------
#
# WHAT WENT WRONG, in one paragraph. The 00:43Z autonomous tick picked Argentina
# and ran 927.9 s for 4,510,885 prompt tokens over 67 tool calls, and committed
# NOTHING: the ``web_access`` pack's invocation governor was already over its
# ``max_invocations_per_hour: 120`` when the tick fired, so 63 of those calls
# came back "not admitted by the pack" and the model spent a quarter of an hour
# being told, in sixty-three different ways, that it could not read the web. The
# loop had no opinion about any of it. Its ONLY exit toward a commit was the
# TOOL BUDGET running out (80 calls) or the model volunteering "REFERENCE
# COMPLETE"; a model that neither exhausts the budget nor volunteers simply runs
# until ``max_rounds`` (320 at the shipped cap), and every round costs a full
# re-send of the conversation. R1 run 1 is the same failure class at a smaller
# scale — 80 of 80 calls spent, 1 of 13 spans verified.
#
# The consequences were NOT confined to the lane. 4.5 M tokens against the
# descriptor's ``budget_tokens_per_day: 4000000`` exhausted the day bucket, so
# the 01:43 tick's budget precheck returned ``exhausted`` and stamped a
# ONE-HOUR GLOBAL COOLDOWN on the actor (``BudgetRetryPolicy.cooldown_seconds``,
# default 3600), the 02:43 tick no-op'd on it, and the liveness watchdog raised
# ``cadence_stall``. A 15-minute turn also outlived the reconciler's 20 s
# ENSURE_ACTIVE heal deadline twice (``actor_turn.budget_exceeded
# op=reconcile.activate``) — a held turn, which is the thing
# ``runtime/actor_turn.py`` exists to make impossible.
#
# So three walls, all of them in code, none of them the model's to choose:
#
#   1. A HARD WALL-CLOCK CAP on the whole build. A build is a bounded thing or
#      it is a liability to the plane that runs it.
#   2. A FORCED COMMIT TURN at ~70% of whichever budget runs out first — tool
#      calls, wall clock, or tokens. The loop stops offering tools, sends one
#      final instruction, and takes whatever the model has. A thin reference is
#      a measurement; no reference is a receipt.
#   3. An EARLY commit when the search plane is demonstrably not answering,
#      rather than spending the rest of the budget proving it twice more.

#: Hard wall-clock cap for ONE build, seconds. Env-overridable; the descriptor
#: knob ``build_max_seconds`` overrides both. 420 s is 7 minutes: comfortably
#: above the 294 s the one good live build needed (Ukraine, 5 developments) and
#: comfortably below BOTH the descriptor's 3000 s cadence cooldown and the
#: 3600 s tick period, so a build can never still be holding the actor's turn
#: when the next reminder fires. It is NOT below the reconciler's 20 s heal
#: deadline and nothing useful could be — see ``tests`` for what is actually
#: asserted and why.
BUILD_MAX_SECONDS_ENV = "LEGBA_REFERENCE_BUILD_MAX_SECONDS"
BUILD_MAX_SECONDS_DEFAULT = 900.0   # 2026-09-20 operator: was 420

#: Hard per-build token ceiling. THE lever on the defect that actually stamped
#: the cooldown: one build may not eat the analyst's whole day bucket. At
#: 1.2 M against ``budget_tokens_per_day: 4000000`` three builds fit a day with
#: headroom, and no single build can exhaust it. R1 measured a good build at
#: 0.95 M-1.69 M total; this sits at the low end ON PURPOSE — a build that is
#: already past 1.2 M is a build that is not converging.
BUILD_MAX_TOKENS_ENV = "LEGBA_REFERENCE_BUILD_MAX_TOKENS"
BUILD_MAX_TOKENS_DEFAULT = 1_200_000

#: Fraction of each budget at which the loop STOPS GATHERING and commits. The
#: one good live build (Israel) committed on its own at 55 of 80 calls, so 70%
#: of the shipped cap (56) is where a converging build already stops; what this
#: changes is only the behaviour of one that is NOT converging.
COMMIT_AT = 0.70

#: Consecutive UNUSABLE searches — an error, a refusal by the pack, or zero
#: results — before the loop stops gathering. Two, because the third is not
#: evidence of absence, it is evidence of the search plane.
SEARCH_DEGRADED_STRIKES = 2


def build_max_seconds() -> float:
    """The wall-clock cap, env-resolved, never non-positive."""
    return _positive_float_env(BUILD_MAX_SECONDS_ENV, BUILD_MAX_SECONDS_DEFAULT)


def build_max_tokens() -> int:
    """The per-build token ceiling, env-resolved, never non-positive."""
    return int(_positive_float_env(
        BUILD_MAX_TOKENS_ENV, float(BUILD_MAX_TOKENS_DEFAULT)
    ))


def _positive_float_env(name: str, default: float) -> float:
    """Mirrors ``runtime.actor_turn._positive_int_env``, deliberately.

    A cap that silently becomes 0 because of a typo is a cap that has been
    disabled without anyone deciding to — and this one exists precisely because
    an unbounded build took the analyst plane down for two hours.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "reference_builder.bad_env name=%s value=%r — using default %s",
            name, raw, default,
        )
        return default
    if value <= 0:
        logger.warning(
            "reference_builder.non_positive_env name=%s value=%s — using "
            "default %s", name, value, default,
        )
        return default
    return value

_NOTE_LINE = re.compile(r"^\s*(?:NOTE:)?\s*\|", re.M)


# ---------------------------------------------------------------------------
# The two tools
# ---------------------------------------------------------------------------

TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="web_search",
        description=(
            "Search the open web through the local metasearch. Returns title, "
            "url, snippet, engine and published date where the engine supplies "
            "one, plus the engines that refused to answer. Results are filtered "
            "to reference-grade sources (official, IGO, central bank, court, "
            "wire, newspaper of record) unless a dimension has been opened to "
            "wider sources, in which case you are told so."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "time_range": {
                    "type": "string",
                    "enum": ["", "day", "week", "month", "year"],
                    "description": (
                        "Optional recency filter. 'month' usually suits a "
                        "14-day window."
                    ),
                },
            },
            "required": ["query"],
        },
    ),
    ToolSpec(
        name="fetch_page",
        description=(
            "Fetch one absolute http(s) URL and return the page's readable "
            "TEXT plus the publish date declared in the page's OWN metadata. "
            "The full text is archived; every decisive_span you record is "
            "later checked as an exact substring of it. A page with no "
            "machine-readable publish date CANNOT carry a development, however "
            "good the quote — the date gate rejects it."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute http(s) URL."},
            },
            "required": ["url"],
        },
    ),
)


@dataclass
class ToolPlane:
    """Search and fetch, governed, archived, licence-gated.

    Everything the model can reach, and nothing else. Constructed with the
    pack binding rather than with a client, so this object cannot make an
    ungoverned request even by mistake.
    """

    binding: Any
    archive: ReferenceArchive
    blocklist: DomainBlocklist
    licence_lookup: Callable[[str], Awaitable[tuple[str, str, str | None]]] | None = None
    page_chars_to_model: int = 7000
    search_timeout: float = 45.0
    fetch_timeout: float = 45.0
    tier12_only: bool = True
    extra_tier2: tuple[str, ...] = ()
    searches: list[dict[str, Any]] = field(default_factory=list)
    fetches: list[dict[str, Any]] = field(default_factory=list)
    #: D6 — THE LOOP'S OWN RECORD OF WHAT IT READ. One entry per usable page,
    #: written here at fetch time and handed back at commit time. No model
    #: cooperation is involved: a build whose model never writes a NOTE still
    #: arrives at its commit turn with every page it read, dated, excerpted and
    #: judged against the window.
    manifest_entries: list[MANIFEST.ManifestEntry] = field(default_factory=list)
    #: Every URL this plane has SHOWN the model, keyed loosely (host without
    #: ``www.`` + path). The 06:43Z build lost its only in-window page by
    #: dropping a ``www.``; this is what repairs that, exactly. See
    #: ``_reference_manifest.repair_url``.
    offered: dict[str, str] = field(default_factory=dict)
    offered_titles: dict[str, str] = field(default_factory=dict)
    queries: MANIFEST.QueryLedger = field(default_factory=MANIFEST.QueryLedger)
    url_repairs: list[dict[str, str]] = field(default_factory=list)
    unfetchable_refused: list[str] = field(default_factory=list)
    unoffered_refused: list[str] = field(default_factory=list)
    #: WHY each fetch ended, tallied — ``{ok, stub, blocked, timed_out,
    #: refused}``, summing to ``len(self.fetches)``. A host that REFUSED this
    #: lane and one that TIMED OUT used to look identical in the receipt, and
    #: they take opposite remedies: refusals are a source problem, timeouts a
    #: transport one (measured on dfat.gov.au / defence.gov.au, which served
    #: 200 from the same network minutes later).
    fetch_outcomes: Counter[str] = field(default_factory=Counter)

    def _tally(self, outcome: str) -> None:
        """One fetch ended as ``outcome``. See :attr:`fetch_outcomes`."""
        self.fetch_outcomes[outcome] += 1

    def admit(self, urls: Sequence[str]) -> int:
        """Add URLs to the ADMITTED set without a search having returned them.

        The one caller is the builder, seeding the pages a previous attempt on
        this target really read (``_reference_notes``' carry). Those URLs were
        recorded by the loop itself after a successful fetch, so they are known
        to exist — which is exactly the property the offered set stands for, and
        the reason a carried lead is not treated as a composed URL.
        """
        added = 0
        for url in urls:
            url = str(url or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            key = MANIFEST.offered_key(url)
            if key not in self.offered:
                self.offered[key] = url
                added += 1
        return added

    def _offered_for(self, host: str) -> list[str]:
        """The URLs on offer for one host — what to fetch INSTEAD."""
        return [
            url for url in self.offered.values() if host_of(url) == host
        ][:5]

    # -- search ------------------------------------------------------------
    async def web_search(self, query: str, time_range: str = "") -> dict[str, Any]:
        # D6 — THE MODEL DOES NOT PRE-FILTER. ``site:`` operators, date literals
        # and the names of hosts this lane cannot read are stripped BEFORE the
        # pack is asked, so the round buys the thirty results the query would
        # have found rather than the two the operator narrowed it to.
        query, rewrite_reasons = MANIFEST.apply_query_ledger(self.queries, query)
        args: dict[str, Any] = {
            "query": query, "limit": SEARCH_RESULTS_TO_MODEL * 3,
        }
        if time_range:
            args["time_range"] = time_range
        try:
            outcome = await asyncio.wait_for(
                self.binding.run_tool("web_search", args),
                timeout=self.search_timeout,
            )
        except asyncio.TimeoutError:
            self.searches.append({"query": query, "error": "timeout", "results": 0})
            return {"error": "web_search timed out"}
        except Exception as exc:  # noqa: BLE001 — degrade, never lose the run
            logger.warning("reference_builder.search_failed err=%s", exc)
            self.searches.append(
                {"query": query, "error": str(exc), "results": 0}
            )
            return {"error": f"web_search failed: {exc}"}

        if not getattr(outcome, "admitted", False):
            cause = getattr(outcome, "block_cause", None)
            self.searches.append(
                {"query": query, "error": f"blocked: {cause}", "results": 0}
            )
            return {"error": f"web_search was not admitted by the pack: {cause}"}

        result = getattr(outcome, "tool_result", None)
        output = dict(getattr(result, "output", None) or {})
        raw_results = list(output.get("results") or [])
        unresponsive = list(output.get("unresponsive_engines") or [])

        kept, dropped_blocked = self.blocklist.filter_results(raw_results)

        # MEASURED-UNREADABLE hosts leave discovery whatever their tier. A
        # result this lane cannot fetch is not a lead, it is a budget sink —
        # the first live build spent 13 of 17 fetch attempts on one such host.
        dropped_unfetchable: list[str] = []
        readable = []
        for item in kept:
            url = str(item.get("url") or "")
            if is_unfetchable(url):
                dropped_unfetchable.append(host_of(url))
                continue
            readable.append(item)
        kept = readable

        dropped_tier: list[str] = []
        if self.tier12_only:
            allowed = []
            for item in kept:
                url = str(item.get("url") or "")
                if is_tier12(url, self.extra_tier2):
                    allowed.append(item)
                else:
                    dropped_tier.append(host_of(url))
            kept = allowed

        shown = []
        for item in kept[:SEARCH_RESULTS_TO_MODEL]:
            snippet = str(item.get("snippet") or item.get("content") or "").strip()
            url = str(item.get("url") or "")
            title = str(item.get("title") or "").strip()[:200]
            if url:
                # THE OFFER IS RECORDED. Everything downstream — the URL repair,
                # the fetch-first rule, the manifest's title column — reads this
                # dict, because it is the only place the loop knows what it
                # actually put in front of the model.
                self.offered.setdefault(MANIFEST.offered_key(url), url)
                self.offered_titles.setdefault(url, title)
            shown.append({
                "title": title,
                "url": item.get("url"),
                "snippet": snippet[:300],
                "engine": item.get("engine"),
                "published": (str(item.get("published") or item.get(
                    "publishedDate") or "")[:10] or None),
            })
        self.searches.append({
            "query": query, "time_range": time_range,
            "results": len(raw_results), "returned": len(shown),
            "dropped_blocked": sorted(set(dropped_blocked)),
            "dropped_unfetchable": sorted(set(dropped_unfetchable)),
            "dropped_off_allowlist": sorted(set(dropped_tier)),
            "unresponsive": unresponsive,
            "snippets": [s["snippet"] for s in shown],
        })
        payload: dict[str, Any] = {
            "query": query,
            "total_results": len(raw_results),
            "results": shown,
            "unresponsive_engines": unresponsive,
            "note": (
                "Engines that refused are listed; an EMPTY result list with "
                "engines refusing is NOT evidence of absence."
            ),
        }
        if dropped_tier:
            payload["dropped_off_allowlist"] = sorted(set(dropped_tier))
            payload["allowlist_note"] = (
                "Results from sources outside the reference-grade tier were "
                "dropped. Find the matter at an official, wire or "
                "newspaper-of-record source."
            )
        if dropped_blocked:
            payload["dropped_blocked_domains"] = sorted(set(dropped_blocked))
        if dropped_unfetchable:
            payload["dropped_unreadable_hosts"] = sorted(set(dropped_unfetchable))
            payload["unreadable_note"] = (
                "These hosts are reference-grade but serve nothing to this "
                "lane (robots refusal, bot challenge or paywall — measured, "
                "repeatedly). They are dropped so you do not spend the budget "
                "learning it again. Find the matter at another outlet; it is "
                "usually carried by several."
            )
        if rewrite_reasons:
            payload["query_as_sent"] = query
            payload["query_rewritten"] = rewrite_reasons
        return payload

    # -- fetch -------------------------------------------------------------
    async def fetch_page(self, url: str) -> dict[str, Any]:
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            return {"error": f"fetch_page refuses non-http(s) url {url!r}"}

        # D6(a) — THE ``www.`` DEFECT. The URL the model writes is repaired to
        # the URL it was SHOWN whenever the two are the same page. Measured on
        # the 06:43Z build: two fetches, the run's only in-window page, lost to
        # four missing characters.
        repaired, repair_note = MANIFEST.repair_url(url, self.offered)
        if repair_note:
            self.url_repairs.append({"asked": url, "fetched": repaired})
            url = repaired

        # D6(b) — THE FETCH FENCE BEHIND THE DISCOVERY FENCE. A host on the
        # measured-unreadable list was already dropped from search RESULTS; the
        # 06:43Z build then spent four of its eight fetch calls on reuters.com
        # and bloomberg.com URLs it wrote from memory. A refusal here costs no
        # tool budget and no pack invocation, which is the whole point.
        if is_unfetchable(url):
            host = host_of(url)
            self.unfetchable_refused.append(host)
            self._tally("refused")
            self.fetches.append(
                {"url": url, "host": host, "status": "refused",
                 "via": "measured-unreadable"}
            )
            return {
                "url": url, "status": "refused",
                "error": (
                    f"{host} is MEASURED unreadable by this lane (robots "
                    "refusal, bot challenge or hard paywall — probed, "
                    "repeatedly, and browser impersonation was measured to "
                    "change nothing). It is dropped from your search results "
                    "for that reason and it will not be fetched however the "
                    "URL reaches you. Find the same matter at another outlet; "
                    "a wire story is carried by several."
                ),
            }

        cached = self.archive.lookup_exact(url)
        if cached is not None and cached.usable:
            # A re-read of a page this run already archived. It cost no egress,
            # and it is still a readable page in front of the model.
            self._tally("ok")
            self.fetches.append(dict(cached.as_record(), cached=True))
            payload = self._page_payload(cached, cached=True)
            if repair_note:
                payload["repair_note"] = repair_note
            return payload

        # D6(d) — A FETCH IS OF AN ADMITTED RESULT, OR IT IS NOT A FETCH.
        # The measured case is the 06:43Z Guardian URL: four characters off the
        # one the model was shown, twice, for the run's only in-window page. The
        # repair above catches that shape; this catches the shape it cannot —
        # a URL with no offered near-match at all, which the loop has no way to
        # know exists.
        #
        # STATED CAREFULLY, because a first reading of the 07:23Z live dry run
        # overstated it. That run spent fifteen fetch records for ONE usable
        # page, and four of them went to ``dfat.gov.au`` and
        # ``defence.gov.au`` paths that looked composed. Probed afterwards,
        # those are REAL pages — one of them dated inside the window — that
        # intermittently hang for this lane (``httpx.ReadTimeout`` with an empty
        # message, surfaced as ``web_fetch.http_error … err=``); the same URLs
        # served 200 from inside the runtime network ten minutes earlier.
        # So the evidence for this fence is the Guardian case and the design
        # rule, NOT a count of composed URLs — and the fence is deliberately
        # cheap and reversible: it costs no tool budget, it names the URLs that
        # ARE on offer for the host, and it stands down entirely when nothing
        # has been offered.
        #
        # ``offered`` is every URL this lane has actually seen: search results
        # shown this run, plus the pages a previous attempt really read (the
        # builder seeds those from the carry — see ``admit``). Empty means
        # nothing has been offered yet, and then this fence stands down rather
        # than making the first call impossible.
        if self.offered and MANIFEST.offered_key(url) not in self.offered:
            self.unoffered_refused.append(host_of(url))
            self._tally("refused")
            self.fetches.append(
                {"url": url, "host": host_of(url), "status": "refused",
                 "via": "never-offered"}
            )
            return {
                "url": url, "status": "refused",
                "error": (
                    "REFUSED: no search result ever offered this URL, so it was "
                    "composed rather than copied. A composed URL is a 404 far "
                    "more often than it is a page, and it costs the same as a "
                    "real one. Fetch one of the `url` values a search actually "
                    "returned, copied character for character — or search for "
                    "the matter and fetch what comes back."
                ),
                "urls_on_offer_for_this_host": self._offered_for(host_of(url)),
            }

        if self.blocklist.blocked(url):
            self._tally("refused")
            self.fetches.append(
                {"url": url, "host": host_of(url), "status": "refused",
                 "via": "domain-blocklist"}
            )
            return {
                "url": url, "status": "refused",
                "error": self.blocklist.refuse(url),
            }

        depth, depth_reason, licence_class = "full_text", "cleared", None
        if self.licence_lookup is not None:
            try:
                depth, depth_reason, licence_class = await self.licence_lookup(url)
            except Exception as exc:  # noqa: BLE001 — fail SAFE, not open
                logger.warning(
                    "reference_builder.licence_lookup_failed url=%s err=%s",
                    url, exc,
                )
                depth, depth_reason = "teaser", "license_unreviewed"
        if depth == "forbidden":
            self._tally("refused")
            self.fetches.append({
                "url": url, "host": host_of(url), "status": "licence_refused",
                "licence_class": licence_class, "depth": depth,
                "depth_reason": depth_reason,
            })
            return {
                "url": url, "status": "refused",
                "error": (
                    f"{host_of(url)} carries a reviewed licence class that "
                    "forbids retrieval; this lane will not fetch it. Find the "
                    "matter at another outlet."
                ),
            }

        try:
            outcome = await asyncio.wait_for(
                self.binding.run_tool("web_fetch", {"url": url}),
                timeout=self.fetch_timeout,
            )
        except asyncio.TimeoutError:
            # The plane's OWN backstop fired — the tool did not get to report.
            return self._fetch_failure(url, 0, "fetch timed out", depth,
                                       depth_reason, licence_class,
                                       outcome="timed_out")
        except Exception as exc:  # noqa: BLE001 — degrade, never lose the run
            return self._fetch_failure(url, 0, f"{type(exc).__name__}: {exc}",
                                       depth, depth_reason, licence_class)

        if not getattr(outcome, "admitted", False):
            return self._fetch_failure(
                url, 0, f"not admitted by the pack: "
                        f"{getattr(outcome, 'block_cause', None)}",
                depth, depth_reason, licence_class,
            )
        result = getattr(outcome, "tool_result", None)
        if str(getattr(result, "status", "")) != "completed":
            # ``web_fetch`` stamps WHY on its output. A timeout is a page
            # this lane failed to REACH (the tool already retried it once);
            # folding it into "blocked" is what hid the defect.
            tool_outcome = str(
                (getattr(result, "output", None) or {}).get("fetch_outcome") or ""
            )
            return self._fetch_failure(
                url, 0, str(getattr(result, "error", "") or "fetch failed"),
                depth, depth_reason, licence_class,
                outcome="timed_out" if tool_outcome == "timed_out" else "blocked",
            )

        output = dict(getattr(result, "output", None) or {})
        status = int(output.get("status_code") or 0)
        body = str(output.get("body") or "")
        final_url = str(output.get("url") or url)
        text, ldjson = coerce_page_text(body)
        # The ladder minus rung 6: a ``web_fetch`` ToolResult carries no
        # response headers, so ``Last-Modified`` cannot answer on this path.
        dated = resolve_publish_date(body, ldjson, url=final_url or url, text=text)

        page = ArchivedPage(
            url=url, final_url=final_url, host=host_of(final_url or url),
            status=status, chars=len(text), publish_date=dated.value,
            date_source=dated.source, date_disagreement=dated.disagreements,
            fetch_ts=now_iso(), licence_class=licence_class, depth=depth,
            depth_reason=depth_reason,
        )
        if depth == "full_text":
            self.archive.put(page, text)
        else:
            # Teaser depth: the licence rule forbids STORING the body. The text
            # is held for THIS run only — long enough to check a span against
            # it — and never written to the archive. The development that
            # results is flagged ``span_source: snippet`` by the fences.
            self.archive.index_unstored(page, text)

        if not page.usable:
            self.blocklist.record_failure(url)
        else:
            # D6(c) — THE NOTE THE LOOP TAKES FOR ITSELF. Written here, inside
            # the call that archived the bytes, so a page at teaser depth (body
            # never stored) still reaches the commit turn with an excerpt the
            # model can quote from. The model's own NOTE is still demanded — see
            # the NoteGate — but the record no longer depends on it arriving.
            self.manifest_entries.append(MANIFEST.record_page(
                page, text,
                title=self.offered_titles.get(page.final_url or page.url, ""),
            ))
        # SERVED. ``usable`` is "200 and not thin" — a paywall interstitial, a
        # consent wall and a challenge page all land here as ``stub``, which is
        # a different fact about the host than a timeout or a refusal.
        self._tally("ok" if page.usable else "stub")
        self.fetches.append(dict(page.as_record(), cached=False))
        payload = self._page_payload(page, cached=False)
        if repair_note:
            payload["repair_note"] = repair_note
        return payload

    def _fetch_failure(
        self, url: str, status: int, error: str,
        depth: str, depth_reason: str, licence_class: str | None,
        *, outcome: str = "blocked",
    ) -> dict[str, Any]:
        self.blocklist.record_failure(url)
        self._tally(outcome)
        self.fetches.append({
            "url": url, "host": host_of(url), "status": status,
            "error": error, "depth": depth, "depth_reason": depth_reason,
            "licence_class": licence_class, "cached": False,
            "outcome": outcome,
        })
        note = (
            "BLOCKED — this host did not serve a readable page. Find the "
            "same matter at another outlet."
        )
        if outcome == "timed_out":
            # Told apart for the MODEL too, not just for the receipt: a
            # timeout is not the host refusing, and a model told "blocked"
            # writes the host off for the rest of the run.
            note = (
                "TIMED OUT — this host did not answer in time; it was already "
                "tried twice. That is NOT a refusal and NOT evidence the page "
                "is missing. Move on to another outlet carrying the same "
                "matter rather than re-fetching this URL."
            )
        return {
            "url": url, "status": status, "error": error,
            "outcome": outcome, "note": note,
        }

    def _page_payload(self, page: ArchivedPage, *, cached: bool) -> dict[str, Any]:
        text = self.archive.text_for(page) or page.text
        payload: dict[str, Any] = {
            "url": page.final_url or page.url,
            "status": page.status,
            "chars": page.chars,
            "publish_date": page.publish_date,
            "date_source": page.date_source,
            "outlet_host": page.host,
            "cached": cached,
            "text": text[: self.page_chars_to_model],
            "truncated": len(text) > self.page_chars_to_model,
        }
        if page.publish_date is None:
            payload["date_warning"] = (
                "THIS PAGE DECLARES NO PUBLISH DATE THIS LANE CAN READ — not "
                "in its metadata, not in its URL, and not as one unambiguous "
                "dateline at the top of its own text. A development anchored "
                "here will be REJECTED by the date gate, whatever the quote "
                "says and whatever date appears elsewhere on the page — a "
                "masthead is not a publish date. Find the matter at a page "
                "that declares its date."
            )
        if not page.usable:
            payload["note"] = (
                f"THIN OR BLOCKED — status {page.status}, only {page.chars} "
                "characters of readable text. This is almost certainly a "
                "paywall, consent or challenge stub, not the article. Do NOT "
                "quote it; find the matter at another outlet."
            )
        if page.depth and page.depth != "full_text":
            payload["licence_note"] = (
                "This host's licence class is unreviewed, so its body is NOT "
                "archived. A span from it is recorded as snippet-sourced."
            )
        return payload


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


@dataclass
class LoopResult:
    reference: dict[str, Any] | None
    final_text: str
    notes: list[str]
    rounds: int
    tool_calls: int
    usage: dict[str, int]
    wall_seconds: float
    stop_reason: str
    pushbacks: int
    tier3_opened: list[str]
    note_gate: dict[str, Any]
    #: WHY the loop stopped gathering — "" when it never did (it broke out
    #: first), "model" when the model volunteered REFERENCE COMPLETE, or the
    #: named wall that forced it: ``tool_budget`` / ``wall_clock`` /
    #: ``token_ceiling`` / ``search_degraded``. This is the line an operator
    #: reads to know whether a thin reference was the window's fault or the
    #: budget's.
    commit_trigger: str = ""
    #: The search plane returned nothing usable ``SEARCH_DEGRADED_STRIKES``
    #: times running. Recorded whether or not it forced the commit, because a
    #: reference built through a degraded search plane under-covers for a
    #: reason that has nothing to do with the window.
    search_degraded: bool = False
    #: The caps this build actually ran under, so the receipt can say what the
    #: run was allowed rather than what the defaults are.
    build_max_seconds: float = BUILD_MAX_SECONDS_DEFAULT
    build_max_tokens: int = BUILD_MAX_TOKENS_DEFAULT
    #: D6 — THE LOOP'S OWN PAGE RECORD, carried off the plane so the caller can
    #: ask the one question that separates a fence failure from a model failure:
    #: was there in-window material in front of the model when it committed
    #: nothing? See ``_reference_manifest.has_material``.
    manifest_entries: list[Any] = field(default_factory=list)
    #: What the query-hygiene rules rewrote, how many URLs were repaired to the
    #: result the model was actually shown, which measured-unreadable hosts were
    #: refused a fetch, and how often the search/fetch ratio guard fired.
    queries: dict[str, Any] = field(default_factory=dict)
    url_repairs: int = 0
    unfetchable_refused: list[str] = field(default_factory=list)
    unoffered_refused: list[str] = field(default_factory=list)
    ratio_nudges: int = 0
    #: WHAT THE FETCHES BOUGHT: ``{ok, stub, blocked, timed_out, refused}`` ->
    #: count, summing to ``len(plane.fetches)``. See
    #: ``ToolPlane.fetch_outcomes`` for why timed_out is separated out.
    fetch_outcomes: dict[str, int] = field(default_factory=dict)

    def total_tokens(self) -> int:
        """Tokens spent, preferring the provider's own total.

        Falls back to prompt + completion: ``total_tokens`` is absent from some
        OpenAI-compatible payloads, and a ceiling that reads 0 because a field
        was missing is not a ceiling.
        """
        total = int(self.usage.get("total_tokens", 0) or 0)
        if total:
            return total
        return int(self.usage.get("prompt_tokens", 0) or 0) + int(
            self.usage.get("completion_tokens", 0) or 0
        )


def count_noted(notes: Sequence[str]) -> int:
    return sum(len(_NOTE_LINE.findall(n)) for n in notes)


def parse_reference_json(text: str) -> dict[str, Any] | None:
    """The final message as ONE JSON object, or ``None``.

    Brace-matched rather than regex-extracted, string-aware, with one
    trailing-comma repair — R1's parser, which handled every real reply both
    runs produced. It never repairs CONTENT, only JSON punctuation.
    """
    body = text.strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*", "", body)
        body = re.sub(r"\s*```\s*$", "", body)
    start = body.find("{")
    if start < 0:
        return None
    depth = end = 0
    end = -1
    in_string = escaped = False
    for index, char in enumerate(body[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end < 0:
        return None
    chunk = body[start:end]
    try:
        parsed = json.loads(chunk)
    except ValueError:
        try:
            parsed = json.loads(re.sub(r",(\s*[}\]])", r"\1", chunk))
        except ValueError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _evict(messages: list[dict[str, Any]]) -> None:
    """Stub out all but the newest N tool payloads."""
    indexes = [
        i for i, m in enumerate(messages)
        if m.get("role") == "tool" and not m.get("_evicted")
    ]
    for index in indexes[:-EVICT_AFTER_TOOL_MESSAGES]:
        message = messages[index]
        message["content"] = message.get("_stub") or (
            "[tool result evicted to save context]"
        )
        message["_evicted"] = True


def _wire(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {k: v for k, v in m.items() if not str(k).startswith("_")}
        for m in messages
    ]


def _accumulate(usage: dict[str, int], response: Any) -> None:
    raw = getattr(response, "usage", None)
    for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens",
                "total_tokens"):
        usage[key] = usage.get(key, 0) + int(getattr(raw, key, 0) or 0)
    usage["rounds"] = usage.get("rounds", 0) + 1


def manifest(archive: ReferenceArchive) -> str:
    """The pages this run actually read, from the ARCHIVE — the older, thinner
    view, kept because the top-up packet and the tests address it by name.

    R1 run 1 cited 11 URLs of 13 it had never fetched, several invented outright
    (one with an ellipsis still in it). With this list in the commit prompt that
    went to zero. The fence that ENFORCES it is in ``_reference_fences``; this
    is the courtesy of telling the model the rule before it breaks it.

    THE COMMIT TURN NO LONGER USES IT. D6 replaced it with
    ``_reference_manifest.commit_prompt``, which carries the same URLs plus the
    page's date judged against the window and the text the model may quote from
    — because a list of URLs and dates is a rule, and what the 06:43Z build
    lacked was not a rule but the sentences.
    """
    lines = [page.manifest_line() for page in archive.fetched_pages()]
    return "\n".join(lines) if lines else "(no page was successfully read)"


def _accepts_tool_choice(llm: Any) -> bool:
    """Whether this model handler will carry ``tool_choice`` to the wire.

    The commit turn already withholds the tool specs, which is what actually
    stops a call; ``tool_choice: "none"`` is the EXPLICIT half of the same
    instruction and the shape an OpenAI-compatible server understands. It is
    sent only when the handler's signature can take it — ``chat_complete``
    doubles in tests and adapters in the tree vary — and a handler that rejects
    it at call time disarms it for the rest of the run rather than losing the
    commit (see ``_complete``).
    """
    try:
        sig = inspect.signature(llm.chat_complete)
    except (AttributeError, TypeError, ValueError):   # pragma: no cover
        return False
    params = sig.parameters
    if "tool_choice" in params:
        return True
    return any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()
    )


async def run_loop(
    *,
    llm: Any,
    plane: ToolPlane,
    instruction: str,
    first_user: str,
    dimensions: Sequence[str],
    tool_call_cap: int,
    note_gate: NoteGate,
    temperature: float = 1.0,
    round_timeout: float = 240.0,
    min_noted: int = MIN_NOTED_DEVELOPMENTS,
    max_seconds: float | None = None,
    max_tokens: int | None = None,
    window_start: date | None = None,
    window_end: date | None = None,
) -> LoopResult:
    """Drive one reference build. Returns whatever the model committed.

    The loop NEVER raises for a model failure: a plane that goes down, a reply
    that will not parse, a budget that runs out all produce a ``LoopResult``
    with a named ``stop_reason`` and whatever was gathered. A build that
    produced nothing is a receipt saying so, which the caller records; a build
    that crashed is a log line nobody reads.

    D3 — IT ALSO ALWAYS TERMINATES, AND ALWAYS ASKS FOR A COMMIT. Three walls
    are enforced here and none of them is the model's to choose: a hard
    wall-clock cap (``max_seconds``), a per-build token ceiling
    (``max_tokens``), and the tool-call cap. At ~70% of whichever comes first
    the loop STOPS OFFERING TOOLS and sends the commit instruction; past 100% of
    the wall clock it breaks out entirely, committed or not. The banner at the
    top of this module carries the incident these walls are the answer to.
    """
    started = time.time()
    cap_seconds = (
        float(max_seconds) if max_seconds and max_seconds > 0
        else build_max_seconds()
    )
    cap_tokens = (
        int(max_tokens) if max_tokens and max_tokens > 0 else build_max_tokens()
    )
    deadline = started + cap_seconds
    commit_after_seconds = cap_seconds * COMMIT_AT
    commit_after_tokens = int(cap_tokens * COMMIT_AT)
    commit_after_calls = max(1, int(tool_call_cap * COMMIT_AT))
    send_tool_choice = _accepts_tool_choice(llm)
    provider = provider_for_subprovider(
        getattr(llm, "subprovider", None)
    ) or PROVIDER_OPENAI_COMPAT
    tools = render_tools(provider, TOOL_SPECS)

    messages: list[dict[str, Any]] = [{"role": "user", "content": first_user}]
    notes: list[str] = []
    usage: dict[str, int] = {}
    tool_calls_used = 0
    pushbacks = 0
    searches_in_a_row = 0
    committing = False
    json_retries = 0
    tier3_opened: list[str] = []
    stop_reason = ""
    final_text = ""
    reference: dict[str, Any] | None = None
    commit_trigger = ""
    search_degraded = False
    search_strikes = 0
    ratio_nudges = 0
    last_ratio_nudge_at = 0
    max_rounds = max(40, tool_call_cap * MAX_ROUNDS_PER_TOOL_CALL)

    def _spent_tokens() -> int:
        total = int(usage.get("total_tokens", 0) or 0)
        if total:
            return total
        return int(usage.get("prompt_tokens", 0) or 0) + int(
            usage.get("completion_tokens", 0) or 0
        )

    def _enter_commit(trigger: str, lead: str) -> None:
        """Stop gathering; ask for the reference from what is already in hand.

        Idempotent: the first wall to fire owns the trigger name, so a build
        that trips the wall clock AND the token ceiling in the same round
        reports the one that actually stopped it.
        """
        nonlocal committing, commit_trigger
        if committing:
            return
        committing = True
        commit_trigger = trigger
        logger.info(
            "reference_builder.forced_commit trigger=%s calls=%d/%d "
            "elapsed=%.1f/%.0fs tokens=%d/%d",
            trigger, tool_calls_used, tool_call_cap, time.time() - started,
            cap_seconds, _spent_tokens(), cap_tokens,
        )
        messages.append({"role": "user", "content": MANIFEST.commit_prompt(
            plane.manifest_entries, lead,
            window_start=window_start, window_end=window_end, forced=True,
        )})

    async def _complete(offer_tools: Any, timeout: float) -> Any:
        """One model round, with ``tool_choice`` disarmed rather than fatal.

        A handler that will not take the kwarg must not cost us the commit —
        that would trade the defect this module exists to fix for a narrower
        one. So a TypeError naming the kwarg retries once without it and stops
        sending it for the rest of the build.
        """
        nonlocal send_tool_choice
        extra: dict[str, Any] = {}
        if committing and send_tool_choice:
            extra["tool_choice"] = "none"
        try:
            return await asyncio.wait_for(
                llm.chat_complete(
                    _wire(messages), tools=offer_tools,
                    temperature=temperature, system=instruction, **extra,
                ),
                timeout=timeout,
            )
        except TypeError as exc:
            if not extra or "tool_choice" not in str(exc):
                raise
            logger.info(
                "reference_builder.tool_choice_unsupported err=%s — the commit "
                "turn withholds the tool specs, which is the binding half", exc,
            )
            send_tool_choice = False
            return await asyncio.wait_for(
                llm.chat_complete(
                    _wire(messages), tools=offer_tools,
                    temperature=temperature, system=instruction,
                ),
                timeout=timeout,
            )

    for _round in range(max_rounds):
        # ---- the walls, checked BEFORE the round is paid for -------------
        remaining = deadline - time.time()
        if remaining <= 0:
            # THE HARD CAP. Past it there is no round left to pay for, not
            # even the commit one — the turn ends here so the actor's queue
            # drains and the receipt names why.
            stop_reason = stop_reason or (
                "build_cap_in_commit" if committing else "build_cap_exceeded"
            )
            logger.warning(
                "reference_builder.build_cap_exceeded elapsed=%.1fs cap=%.0fs "
                "committing=%s calls=%d tokens=%d",
                time.time() - started, cap_seconds, committing,
                tool_calls_used, _spent_tokens(),
            )
            break
        if not committing:
            if (time.time() - started) >= commit_after_seconds:
                _enter_commit("wall_clock", (
                    f"WALL-CLOCK CAP: {time.time() - started:.0f}s of this "
                    f"build's {cap_seconds:.0f}s are spent."
                ))
            elif _spent_tokens() >= commit_after_tokens:
                _enter_commit("token_ceiling", (
                    f"TOKEN CEILING: {_spent_tokens():,} of this build's "
                    f"{cap_tokens:,} tokens are spent."
                ))

        _evict(messages)
        try:
            response = await _complete(
                None if committing else tools,
                # Never let ONE round outlive the build's own cap.
                min(round_timeout, max(1.0, deadline - time.time())),
            )
        except asyncio.TimeoutError:
            stop_reason = "model_timeout"
            break
        except Exception as exc:  # noqa: BLE001 — degrade, never lose the run
            logger.warning("reference_builder.model_failed err=%s", exc)
            stop_reason = f"model_failed: {type(exc).__name__}"
            break
        _accumulate(usage, response)

        calls = [] if committing else parse_tool_calls(provider, response)
        text = visible_text(response)

        # ---- the commit phase -------------------------------------------
        if committing:
            final_text = text
            parsed = parse_reference_json(final_text)
            if parsed is not None:
                reference = parsed
                stop_reason = "committed"
                break
            json_retries += 1
            if json_retries > 1:
                stop_reason = "malformed_json_after_retry"
                break
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": (
                "That was not parseable as a single JSON object. Emit the SAME "
                "reference again as ONE valid JSON object and nothing else — "
                "no prose, no markdown fence, no trailing commas. Keep every "
                "development and every span exactly as you already wrote them."
            )})
            continue

        # ---- F3: the NOTE gate ------------------------------------------
        # D6 — A NOTE THAT ARRIVES BESIDE A TOOL CALL IS STILL A NOTE. The
        # 06:43Z build wrote exactly one span-carrying NOTE line in 71 rounds
        # and the loop threw it away, because the gathering branch below only
        # ever looked at ``calls`` and a turn with both was read as a turn with
        # no text. Recording it here, BEFORE the gate is consulted, also
        # satisfies the gate — which is why that build spent 18 rounds being
        # refused for skipping a note it had in fact written.
        if calls and text.strip():
            notes.append(text.strip())
            note_gate.note_written()

        if calls and note_gate.should_refuse():
            messages.append(assistant_tool_turn(provider, response, calls))
            refusal = note_gate.refuse()
            messages.extend(_refusal_messages(provider, calls, refusal))
            continue

        # ---- the gathering phase ----------------------------------------
        if calls:
            messages.append(assistant_tool_turn(provider, response, calls))
            results, stubs = [], []
            for call in calls:
                payload, stub = await _execute(plane, call)
                results.append(json.dumps(payload, ensure_ascii=False))
                stubs.append(stub)
                # A REFUSED call costs no budget. The fences exist to SAVE tool
                # calls — the domain blocklist's whole measured value in R1 was
                # freeing the budget that a dead host was eating — so charging
                # for a refusal spends exactly what the fence was built to
                # protect. Measured: the first live R2 build burned 11 of its 80
                # calls being told, eleven times, that it could not read a host
                # it had already failed twice.
                if payload.get("status") != "refused":
                    tool_calls_used += 1
                if call.name == "fetch_page":
                    note_gate.after_fetch(ok=not payload.get("error"))
                    searches_in_a_row = 0
                elif call.name == "web_search":
                    searches_in_a_row += 1
                    # D3(d) — THE SEARCH PLANE, NOT THE WINDOW. An error, a
                    # pack refusal or an empty result list are the same fact
                    # from the loop's side: this query produced no lead. Two
                    # running means the next one will not either, and the
                    # 00:43Z build spent 63 calls proving that. Any usable
                    # answer clears the count — a single narrow query is not a
                    # degraded plane.
                    if _search_unusable(payload):
                        search_strikes += 1
                    else:
                        search_strikes = 0
            for message, stub in zip(
                tool_result_messages(provider, calls, results), stubs
            ):
                messages.append(dict(message, _stub=stub))

            if searches_in_a_row >= SEARCH_RUN_NUDGE:
                searches_in_a_row = 0
                messages.append({"role": "user", "content": (
                    f"You have run {SEARCH_RUN_NUDGE} searches without "
                    "fetching a page. " + MANIFEST.FETCH_FIRST_RULE
                )})
            # D6 — THE RATIO GUARD. The run-length rule above catches a streak;
            # this catches the SHAPE of a whole build. 36 searches against 8
            # fetches is not a research strategy, and the 06:43Z build never
            # tripped the streak rule often enough for anyone to notice.
            elif (
                MANIFEST.ratio_breached(
                    searches=len(plane.searches), fetches=len(plane.fetches),
                    tool_calls=tool_calls_used,
                )
                and tool_calls_used - last_ratio_nudge_at
                >= MANIFEST.RATIO_GUARD_AFTER_CALLS
            ):
                last_ratio_nudge_at = tool_calls_used
                ratio_nudges += 1
                messages.append({"role": "user", "content": MANIFEST.ratio_nudge(
                    searches=len(plane.searches), fetches=len(plane.fetches),
                )})
            # THE BUDGET-PRESSURE OPENING. Once most of the budget is gone,
            # any dimension still short of two reference-grade developments is
            # opened to wider sources — because the alternative is not a purer
            # reference, it is an EMPTY dimension, and an empty dimension
            # teaches the grader nothing at all. Items admitted this way are
            # still marked, so what rests on a lower-tier source stays visible.
            if (
                not tier3_opened
                and plane.tier12_only
                and tool_calls_used >= int(tool_call_cap * TIER3_OPEN_AT)
            ):
                opened = tier3_dimensions_after_first_pass(
                    _noted_developments(notes), dimensions
                )
                if opened:
                    tier3_opened = opened
                    plane.tier12_only = False
                    messages.append({
                        "role": "user",
                        "content": MANIFEST.opened_prompt(
                            opened, tool_call_cap - tool_calls_used
                        ),
                    })

            if search_strikes >= SEARCH_DEGRADED_STRIKES and not search_degraded:
                search_degraded = True
                logger.warning(
                    "reference_builder.search_degraded strikes=%d calls=%d — "
                    "committing early rather than spending the rest of the "
                    "budget on a plane that is not answering",
                    search_strikes, tool_calls_used,
                )
                _enter_commit("search_degraded", (
                    f"SEARCH DEGRADED: {search_strikes} searches running "
                    "returned nothing usable (an error, a refusal or no "
                    "results). The rest of this budget would buy more of the "
                    "same, so stop gathering."
                ))
            elif tool_calls_used >= commit_after_calls:
                _enter_commit("tool_budget", (
                    f"TOOL BUDGET SPENT ({tool_calls_used} of "
                    f"{tool_call_cap} calls). Stop gathering."
                ))
            continue

        # ---- a plain-text turn ------------------------------------------
        messages.append({"role": "assistant", "content": text})
        if text.strip():
            notes.append(text.strip())
            note_gate.note_written()
        noted = count_noted(notes)

        # D6 — A MODEL THAT EMITS THE REFERENCE IS DONE GATHERING, whatever
        # words it used. The 06:43Z build emitted its complete final JSON as a
        # plain-text turn at 230 s; it did not contain "REFERENCE COMPLETE", so
        # the loop answered "Noted. Continue." and bought thirteen more tool
        # calls that changed nothing before the wall clock forced the same JSON
        # back out of it. Treat the object as the signal it is — and note what
        # this does NOT do: it does not ACCEPT that object. It enters the commit
        # turn, which hands the model the manifest and asks again.
        volunteered = "REFERENCE COMPLETE" in text.upper()
        emitted_reference = not volunteered and _looks_like_reference(text)
        if volunteered or emitted_reference:
            if noted < min_noted and pushbacks < MAX_GATHER_PUSHBACKS:
                pushbacks += 1
                opened = tier3_dimensions_after_first_pass(
                    _noted_developments(notes), dimensions
                )
                if opened and not tier3_opened:
                    tier3_opened = opened
                    plane.tier12_only = False
                messages.append({
                    "role": "user",
                    "content": MANIFEST.pushback_prompt(
                        noted, min_noted, dimensions, notes,
                        tool_call_cap - tool_calls_used, opened,
                    ),
                })
                continue
            committing = True
            commit_trigger = commit_trigger or (
                "model_emitted_json" if emitted_reference else "model"
            )
            messages.append({"role": "user", "content": MANIFEST.commit_prompt(
                plane.manifest_entries, "Good.",
                window_start=window_start, window_end=window_end,
            )})
            continue
        messages.append({"role": "user", "content": "Noted. Continue."})
    else:
        stop_reason = stop_reason or "max_rounds"

    return LoopResult(
        reference=reference,
        final_text=final_text,
        notes=notes,
        rounds=usage.get("rounds", 0),
        tool_calls=tool_calls_used,
        usage=usage,
        wall_seconds=round(time.time() - started, 1),
        stop_reason=stop_reason or "loop_end",
        pushbacks=pushbacks,
        tier3_opened=tier3_opened,
        note_gate=note_gate.as_record(),
        commit_trigger=commit_trigger,
        search_degraded=search_degraded,
        build_max_seconds=cap_seconds,
        build_max_tokens=cap_tokens,
        manifest_entries=list(plane.manifest_entries),
        queries=plane.queries.as_record(),
        url_repairs=len(plane.url_repairs),
        unfetchable_refused=sorted(set(plane.unfetchable_refused)),
        unoffered_refused=sorted(set(plane.unoffered_refused)),
        ratio_nudges=ratio_nudges,
        fetch_outcomes=dict(plane.fetch_outcomes),
    )


async def _execute(plane: ToolPlane, call: ToolCall) -> tuple[dict[str, Any], str]:
    # gpt-oss leaks harmony channel markers into the tool NAME
    # ("web_search<|channel|>commentary"). R1 run 1 lost 2 of 80 calls to this;
    # strip it rather than fail the call.
    name = str(call.name or "").split("<|")[0].strip()
    args = call.args if isinstance(call.args, Mapping) else {}
    if name == "web_search":
        query = str(args.get("query", ""))
        payload = await plane.web_search(
            query, str(args.get("time_range", "") or "")
        )
        return payload, f"[search results evicted — query was {query[:120]!r}]"
    if name == "fetch_page":
        url = str(args.get("url", ""))
        payload = await plane.fetch_page(url)
        return payload, (
            f"[page text evicted — {url[:160]} ; archived, re-fetching is free]"
        )
    return {"error": f"unknown tool {name!r}"}, "[unknown tool]"


def _search_unusable(payload: Mapping[str, Any]) -> bool:
    """True when a search turn produced no lead the model could act on.

    ``error`` covers a timeout, an exception and the pack's own "not admitted"
    refusal — which is the shape the 00:43Z Argentina build saw 63 times,
    because the ``web_access`` governor was already over
    ``max_invocations_per_hour`` when the tick fired. Otherwise the test is
    ``total_results``: what the metasearch ACTUALLY returned, before this
    module touched it.

    THE RAW COUNT, NEVER ``results``, and the difference is the whole point.
    ``results`` is what survived the Tier 1-2 allowlist, the domain blocklist
    and the measured-unreadable-host drop — all of them FENCES, all of them
    ours. A query that found thirty items and had every one dropped for being
    off-allowlist is a strict build, not a dead search plane, and reading it as
    one would cut every build short for the most ordinary reason there is. The
    first live run of this wall did exactly that: it committed early on an
    Argentina search whose engines had answered. The budget-pressure Tier-3
    opening is what handles an over-filtered build; this signal is only ever
    about whether anything answered at all.
    """
    if payload.get("error"):
        return True
    if "total_results" in payload:
        return not int(payload.get("total_results") or 0)
    return not payload.get("results")


def _refusal_messages(
    provider: str, calls: Sequence[ToolCall], refusal: str
) -> list[dict[str, Any]]:
    bodies = [json.dumps({"error": refusal}, ensure_ascii=False)] * len(calls)
    return [
        dict(m, _stub="[refused: NOTE required first]")
        for m in tool_result_messages(provider, calls, bodies)
    ]


def _noted_developments(notes: Sequence[str]) -> list[dict[str, Any]]:
    """Parse the NOTE lines into development-shaped dicts, for the tier count.

    Deliberately forgiving: this feeds the Tier-3 ADMISSION decision, not the
    reference. A NOTE line the parser cannot read simply does not count toward
    opening a dimension, which errs toward keeping the allowlist closed.
    """
    out: list[dict[str, Any]] = []
    for note in notes:
        for line in note.splitlines():
            if not _NOTE_LINE.match(line):
                continue
            fields: dict[str, Any] = {}
            for part in line.split("|"):
                part = part.strip()
                for key, name in (
                    ("URL:", "source_url"), ("TIER:", "source_tier"),
                    ("DIMS:", "dimension"), ("DATE:", "publish_date"),
                ):
                    if part.upper().startswith(key):
                        fields[name] = part[len(key):].strip()
            if not fields.get("source_url"):
                continue
            url = str(fields["source_url"])
            fields["source_tier"] = 2 if is_tier12(url) else 3
            dims = str(fields.get("dimension") or "")
            fields["dimension"] = [d.strip() for d in dims.split(",") if d.strip()]
            out.append(fields)
    return out


def _looks_like_reference(text: str) -> bool:
    """Is this plain-text turn the FINAL REFERENCE, emitted without the phrase?

    The 06:43Z Australia build emitted its complete reference object — header,
    ref_bands, ref_developments, gaps — as an ordinary turn at 230 s, and the
    loop replied "Noted. Continue." because the words REFERENCE COMPLETE were
    not in it. Thirteen more tool calls and 76 s later the wall clock forced the
    same object back out of it.

    The test is STRUCTURAL and deliberately narrow: one parseable JSON object
    carrying at least two of the contract's own top-level keys. A NOTE, a
    paragraph of prose and a JSON snippet quoted out of a page all fail it,
    which is the direction to fail in — a false positive here ends the
    gathering phase early, and the cost of a false negative is only the turn
    the old behaviour spent anyway.
    """
    if "{" not in text:
        return False
    parsed = parse_reference_json(text)
    if not isinstance(parsed, Mapping):
        return False
    contract = {"ref_developments", "ref_bands", "ref_direction", "header"}
    return len(contract & set(parsed)) >= 2


__all__ = [
    "BUILD_MAX_SECONDS_DEFAULT",
    "BUILD_MAX_SECONDS_ENV",
    "BUILD_MAX_TOKENS_DEFAULT",
    "BUILD_MAX_TOKENS_ENV",
    "COMMIT_AT",
    "EVICT_AFTER_TOOL_MESSAGES",
    "MAX_GATHER_PUSHBACKS",
    "MIN_NOTED_DEVELOPMENTS",
    "SEARCH_DEGRADED_STRIKES",
    "SEARCH_RESULTS_TO_MODEL",
    "TIER3_OPEN_AT",
    "TOOL_SPECS",
    "LoopResult",
    "ToolPlane",
    "build_max_seconds",
    "build_max_tokens",
    "count_noted",
    "manifest",
    "parse_reference_json",
    "run_loop",
]
