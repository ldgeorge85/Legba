# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SearXNG meta-search handler — the deployed discovery engine.

SearXNG forwards a query to many upstream engines (DuckDuckGo, Brave, Mojeek,
Wikipedia, …) and merges their results; it has no index of its own. AGPL-3.0,
identical to Legba's own licence; $0 per query; no key, no quota.

Wire shape (``GET /search?q=…&format=json``)::

    {"query": …, "number_of_results": …,
     "results": [{"url", "title", "content", "publishedDate", "engine",
                  "engines": [...], "score", "category"}, …],
     "answers": [...], "suggestions": [...], "corrections": [...],
     "infoboxes": [...], "unresponsive_engines": [...]}

JSON output is OFF by default in SearXNG and must be enabled in
``settings.yml`` (``search.formats: [html, json]``) or every query returns HTML
and this handler raises ``HardSearchFailure("search response not JSON")``.

Field map (normalized ← SearXNG):

    ==================  ============================
    ``url``             ``url``
    ``title``           ``title``          (≤512)
    ``snippet``         ``content``        (≤1024)
    ``published_at``    ``publishedDate``
    ``engine``          ``engine``
    ``score``           ``score``
    ``rank``            position in ``results[]``
    ``extracted_text``  — (SearXNG returns none, ever)
    ``degraded``        ``unresponsive_engines`` non-empty
    ==================  ============================

THE DEGRADATION READ IS THE POINT OF THIS MODULE
-------------------------------------------------
SearXNG's one structural weakness is that upstream engines classify it as a bot
and CAPTCHA / rate-limit / ban it. Its settings schema has first-class fields
for exactly this (``ban_time_on_fail``, ``suspended_times`` keyed on
``SearxEngineAccessDenied`` / ``SearxEngineCaptcha`` /
``SearxEngineTooManyRequests``) — that is the project saying engines WILL get
banned and asking how long to sideline them.

When they are banned the response is still **HTTP 200**; the banned engines are
simply dropped from the merge and named in ``unresponsive_engines``. A live
probe of the deployed instance returned 200 with 20 results while
``unresponsive_engines`` carried ``brave: too many requests``,
``duckduckgo: CAPTCHA``, ``startpage: CAPTCHA``. Had ALL engines refused, the
same 200 would have carried ``"results": []`` — indistinguishable from "nothing
exists" unless something reads that list.

:func:`parse_searxng_payload` reads it, on every response, and sets
``degraded=True`` whenever it is non-empty. The empty-and-degraded case becomes
:attr:`~..base.SearchStatus.DEGRADED_EMPTY`, whose
``supports_absence_claim`` is False — so a blocked search can never be rendered
as evidence of absence.
"""

from __future__ import annotations

from typing import Any, ClassVar, Mapping

from .base import (
    MAX_RESULTS_CAP,
    SearchProviderHandler,
    SearchResponse,
    SearchResult,
    clamp_snippet,
    clamp_title,
    coerce_engine_names,
)


def parse_searxng_payload(
    payload: Any, *, query: str, limit: int = MAX_RESULTS_CAP,
) -> SearchResponse:
    """Coerce a SearXNG JSON body into the normalized :class:`SearchResponse`.

    A pure function so the degradation contract is unit-testable without a
    network. A non-dict payload, or one with no ``results`` list, is a
    STRUCTURAL surprise rather than "nothing found" — it is reported as
    degraded with an explicit detail, never as a clean empty result.
    """
    if not isinstance(payload, Mapping):
        return SearchResponse(
            query=query, results=[], degraded=True,
            degraded_detail=(
                f"search response was {type(payload).__name__}, not a JSON object "
                "— cannot distinguish 'no results' from a malformed reply"
            ),
        )

    unresponsive = coerce_engine_names(payload.get("unresponsive_engines"))

    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return SearchResponse(
            query=query, results=[], degraded=True,
            degraded_detail=(
                "search response carried no 'results' list "
                "— cannot distinguish 'no results' from a malformed reply"
            ),
            unresponsive_engines=unresponsive,
        )

    results: list[SearchResult] = []
    for item in raw_results:
        if len(results) >= limit:
            break
        if not isinstance(item, Mapping):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        score = item.get("score")
        try:
            score_value = float(score) if score is not None else None
        except (TypeError, ValueError):
            score_value = None
        published = item.get("publishedDate") or item.get("published_date")
        results.append(SearchResult(
            url=url,
            title=clamp_title(item.get("title")),
            # SearXNG's snippet field is `content`; `snippet` is accepted as a
            # courtesy for compatible engines that use the other spelling.
            snippet=clamp_snippet(item.get("content") or item.get("snippet")),
            published_at=str(published) if published else None,
            engine=str(item.get("engine")) if item.get("engine") else None,
            score=score_value,
            rank=len(results) + 1,
            # SearXNG NEVER returns clean main text — it returns a snippet the
            # upstream engine wrote. Leaving this None is what routes retrieval
            # through web_fetch → evidence archive → Trafilatura.
            extracted_text=None,
            extract_source=None,
            # A search hit carries NO license verdict (see SearchResult).
            license_class=None,
            raw=dict(item),
        ))

    degraded = bool(unresponsive)
    detail = (
        "unresponsive_engines: " + ", ".join(unresponsive) if unresponsive else ""
    )
    return SearchResponse(
        query=query,
        results=results,
        degraded=degraded,
        degraded_detail=detail,
        unresponsive_engines=unresponsive,
    )


class SearxngSearchHandler(SearchProviderHandler):
    """``search.searxng.*`` — the deployed default provider."""

    subprovider: ClassVar[str] = "searxng"
    #: Discovery only. SearXNG returns snippets, never clean main text, so it
    #: advertises neither "fetch" nor "extract" — the retrieval leg stays on
    #: the existing, working web_fetch → archive → Trafilatura path.
    capabilities: ClassVar[frozenset[str]] = frozenset({"search"})
    default_port: ClassVar[int] = 8080

    def _build_params(self, query: str, *, limit: int, **opts: Any) -> dict[str, str]:
        cfg = self._require_configured()
        params: dict[str, str] = {"q": query, "format": "json"}
        engines = [str(e) for e in (cfg.engines.raw or []) if str(e).strip()]
        if engines:
            params["engines"] = ",".join(engines)
        categories = [str(c) for c in (cfg.categories.raw or []) if str(c).strip()]
        if categories:
            params["categories"] = ",".join(categories)
        language = str(cfg.language.raw or "").strip()
        if language:
            params["language"] = language
        # Operator escape hatch, last so it wins.
        extra = opts.get("params")
        if isinstance(extra, Mapping):
            params.update({str(k): str(v) for k, v in extra.items()})
        return params

    def _parse_payload(
        self, payload: Any, *, query: str, limit: int,
    ) -> SearchResponse:
        return parse_searxng_payload(payload, query=query, limit=limit)


__all__ = ["SearxngSearchHandler", "parse_searxng_payload"]
