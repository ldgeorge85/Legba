# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""serper.dev — the paid SERP rung: wire shape, credential fence, field map.

WHY A MODULE AND NOT A CONFIGURED ``json`` COMPONENT is the first thing pinned
here, because it is the decision a reviewer will question: the generic handler
already maps serper's RESULT shape, and cannot reach the API at all —
``json_generic`` inherits the base's GET transport and ``Authorization: Bearer``
scheme, while serper is POST-only with ``X-API-KEY``. Those two facts are
asserted directly, so a future change that makes the generic handler capable
also makes this file's justification fail loudly.

The rest is the brave.py contract, restated for this provider: a keyless
metered handler REFUSES rather than answering nothing, the endpoint host is
fenced so the key cannot be shipped elsewhere, and a structural surprise is
DEGRADED rather than a clean empty.
"""
from __future__ import annotations

import pytest

from legba.data.schemas.stack import SearchProviderConfig
from legba.data.stack.search import (
    MAX_RESULTS_CAP,
    SEARCH_HANDLERS,
    SearchHandlerContext,
    SearchProviderHandler,
    SearchStatus,
    SerperSearchHandler,
    parse_serper_payload,
    resolve_handler,
)
from legba.data.stack.search.base import SearchProviderUnresolved
from legba.data.stack.search.json_generic import GenericJsonSearchHandler
from legba.data.stack.search.serper import (
    SERPER_API_KEY_SECRET_ID,
    SERPER_API_HOST,
    SERPER_MAX_NUM,
    SERPER_NEWS_ENDPOINT,
    SERPER_WEB_ENDPOINT,
)
from legba.data.registry.health import KEY_REQUIRED_SEARCH_SUBPROVIDERS

#: A real-shaped serper web body (values synthesised, keys verbatim).
SERPER_WEB_BODY = {
    "searchParameters": {"q": "australia defence policy", "type": "search",
                         "num": 10, "engine": "google"},
    "organic": [
        {"title": "Defence Strategic Review", "link": "https://defence.gov.au/dsr",
         "snippet": "The review sets out ...", "date": "12 Sep 2026",
         "position": 1},
        {"title": "AUKUS update", "link": "https://dfat.gov.au/aukus",
         "snippet": "Australia and the United Kingdom ...", "position": 2},
        {"title": "No date here", "link": "https://abc.net.au/story",
         "snippet": "...", "position": 3},
    ],
    "credits": 1,
}

SERPER_NEWS_BODY = {
    "searchParameters": {"q": "x", "type": "news"},
    "news": [{"title": "T", "link": "https://n.example/1", "snippet": "s",
              "date": "2 hours ago"}],
}


def _config(**over):
    body = {
        "subprovider": {
            "factory_kind": "dropdown_static", "raw": "serper",
            "options": ["searxng", "json", "firecrawl", "jina", "tavily",
                        "brave", "serper", "agent"],
        },
        "endpoint": {"factory_kind": "text", "raw": SERPER_WEB_ENDPOINT},
        "api_key": {"factory_kind": "secret", "raw": SERPER_API_KEY_SECRET_ID},
    }
    body.update(over)
    return SearchProviderConfig.model_validate(body)


class _Secrets:
    """The credential resolver slice the base actually calls."""

    def __init__(self, value: str) -> None:
        self._value = value

    async def resolve(self, secret_id: str) -> bytes:
        assert secret_id == SERPER_API_KEY_SECRET_ID
        return self._value.encode("utf-8")


async def _configured(key="test-key-not-a-real-one", **over):
    handler = SerperSearchHandler()
    await handler.on_configure(SearchHandlerContext(
        instance_id="search.serper.paid", config=_config(**over),
        secrets=_Secrets(key),
    ))
    return handler


# ---------------------------------------------------------------------------
# 1) Why this is a module: the two facts config cannot express
# ---------------------------------------------------------------------------


def test_serper_is_post_only_and_the_generic_handler_is_not():
    """If this ever fails, serper CAN be a configured ``json`` component."""
    assert SerperSearchHandler.http_method == "POST"
    assert GenericJsonSearchHandler.http_method == "GET"
    assert SearchProviderHandler.http_method == "GET"


def test_serper_authenticates_with_its_own_header_not_a_bearer():
    handler = SerperSearchHandler()
    handler._api_key = "k"
    headers = handler._auth_headers()
    assert headers["X-API-KEY"] == "k"
    assert "Authorization" not in headers


def test_the_handler_is_registered_and_resolvable_by_name():
    assert SEARCH_HANDLERS["serper"] is SerperSearchHandler
    assert resolve_handler("serper") is SerperSearchHandler
    # And the health checker knows it CANNOT run keyless — TCP to
    # google.serper.dev:443 succeeds from anywhere, so without this the
    # healthcheck is green for a provider that refuses every query.
    assert "serper" in KEY_REQUIRED_SEARCH_SUBPROVIDERS


# ---------------------------------------------------------------------------
# 2) The field map, on a fixture body
# ---------------------------------------------------------------------------


def test_the_web_body_maps_onto_the_normalized_result():
    resp = parse_serper_payload(SERPER_WEB_BODY, query="q")
    assert resp.count == 3
    first = resp.results[0]
    assert first.url == "https://defence.gov.au/dsr"
    assert first.title == "Defence Strategic Review"
    assert first.snippet.startswith("The review sets out")
    assert first.published_at == "12 Sep 2026"
    assert first.engine == "serper"
    assert [r.rank for r in resp.results] == [1, 2, 3]
    # A hit with no date is carried, not dropped — and its date is None, not "".
    assert resp.results[2].published_at is None
    assert resp.results[1].published_at is None


def test_serper_never_fabricates_extracted_text_a_score_or_a_licence():
    for result in parse_serper_payload(SERPER_WEB_BODY, query="q").results:
        # None routes retrieval through web_fetch -> archive -> Trafilatura.
        assert result.extracted_text is None
        assert result.extract_source is None
        # serper publishes no relevance score; a rank-derived float would look
        # comparable to SearXNG's real ones and is not.
        assert result.score is None
        # A search hit carries NO license verdict.
        assert result.license_class is None
        assert result.raw


def test_the_news_corpus_reads_its_own_key():
    resp = parse_serper_payload(SERPER_NEWS_BODY, query="q", mode="news")
    assert resp.count == 1
    assert resp.results[0].url == "https://n.example/1"


def test_the_limit_is_honoured():
    assert parse_serper_payload(SERPER_WEB_BODY, query="q", limit=2).count == 2


# ---------------------------------------------------------------------------
# 3) A structural surprise is DEGRADED, never a clean empty
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload,tell", [
    ("not a dict", "not a JSON object"),
    ({"searchParameters": {}}, "carried no list at 'organic'"),
    ({"message": "Unauthorized."}, "serper returned an error body"),
])
def test_every_non_result_shape_is_degraded_with_a_reason(payload, tell):
    resp = parse_serper_payload(payload, query="q")
    assert resp.degraded is True
    assert tell in resp.degraded_detail
    # DEGRADED_EMPTY, whose supports_absence_claim is False — a malformed
    # reply can never be rendered as evidence of absence.
    assert resp.status is SearchStatus.DEGRADED_EMPTY
    assert resp.supports_absence_claim is False


def test_a_well_formed_empty_is_a_clean_empty_not_a_degradation():
    """The whole point of buying this rung. serper has no partial-service
    channel, so an empty body IS an empty — and only the liveness probe
    licenses an absence claim over it."""
    resp = parse_serper_payload({"organic": []}, query="q")
    assert resp.degraded is False
    assert resp.status is SearchStatus.EMPTY
    assert resp.supports_absence_claim is False  # until liveness says LIVE


# ---------------------------------------------------------------------------
# 4) The wire request, and the credential fence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_request_is_a_post_with_the_query_in_the_body():
    handler = await _configured()
    seen = {}

    async def _fake_get_json(endpoint, *, params, timeout, body=None):
        seen.update(endpoint=endpoint, params=dict(params), body=dict(body or {}))
        return SERPER_WEB_BODY

    handler._get_json = _fake_get_json
    resp = await handler.search("australia defence policy", limit=10)

    assert seen["endpoint"] == SERPER_WEB_ENDPOINT
    assert seen["params"] == {}, "nothing rides the query string"
    assert seen["body"]["q"] == "australia defence policy"
    assert seen["body"]["num"] == 10
    assert resp.count == 3
    assert resp.provider == "search.serper.paid"
    assert resp.subprovider == "serper"


@pytest.mark.asyncio
async def test_num_is_clamped_to_serpers_own_ceiling():
    handler = await _configured(
        max_results={"factory_kind": "number", "raw": 50,
                     "minimum": 1, "maximum": 50},
    )
    seen = {}

    async def _fake_get_json(endpoint, *, params, timeout, body=None):
        seen.update(body=dict(body or {}))
        return {"organic": []}

    handler._get_json = _fake_get_json
    await handler.search("q", limit=MAX_RESULTS_CAP)
    assert seen["body"]["num"] <= SERPER_MAX_NUM
    assert seen["body"]["num"] == MAX_RESULTS_CAP


@pytest.mark.asyncio
async def test_a_metered_rung_never_pages_itself():
    """A second page is a second charge for the same claim."""
    handler = await _configured()
    calls = 0

    async def _fake_get_json(endpoint, *, params, timeout, body=None):
        nonlocal calls
        calls += 1
        return SERPER_WEB_BODY

    handler._get_json = _fake_get_json
    await handler.search("q", limit=MAX_RESULTS_CAP)
    assert calls == 1
    assert SerperSearchHandler.max_extra_pages == 0


@pytest.mark.asyncio
async def test_the_news_endpoint_is_derived_by_path_swap():
    handler = await _configured(
        categories={"factory_kind": "list", "item_kind": "text",
                    "raw": ["news"]},
    )
    assert handler._endpoint_for(SERPER_WEB_ENDPOINT) == SERPER_NEWS_ENDPOINT


@pytest.mark.asyncio
async def test_a_keyless_component_refuses_and_says_no_query_was_issued():
    handler = SerperSearchHandler()
    body = _config()
    body.api_key = None
    with pytest.raises(SearchProviderUnresolved) as exc:
        await handler.on_configure(
            SearchHandlerContext(instance_id="search.serper.paid", config=body)
        )
    assert "NO query was issued" in str(exc.value)
    assert SERPER_API_KEY_SECRET_ID in str(exc.value)


@pytest.mark.asyncio
async def test_an_empty_resolved_key_refuses_too():
    with pytest.raises(SearchProviderUnresolved) as exc:
        await _configured(key="   ")
    assert "NO query was issued" in str(exc.value)


@pytest.mark.asyncio
async def test_search_refuses_when_configure_was_bypassed():
    handler = SerperSearchHandler()
    with pytest.raises(SearchProviderUnresolved) as exc:
        await handler.search("q")
    assert "not an empty result set" in str(exc.value)


@pytest.mark.asyncio
async def test_the_key_is_fenced_to_one_host():
    """A tampered endpoint must not ship the paid key to another host."""
    assert SerperSearchHandler.allowed_endpoint_hosts == frozenset({SERPER_API_HOST})
    handler = await _configured(
        endpoint={"factory_kind": "text", "raw": "https://evil.example/search"},
    )
    with pytest.raises(Exception) as exc:
        await handler.search("q")
    assert "NO query was issued" in str(exc.value)
