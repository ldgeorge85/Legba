# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R-3b Task 1 — the modular ``search_provider`` stack family.

Pure tests (no DB, no network): provider-protocol conformance for BOTH shipped
handlers, the normalized-result mapping for each, the family's standing in the
registry (kind catalog + health checker + NOT a first-run requirement), the
bind-time family/subprovider validation that ``expected_family`` does not
perform, the ``StackRef`` route ladder incl. its rung-0 opt-in gate, and the
package-wide "no bare httpx client" egress rule.

The degraded-vs-empty contract has its own file
(``test_search_degraded_vs_empty.py``) because it is a separate honesty claim.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import legba.data.stack.search
from legba.data.registry.api import REQUIRED_MODEL_COMPONENT_KINDS
from legba.data.registry.health import HEALTH_CHECKERS, HealthState
from legba.data.registry.stack import (
    HEALTH_CHECKER_KIND,
    KIND_MODELS,
    MODEL_KINDS,
    kind_from_schema_uri,
)
from legba.data.schemas.stack import SearchProvider, SearchProviderConfig
from legba.data.stack.search import (
    SEARCH_HANDLERS,
    SEARCH_STACK_REF_ENV,
    GenericJsonSearchHandler,
    HardSearchFailure,
    SearchHandlerContext,
    SearchProviderHandler,
    SearchResponse,
    SearchRoute,
    SearxngSearchHandler,
    build_handler,
    parse_generic_payload,
    parse_searxng_payload,
    resolve_handler,
    resolve_search_route,
    resolve_tool_search_route,
)

#: Resolved from the imported package, never a hardcoded checkout path — a
#: worktree runs against its own tree.
SEARCH_PKG = Path(legba.data.stack.search.__file__).parent


def _component(**config_overrides) -> dict:
    config = {
        "subprovider": {
            "factory_kind": "dropdown_static", "raw": "searxng",
            "options": ["searxng", "json", "firecrawl", "jina", "tavily",
                        "brave", "agent"],
        },
        "endpoint": {"factory_kind": "text", "raw": "http://searxng:8080/search"},
    }
    config.update(config_overrides)
    return {
        "id": "search.searxng.local",
        "schema_uri": "legba/stack/search_provider/1.0.0",
        "config": config,
    }


async def _configured(handler_cls, *, endpoint="https://example.invalid/search",
                      **cfg):
    handler = handler_cls()
    config = SearchProviderConfig.model_validate({
        "subprovider": {
            "factory_kind": "dropdown_static", "raw": handler_cls.subprovider,
            "options": ["searxng", "json", "firecrawl", "jina", "tavily",
                        "brave", "agent"],
        },
        "endpoint": {"factory_kind": "text", "raw": endpoint},
        **cfg,
    })
    await handler.on_configure(
        SearchHandlerContext(instance_id="search.test.local", config=config)
    )
    return handler


# ---------------------------------------------------------------------------
# 1) Provider-protocol conformance — both handlers
# ---------------------------------------------------------------------------

ALL_HANDLERS = (SearxngSearchHandler, GenericJsonSearchHandler)


@pytest.mark.parametrize("handler_cls", ALL_HANDLERS)
def test_handler_declares_the_stack_component_classvars(handler_cls):
    assert handler_cls.kind == "search_provider"
    assert handler_cls.family == "stack"
    assert handler_cls.schema_version == "legba/stack.search_provider/1-0-0"
    assert handler_cls.config_schema is SearchProviderConfig
    assert isinstance(handler_cls.handler_version, str) and handler_cls.handler_version
    assert issubclass(handler_cls, SearchProviderHandler)


@pytest.mark.parametrize("handler_cls", ALL_HANDLERS)
def test_handler_advertises_capabilities_including_search(handler_cls):
    caps = handler_cls.capabilities
    assert isinstance(caps, frozenset)
    # "search" is REQUIRED of every handler; the rest are the extension seam.
    assert "search" in caps
    assert caps <= {"search", "fetch", "extract"}


@pytest.mark.parametrize("handler_cls", ALL_HANDLERS)
@pytest.mark.parametrize(
    "hook",
    ["on_configure", "on_activate", "on_pause", "on_resume", "on_retire",
     "health_check", "search", "fetch"],
)
def test_handler_implements_the_async_surface(handler_cls, hook):
    fn = getattr(handler_cls, hook)
    assert inspect.iscoroutinefunction(fn), f"{handler_cls.__name__}.{hook}"


@pytest.mark.parametrize("handler_cls", ALL_HANDLERS)
def test_handler_exposes_a_telemetry_handle(handler_cls):
    assert handler_cls().telemetry() is not None


@pytest.mark.parametrize("handler_cls", ALL_HANDLERS)
def test_subprovider_is_explicit_and_registered(handler_cls):
    sub = handler_cls.subprovider
    assert sub and sub != "base"
    assert SEARCH_HANDLERS[sub] is handler_cls
    assert resolve_handler(sub) is handler_cls


def test_resolve_handler_names_the_known_set_on_a_typo():
    with pytest.raises(KeyError) as exc:
        resolve_handler("searxgn")
    message = str(exc.value)
    assert "searxgn" in message
    assert "searxng" in message and "json" in message


@pytest.mark.parametrize("handler_cls", ALL_HANDLERS)
async def test_unconfigured_handler_fails_loud_not_empty(handler_cls):
    """The honest-degradation rule: never an empty list to signal failure."""
    handler = handler_cls()
    with pytest.raises(HardSearchFailure) as exc:
        await handler.search("anything")
    assert "not configured" in str(exc.value)


@pytest.mark.parametrize("handler_cls", ALL_HANDLERS)
async def test_unconfigured_health_check_is_unhealthy(handler_cls):
    health = await handler_cls().health_check()
    assert health.state is HealthState.UNHEALTHY
    assert health.kind == "search_provider"


@pytest.mark.parametrize("handler_cls", ALL_HANDLERS)
async def test_non_http_endpoint_is_a_hard_failure(handler_cls):
    handler = await _configured(handler_cls, endpoint="file:///etc/passwd")
    with pytest.raises(HardSearchFailure) as exc:
        await handler.search("q")
    assert "http(s)" in str(exc.value)


@pytest.mark.parametrize("handler_cls", ALL_HANDLERS)
async def test_empty_query_is_a_hard_failure(handler_cls):
    handler = await _configured(handler_cls)
    with pytest.raises(HardSearchFailure):
        await handler.search("   ")


async def test_fetch_capability_is_gated_not_faked():
    """SearXNG does not advertise 'fetch' — asking for it must refuse, not
    silently return an empty document."""
    handler = await _configured(SearxngSearchHandler)
    assert "fetch" not in handler.capabilities
    with pytest.raises(HardSearchFailure) as exc:
        await handler.fetch("https://example.invalid/a")
    assert "fetch" in str(exc.value)


async def test_healthy_probe_records_the_engine_blindness_caveat():
    """A HEALTHY search provider does NOT mean search works — the probe says so
    in its own payload rather than leaving the operator to infer it."""
    handler = await _configured(SearxngSearchHandler)
    health = await handler.health_check()
    assert health.extra["probe"] == "tcp_only"
    assert "banned" in health.extra["caveat"]


# ---------------------------------------------------------------------------
# 2) The family's standing in the registry
# ---------------------------------------------------------------------------


def test_search_provider_is_a_registered_stack_kind():
    assert KIND_MODELS["search_provider"] is SearchProvider
    assert MODEL_KINDS[SearchProvider] == "search_provider"
    assert HEALTH_CHECKER_KIND["search_provider"] == "search_provider"
    assert kind_from_schema_uri("legba/stack/search_provider/1.0.0") == "search_provider"


def test_search_provider_has_a_health_checker():
    checker = HEALTH_CHECKERS["search_provider"]
    assert checker.kind == "search_provider"


def test_search_is_optional_capability_not_first_run_readiness():
    """Deliberate: a deployment with no search component is COMPLETE."""
    assert "search_provider" not in REQUIRED_MODEL_COMPONENT_KINDS


def test_component_id_pattern_accepts_the_three_part_convention():
    for component_id in ("search.searxng.local", "search.jina.reader",
                         "search.agent.deep"):
        model = SearchProvider.model_validate({
            "id": component_id,
            "name": "x",
            "schema_uri": "legba/stack/search_provider/1.0.0",
            "version": "a" * 16,
            "owner": "o",
            "config": _component()["config"],
        })
        assert model.id == component_id


# ---------------------------------------------------------------------------
# 3) build_handler — the bind-time validation expected_family does NOT do
# ---------------------------------------------------------------------------


async def test_build_handler_returns_the_declared_subprovider():
    handler = await build_handler(_component())
    assert isinstance(handler, SearxngSearchHandler)
    assert handler.component_id == "search.searxng.local"


async def test_build_handler_refuses_a_component_of_another_family():
    """`expected_family` on a StackRef is discarded at bind time — so the check
    has to live here or it does not exist."""
    bad = _component()
    bad["schema_uri"] = "legba/stack/llm_provider/1.0.0"
    with pytest.raises(HardSearchFailure) as exc:
        await build_handler(bad)
    assert "llm_provider" in str(exc.value)
    assert "search_provider" in str(exc.value)


async def test_build_handler_refuses_an_unshipped_subprovider_by_name():
    comp = _component()
    comp["config"]["subprovider"] = {
        "factory_kind": "dropdown_static", "raw": "firecrawl",
        "options": ["searxng", "json", "firecrawl", "jina", "tavily", "brave",
                    "agent"],
    }
    with pytest.raises(HardSearchFailure) as exc:
        await build_handler(comp)
    message = str(exc.value)
    assert "firecrawl" in message and "SEARCH_HANDLERS" in message


# ---------------------------------------------------------------------------
# 4) Normalized-result mapping — SearXNG
# ---------------------------------------------------------------------------

_SEARXNG_BODY = {
    "query": "strait closure",
    "number_of_results": 2,
    "results": [
        {
            "url": "https://example.org/a",
            "title": "T" * 900,
            "content": "C" * 2000,
            "publishedDate": "2026-07-20T00:00:00Z",
            "engine": "mojeek",
            "engines": ["mojeek"],
            "score": 3.5,
            "category": "general",
        },
        {
            "url": "https://example.org/b",
            "title": "Second",
            "snippet": "alternate snippet spelling",
            "engine": "wikipedia",
            "score": "not-a-number",
        },
        {"title": "no url — dropped", "content": "x"},
        "not-a-dict",
    ],
    "unresponsive_engines": [],
}


def test_searxng_maps_every_normalized_field():
    resp = parse_searxng_payload(_SEARXNG_BODY, query="strait closure")
    assert isinstance(resp, SearchResponse)
    assert resp.count == 2  # url-less + non-dict members dropped, not faked
    first, second = resp.results
    assert first.url == "https://example.org/a"
    assert first.engine == "mojeek"
    assert first.score == 3.5
    assert first.rank == 1
    assert first.published_at == "2026-07-20T00:00:00Z"
    assert second.rank == 2
    # `content` is SearXNG's snippet field; `snippet` accepted as a courtesy.
    assert second.snippet == "alternate snippet spelling"
    # An unparseable score is dropped, never coerced to a misleading 0.0.
    assert second.score is None


def test_searxng_preserves_the_legacy_field_caps():
    """512/1024 are exactly the caps web_tools._parse_search_results applied —
    so re-pointing the legacy tool at this layer is lossless."""
    resp = parse_searxng_payload(_SEARXNG_BODY, query="q")
    assert len(resp.results[0].title) == 512
    assert len(resp.results[0].snippet) == 1024


def test_searxng_never_fabricates_extracted_text_or_a_license():
    resp = parse_searxng_payload(_SEARXNG_BODY, query="q")
    for result in resp.results:
        # None routes retrieval through web_fetch → archive → Trafilatura.
        assert result.extracted_text is None
        assert result.extract_source is None
        # A search hit carries NO license verdict — the retention gate reads it.
        assert result.license_class is None
        assert result.raw  # the provider's item survives verbatim


def test_searxng_honours_the_result_limit():
    body = {"results": [{"url": f"https://e/{i}"} for i in range(25)]}
    assert parse_searxng_payload(body, query="q", limit=3).count == 3
    # The package-wide cap still applies above the caller's ask.
    assert parse_searxng_payload(body, query="q").count == 10


# ---------------------------------------------------------------------------
# 5) Normalized-result mapping — generic JSON
# ---------------------------------------------------------------------------


def test_generic_json_maps_alternate_key_spellings():
    body = {
        "results": [
            {"link": "https://example.org/a", "name": "A", "description": "d"},
            {"href": "https://example.org/b", "heading": "B", "abstract": "e"},
        ]
    }
    resp = parse_generic_payload(body, query="q")
    assert [r.url for r in resp.results] == [
        "https://example.org/a", "https://example.org/b",
    ]
    assert [r.title for r in resp.results] == ["A", "B"]
    assert [r.snippet for r in resp.results] == ["d", "e"]
    assert [r.rank for r in resp.results] == [1, 2]


def test_generic_json_reads_a_dotted_results_path():
    """A Firecrawl-shaped body needs config, not code."""
    body = {"success": True, "data": {"web": [{"url": "https://e/1"}]}}
    resp = parse_generic_payload(body, query="q", results_key="data.web")
    assert resp.count == 1


def test_generic_json_propagates_provider_supplied_text_and_stamps_its_source():
    body = {"results": [{"url": "https://e/1", "markdown": "# clean text"}]}
    result = parse_generic_payload(body, query="q").results[0]
    assert result.extracted_text == "# clean text"
    assert result.extract_source == "provider"


def test_generic_json_leaves_extracted_text_none_when_absent():
    body = {"results": [{"url": "https://e/1", "description": "just a snippet"}]}
    result = parse_generic_payload(body, query="q").results[0]
    # NEVER synthesized from the snippet.
    assert result.extracted_text is None
    assert result.extract_source is None


# ---------------------------------------------------------------------------
# 6) The StackRef route ladder
# ---------------------------------------------------------------------------


def test_route_rung0_opt_in_gate(monkeypatch):
    monkeypatch.delenv(SEARCH_STACK_REF_ENV, raising=False)
    # No block at all, and a block with neither key → no route, EVER.
    assert resolve_search_route(None) is None
    assert resolve_search_route({}) is None
    assert resolve_search_route({"something_else": "x"}) is None


def test_route_env_repoints_but_never_enables(monkeypatch):
    monkeypatch.setenv(SEARCH_STACK_REF_ENV, "search.other.local")
    # Opted-out descriptor stays opted out even with the env set.
    assert resolve_search_route({}) is None
    # Opted-in descriptor is repointed.
    route = resolve_search_route(
        {"primary": {"factory_kind": "stack_ref", "raw": "search.searxng.local"}}
    )
    assert route == SearchRoute("search.other.local", f"env:{SEARCH_STACK_REF_ENV}")
    assert route.route_class == "configured"


def test_route_primary_then_fallback(monkeypatch):
    monkeypatch.delenv(SEARCH_STACK_REF_ENV, raising=False)
    block = {
        "primary": {"factory_kind": "stack_ref", "raw": "search.searxng.local"},
        "fallback": {"factory_kind": "stack_ref", "raw": "search.jina.reader"},
    }
    route = resolve_search_route(block)
    assert route.component_id == "search.searxng.local"
    assert route.source == "method.search.primary"
    assert route.route_class == "configured"

    fallback_only = {"fallback": block["fallback"]}
    route = resolve_search_route(fallback_only)
    assert route.component_id == "search.jina.reader"
    assert route.route_class == "fallback"


def test_route_accepts_every_stack_ref_shape(monkeypatch):
    monkeypatch.delenv(SEARCH_STACK_REF_ENV, raising=False)
    for shape in (
        {"factory_kind": "stack_ref", "raw": "search.searxng.local"},
        "search.searxng.local",
    ):
        route = resolve_search_route({"primary": shape})
        assert route.component_id == "search.searxng.local"


def test_tool_config_shorthand_and_nested_block(monkeypatch):
    monkeypatch.delenv(SEARCH_STACK_REF_ENV, raising=False)
    shorthand = {
        "provider": {"factory_kind": "stack_ref", "raw": "search.searxng.local"}
    }
    route = resolve_tool_search_route(shorthand)
    assert route == SearchRoute("search.searxng.local", "config.provider")

    nested = {"provider": {"primary": {"factory_kind": "stack_ref",
                                       "raw": "search.searxng.local"}}}
    route = resolve_tool_search_route(nested)
    assert route.source == "config.provider.primary"

    # No provider key at all → the legacy endpoint path, not a fabricated route.
    assert resolve_tool_search_route({"timeout_seconds": 15}) is None


# ---------------------------------------------------------------------------
# 7) Egress rule — no bare httpx client anywhere in the package
# ---------------------------------------------------------------------------


def test_package_never_constructs_a_bare_httpx_client():
    """Rule 1: search egresses ONLY through the SSRF-guarded transport.

    Result URLs come from the open web, so the guard that refuses private /
    loopback / link-local / metadata targets on every redirect hop has to bound
    every request this package makes.
    """
    if not SEARCH_PKG.is_dir():  # pragma: no cover — worktree layouts
        pytest.skip(f"{SEARCH_PKG} not present")
    offenders = []
    for path in sorted(SEARCH_PKG.glob("*.py")):
        text = path.read_text()
        if "httpx.AsyncClient(" in text or "httpx.Client(" in text:
            offenders.append(path.name)
    assert not offenders, f"bare httpx client in {offenders}"


def test_package_uses_the_guarded_client():
    base = (SEARCH_PKG / "base.py").read_text() if SEARCH_PKG.is_dir() else ""
    if not base:  # pragma: no cover
        pytest.skip("search package not present")
    assert "guarded_async_client" in base
