# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for :class:`legba.data.sources.mediacloud.MediaCloudSourceHandler`
(L-133).

Coverage:

  * Config schema validation (required api_key_secret + query, defaults,
    rejection of unknown fields).
  * L-102 source-kind class-var contract conformance.
  * Happy path: single page → multiple Signals, payload shape, cursor
    persistence, health record on success.
  * Pagination: multi-page sweep — pagination_token flows through and the
    state store carries the *last* token across calls.
  * Cursor resume: a prior persisted token is replayed verbatim.
  * 429 rate-limit: backs off + retries; raises ``MediaCloudRateLimited``
    after exhausting retries; ``Retry-After`` header honoured.
  * 4xx hard failure: surfaces ``MediaCloudHttpError``.
  * Authorization header: contains ``Token <api_key>`` from the resolver.
  * Missing ``story_text`` → optional full-text fetch fires and populates
    ``raw_body``.
  * ``fetch_missing_text=False`` short-circuits the full-text fetch.
  * Healthcheck: hits the tiny probe endpoint, surfaces the rate-limit
    header; unhealthy on probe failure.
  * Live integration: gated on ``LEGBA_MEDIACLOUD_API_KEY`` env var.

We mock httpx via ``httpx.MockTransport`` so the handler exercises real
``httpx.AsyncClient`` machinery against a deterministic transport.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
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
from legba.data.sources.mediacloud import (
    MediaCloudConfig,
    MediaCloudHttpError,
    MediaCloudRateLimited,
    MediaCloudSourceHandler,
)


# Real src bug surfaced by the source-first pivot (commit fa3e598): the pivot
# re-cut the Signal model (src/legba/data/sources/_contract.py) to drop
# ``target_id`` and set ``extra='forbid'`` (observations are now source-owned,
# not target-owned), but the source handlers were NOT updated — every handler
# still calls ``Signal(..., target_id=ctx.target_id, ...)``. So the very first
# Signal a ``pull()`` constructs raises
# ``ValidationError: target_id Extra inputs are not permitted``. This is a bug
# in src (mediacloud.py:460), not a stale-test/schema issue, so per the
# migration constraints it is FLAGGED in real_src_bugs_flagged and the
# pull-exercising tests are skipped (do not edit src to mask it). See
# PIVOT_BUILD_PLAN.
_SRC_BUG_TARGET_ID = (
    "src bug (pivot fa3e598, mediacloud.py:460): handler still passes "
    "target_id=ctx.target_id into the pivoted Signal model (extra='forbid', "
    "target_id dropped) so pull() raises ValidationError. Flagged in "
    "real_src_bugs_flagged; src not edited. See PIVOT_BUILD_PLAN."
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _ctx(
    state: InMemoryStateStore | None = None,
    config: MediaCloudConfig | None = None,
) -> SourceContext:
    return SourceContext(
        target_id="target.india_energy",
        target_version="v-test",
        source_id="src.mediacloud_br",
        config=config or MediaCloudConfig(
            api_key_secret=_secret_ref(), query="energy AND brazil"
        ),
        state_store=state or InMemoryStateStore(),
        scope_geo=["BR"],
        scope_languages=["pt", "en"],
    )


def _secret_ref(name: str = "vault.mediacloud_key") -> SecretRef:
    return SecretRef(raw=name)


def _make_handler(
    transport_handler: Callable[[httpx.Request], httpx.Response],
    *,
    config: MediaCloudConfig | None = None,
    api_key: str = "test-key-xyz",
) -> MediaCloudSourceHandler:
    transport = httpx.MockTransport(transport_handler)
    client = httpx.AsyncClient(
        transport=transport,
        timeout=5,
    )
    cfg = config or MediaCloudConfig(
        api_key_secret=_secret_ref(),
        query="energy AND brazil",
        lookback_days=3,
    )

    async def resolver(_ref: SecretRef) -> str:
        return api_key

    return MediaCloudSourceHandler(
        cfg, secret_resolver=resolver, http_client=client
    )


async def _collect(agen):
    out: list[Signal] = []
    async for s in agen:
        out.append(s)
    return out


def _story(stories_id: int, **overrides) -> dict:
    base = {
        "stories_id": stories_id,
        "title": f"Story {stories_id}",
        "url": f"https://news.example.invalid/story/{stories_id}",
        "publish_date": "2026-05-15T12:00:00Z",
        "media_id": 100 + stories_id % 7,
        "media_name": "Example Media",
        "language": "en",
        "story_text": f"Body of story {stories_id}.",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Config + class-var contract
# ---------------------------------------------------------------------------


def test_config_defaults():
    cfg = MediaCloudConfig(
        api_key_secret=_secret_ref(), query="ai AND policy"
    )
    assert cfg.lookback_days == 1
    assert cfg.page_size == 1000
    assert cfg.api_base_url.startswith("https://")
    assert cfg.rate_limit_max_retries == 4
    assert cfg.fetch_missing_text is True


def test_config_rejects_blank_query():
    with pytest.raises(ValidationError):
        MediaCloudConfig(api_key_secret=_secret_ref(), query="")


def test_config_rejects_unknown_field():
    with pytest.raises(ValidationError):
        MediaCloudConfig(
            api_key_secret=_secret_ref(),
            query="x",
            what="ever",  # type: ignore[call-arg]
        )


def test_config_collections_and_language():
    cfg = MediaCloudConfig(
        api_key_secret=_secret_ref(),
        query="x",
        collections=[34412234, 34412409],
        language="pt",
    )
    assert cfg.collections == [34412234, 34412409]
    assert cfg.language == "pt"


def test_handler_class_contract():
    assert MediaCloudSourceHandler.kind == "mediacloud"
    assert MediaCloudSourceHandler.family == "source"
    assert MediaCloudSourceHandler.schema_version == "legba/source.mediacloud/1-0-0"
    assert MediaCloudSourceHandler.config_schema is MediaCloudConfig
    assert MediaCloudSourceHandler.handler_version


# ---------------------------------------------------------------------------
# Happy path — single page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_single_page_yields_signals():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(
            200,
            json={
                "stories": [_story(1), _story(2), _story(3)],
                "pagination_token": None,
            },
            headers={"X-RateLimit-Remaining": "999"},
            request=req,
        )

    state = InMemoryStateStore()
    ctx = _ctx(state)
    mc = _make_handler(handler)

    signals = await _collect(mc.pull(ctx, since=None))

    assert len(signals) == 3
    for s in signals:
        assert isinstance(s, Signal)
        assert s.source_id == "src.mediacloud_br"
        # Source-first pivot: Signal is target-agnostic (target_id dropped).
        assert s.content_hash
        assert s.canonical_url
        # External-id present + url + language hint
        assert s.payload["external_id"]
        assert s.payload["url"].startswith("https://")
        assert s.language_hint == "en"
        # raw_provenance contains MC story id + media id
        assert s.raw_provenance["mediacloud_stories_id"]

    # Cursor persisted with no more token (end of stream).
    cursor = state.snapshot().get("mediacloud_cursor")
    assert cursor is not None
    assert cursor["pagination_token"] is None
    assert cursor["start_date"]

    # Auth header transmitted on the request.
    assert captured
    assert captured[0].headers["Authorization"] == "Token test-key-xyz"
    # Query string included our config query.
    assert "q=" in str(captured[0].url) and "energy" in str(captured[0].url)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_paginates_until_token_none():
    """First response has a pagination_token → handler issues a second
    request → second response has token=None → loop exits."""
    call_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        token = req.url.params.get("pagination_token")
        if call_count["n"] == 1:
            assert token is None
            return httpx.Response(
                200,
                json={
                    "stories": [_story(1), _story(2)],
                    "pagination_token": "TOKEN-PAGE-2",
                },
                request=req,
            )
        else:
            assert token == "TOKEN-PAGE-2"
            return httpx.Response(
                200,
                json={
                    "stories": [_story(3)],
                    "pagination_token": None,
                },
                request=req,
            )

    state = InMemoryStateStore()
    ctx = _ctx(state)
    mc = _make_handler(handler)
    sigs = await _collect(mc.pull(ctx, since=None))

    assert call_count["n"] == 2
    assert {s.payload["external_id"] for s in sigs} == {"1", "2", "3"}

    cursor = state.snapshot()["mediacloud_cursor"]
    assert cursor["pagination_token"] is None


@pytest.mark.asyncio
async def test_pull_resumes_from_persisted_cursor():
    """If state holds a pagination_token matching the same start_date the
    handler resumes by passing the token verbatim on the first request."""
    today = datetime.now(tz=timezone.utc).date().isoformat()
    state = InMemoryStateStore(
        initial={
            "mediacloud_cursor": {
                "start_date": today,
                "pagination_token": "RESUME-TOK",
                "last_fetched_at": "2026-05-14T00:00:00+00:00",
            }
        }
    )

    seen_tokens: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_tokens.append(req.url.params.get("pagination_token"))
        return httpx.Response(
            200,
            json={"stories": [_story(42)], "pagination_token": None},
            request=req,
        )

    # Use a since matching today's date so start_date == cursor's start_date.
    since = datetime.combine(
        datetime.now(tz=timezone.utc).date(),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )

    ctx = _ctx(state)
    mc = _make_handler(handler)
    sigs = await _collect(mc.pull(ctx, since=since))

    assert len(sigs) == 1
    assert seen_tokens[0] == "RESUME-TOK"


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_429_backs_off_then_succeeds(monkeypatch):
    """First call returns 429 with Retry-After=0.01, second returns 200."""
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    n = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(
                429,
                json={"error": "slow down"},
                headers={"Retry-After": "0.01"},
                request=req,
            )
        return httpx.Response(
            200,
            json={"stories": [_story(7)], "pagination_token": None},
            request=req,
        )

    ctx = _ctx()
    mc = _make_handler(handler)
    sigs = await _collect(mc.pull(ctx, since=None))

    assert len(sigs) == 1
    # We slept at least once for the 429 backoff.
    assert sleeps, "expected at least one asyncio.sleep on 429 backoff"
    assert sleeps[0] == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_pull_429_exhaustion_raises(monkeypatch):
    """All retries 429 → raises MediaCloudRateLimited."""
    async def fake_sleep(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": "still slow"},
            headers={"Retry-After": "0"},
            request=req,
        )

    cfg = MediaCloudConfig(
        api_key_secret=_secret_ref(),
        query="x",
        rate_limit_max_retries=1,  # allow 1 retry then give up
    )
    mc = _make_handler(handler, config=cfg)
    ctx = _ctx()

    with pytest.raises(MediaCloudRateLimited):
        await _collect(mc.pull(ctx, since=None))


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_4xx_raises_http_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": "forbidden"},
            request=req,
        )

    mc = _make_handler(handler)
    ctx = _ctx()

    with pytest.raises(MediaCloudHttpError) as ei:
        await _collect(mc.pull(ctx, since=None))
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_pull_skips_malformed_story():
    """Stories missing both stories_id and url-or-title are dropped."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "stories": [
                    {},  # entirely empty — dropped.
                    {"stories_id": 10},  # no url + no title — dropped.
                    _story(11),  # valid.
                ],
                "pagination_token": None,
            },
            request=req,
        )

    mc = _make_handler(handler)
    sigs = await _collect(mc.pull(_ctx(), since=None))
    assert len(sigs) == 1
    assert sigs[0].payload["external_id"] == "11"


# ---------------------------------------------------------------------------
# Full-text fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_story_text_triggers_full_text_fetch():
    """If a story has no story_text and fetch_missing_text=True, the
    handler issues a GET for the URL and uses the body as raw_body."""
    def handler(req: httpx.Request) -> httpx.Response:
        if "story-list" in str(req.url):
            # Strip story_text so the handler falls back to URL fetch.
            return httpx.Response(
                200,
                json={
                    "stories": [
                        _story(50, story_text="", url="https://news.example.invalid/story/50"),
                    ],
                    "pagination_token": None,
                },
                request=req,
            )
        # The URL fetch.
        return httpx.Response(
            200,
            text="<html><body>Article body html</body></html>",
            request=req,
        )

    mc = _make_handler(handler)
    sigs = await _collect(mc.pull(_ctx(), since=None))
    assert len(sigs) == 1
    assert "Article body html" in sigs[0].payload["raw_body"]
    assert sigs[0].raw_provenance["fetched_full_text"] is True


@pytest.mark.asyncio
async def test_fetch_missing_text_disabled_leaves_body_empty():
    def handler(req: httpx.Request) -> httpx.Response:
        if "story-list" in str(req.url):
            return httpx.Response(
                200,
                json={
                    "stories": [_story(60, story_text="")],
                    "pagination_token": None,
                },
                request=req,
            )
        pytest.fail(f"unexpected request to {req.url}")

    cfg = MediaCloudConfig(
        api_key_secret=_secret_ref(),
        query="x",
        fetch_missing_text=False,
    )
    mc = _make_handler(handler, config=cfg)
    sigs = await _collect(mc.pull(_ctx(), since=None))
    assert sigs[0].payload["raw_body"] == ""


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_healthy_records_rate_limit():
    def handler(req: httpx.Request) -> httpx.Response:
        # Probe should use limit=1 + q=*.
        assert req.url.params.get("limit") == "1"
        assert req.url.params.get("q") == "*"
        return httpx.Response(
            200,
            json={"stories": [], "pagination_token": None},
            headers={"X-RateLimit-Remaining": "750"},
            request=req,
        )

    mc = _make_handler(handler)
    h = await mc.health_check(_ctx())

    assert isinstance(h, SourceHealth)
    assert h.state == "healthy"
    assert h.rate_limit_remaining == 750


@pytest.mark.asyncio
async def test_health_check_unhealthy_on_4xx():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": "unauthorized"}, request=req
        )

    mc = _make_handler(handler)
    h = await mc.health_check(_ctx())
    assert h.state == "unhealthy"
    assert "401" in (h.last_error or "")


@pytest.mark.asyncio
async def test_health_check_degraded_on_rate_limit(monkeypatch):
    async def fake_sleep(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": "slow"},
            headers={"Retry-After": "0"},
            request=req,
        )

    mc = _make_handler(handler)
    h = await mc.health_check(_ctx())
    # Probe disables retries so we go straight to MediaCloudRateLimited
    # which surfaces as degraded.
    assert h.state == "degraded"


# ---------------------------------------------------------------------------
# Credential plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_resolver_wired_raises_runtime_error():
    cfg = MediaCloudConfig(api_key_secret=_secret_ref(), query="x")
    mc = MediaCloudSourceHandler(cfg)  # no resolver, no client
    with pytest.raises(RuntimeError):
        await _collect(mc.pull(_ctx(), since=None))


@pytest.mark.asyncio
async def test_resolver_called_each_pull():
    calls = {"n": 0}

    async def resolver(_ref):
        calls["n"] += 1
        return "key-from-resolver"

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Token key-from-resolver"
        return httpx.Response(
            200, json={"stories": [], "pagination_token": None}, request=req
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, timeout=5)
    cfg = MediaCloudConfig(api_key_secret=_secret_ref(), query="x")
    mc = MediaCloudSourceHandler(cfg, secret_resolver=resolver, http_client=client)

    await _collect(mc.pull(_ctx(), since=None))
    await _collect(mc.pull(_ctx(), since=None))
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Lookback / since handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_since_overrides_lookback():
    captured: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req.url.params.get("start_date"))
        return httpx.Response(
            200, json={"stories": [], "pagination_token": None}, request=req
        )

    mc = _make_handler(handler)
    since = datetime(2024, 1, 15, tzinfo=timezone.utc)
    await _collect(mc.pull(_ctx(), since=since))
    assert captured == ["2024-01-15"]


@pytest.mark.asyncio
async def test_default_start_date_uses_lookback():
    captured: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req.url.params.get("start_date"))
        return httpx.Response(
            200, json={"stories": [], "pagination_token": None}, request=req
        )

    cfg = MediaCloudConfig(
        api_key_secret=_secret_ref(), query="x", lookback_days=7
    )
    mc = _make_handler(handler, config=cfg)
    await _collect(mc.pull(_ctx(), since=None))

    expected = (
        datetime.now(tz=timezone.utc) - timedelta(days=7)
    ).date().isoformat()
    assert captured == [expected]


# ---------------------------------------------------------------------------
# Live integration (opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_mediacloud_probe():
    """Real-network probe. Only runs when LEGBA_MEDIACLOUD_API_KEY is set.

    Confirms credentials + reachability against the real MediaCloud API by
    issuing the healthcheck probe (limit 1)."""
    api_key = os.environ.get("LEGBA_MEDIACLOUD_API_KEY")
    if not api_key:
        pytest.skip("LEGBA_MEDIACLOUD_API_KEY not set")

    cfg = MediaCloudConfig(
        api_key_secret=_secret_ref("env.mediacloud"),
        query="climate",
        lookback_days=1,
    )

    async def resolver(_ref):
        return api_key

    mc = MediaCloudSourceHandler(cfg, secret_resolver=resolver)
    try:
        h = await mc.health_check(_ctx())
    finally:
        await mc._maybe_close_client()

    assert h.state in ("healthy", "degraded"), (
        f"unexpected state {h.state}: {h.last_error}"
    )
