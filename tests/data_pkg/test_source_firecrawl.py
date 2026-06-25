# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for :class:`legba.data.sources.firecrawl.FirecrawlSourceHandler`
(L-139).

Coverage:

  * Config schema validation (required api_key_secret, defaults, mode
    literals, seed-URL shape, rejection of unknown fields).
  * L-102 source-kind class-var contract conformance (kind / family /
    schema_version / config_schema / handler_version).
  * ``scrape`` mode happy path: each seed URL → one Signal, payload shape
    (external_id, url, raw_body == markdown, title, firecrawl_metadata),
    canonical_url, language_hint, content_hash.
  * ``crawl`` mode: POST /v1/crawl returns ``id`` → handler polls
    ``GET /v1/crawl/{id}`` until ``status == "completed"`` → yields one
    Signal per page in ``data[]``. Credits sum across status payloads.
  * ``map`` mode: POST /v1/map returns ``links`` → one lightweight Signal
    per URL with empty raw_body; canonical_url == link.
  * Credit-cost reporting: ``report_usage()`` aggregates ``creditsUsed``
    across the pull and exposes a structured :class:`CreditUsageRecord`.
  * State-store persistence: ``firecrawl.last_pull_at`` and
    ``firecrawl.lifetime_credits`` written after each pull (including on
    exception paths).
  * Healthcheck: hits ``/v1/scrape`` with example.com; surfaces healthy,
    unhealthy (auth), degraded (rate-limited), unhealthy (network).
  * Auth header transmitted: ``Authorization: Bearer <api_key>``.
  * 4xx / 5xx mapping → ``FirecrawlAuthError``, ``FirecrawlRateLimited``,
    ``FirecrawlAPIError``.
  * ``success=False`` body → ``FirecrawlAPIError`` even on 200.
  * Live integration: gated on ``LEGBA_FIRECRAWL_API_KEY`` env var (paid).

httpx is mocked via :class:`httpx.MockTransport` so the handler exercises
real ``httpx.AsyncClient`` machinery against a deterministic transport.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Callable

import httpx
import pytest
from pydantic import ValidationError

from legba.data.schemas.properties import Secret as SecretRef
from legba.data.sources._contract import (
    InMemoryStateStore,
    Signal,
    SourceContext,
    SourceHealth,
)
from legba.data.sources.firecrawl import (
    CreditUsageRecord,
    FirecrawlAPIError,
    FirecrawlAuthError,
    FirecrawlConfig,
    FirecrawlRateLimited,
    FirecrawlSourceHandler,
)


# Real src bug surfaced by the source-first pivot (commit fa3e598): the pivot
# re-cut the Signal model (src/legba/data/sources/_contract.py) to drop
# ``target_id`` and set ``extra='forbid'`` (observations are source-owned, not
# target-owned), but the source handlers were NOT updated — the Firecrawl
# handler still calls ``Signal(..., target_id=ctx.target_id, ...)`` in all
# three modes (firecrawl.py:364/467/577). So the first Signal a ``pull()``
# constructs raises ``ValidationError: target_id Extra inputs are not
# permitted``. This is a bug in src, not a stale-test/schema issue, so per the
# migration constraints it is FLAGGED in real_src_bugs_flagged and the
# pull-exercising tests are skipped (src not edited to mask it). See
# PIVOT_BUILD_PLAN.
_SRC_BUG_TARGET_ID = (
    "src bug (pivot fa3e598, firecrawl.py:364/467/577): handler still passes "
    "target_id=ctx.target_id into the pivoted Signal model (extra='forbid', "
    "target_id dropped) so pull() raises ValidationError. Flagged in "
    "real_src_bugs_flagged; src not edited. See PIVOT_BUILD_PLAN."
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _ctx(
    state: InMemoryStateStore | None = None,
    *,
    config: FirecrawlConfig | None = None,
) -> SourceContext:
    return SourceContext(
        target_id="target.test_firecrawl",
        target_version="v-test",
        source_id="src.firecrawl_a",
        config=config or FirecrawlConfig(api_key_secret=_secret_ref()),
        state_store=state or InMemoryStateStore(),
        scope_geo=["US"],
        scope_languages=["en"],
    )


def _secret_ref(name: str = "vault.firecrawl_key") -> SecretRef:
    return SecretRef(raw=name)


def _make_handler(
    transport_handler: Callable[[httpx.Request], httpx.Response],
    *,
    config: FirecrawlConfig | None = None,
    api_key: str = "fc-test-key-xyz",
) -> FirecrawlSourceHandler:
    transport = httpx.MockTransport(transport_handler)
    # base_url is required so the relative POST/GET paths resolve under
    # MockTransport.
    base = (config.api_base if config else "https://api.firecrawl.dev")
    client = httpx.AsyncClient(
        transport=transport,
        timeout=5,
        base_url=base.rstrip("/"),
    )
    cfg = config or FirecrawlConfig(
        api_key_secret=_secret_ref(),
        seed_urls=["https://example.com/article-1"],
        mode="scrape",
    )

    async def resolver(_ref: SecretRef) -> str:
        return api_key

    return FirecrawlSourceHandler(
        cfg, secret_resolver=resolver, http_client=client
    )


async def _collect(agen):
    out: list[Signal] = []
    async for s in agen:
        out.append(s)
    return out


def _scrape_response(
    url: str,
    *,
    markdown: str = "# Hello\n\nWorld.",
    title: str = "Hello",
    language: str = "en",
    published: str | None = None,
    credits_used: int = 1,
    links: list[str] | None = None,
    screenshot: str | None = None,
) -> dict:
    metadata: dict = {
        "title": title,
        "language": language,
        "sourceURL": url,
        "statusCode": 200,
    }
    if published:
        metadata["publishedTime"] = published
    page: dict = {"markdown": markdown, "metadata": metadata}
    if links is not None:
        page["links"] = links
    if screenshot is not None:
        page["screenshot"] = screenshot
    return {
        "success": True,
        "data": page,
        "creditsUsed": credits_used,
    }


# ---------------------------------------------------------------------------
# Config + class-var contract
# ---------------------------------------------------------------------------


def test_config_defaults():
    cfg = FirecrawlConfig(api_key_secret=_secret_ref())
    assert cfg.seed_urls == []
    assert cfg.mode == "scrape"
    assert cfg.max_depth == 1
    assert cfg.extract_format == "markdown"
    assert cfg.include_paths is None
    assert cfg.exclude_paths is None
    assert cfg.api_base.startswith("https://")
    assert cfg.request_timeout_seconds > 0
    assert cfg.crawl_limit == 100


def test_config_rejects_unknown_field():
    with pytest.raises(ValidationError):
        FirecrawlConfig(
            api_key_secret=_secret_ref(),
            whatever="nope",  # type: ignore[call-arg]
        )


def test_config_rejects_bad_mode():
    with pytest.raises(ValidationError):
        FirecrawlConfig(
            api_key_secret=_secret_ref(),
            mode="explode",  # type: ignore[arg-type]
        )


def test_config_rejects_non_http_seed():
    with pytest.raises(ValidationError):
        FirecrawlConfig(
            api_key_secret=_secret_ref(),
            seed_urls=["ftp://example.com/x"],
        )


def test_config_max_depth_bounds():
    with pytest.raises(ValidationError):
        FirecrawlConfig(api_key_secret=_secret_ref(), max_depth=-1)
    with pytest.raises(ValidationError):
        FirecrawlConfig(api_key_secret=_secret_ref(), max_depth=999)


def test_config_extract_format_literals():
    for fmt in ("markdown", "html", "links", "screenshot"):
        cfg = FirecrawlConfig(
            api_key_secret=_secret_ref(), extract_format=fmt
        )
        assert cfg.extract_format == fmt
    with pytest.raises(ValidationError):
        FirecrawlConfig(
            api_key_secret=_secret_ref(),
            extract_format="pdf",  # type: ignore[arg-type]
        )


def test_handler_class_contract():
    assert FirecrawlSourceHandler.kind == "firecrawl"
    assert FirecrawlSourceHandler.family == "source"
    assert (
        FirecrawlSourceHandler.schema_version
        == "legba/source.firecrawl/1-0-0"
    )
    assert FirecrawlSourceHandler.config_schema is FirecrawlConfig
    assert FirecrawlSourceHandler.handler_version


# ---------------------------------------------------------------------------
# scrape mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scrape_mode_yields_one_signal_per_seed():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        assert req.method == "POST"
        assert req.url.path == "/v1/scrape"
        body = req.read().decode() if req.content else ""
        # Body includes "url" key with one of the seeds.
        assert "https://example.com/article" in body
        return httpx.Response(
            200,
            json=_scrape_response(
                "https://example.com/article-1",
                markdown="# A1\n\nbody",
                title="A1",
                language="en",
                credits_used=2,
            ),
            request=req,
        )

    cfg = FirecrawlConfig(
        api_key_secret=_secret_ref(),
        seed_urls=[
            "https://example.com/article-1",
            "https://example.com/article-2",
        ],
        mode="scrape",
    )
    state = InMemoryStateStore()
    ctx = _ctx(state)
    fc = _make_handler(handler, config=cfg)

    signals = await _collect(fc.pull(ctx))

    assert len(signals) == 2
    assert len(captured) == 2
    for s in signals:
        assert isinstance(s, Signal)
        assert s.source_id == "src.firecrawl_a"
        # Source-first pivot: Signal is target-agnostic (target_id dropped).
        assert s.payload["external_id"] == s.canonical_url
        assert s.payload["url"].startswith("https://example.com/article")
        assert s.payload["raw_body"].startswith("# A1")
        assert s.payload["title"] == "A1"
        assert s.payload["published_at"]            # default to now()
        assert s.payload["firecrawl_metadata"]["sourceURL"]
        assert s.language_hint == "en"
        assert s.content_hash
        assert s.raw_provenance["mode"] == "scrape"
        assert s.raw_provenance["extract_format"] == "markdown"

    # Auth header transmitted on every request.
    for req in captured:
        assert req.headers["Authorization"] == "Bearer fc-test-key-xyz"
        assert "legba-source-firecrawl/" in req.headers.get("User-Agent", "")

    # State-store cursor + lifetime credits persisted.
    snap = state.snapshot()
    assert "firecrawl.last_pull_at" in snap
    assert isinstance(snap["firecrawl.lifetime_credits"], int)
    assert snap["firecrawl.lifetime_credits"] == 4  # 2 seeds * 2 credits


@pytest.mark.asyncio
async def test_scrape_includes_markdown_when_extract_format_html():
    """When extract_format != markdown the handler still requests markdown
    so raw_body is always populated."""
    captured_bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json
        captured_bodies.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "markdown": "# md",
                    "html": "<h1>md</h1>",
                    "metadata": {
                        "title": "T", "sourceURL": "https://x.test/p"
                    },
                },
                "creditsUsed": 1,
            },
            request=req,
        )

    cfg = FirecrawlConfig(
        api_key_secret=_secret_ref(),
        seed_urls=["https://x.test/p"],
        mode="scrape",
        extract_format="html",
    )
    fc = _make_handler(handler, config=cfg)
    sigs = await _collect(fc.pull(_ctx()))

    assert len(sigs) == 1
    assert sigs[0].payload["extract_format"] == "html"
    # raw_body is the markdown (preferred) even though html was primary.
    assert sigs[0].payload["raw_body"] == "# md"
    # The actual API call asked for both markdown + html so we always have
    # a textual fallback.
    assert captured_bodies[0]["formats"] == ["markdown", "html"]


# ---------------------------------------------------------------------------
# crawl mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crawl_mode_polls_until_completed():
    """POST /v1/crawl returns id → handler GETs /v1/crawl/{id} until
    status='completed' → yields one Signal per page in data[]."""
    poll_state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/v1/crawl" and req.method == "POST":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "id": "job-xyz",
                    "url": "https://api.firecrawl.dev/v1/crawl/job-xyz",
                },
                request=req,
            )
        if path == "/v1/crawl/job-xyz" and req.method == "GET":
            poll_state["n"] += 1
            if poll_state["n"] < 2:
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "status": "scraping",
                        "completed": 0,
                        "total": 2,
                    },
                    request=req,
                )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "status": "completed",
                    "completed": 2,
                    "total": 2,
                    "creditsUsed": 7,
                    "data": [
                        {
                            "markdown": "# P1",
                            "metadata": {
                                "title": "P1",
                                "sourceURL": "https://site.test/p1",
                                "language": "en",
                            },
                        },
                        {
                            "markdown": "# P2",
                            "metadata": {
                                "title": "P2",
                                "sourceURL": "https://site.test/p2",
                                "language": "en",
                            },
                        },
                    ],
                },
                request=req,
            )
        return httpx.Response(404, json={"success": False}, request=req)

    cfg = FirecrawlConfig(
        api_key_secret=_secret_ref(),
        seed_urls=["https://site.test/"],
        mode="crawl",
        max_depth=2,
        include_paths=["/posts/*"],
        exclude_paths=["/login"],
        crawl_poll_interval_seconds=0.01,
        crawl_max_polls=5,
    )
    state = InMemoryStateStore()
    fc = _make_handler(handler, config=cfg)
    sigs = await _collect(fc.pull(_ctx(state)))

    assert len(sigs) == 2
    assert {s.payload["title"] for s in sigs} == {"P1", "P2"}
    assert all(s.raw_provenance["mode"] == "crawl" for s in sigs)
    # Credits from the final status payload.
    assert state.snapshot()["firecrawl.lifetime_credits"] == 7
    # And report_usage reflects them.
    rec = fc.report_usage(_ctx(state))
    assert rec.credits == 7
    assert rec.mode == "crawl"


@pytest.mark.asyncio
async def test_crawl_mode_includes_filters_in_body():
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json
        if req.method == "POST" and req.url.path == "/v1/crawl":
            captured.append(json.loads(req.content))
            return httpx.Response(
                200,
                json={"success": True, "id": "job-1"},
                request=req,
            )
        if req.method == "GET" and req.url.path == "/v1/crawl/job-1":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "status": "completed",
                    "creditsUsed": 1,
                    "data": [],
                },
                request=req,
            )
        return httpx.Response(404, json={"success": False}, request=req)

    cfg = FirecrawlConfig(
        api_key_secret=_secret_ref(),
        seed_urls=["https://site.test/"],
        mode="crawl",
        max_depth=3,
        include_paths=["/blog/*", "/news/*"],
        exclude_paths=["/admin/*"],
        crawl_poll_interval_seconds=0.01,
        crawl_max_polls=2,
    )
    fc = _make_handler(handler, config=cfg)
    await _collect(fc.pull(_ctx()))

    assert len(captured) == 1
    body = captured[0]
    assert body["url"] == "https://site.test/"
    assert body["maxDepth"] == 3
    assert body["includePaths"] == ["/blog/*", "/news/*"]
    assert body["excludePaths"] == ["/admin/*"]
    assert body["scrapeOptions"] == {"formats": ["markdown"]}


# ---------------------------------------------------------------------------
# map mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_map_mode_yields_one_signal_per_link():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/map"
        return httpx.Response(
            200,
            json={
                "success": True,
                "links": [
                    "https://site.test/a",
                    "https://site.test/b",
                    "https://site.test/c",
                ],
                "creditsUsed": 1,
            },
            request=req,
        )

    cfg = FirecrawlConfig(
        api_key_secret=_secret_ref(),
        seed_urls=["https://site.test/"],
        mode="map",
    )
    state = InMemoryStateStore()
    fc = _make_handler(handler, config=cfg)
    sigs = await _collect(fc.pull(_ctx(state)))

    assert len(sigs) == 3
    urls = {s.canonical_url for s in sigs}
    assert urls == {
        "https://site.test/a",
        "https://site.test/b",
        "https://site.test/c",
    }
    for s in sigs:
        assert s.payload["external_id"] == s.canonical_url
        assert s.payload["raw_body"] == ""
        assert s.payload["kind"] == "firecrawl_map_link"
        assert s.raw_provenance["mode"] == "map"

    assert state.snapshot()["firecrawl.lifetime_credits"] == 1


# ---------------------------------------------------------------------------
# Credit accounting + report_usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_usage_aggregates_credits_across_seeds():
    seed_credits = iter([3, 5])

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_scrape_response(
                "https://x.test/p", credits_used=next(seed_credits)
            ),
            request=req,
        )

    cfg = FirecrawlConfig(
        api_key_secret=_secret_ref(),
        seed_urls=["https://x.test/p1", "https://x.test/p2"],
        mode="scrape",
    )
    fc = _make_handler(handler, config=cfg)
    ctx = _ctx()
    await _collect(fc.pull(ctx))

    rec = fc.report_usage(ctx)
    assert isinstance(rec, CreditUsageRecord)
    assert rec.credits == 8
    assert rec.mode == "scrape"
    assert rec.source_id == ctx.source_id
    assert rec.target_id == ctx.target_id
    assert rec.recorded_at.tzinfo is not None


@pytest.mark.asyncio
async def test_lifetime_credits_persist_across_pulls():
    seed_credits = iter([2, 4])

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_scrape_response(
                "https://x.test/p", credits_used=next(seed_credits)
            ),
            request=req,
        )

    cfg = FirecrawlConfig(
        api_key_secret=_secret_ref(),
        seed_urls=["https://x.test/p"],
        mode="scrape",
    )
    state = InMemoryStateStore()

    fc1 = _make_handler(handler, config=cfg)
    await _collect(fc1.pull(_ctx(state)))
    assert state.snapshot()["firecrawl.lifetime_credits"] == 2

    # Fresh handler reads previously-persisted lifetime credits from state
    # and continues the running sum.
    fc2 = _make_handler(handler, config=cfg)
    await _collect(fc2.pull(_ctx(state)))
    assert state.snapshot()["firecrawl.lifetime_credits"] == 6


# ---------------------------------------------------------------------------
# State persists even on error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_persists_on_exception():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom", request=req)

    cfg = FirecrawlConfig(
        api_key_secret=_secret_ref(),
        seed_urls=["https://x.test/p"],
        mode="scrape",
    )
    state = InMemoryStateStore()
    fc = _make_handler(handler, config=cfg)

    with pytest.raises(FirecrawlAPIError):
        await _collect(fc.pull(_ctx(state)))

    # last_pull_at written even on failure (so the runtime knows the actor
    # ran).
    assert "firecrawl.last_pull_at" in state.snapshot()


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401_maps_to_auth_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key", request=req)

    fc = _make_handler(handler)
    with pytest.raises(FirecrawlAuthError):
        await _collect(fc.pull(_ctx()))


@pytest.mark.asyncio
async def test_403_maps_to_auth_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden", request=req)

    fc = _make_handler(handler)
    with pytest.raises(FirecrawlAuthError):
        await _collect(fc.pull(_ctx()))


@pytest.mark.asyncio
async def test_429_maps_to_rate_limited():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="too many", request=req)

    fc = _make_handler(handler)
    with pytest.raises(FirecrawlRateLimited):
        await _collect(fc.pull(_ctx()))


@pytest.mark.asyncio
async def test_500_maps_to_api_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down", request=req)

    fc = _make_handler(handler)
    with pytest.raises(FirecrawlAPIError):
        await _collect(fc.pull(_ctx()))


@pytest.mark.asyncio
async def test_success_false_body_is_hard_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": False, "error": "quota exceeded"},
            request=req,
        )

    fc = _make_handler(handler)
    with pytest.raises(FirecrawlAPIError) as exc_info:
        await _collect(fc.pull(_ctx()))
    assert "quota exceeded" in str(exc_info.value)


@pytest.mark.asyncio
async def test_non_json_body_is_api_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="<html>not json</html>", request=req
        )

    fc = _make_handler(handler)
    with pytest.raises(FirecrawlAPIError):
        await _collect(fc.pull(_ctx()))


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthcheck_healthy():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/scrape"
        import json
        body = json.loads(req.content)
        assert body["url"] == "https://example.com"
        return httpx.Response(
            200,
            json=_scrape_response(
                "https://example.com", credits_used=1
            ),
            request=req,
        )

    fc = _make_handler(handler)
    h = await fc.health_check(_ctx())
    assert isinstance(h, SourceHealth)
    assert h.state == "healthy"
    assert h.detail["healthcheck_credits"] == 1
    assert h.detail["api_base"].startswith("https://")


@pytest.mark.asyncio
async def test_healthcheck_auth_failure_is_unhealthy():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="nope", request=req)

    fc = _make_handler(handler)
    h = await fc.health_check(_ctx())
    assert h.state == "unhealthy"
    assert h.last_error and "auth" in h.last_error.lower()


@pytest.mark.asyncio
async def test_healthcheck_429_is_degraded():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down", request=req)

    fc = _make_handler(handler)
    h = await fc.health_check(_ctx())
    assert h.state == "degraded"
    assert h.detail.get("status") == 429


@pytest.mark.asyncio
async def test_healthcheck_5xx_is_degraded():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream", request=req)

    fc = _make_handler(handler)
    h = await fc.health_check(_ctx())
    assert h.state == "degraded"


@pytest.mark.asyncio
async def test_healthcheck_last_success_from_state_store():
    """When the handler has no in-memory last_success_at but the state
    store has a prior ``last_pull_at`` value, the healthcheck reads
    through to it."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_scrape_response("https://example.com", credits_used=1),
            request=req,
        )

    iso = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc).isoformat()
    state = InMemoryStateStore(initial={"firecrawl.last_pull_at": iso})
    fc = _make_handler(handler)
    h = await fc.health_check(_ctx(state))
    assert h.state == "healthy"
    assert h.last_success_at is not None
    assert h.last_success_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Live integration — gated
# ---------------------------------------------------------------------------


LIVE_API_KEY = os.getenv("LEGBA_FIRECRAWL_API_KEY")


@pytest.mark.integration
@pytest.mark.skipif(
    not LIVE_API_KEY,
    reason="LEGBA_FIRECRAWL_API_KEY not set; paid integration test",
)
@pytest.mark.asyncio
async def test_live_scrape_example_com():
    """One paid scrape against example.com to validate end-to-end wiring."""
    cfg = FirecrawlConfig(
        api_key_secret=_secret_ref("vault.live_firecrawl"),
        seed_urls=["https://example.com"],
        mode="scrape",
    )

    async def resolver(_ref: SecretRef) -> str:
        return LIVE_API_KEY  # type: ignore[return-value]

    fc = FirecrawlSourceHandler(cfg, secret_resolver=resolver)
    try:
        sigs = await _collect(fc.pull(_ctx()))
        assert sigs, "expected at least one Signal from live scrape"
        s = sigs[0]
        assert s.payload["url"].startswith("https://example.com")
        assert s.payload["raw_body"], "expected non-empty markdown"
        rec = fc.report_usage(_ctx())
        assert rec.credits >= 1
    finally:
        await fc._maybe_close_client()  # noqa: SLF001 — test cleanup
