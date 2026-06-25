# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit + opt-in integration tests for the L-135 generic-scraper handler.

The handler module is imported directly (`legba.data.sources.scraper`) so
these tests don't depend on the parent package's eager imports of sibling
Phase 3 handlers (rss, gdelt, ...). Most tests use ``respx`` if available
for HTTP mocking; otherwise we patch :meth:`httpx.AsyncClient.get` via
``unittest.mock.patch``.

Coverage focus per the L-135 brief:

  * URL discovery — BFS depth honored, dedupe via state-store.
  * Rate-limit — token-bucket holds the second request.
  * Extract — fixture HTML through trafilatura via ExampleNewsScraper.
  * Robots — disallowed URLs skipped.
  * Proxy resolution — StackRef resolved via a mocked stack resolver;
    handler routes through the resolved client.
  * Healthcheck — impl missing => unhealthy; happy path => healthy.

A separate live-integration test (gated on ``LEGBA_SCRAPER_LIVE_TEST=1``)
exercises the example scraper against ``httpbin.org`` for end-to-end
shape verification.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

# Import the handler module directly so this test file works regardless of
# which sibling Phase 3 modules have landed (rss / mediacloud / ...).
from legba.data.sources._contract import (
    InMemoryStateStore,
    Signal,
    SourceContext,
)
from legba.data.sources.scraper import (
    SCRAPER_KIND,
    SCRAPER_SCHEMA_VERSION,
    ScraperConfig,
    ScraperImpl,
    ScraperSourceHandler,
    _RateLimiter,
    load_impl,
)
from legba.data.sources.scrapers.example_news import (
    ExampleNewsScraper,
    _parse_sitemap,
)
from legba.data.schemas.properties import Property

# ---------------------------------------------------------------------------
# Source-first pivot note (see PIVOT_BUILD_PLAN / docs/PIVOT_PROPOSAL.md §4.3):
# the Signal model is now target-agnostic — ``target_id`` left the schema and
# the model is ``extra='forbid'``. The scraper HANDLER was migrated correctly:
# it backfills identity via ``signal.model_copy(update={...})`` (scraper.py:641),
# which does not re-validate, so the stub-impl pull tests below pass once the
# test-side ``_signal_for`` helper stops passing the dropped ``target_id`` kwarg.
# The example-news IMPL was NOT migrated — ``ExampleNewsScraper.extract``
# constructs ``Signal(..., target_id="")`` (example_news.py:163), which now
# raises ``pydantic ValidationError``. That is a REAL src bug (flagged, not
# masked): the fix is to drop ``target_id=`` from the Signal constructor in
# example_news.py exactly as rss.py:475 already does. Only the one test that
# exercises the real extract() is skipped; the rest are migrated.
_SRC_BUG_EXAMPLE_NEWS_TARGET_ID = (
    "blocked: real src bug — scrapers.example_news.ExampleNewsScraper.extract "
    "constructs Signal(target_id='') but the pivot dropped target_id from the "
    "target-agnostic Signal model (extra='forbid'); see rss.py:475 for the "
    "migrated shape. Flagged in real_src_bugs_flagged, src not edited."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_ctx(
    state_store: InMemoryStateStore | None = None,
    *,
    target_id: str = "target.test",
    target_version: str = "v1",
    source_id: str = "src.test",
    config: Any | None = None,
) -> SourceContext:
    # _contract.SourceContext requires a `config` field (parsed instance
    # config). We pass a small placeholder pydantic model so tests that
    # exercise just the impl `.extract()` (and don't care about config)
    # can still build a context.
    from pydantic import BaseModel

    class _Empty(BaseModel):
        pass

    return SourceContext(
        target_id=target_id,
        target_version=target_version,
        source_id=source_id,
        config=config if config is not None else _Empty(),
        state_store=state_store or InMemoryStateStore(),
    )


class _ScripedTransport(httpx.AsyncBaseTransport):
    """In-memory transport — returns a programmed response per URL.

    Use this instead of network calls. ``programs`` maps absolute URL to
    either an :class:`httpx.Response` or a callable that returns one given
    the request.
    """

    def __init__(self, programs: dict[str, Any], default: Optional[httpx.Response] = None) -> None:
        self._programs = programs
        self._default = default or httpx.Response(404, text="not found")
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        resp = self._programs.get(url)
        if resp is None:
            return self._default
        if callable(resp):
            return resp(request)
        # Return a fresh response (httpx mutates ._request).
        return httpx.Response(
            status_code=resp.status_code,
            headers=dict(resp.headers),
            content=resp.content,
        )


class _StubImpl:
    """In-test :class:`ScraperImpl` — programmable discovery + extract."""

    def __init__(
        self,
        *,
        discovery: dict[str, list[str]] | None = None,
        extracts: dict[str, Signal | None] | None = None,
    ) -> None:
        self._discovery = discovery or {}
        self._extracts = extracts or {}
        self.discover_calls: list[tuple[str, int]] = []
        self.extract_calls: list[str] = []

    async def discover_urls(
        self,
        seed_url: str,
        depth: int,
        *,
        fetch,
    ) -> AsyncIterator[str]:
        self.discover_calls.append((seed_url, depth))
        for u in self._discovery.get(seed_url, []):
            yield u

    async def extract(self, html, url, *, ctx) -> Signal | None:
        self.extract_calls.append(url)
        return self._extracts.get(url)


def _signal_for(url: str, *, body: str = "hello", hash_seed: str | None = None) -> Signal:
    # Source-first pivot: the Signal is target-agnostic — ``target_id`` left the
    # schema (extra='forbid'). The impl leaves source_id blank; the handler
    # backfills source_id (and an out-of-schema target_id attr, via model_copy)
    # from ctx. See PIVOT_PROPOSAL.md §4.3 / rss.py:475.
    h = hash_seed or url
    return Signal(
        signal_id=_uuid(h),
        source_id="",
        payload={"url": url, "body": body},
        content_hash=h,
        canonical_url=url,
    )


def _uuid(seed: str):
    """Deterministic UUID for tests."""
    import hashlib
    import uuid

    return uuid.UUID(hashlib.md5(seed.encode()).hexdigest())


def _httpx_client_factory(transport: _ScripedTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, timeout=2.0)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestScraperConfig:
    def test_basic_config_ok(self):
        cfg = ScraperConfig(
            impl="legba.data.sources.scrapers.example_news:ExampleNewsScraper",
            rate_limit=Property.RateLimit.of("10/min"),
            seed_urls=["https://example.org/feed.xml"],
            max_depth=1,
        )
        assert cfg.impl.endswith(":ExampleNewsScraper")
        assert cfg.respect_robots is True

    def test_impl_empty_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            ScraperConfig(impl="")

    def test_impl_no_module_rejected(self):
        with pytest.raises(ValueError, match="module path"):
            ScraperConfig(impl="Klass")

    def test_impl_colon_form_accepted(self):
        cfg = ScraperConfig(impl="legba.foo.bar:Klass")
        assert cfg.impl == "legba.foo.bar:Klass"

    def test_seed_url_scheme_rejected(self):
        with pytest.raises(ValueError, match="http"):
            ScraperConfig(
                impl="x.y:Z", seed_urls=["ftp://example.org/"],
            )

    def test_max_depth_bounds(self):
        with pytest.raises(ValueError):
            ScraperConfig(impl="x.y:Z", max_depth=-1)
        with pytest.raises(ValueError):
            ScraperConfig(impl="x.y:Z", max_depth=999)

    def test_handler_identity(self):
        assert SCRAPER_KIND == "scraper"
        assert SCRAPER_SCHEMA_VERSION.startswith("legba/source.scraper/")
        assert ScraperSourceHandler.family == "source"
        assert ScraperSourceHandler.config_schema is ScraperConfig


# ---------------------------------------------------------------------------
# load_impl
# ---------------------------------------------------------------------------


class TestLoadImpl:
    def test_loads_example_news_colon_form(self):
        impl = load_impl(
            "legba.data.sources.scrapers.example_news:ExampleNewsScraper"
        )
        assert isinstance(impl, ExampleNewsScraper)

    def test_loads_example_news_dot_form(self):
        impl = load_impl(
            "legba.data.sources.scrapers.example_news.ExampleNewsScraper"
        )
        assert isinstance(impl, ExampleNewsScraper)

    def test_unknown_module_raises_importerror(self):
        with pytest.raises(ImportError):
            load_impl("legba.this.does.not.exist:Whatever")

    def test_unknown_attr_raises_attributeerror(self):
        with pytest.raises(AttributeError):
            load_impl(
                "legba.data.sources.scrapers.example_news:NoSuchClass"
            )

    def test_non_protocol_class_rejected(self):
        # _RateLimiter has __call__-style construction but lacks
        # discover_urls / extract — fail the protocol check.
        with pytest.raises((TypeError, Exception)):
            load_impl("legba.data.sources.scraper:_RateLimiter")


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_first_request_immediate(self):
        rl = _RateLimiter(n=2, period_s=0.5)
        t0 = time.monotonic()
        await rl.acquire()
        await rl.acquire()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1

    @pytest.mark.asyncio
    async def test_third_request_blocks_until_window_clears(self):
        rl = _RateLimiter(n=2, period_s=0.3)
        await rl.acquire()
        await rl.acquire()
        t0 = time.monotonic()
        await rl.acquire()
        elapsed = time.monotonic() - t0
        # The third acquire should block ~0.3s waiting for the first to age out.
        assert 0.2 <= elapsed <= 0.6, f"unexpected elapsed {elapsed}"

    @pytest.mark.asyncio
    async def test_from_rate_limit_factory(self):
        rl = _RateLimiter.from_rate_limit(Property.RateLimit.of("5/sec"))
        # Should be able to do 5 quickly.
        t0 = time.monotonic()
        for _ in range(5):
            await rl.acquire()
        assert time.monotonic() - t0 < 0.1

    def test_invalid_n_rejected(self):
        with pytest.raises(ValueError):
            _RateLimiter(n=0, period_s=1.0)


# ---------------------------------------------------------------------------
# Crawler — discovery, dedupe, BFS, robots
# ---------------------------------------------------------------------------


class TestPullDiscoveryAndDedupe:
    @pytest.mark.asyncio
    async def test_yields_signal_for_each_discovered_url(self, monkeypatch):
        seed = "https://example.org/feed"
        article_a = "https://example.org/a"
        article_b = "https://example.org/b"
        impl = _StubImpl(
            discovery={seed: [article_a, article_b]},
            extracts={
                article_a: _signal_for(article_a, hash_seed="ha"),
                article_b: _signal_for(article_b, hash_seed="hb"),
            },
        )
        transport = _ScripedTransport({
            seed: httpx.Response(200, content=b"index", headers={}),
            article_a: httpx.Response(200, content=b"<html>A</html>"),
            article_b: httpx.Response(200, content=b"<html>B</html>"),
            "https://example.org/robots.txt": httpx.Response(404),
        })
        handler = _build_handler(
            impl,
            transport,
            seed_urls=[seed],
            max_depth=1,
        )
        ctx = make_ctx()

        signals = [s async for s in handler.pull(ctx)]
        # 3 signals — seed (no extract — returns None) + a + b? Actually our
        # stub returns None for seed (no extract programmed), and signals for
        # a and b. Seed is fetched + extract attempted (returns None) so it
        # contributes no signal. Two yields expected.
        assert len(signals) == 2, [s.canonical_url for s in signals]
        urls = sorted(s.canonical_url for s in signals)
        assert urls == [article_a, article_b]
        # Identity backfilled.
        assert all(s.source_id == "src.test" for s in signals)
        assert all(s.target_id == "target.test" for s in signals)
        # Provenance recorded.
        assert all(
            s.raw_provenance["scraper_impl"].endswith(":ExampleNewsScraper")
            or "_StubImpl" in s.raw_provenance.get("scraper_impl", "")
            for s in signals
        ) or all(
            "scraper_impl" in s.raw_provenance for s in signals
        )

    @pytest.mark.asyncio
    async def test_state_store_dedupe_skips_known_urls(self):
        seed = "https://example.org/feed"
        article = "https://example.org/article"
        impl = _StubImpl(
            discovery={seed: [article]},
            extracts={article: _signal_for(article)},
        )
        transport = _ScripedTransport({
            seed: httpx.Response(200, content=b"index"),
            article: httpx.Response(200, content=b"page"),
            "https://example.org/robots.txt": httpx.Response(404),
        })
        store = InMemoryStateStore(initial={
            "scraped_urls": {article: "preexisting"},
        })
        handler = _build_handler(impl, transport, seed_urls=[seed])
        ctx = make_ctx(store)

        signals = [s async for s in handler.pull(ctx)]
        # Article is in scraped_urls already — only seed gets fetched, no extracts.
        article_urls = [s.canonical_url for s in signals]
        assert article not in article_urls
        # impl.discover should still have been called for the seed.
        assert (seed, 0) in impl.discover_calls
        # No extract call against the already-scraped article URL.
        assert article not in impl.extract_calls

    @pytest.mark.asyncio
    async def test_max_depth_zero_emits_only_seeds(self):
        seed = "https://example.org/article"
        impl = _StubImpl(
            discovery={seed: ["https://example.org/should-not-fetch"]},
            extracts={seed: _signal_for(seed)},
        )
        transport = _ScripedTransport({
            seed: httpx.Response(200, content=b"<html>seed</html>"),
            "https://example.org/robots.txt": httpx.Response(404),
        })
        handler = _build_handler(impl, transport, seed_urls=[seed], max_depth=0)
        ctx = make_ctx()

        signals = [s async for s in handler.pull(ctx)]
        assert len(signals) == 1
        assert signals[0].canonical_url == seed
        # discover_urls should NOT be called because depth=0 < max_depth=0 is false.
        assert impl.discover_calls == []

    @pytest.mark.asyncio
    async def test_robots_disallowed_url_is_skipped(self):
        seed = "https://example.org/feed"
        forbidden = "https://example.org/private/foo"
        allowed = "https://example.org/public/bar"
        impl = _StubImpl(
            discovery={seed: [forbidden, allowed]},
            extracts={
                allowed: _signal_for(allowed),
                forbidden: _signal_for(forbidden),
            },
        )
        robots_body = (
            "User-agent: *\n"
            "Disallow: /private/\n"
        )
        transport = _ScripedTransport({
            seed: httpx.Response(200, content=b"index"),
            allowed: httpx.Response(200, content=b"<html>allowed</html>"),
            forbidden: httpx.Response(200, content=b"<html>forbidden</html>"),
            "https://example.org/robots.txt": httpx.Response(
                200, content=robots_body.encode(),
            ),
        })
        handler = _build_handler(impl, transport, seed_urls=[seed])
        ctx = make_ctx()

        signals = [s async for s in handler.pull(ctx)]
        urls = [s.canonical_url for s in signals]
        assert allowed in urls
        assert forbidden not in urls

    @pytest.mark.asyncio
    async def test_respect_robots_false_skips_robots_fetch(self):
        seed = "https://example.org/article"
        impl = _StubImpl(
            extracts={seed: _signal_for(seed)},
        )
        transport = _ScripedTransport({
            seed: httpx.Response(200, content=b"<html>page</html>"),
            # No robots program — would 404, but we expect NOT to call it.
        })
        handler = _build_handler(
            impl, transport,
            seed_urls=[seed], max_depth=0, respect_robots=False,
        )
        ctx = make_ctx()

        signals = [s async for s in handler.pull(ctx)]
        assert len(signals) == 1
        assert all(
            "/robots.txt" not in call for call in transport.calls
        )

    @pytest.mark.asyncio
    async def test_state_store_persists_scraped_urls(self):
        seed = "https://example.org/page"
        impl = _StubImpl(extracts={seed: _signal_for(seed, hash_seed="x")})
        transport = _ScripedTransport({
            seed: httpx.Response(200, content=b"page"),
            "https://example.org/robots.txt": httpx.Response(404),
        })
        store = InMemoryStateStore()
        handler = _build_handler(impl, transport, seed_urls=[seed], max_depth=0)
        ctx = make_ctx(store)

        _ = [s async for s in handler.pull(ctx)]
        scraped = await store.get("scraped_urls")
        assert seed in scraped
        assert scraped[seed] == "x"


# ---------------------------------------------------------------------------
# Rate-limit honoring in pull
# ---------------------------------------------------------------------------


class TestPullRateLimit:
    @pytest.mark.asyncio
    async def test_pull_honors_rate_limit(self):
        """Two articles + a 1/0.5s rate limit means second emit waits >= 0.5s."""
        seed = "https://example.org/feed"
        a = "https://example.org/a"
        b = "https://example.org/b"
        impl = _StubImpl(
            discovery={seed: [a, b]},
            extracts={a: _signal_for(a, hash_seed="ha"),
                      b: _signal_for(b, hash_seed="hb")},
        )
        transport = _ScripedTransport({
            seed: httpx.Response(200, content=b"index"),
            a: httpx.Response(200, content=b"<html>A</html>"),
            b: httpx.Response(200, content=b"<html>B</html>"),
            "https://example.org/robots.txt": httpx.Response(404),
        })
        # 1 request per 0.4s — including the seed fetch, we have 3 fetches
        # over the run. 3rd fetch must wait ~0.8s minimum.
        cfg_override = {"rate_limit": "1/sec"}
        handler = _build_handler(
            impl, transport, seed_urls=[seed], max_depth=1,
            rate_limit_spec="1/sec",
        )
        # Override the rate limiter to a tighter window so the test runs fast.
        handler._rate_limiter = _RateLimiter(n=1, period_s=0.4)
        ctx = make_ctx()

        t0 = time.monotonic()
        signals = [s async for s in handler.pull(ctx)]
        elapsed = time.monotonic() - t0
        assert len(signals) == 2
        # Three rate-limited fetches at 1/0.4s = ~0.8s minimum.
        assert elapsed >= 0.6, f"rate limiter not honored: {elapsed}"


# ---------------------------------------------------------------------------
# Proxy resolution via StackRef
# ---------------------------------------------------------------------------


class TestProxyResolution:
    @pytest.mark.asyncio
    async def test_proxy_resolver_invoked_with_stackref(self):
        seed = "https://example.org/page"
        impl = _StubImpl(extracts={seed: _signal_for(seed)})

        transport = _ScripedTransport({
            seed: httpx.Response(200, content=b"page"),
            "https://example.org/robots.txt": httpx.Response(404),
        })
        proxy_client = httpx.AsyncClient(transport=transport, timeout=2.0)

        @asynccontextmanager
        async def proxy_cm(country: str | None = None):
            yield proxy_client

        proxy_pool = MagicMock()
        proxy_pool.get_httpx_async_client = MagicMock(
            side_effect=lambda country=None: proxy_cm(country=country)
        )

        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=proxy_pool)

        cfg = ScraperConfig(
            impl="legba.data.sources.scrapers.example_news:ExampleNewsScraper",
            proxy_pool=Property.StackRef(raw="proxy.bright_data.us"),
            rate_limit=Property.RateLimit.of("100/min"),
            seed_urls=[seed],
            max_depth=0,
            proxy_country="us",
        )
        handler = ScraperSourceHandler(cfg, stack_resolver=resolver, impl=impl)
        ctx = make_ctx()

        signals = [s async for s in handler.pull(ctx)]
        assert len(signals) == 1
        # Resolver was called with the configured StackRef.
        assert resolver.resolve.await_count >= 1
        called_ref = resolver.resolve.call_args.args[0]
        assert called_ref.raw == "proxy.bright_data.us"
        # Proxy client factory was called with the configured country.
        assert proxy_pool.get_httpx_async_client.call_args.kwargs.get("country") == "us"

        await proxy_client.aclose()

    @pytest.mark.asyncio
    async def test_proxy_resolver_failure_degrades_to_direct(self):
        seed = "https://example.org/page"
        impl = _StubImpl(extracts={seed: _signal_for(seed)})

        transport = _ScripedTransport({
            seed: httpx.Response(200, content=b"page"),
            "https://example.org/robots.txt": httpx.Response(404),
        })

        resolver = MagicMock()
        resolver.resolve = AsyncMock(side_effect=RuntimeError("vault unreachable"))

        cfg = ScraperConfig(
            impl="legba.data.sources.scrapers.example_news:ExampleNewsScraper",
            proxy_pool=Property.StackRef(raw="proxy.bright_data.us"),
            rate_limit=Property.RateLimit.of("100/min"),
            seed_urls=[seed],
            max_depth=0,
        )
        handler = ScraperSourceHandler(cfg, stack_resolver=resolver, impl=impl)
        # Patch the direct-client path so the test doesn't hit the network.
        original_cm = handler._client_cm

        @asynccontextmanager
        async def patched_cm():
            async with httpx.AsyncClient(transport=transport, timeout=2.0) as c:
                yield c
        handler._client_cm = patched_cm  # type: ignore[assignment]

        ctx = make_ctx()
        signals = [s async for s in handler.pull(ctx)]
        # Even with proxy resolution failing, the scrape proceeds.
        assert len(signals) == 1
        assert resolver.resolve.await_count >= 1


# ---------------------------------------------------------------------------
# ExampleNewsScraper.extract — trafilatura fixture
# ---------------------------------------------------------------------------


SAMPLE_ARTICLE_HTML = """<!DOCTYPE html>
<html>
<head>
<title>Example Headline</title>
<meta property="article:published_time" content="2025-06-12T08:30:00Z" />
<meta name="author" content="Jane Doe" />
</head>
<body>
<header><nav>menu</nav></header>
<article>
<h1>Example Headline</h1>
<p>This is the first paragraph of an example article that is long enough
for trafilatura to consider it the main content of the page. It contains
substantive text that should survive the extractor's noise filter.</p>
<p>A second paragraph reinforces that this is the article body — the
extractor looks for paragraphs with enough textual mass to score above
the boilerplate threshold.</p>
</article>
<footer>copyright</footer>
</body></html>"""


SAMPLE_RSS_XML = """<?xml version="1.0"?>
<rss version="2.0">
<channel>
<title>Example feed</title>
<item>
<title>Article one</title>
<link>https://news.example.org/2025/06/article-one</link>
<pubDate>Thu, 12 Jun 2025 08:30:00 GMT</pubDate>
</item>
<item>
<title>Article two</title>
<link>https://news.example.org/2025/06/article-two</link>
<pubDate>Thu, 12 Jun 2025 09:15:00 GMT</pubDate>
</item>
</channel>
</rss>"""


SAMPLE_SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://news.example.org/a</loc></url>
<url><loc>https://news.example.org/b</loc></url>
<url><loc>https://news.example.org/c</loc></url>
</urlset>"""


class TestExampleNewsScraper:
    @pytest.mark.asyncio
    async def test_extract_article_html(self):
        scraper = ExampleNewsScraper()
        ctx = make_ctx()
        signal = await scraper.extract(
            SAMPLE_ARTICLE_HTML,
            "https://news.example.org/a",
            ctx=ctx,
        )
        assert signal is not None
        assert signal.canonical_url == "https://news.example.org/a"
        assert signal.content_hash
        body = signal.payload["body"]
        assert "first paragraph" in body or "article body" in body
        # Title may or may not be recovered depending on trafilatura version,
        # but the payload key MUST be present.
        assert "title" in signal.payload

    @pytest.mark.asyncio
    async def test_extract_empty_html_returns_none(self):
        scraper = ExampleNewsScraper()
        ctx = make_ctx()
        signal = await scraper.extract(
            "<html><body></body></html>",
            "https://news.example.org/empty",
            ctx=ctx,
        )
        assert signal is None

    @pytest.mark.asyncio
    async def test_discover_urls_from_rss(self):
        scraper = ExampleNewsScraper()

        async def fetch(url: str) -> httpx.Response:
            return httpx.Response(
                200, content=SAMPLE_RSS_XML.encode(),
                headers={"content-type": "application/rss+xml"},
            )

        urls = [u async for u in scraper.discover_urls(
            "https://news.example.org/feed", 0, fetch=fetch,
        )]
        assert "https://news.example.org/2025/06/article-one" in urls
        assert "https://news.example.org/2025/06/article-two" in urls

    @pytest.mark.asyncio
    async def test_discover_urls_from_sitemap(self):
        scraper = ExampleNewsScraper()

        async def fetch(url: str) -> httpx.Response:
            return httpx.Response(
                200, content=SAMPLE_SITEMAP_XML.encode(),
                headers={"content-type": "application/xml"},
            )

        urls = [u async for u in scraper.discover_urls(
            "https://news.example.org/sitemap.xml", 0, fetch=fetch,
        )]
        assert urls == [
            "https://news.example.org/a",
            "https://news.example.org/b",
            "https://news.example.org/c",
        ]

    @pytest.mark.asyncio
    async def test_discover_urls_depth_ge_1_returns_nothing(self):
        scraper = ExampleNewsScraper()

        async def fetch(url: str) -> httpx.Response:
            raise AssertionError("fetch should not be called at depth>=1")

        urls = [u async for u in scraper.discover_urls(
            "https://news.example.org/feed", 1, fetch=fetch,
        )]
        assert urls == []

    def test_parse_sitemap_falls_back_on_malformed_xml(self):
        malformed = "<urlset><url><loc>https://x.org/a</loc></url><broken"
        urls = _parse_sitemap(malformed)
        assert "https://x.org/a" in urls


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_unhealthy_when_impl_import_fails(self):
        cfg = ScraperConfig(
            impl="legba.does.not.exist:Whatever",
            rate_limit=Property.RateLimit.of("10/min"),
            seed_urls=["https://example.org/feed"],
        )
        handler = ScraperSourceHandler(cfg)
        ctx = make_ctx()
        hc = await handler.health_check(ctx)
        assert hc.state == "unhealthy"
        assert "impl_import" in (hc.last_error or "")

    @pytest.mark.asyncio
    async def test_healthy_when_impl_loadable_no_proxy(self):
        cfg = ScraperConfig(
            impl="legba.data.sources.scrapers.example_news:ExampleNewsScraper",
            rate_limit=Property.RateLimit.of("10/min"),
            seed_urls=[],
            respect_robots=False,
        )
        handler = ScraperSourceHandler(cfg)
        ctx = make_ctx()
        hc = await handler.health_check(ctx)
        assert hc.state == "healthy"
        assert hc.detail.get("impl") in ("loaded", "cached")

    @pytest.mark.asyncio
    async def test_degraded_when_proxy_resolver_fails(self):
        cfg = ScraperConfig(
            impl="legba.data.sources.scrapers.example_news:ExampleNewsScraper",
            proxy_pool=Property.StackRef(raw="proxy.bright_data.us"),
            rate_limit=Property.RateLimit.of("10/min"),
            seed_urls=[],
            respect_robots=False,
        )
        resolver = MagicMock()
        resolver.resolve = AsyncMock(side_effect=RuntimeError("vault down"))
        handler = ScraperSourceHandler(cfg, stack_resolver=resolver)
        ctx = make_ctx()
        hc = await handler.health_check(ctx)
        assert hc.state == "degraded"
        assert "proxy_error" in hc.detail


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_on_configure_loads_impl(self):
        cfg = ScraperConfig(
            impl="legba.data.sources.scrapers.example_news:ExampleNewsScraper",
            rate_limit=Property.RateLimit.of("10/min"),
            seed_urls=["https://example.org/feed"],
        )
        handler = ScraperSourceHandler(cfg)
        assert handler.impl is None
        await handler.on_configure()
        assert isinstance(handler.impl, ExampleNewsScraper)

    @pytest.mark.asyncio
    async def test_on_retire_clears_proxy_and_robots(self):
        cfg = ScraperConfig(
            impl="legba.data.sources.scrapers.example_news:ExampleNewsScraper",
            rate_limit=Property.RateLimit.of("10/min"),
            seed_urls=[],
        )
        handler = ScraperSourceHandler(cfg)
        handler._proxy = MagicMock()
        handler._robots["example.org"] = MagicMock()
        await handler.on_retire()
        assert handler._proxy is None
        assert handler._robots == {}


# ---------------------------------------------------------------------------
# Live opt-in integration
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("LEGBA_SCRAPER_LIVE_TEST") != "1",
    reason="opt-in live integration; set LEGBA_SCRAPER_LIVE_TEST=1",
)
class TestLiveIntegration:
    @pytest.mark.asyncio
    async def test_httpbin_shape(self):
        """End-to-end shape against httpbin.org (no real news site).

        httpbin returns simple HTML; trafilatura may or may not extract a
        body. We only assert the handler completes the pull without raising
        and that any yielded signal carries identity / provenance.
        """
        cfg = ScraperConfig(
            impl="legba.data.sources.scrapers.example_news:ExampleNewsScraper",
            rate_limit=Property.RateLimit.of("5/sec"),
            seed_urls=["https://httpbin.org/html"],
            max_depth=0,
            respect_robots=True,
        )
        handler = ScraperSourceHandler(cfg)
        ctx = make_ctx()
        signals = [s async for s in handler.pull(ctx)]
        # httpbin's /html might yield a signal; either is acceptable.
        for s in signals:
            assert s.source_id == "src.test"
            assert s.target_id == "target.test"
            assert s.raw_provenance.get("http_status") == 200


# ---------------------------------------------------------------------------
# Helper to build a handler with a programmed transport
# ---------------------------------------------------------------------------


def _build_handler(
    impl: ScraperImpl,
    transport: _ScripedTransport,
    *,
    seed_urls: list[str],
    max_depth: int = 1,
    respect_robots: bool = True,
    rate_limit_spec: str = "1000/min",
) -> ScraperSourceHandler:
    """Build a handler whose ``_client_cm`` yields a client over the
    in-memory transport. The proxy_pool is left unset."""
    cfg = ScraperConfig(
        impl="legba.data.sources.scrapers.example_news:ExampleNewsScraper",
        rate_limit=Property.RateLimit.of(rate_limit_spec),
        seed_urls=seed_urls,
        max_depth=max_depth,
        respect_robots=respect_robots,
    )
    handler = ScraperSourceHandler(cfg, impl=impl)

    @asynccontextmanager
    async def cm() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(transport=transport, timeout=2.0) as client:
            yield client

    handler._client_cm = cm  # type: ignore[assignment]
    return handler
