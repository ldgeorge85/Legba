# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for :class:`legba.data.sources.rss.RSSSourceHandler` (L-130).

Coverage:

  * Config schema validation (defaults, parser literal, bad URL).
  * Conformance with the L-102 source-kind class-var contract.
  * 200 happy path: feed parse, ``Signal`` shape, cursor persistence,
    health record on success.
  * 304 path: no Signals, no cursor mutation, health = healthy.
  * ``since`` filter: entries strictly after the cursor pass.
  * Conditional headers: stored ETag / Last-Modified → ``If-None-Match`` /
    ``If-Modified-Since`` on subsequent pulls.
  * Malformed feed: degraded health, no exceptions, empty iterator.
  * Transient HTTP (503): one retry, then yields empty + degraded health.
  * Persistent 4xx: unhealthy state, empty iterator.
  * Live integration: optional, against ``LEGBA_RSS_TEST_URL`` if set.

We mock ``httpx`` via :class:`httpx.MockTransport` so the handler hits a
deterministic transport while still exercising the real ``httpx.AsyncClient``
machinery. ``feedparser`` runs for real (it's pure-Python; faster than
trying to fake it).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from legba.data.sources._contract import (
    InMemoryStateStore,
    Signal,
    SourceContext,
    SourceHealth,
)
from legba.data.sources.rss import (
    RSSConfig,
    RSSSourceHandler,
    _RSS_CURSOR_KEY,
    _RSS_HEALTH_KEY,
)


# ---------------------------------------------------------------------------
# Fixtures + sample feeds
# ---------------------------------------------------------------------------


SAMPLE_RSS_2 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel>
  <title>Sample Energy News</title>
  <link>https://example.invalid/news</link>
  <description>RSS 2.0 fixture for legba test suite</description>
  <language>en-us</language>
  <item>
    <title>Brazil approves new transmission auction</title>
    <link>https://example.invalid/news/brazil-tx-auction</link>
    <description>The regulator approved a new line.</description>
    <author>reporter@example.invalid</author>
    <category>energy</category>
    <category>brazil</category>
    <pubDate>Mon, 19 May 2025 14:00:00 +0000</pubDate>
    <guid isPermaLink="false">tag:example.invalid:1001</guid>
  </item>
  <item>
    <title>Hydrogen pilot funded in Bahia</title>
    <link>https://example.invalid/news/h2-bahia</link>
    <description>R$ 200M green hydrogen pilot.</description>
    <pubDate>Tue, 20 May 2025 09:30:00 +0000</pubDate>
    <guid>https://example.invalid/news/h2-bahia</guid>
  </item>
  <item>
    <title>Old story we should filter out</title>
    <link>https://example.invalid/news/old</link>
    <description>Way in the past.</description>
    <pubDate>Sun, 01 Jan 2023 00:00:00 +0000</pubDate>
    <guid>tag:example.invalid:archive-0</guid>
  </item>
</channel>
</rss>
"""


SAMPLE_ATOM_1 = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Feed Fixture</title>
  <link href="https://example.invalid/atom" rel="self"/>
  <updated>2025-05-20T15:00:00Z</updated>
  <id>urn:uuid:atom-feed-fixture</id>
  <entry>
    <title>Atom entry one</title>
    <link href="https://example.invalid/atom/one"/>
    <id>urn:uuid:atom-entry-one</id>
    <updated>2025-05-20T12:00:00Z</updated>
    <published>2025-05-20T12:00:00Z</published>
    <summary>A first atom entry.</summary>
    <author><name>Test Author</name></author>
    <category term="energy"/>
    <content type="html">&lt;p&gt;Full body.&lt;/p&gt;</content>
  </entry>
</feed>
"""


MALFORMED_FEED = "<not-xml>this is not a feed at all</not-xml-broken"


def _make_ctx(
    state: InMemoryStateStore | None = None,
    *,
    config: RSSConfig | None = None,
) -> SourceContext:
    return SourceContext(
        target_id="target.energy_brazil",
        target_version="v-test",
        source_id="src.epe_feed",
        config=config or RSSConfig(url="https://example.invalid/feed.xml"),
        state_store=state or InMemoryStateStore(),
        scope_geo=["BR"],
        scope_languages=["pt", "en"],
    )


def _make_handler(
    transport_handler: Callable[[httpx.Request], httpx.Response],
    *,
    config: RSSConfig | None = None,
) -> RSSSourceHandler:
    transport = httpx.MockTransport(transport_handler)
    client = httpx.AsyncClient(
        transport=transport,
        follow_redirects=True,
        timeout=5,
    )
    cfg = config or RSSConfig(url="https://example.invalid/feed.xml")
    return RSSSourceHandler(cfg, http_client=client)


async def _collect(it):
    """Collect all signals from an async-generator pull."""
    out: list[Signal] = []
    async for s in it:
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# Config + class-var contract
# ---------------------------------------------------------------------------


def test_rss_config_defaults():
    cfg = RSSConfig(url="https://x.invalid/feed")
    assert cfg.parser == "auto"
    assert cfg.user_agent == "Legba/2.0"
    assert cfg.timeout_seconds == 30


def test_rss_config_rejects_bad_parser():
    with pytest.raises(ValidationError):
        RSSConfig(url="https://x.invalid/feed", parser="json")


def test_rss_config_rejects_unknown_field():
    with pytest.raises(ValidationError):
        RSSConfig(url="https://x.invalid/feed", what="ever")  # type: ignore[call-arg]


def test_rss_config_rejects_blank_url():
    with pytest.raises(ValidationError):
        RSSConfig(url="")


def test_handler_class_contract():
    """Sanity check the L-102 class-var contract."""
    assert RSSSourceHandler.kind == "rss"
    assert RSSSourceHandler.family == "source"
    assert RSSSourceHandler.schema_version == "legba/source.rss/2-0-0"
    assert RSSSourceHandler.config_schema is RSSConfig
    assert RSSSourceHandler.handler_version


# ---------------------------------------------------------------------------
# Happy path: 200 + parse + cursor persist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_200_yields_signals_and_persists_cursor():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(
            200,
            text=SAMPLE_RSS_2,
            headers={
                "Content-Type": "application/rss+xml",
                "ETag": 'W/"abc123"',
                "Last-Modified": "Tue, 20 May 2025 10:00:00 GMT",
            },
            request=req,
        )

    state = InMemoryStateStore()
    ctx = _make_ctx(state)
    rss = _make_handler(handler)

    signals = await _collect(rss.pull(ctx, since=None))
    await rss.aclose()

    # Three items in the fixture.
    assert len(signals) == 3

    s = signals[0]
    assert isinstance(s, Signal)
    assert s.source_id == "src.epe_feed"
    # Source-first pivot (migration 0024): the Signal is target-agnostic —
    # ``target_id`` left the schema entirely (it lives only on derived analyst
    # outputs). The handler stamps ``source_id`` from ctx.source_id; there is
    # no longer a ``target_id`` attribute to assert.
    assert s.payload["title"].startswith("Brazil approves")
    assert s.payload["link"] == "https://example.invalid/news/brazil-tx-auction"
    assert s.payload["author"] == "reporter@example.invalid"
    assert s.payload["source_url"] == "https://example.invalid/feed.xml"
    assert "energy" in s.payload["tags"]
    assert s.payload["external_id"] == "tag:example.invalid:1001"
    assert s.payload["published_at"].startswith("2025-05-19T14:00:00")
    assert s.canonical_url == "https://example.invalid/news/brazil-tx-auction"
    assert len(s.content_hash) == 64

    # Cursor was stored.
    cursor = state.snapshot()[_RSS_CURSOR_KEY]
    assert cursor["etag"] == 'W/"abc123"'
    assert cursor["last_modified"] == "Tue, 20 May 2025 10:00:00 GMT"

    # Health was recorded healthy.
    health = state.snapshot()[_RSS_HEALTH_KEY]
    assert health["state"] == "healthy"
    assert health["detail"]["entries_yielded"] == 3

    # First pull sent no conditional headers (no cursor).
    assert "If-None-Match" not in captured[0].headers
    assert "If-Modified-Since" not in captured[0].headers
    assert captured[0].headers["user-agent"] == "Legba/2.0"


@pytest.mark.asyncio
async def test_pull_atom_feed_yields_signals():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=SAMPLE_ATOM_1,
            headers={"Content-Type": "application/atom+xml"},
            request=req,
        )

    ctx = _make_ctx()
    rss = _make_handler(handler)
    signals = await _collect(rss.pull(ctx))
    await rss.aclose()

    assert len(signals) == 1
    s = signals[0]
    assert s.payload["title"] == "Atom entry one"
    assert s.payload["author"] == "Test Author"
    assert s.payload["external_id"] == "urn:uuid:atom-entry-one"
    # content over summary.
    assert "<p>Full body." in s.payload["raw_body"]
    assert s.canonical_url == "https://example.invalid/atom/one"


# ---------------------------------------------------------------------------
# Conditional GET: ETag / Last-Modified on subsequent pulls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subsequent_pull_sends_conditional_headers():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        # First call returns 200 + ETag; second call returns 304.
        if len(captured) == 1:
            return httpx.Response(
                200,
                text=SAMPLE_RSS_2,
                headers={
                    "ETag": '"v1"',
                    "Last-Modified": "Tue, 20 May 2025 10:00:00 GMT",
                },
                request=req,
            )
        return httpx.Response(304, request=req)

    state = InMemoryStateStore()
    ctx = _make_ctx(state)
    rss = _make_handler(handler)

    first = await _collect(rss.pull(ctx))
    second = await _collect(rss.pull(ctx))
    await rss.aclose()

    assert len(first) == 3
    assert second == []

    assert captured[1].headers.get("If-None-Match") == '"v1"'
    assert (
        captured[1].headers.get("If-Modified-Since")
        == "Tue, 20 May 2025 10:00:00 GMT"
    )

    # Cursor should still be the v1 cursor (304 doesn't overwrite).
    cursor = state.snapshot()[_RSS_CURSOR_KEY]
    assert cursor["etag"] == '"v1"'

    # Health on 304 should be healthy.
    health = state.snapshot()[_RSS_HEALTH_KEY]
    assert health["state"] == "healthy"
    assert health["detail"]["status"] == 304


# ---------------------------------------------------------------------------
# since filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_since_filter_excludes_older_entries():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SAMPLE_RSS_2, request=req)

    ctx = _make_ctx()
    rss = _make_handler(handler)

    # Cutoff: 2024-01-01. Two entries from 2025 should pass; the 2023 one
    # should be filtered out.
    since = datetime(2024, 1, 1, tzinfo=timezone.utc)
    signals = await _collect(rss.pull(ctx, since=since))
    await rss.aclose()

    titles = [s.payload["title"] for s in signals]
    assert "Brazil approves new transmission auction" in titles
    assert "Hydrogen pilot funded in Bahia" in titles
    assert all("Old story" not in t for t in titles)
    assert len(signals) == 2


@pytest.mark.asyncio
async def test_since_filter_strict_after_only():
    """An entry whose published_at == since must NOT be re-emitted."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SAMPLE_RSS_2, request=req)

    ctx = _make_ctx()
    rss = _make_handler(handler)

    # Equal to the latest entry's pubDate (Tue, 20 May 2025 09:30:00 +0000)
    since = datetime(2025, 5, 20, 9, 30, 0, tzinfo=timezone.utc)
    signals = await _collect(rss.pull(ctx, since=since))
    await rss.aclose()

    titles = [s.payload["title"] for s in signals]
    # The "equal" entry (h2-bahia) should be filtered (strict >).
    assert "Hydrogen pilot funded in Bahia" not in titles
    # The older 2025-05-19 entry is now also "<=since", so out.
    assert "Brazil approves new transmission auction" not in titles


# ---------------------------------------------------------------------------
# Malformed feed → degraded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_feed_returns_empty_and_degraded():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=MALFORMED_FEED,
            headers={"Content-Type": "text/plain"},
            request=req,
        )

    state = InMemoryStateStore()
    ctx = _make_ctx(state)
    rss = _make_handler(handler)

    signals = await _collect(rss.pull(ctx))
    await rss.aclose()

    assert signals == []
    health = state.snapshot()[_RSS_HEALTH_KEY]
    assert health["state"] == "degraded"
    assert "parse" in (health["last_error"] or "").lower()


# ---------------------------------------------------------------------------
# 4xx / 5xx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persistent_4xx_returns_empty_and_unhealthy():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found", request=req)

    state = InMemoryStateStore()
    ctx = _make_ctx(state)
    rss = _make_handler(handler)

    signals = await _collect(rss.pull(ctx))
    await rss.aclose()

    assert signals == []
    health = state.snapshot()[_RSS_HEALTH_KEY]
    assert health["state"] == "unhealthy"
    assert "404" in (health["last_error"] or "")


@pytest.mark.asyncio
async def test_transient_5xx_retries_then_empty():
    call_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(503, text="busy", request=req)

    state = InMemoryStateStore()
    ctx = _make_ctx(state)
    rss = _make_handler(handler)

    signals = await _collect(rss.pull(ctx))
    await rss.aclose()

    assert signals == []
    # Exactly one retry: 2 total calls.
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_transient_network_error_retries_then_empty():
    call_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        raise httpx.ConnectError("simulated network down")

    state = InMemoryStateStore()
    ctx = _make_ctx(state)
    rss = _make_handler(handler)

    signals = await _collect(rss.pull(ctx))
    await rss.aclose()

    assert signals == []
    assert call_count["n"] == 2
    health = state.snapshot()[_RSS_HEALTH_KEY]
    assert health["state"] == "degraded"
    assert "transient" in (health["last_error"] or "").lower()


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_200_is_healthy():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SAMPLE_RSS_2, request=req)

    ctx = _make_ctx()
    rss = _make_handler(handler)
    h = await rss.health_check(ctx)
    await rss.aclose()

    assert isinstance(h, SourceHealth)
    assert h.state == "healthy"
    assert h.detail["status"] == 200


@pytest.mark.asyncio
async def test_health_check_304_is_healthy():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(304, request=req)

    state = InMemoryStateStore({_RSS_CURSOR_KEY: {"etag": '"x"', "last_modified": ""}})
    ctx = _make_ctx(state)
    rss = _make_handler(handler)
    h = await rss.health_check(ctx)
    await rss.aclose()
    assert h.state == "healthy"
    assert h.detail["status"] == 304


@pytest.mark.asyncio
async def test_health_check_503_is_degraded():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=req)

    ctx = _make_ctx()
    rss = _make_handler(handler)
    h = await rss.health_check(ctx)
    await rss.aclose()
    assert h.state == "degraded"


@pytest.mark.asyncio
async def test_health_check_network_error_returns_degraded_with_context():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    ctx = _make_ctx()
    rss = _make_handler(handler)
    h = await rss.health_check(ctx)
    await rss.aclose()
    assert h.state == "degraded"
    assert "transient" in (h.last_error or "").lower()


# ---------------------------------------------------------------------------
# Conditional headers: derived from stored cursor only when populated
# ---------------------------------------------------------------------------


def test_build_conditional_headers_empty_cursor():
    h = RSSSourceHandler._build_conditional_headers({})
    assert h == {}


def test_build_conditional_headers_full_cursor():
    h = RSSSourceHandler._build_conditional_headers(
        {"etag": 'W/"e"', "last_modified": "Mon, 19 May 2025 14:00:00 GMT"}
    )
    assert h["If-None-Match"] == 'W/"e"'
    assert h["If-Modified-Since"] == "Mon, 19 May 2025 14:00:00 GMT"


def test_build_conditional_headers_partial_cursor():
    h = RSSSourceHandler._build_conditional_headers({"etag": "", "last_modified": "x"})
    assert "If-None-Match" not in h
    assert h["If-Modified-Since"] == "x"


# ---------------------------------------------------------------------------
# Optional live integration test (only runs when env-var is set)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_rss_feed_if_configured():
    url = os.environ.get("LEGBA_RSS_TEST_URL")
    if not url:
        pytest.skip("LEGBA_RSS_TEST_URL not set; skipping live RSS integration")

    cfg = RSSConfig(url=url, timeout_seconds=15)
    rss = RSSSourceHandler(cfg)
    try:
        ctx = _make_ctx()
        signals = await _collect(rss.pull(ctx, since=None))
    finally:
        await rss.aclose()

    # We don't assert content (live feeds vary); just that we got something
    # plausible without exceptions, and that signals carry the required keys.
    assert isinstance(signals, list)
    for s in signals[:5]:
        assert isinstance(s, Signal)
        assert "title" in s.payload
        assert "link" in s.payload
        assert "external_id" in s.payload
        assert "raw_body" in s.payload
        assert s.payload["source_url"] == url
