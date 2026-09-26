# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The search result cap is 30, end to end, and SearXNG pages up to reach it.

WHY THIS FILE EXISTS. Every web search in the platform was clamped to 10
results — ``MAX_RESULTS_CAP`` in the stack layer, ``_MAX_SEARCH_RESULTS`` in
``web_search``, ``_MAX_RESULTS`` in ``web_evidence`` — while a census of the
deployed SearXNG measured 27-29 results per query. The auditor, the reference
builder, the researcher and the consult all saw a third of what search found,
and none of them could tell: a clamp leaves no trace in the response.

What is pinned here:

  * the three caps agree, and they are 30;
  * a 28-result SearXNG page survives the parser INTACT at the cap (the census
    shape — the old cap cut it to 10);
  * the handler asks for a SECOND page only when the first came back short of
    the ask, dedupes on URL, and re-ranks densely;
  * a failure on page 2 never destroys page 1;
  * Brave still asks its own wire for at most 20 — its ``count`` ceiling, above
    which the API 422s — however high the package cap goes.
"""
from __future__ import annotations

import pytest

from legba.data.analysts.agency.research_tools import _MAX_RESULTS
from legba.data.analysts.agency.web_tools import _MAX_SEARCH_RESULTS
from legba.data.schemas.stack import SearchProviderConfig
from legba.data.stack.search import (
    MAX_RESULTS_CAP,
    HardSearchFailure,
    SearchHandlerContext,
    SearxngSearchHandler,
    TransientSearchFailure,
    parse_searxng_payload,
)
from legba.data.stack.search.brave import (
    BRAVE_MAX_COUNT,
    BraveSearchHandler,
    parse_brave_payload,
)


def _searxng_page(start: int, count: int) -> dict:
    """A SearXNG JSON body of ``count`` hits numbered from ``start``."""
    return {
        "query": "q",
        "results": [
            {"url": f"https://outlet{i}.example/story", "title": f"t{i}",
             "content": f"c{i}", "engine": "duckduckgo", "score": 1.0}
            for i in range(start, start + count)
        ],
        "unresponsive_engines": [],
    }


#: The census shape: what one page of the deployed instance actually returns.
CENSUS_PAGE = _searxng_page(0, 28)


async def _configured(handler_cls, **cfg):
    handler = handler_cls()
    config = SearchProviderConfig.model_validate({
        "subprovider": {
            "factory_kind": "dropdown_static", "raw": handler_cls.subprovider,
            "options": ["searxng", "json", "firecrawl", "jina", "tavily",
                        "brave", "agent"],
        },
        "endpoint": {"factory_kind": "text",
                     "raw": "http://searxng:8080/search"},
        **cfg,
    })
    await handler.on_configure(
        SearchHandlerContext(instance_id="search.test.local", config=config)
    )
    return handler


# ---------------------------------------------------------------------------
# 1) The cap itself
# ---------------------------------------------------------------------------


def test_the_cap_is_thirty_everywhere():
    """One number, three call sites. A drift here re-creates the defect."""
    assert MAX_RESULTS_CAP == 30
    assert _MAX_SEARCH_RESULTS == MAX_RESULTS_CAP
    assert _MAX_RESULTS == MAX_RESULTS_CAP


def test_the_operator_knob_defaults_to_the_cap_and_still_bounds_at_fifty():
    """``max_results`` is the per-provider knob; the cap is the ceiling."""
    field = SearchProviderConfig.model_fields["max_results"]
    default = field.default_factory()
    assert default.raw == MAX_RESULTS_CAP
    assert default.maximum == 50


def test_a_twenty_eight_result_page_survives_the_parser_intact():
    """The measured census shape. Under the old cap this returned 10."""
    resp = parse_searxng_payload(CENSUS_PAGE, query="q")
    assert resp.count == 28
    assert [r.rank for r in resp.results] == list(range(1, 29))


def test_the_callers_ask_is_still_honoured_below_the_cap():
    assert parse_searxng_payload(CENSUS_PAGE, query="q", limit=5).count == 5


# ---------------------------------------------------------------------------
# 2) The second page — asked for ONLY when one page is short of the ask
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_searxng_tops_up_from_page_two_to_reach_the_cap():
    """28 on page 1 + a deduped page 2 = the full 30, ranked 1..30."""
    handler = await _configured(SearxngSearchHandler)
    asked: list[dict] = []

    async def _fake_get_json(endpoint, *, params, timeout):
        asked.append(dict(params))
        if params.get("pageno") == "2":
            # Pages OVERLAP on the real instance — 10 of 35 in the live probe.
            # The last 8 of page 1 repeat, then 12 genuinely new hits.
            return _searxng_page(20, 20)
        return CENSUS_PAGE

    handler._get_json = _fake_get_json
    resp = await handler.search("q", limit=MAX_RESULTS_CAP)

    assert resp.count == MAX_RESULTS_CAP
    assert len(asked) == 2
    assert "pageno" not in asked[0]
    assert asked[1]["pageno"] == "2"
    assert asked[1]["q"] == asked[0]["q"]
    # Deduped on URL, and rank stays dense over the MERGED list.
    urls = [r.url for r in resp.results]
    assert len(set(urls)) == len(urls)
    assert [r.rank for r in resp.results] == list(range(1, 31))


@pytest.mark.asyncio
async def test_no_second_page_when_one_page_already_satisfies_the_ask():
    """A caller asking for 5 costs ONE request, as it always did."""
    handler = await _configured(SearxngSearchHandler)
    calls = 0

    async def _fake_get_json(endpoint, *, params, timeout):
        nonlocal calls
        calls += 1
        return CENSUS_PAGE

    handler._get_json = _fake_get_json
    resp = await handler.search("q", limit=5)
    assert resp.count == 5
    assert calls == 1


@pytest.mark.asyncio
async def test_no_second_page_when_the_provider_admitted_degradation():
    """A degraded engine set is not fixed by paging it; don't spend the call."""
    handler = await _configured(SearxngSearchHandler)
    calls = 0

    async def _fake_get_json(endpoint, *, params, timeout):
        nonlocal calls
        calls += 1
        return dict(_searxng_page(0, 4),
                    unresponsive_engines=["brave: too many requests"])

    handler._get_json = _fake_get_json
    resp = await handler.search("q", limit=MAX_RESULTS_CAP)
    assert calls == 1
    assert resp.degraded is True
    assert resp.count == 4


@pytest.mark.asyncio
async def test_a_page_that_adds_nothing_new_stops_the_walk():
    """A provider that ignores ``pageno`` costs one wasted call, not a loop."""
    handler = await _configured(SearxngSearchHandler)
    calls = 0

    async def _fake_get_json(endpoint, *, params, timeout):
        nonlocal calls
        calls += 1
        return _searxng_page(0, 24)

    handler._get_json = _fake_get_json
    resp = await handler.search("q", limit=MAX_RESULTS_CAP)
    assert calls == 2
    assert resp.count == 24


@pytest.mark.parametrize("yield_", [0, 1, 4, 19])
@pytest.mark.asyncio
async def test_a_query_that_exhausted_its_engines_is_never_paged(yield_):
    """SearXNG has no page-size parameter. A query that returned four hits did
    not return a short page — it ran out, and page 2 spends upstream goodwill
    (the resource that keeps the instance unbanned) to be told so again."""
    handler = await _configured(SearxngSearchHandler)
    calls = 0

    async def _fake_get_json(endpoint, *, params, timeout):
        nonlocal calls
        calls += 1
        return _searxng_page(0, yield_)

    handler._get_json = _fake_get_json
    resp = await handler.search("q", limit=MAX_RESULTS_CAP)
    assert calls == 1
    assert resp.count == yield_
    assert yield_ < SearxngSearchHandler.page_topup_floor


@pytest.mark.parametrize("failure", [
    TransientSearchFailure("upstream 503"),
    HardSearchFailure("search response not JSON"),
])
@pytest.mark.asyncio
async def test_a_failing_second_page_never_destroys_the_first(failure):
    """Page 1 was REAL. Raising here would render it "the web has nothing"."""
    handler = await _configured(SearxngSearchHandler)

    async def _fake_get_json(endpoint, *, params, timeout):
        if params.get("pageno") == "2":
            raise failure
        return CENSUS_PAGE

    handler._get_json = _fake_get_json
    resp = await handler.search("q", limit=MAX_RESULTS_CAP)
    assert resp.count == 28
    assert resp.results[0].url.endswith("/story")


@pytest.mark.asyncio
async def test_the_component_knob_still_clamps_below_the_cap():
    """An operator who wants 10 results still gets 10 — and one request."""
    handler = await _configured(
        SearxngSearchHandler,
        max_results={"factory_kind": "number", "raw": 10,
                     "minimum": 1, "maximum": 50},
    )
    calls = 0

    async def _fake_get_json(endpoint, *, params, timeout):
        nonlocal calls
        calls += 1
        return CENSUS_PAGE

    handler._get_json = _fake_get_json
    resp = await handler.search("q", limit=MAX_RESULTS_CAP)
    assert resp.count == 10
    assert calls == 1


# ---------------------------------------------------------------------------
# 3) Brave stays provider-correct: <= 20 per page, whatever the cap is
# ---------------------------------------------------------------------------


def test_brave_pages_at_twenty_not_at_the_package_cap():
    """``count`` above Brave's ceiling is a 422 — clamp, never inherit 30."""
    assert BRAVE_MAX_COUNT == 20
    handler = BraveSearchHandler()
    handler._cfg = SearchProviderConfig.model_validate({
        "subprovider": {
            "factory_kind": "dropdown_static", "raw": "brave",
            "options": ["searxng", "json", "firecrawl", "jina", "tavily",
                        "brave", "agent"],
        },
        "endpoint": {"factory_kind": "text",
                     "raw": "https://api.search.brave.com/res/v1/web/search"},
    })
    params = handler._build_params("q", limit=MAX_RESULTS_CAP)
    assert params["count"] == str(BRAVE_MAX_COUNT)


def test_brave_does_not_opt_into_paging():
    """No ``max_extra_pages`` for a METERED provider: a page is a charge."""
    assert BraveSearchHandler.max_extra_pages == 0
    assert BraveSearchHandler()._page_params({"q": "x"}, page=2) is None


def test_brave_parser_still_honours_whatever_limit_it_is_handed():
    body = {"type": "search", "web": {"results": [
        {"url": f"https://b{i}.example", "title": f"t{i}",
         "description": "d"} for i in range(25)
    ]}}
    assert parse_brave_payload(body, query="q", limit=20).count == 20
