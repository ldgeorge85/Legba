# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the L-151 4-tier dedupe filter handler.

Layout:

  * Unit tests for the per-tier primitives (URL canonicalization,
    normalized content hashing, normalized Levenshtein distance,
    normalized title).
  * Tier-by-tier behavioral tests using a process-local Redis fake +
    process-local Qdrant fake + deterministic embedder. Each tier is
    exercised independently with the other three disabled.
  * Integration test against the real Redis container + real Qdrant
    container the conftest brings up. Uses a deterministic per-token
    embedder (no external model — L-122's BGE-M3 may not be live yet at
    handler-merge time; the production embedder slots in as a typed
    sub-connection without changing this test).
  * 5-signal end-to-end test: original + URL-dup + content-near-dup +
    semantic-dup + temporal-dup → asserts dedupe_tier annotations.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

import pytest
import pytest_asyncio

from legba.data.filters._contract import FilterContext
from legba.data.filters.dedupe import (
    Dedupe4TierConfig,
    Dedupe4TierHandler,
    EmbeddingService,
    QdrantLike,
    RedisLike,
    Tier3Config,
    Tier4Config,
    TierToggle,
    _normalized_levenshtein,
    _safe_name,
)
from legba.data.sources._contract import Signal


# ---------------------------------------------------------------------------
# Process-local fakes for unit + tier-isolation tests
# ---------------------------------------------------------------------------


class FakeRedis:
    """Minimal in-process Redis satisfying :class:`RedisLike`.

    Supports the subset of commands the dedupe handler uses: ``get``,
    ``set`` (with optional ``ex`` TTL — ignored for tests; we don't
    simulate TTL expiration), ``expire`` (no-op), ``zadd``,
    ``zrangebyscore``, ``zremrangebyscore``.
    """

    def __init__(self) -> None:
        self._kv: dict[str, bytes] = {}
        self._zset: dict[str, dict[str, float]] = {}

    async def get(self, name: str) -> Any:
        return self._kv.get(name)

    async def set(self, name: str, value: Any, ex: int | None = None) -> Any:
        if isinstance(value, str):
            value = value.encode("utf-8")
        elif not isinstance(value, (bytes, bytearray)):
            value = str(value).encode("utf-8")
        self._kv[name] = bytes(value)
        return True

    async def expire(self, name: str, seconds: int) -> bool:
        return True

    async def zadd(self, name: str, mapping: Mapping[str, float]) -> int:
        bucket = self._zset.setdefault(name, {})
        added = 0
        for member, score in mapping.items():
            if member not in bucket:
                added += 1
            bucket[member] = score
        return added

    async def zrangebyscore(
        self,
        name: str,
        min: float,
        max: float,
        withscores: bool = False,
    ) -> list:
        bucket = self._zset.get(name, {})
        members = [(m, s) for m, s in bucket.items() if min <= s <= max]
        members.sort(key=lambda x: x[1])
        if withscores:
            return [(m.encode("utf-8"), s) for m, s in members]
        return [m.encode("utf-8") for m, _ in members]

    async def zremrangebyscore(
        self, name: str, min: float, max: float
    ) -> int:
        bucket = self._zset.get(name)
        if not bucket:
            return 0
        to_remove = [m for m, s in bucket.items() if min <= s <= max]
        for m in to_remove:
            del bucket[m]
        return len(to_remove)


class FakeQdrantHit:
    def __init__(self, point_id: str, score: float, payload: dict[str, Any]):
        self.id = point_id
        self.score = score
        self.payload = payload


class FakeQdrantCollections:
    def __init__(self, names: list[str]):
        self.collections = [type("C", (), {"name": n})() for n in names]


class FakeQdrantQueryResponse:
    def __init__(self, points: list):
        self.points = points


class FakeQdrant:
    """In-process Qdrant fake supporting create_collection + upsert + query_points."""

    def __init__(self) -> None:
        self._collections: dict[str, list[tuple[str, list[float], dict]]] = {}

    async def get_collections(self) -> FakeQdrantCollections:
        return FakeQdrantCollections(list(self._collections.keys()))

    async def create_collection(self, **kwargs: Any) -> Any:
        name = kwargs.get("collection_name")
        if name and name not in self._collections:
            self._collections[name] = []
        return True

    async def upsert(self, **kwargs: Any) -> Any:
        name = kwargs["collection_name"]
        points = kwargs["points"]
        bucket = self._collections.setdefault(name, [])
        for p in points:
            bucket.append((str(p.id), list(p.vector), dict(p.payload or {})))
        return True

    async def query_points(self, **kwargs: Any) -> FakeQdrantQueryResponse:
        name = kwargs["collection_name"]
        query = list(kwargs["query"])
        threshold = kwargs.get("score_threshold", 0.0)
        limit = kwargs.get("limit", 10)
        bucket = self._collections.get(name, [])
        scored: list[FakeQdrantHit] = []
        for pid, vec, payload in bucket:
            score = _cosine(query, vec)
            if score >= threshold:
                scored.append(FakeQdrantHit(pid, score, payload))
        scored.sort(key=lambda h: h.score, reverse=True)
        return FakeQdrantQueryResponse(scored[:limit])


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        # Pad shorter to longer with zeros so dimension mismatches don't error.
        if len(a) < len(b):
            a = a + [0.0] * (len(b) - len(a))
        else:
            b = b + [0.0] * (len(a) - len(b))
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class DeterministicEmbedder:
    """Token-bag embedder for deterministic similarity tests.

    Each token hashes to a stable dim index; weights are uniform. Two
    texts sharing most tokens land at high cosine similarity; texts with
    no token overlap land near zero. Good enough to exercise the Tier 3
    code path end-to-end without requiring the production BGE-M3 model.
    """

    def __init__(self, dim: int = 1024):
        self._dim = dim

    async def embed(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for token in _tokenize(text):
            idx = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % self._dim
            vec[idx] += 1.0
        # L2-normalize so cosine == dot product.
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


def _tokenize(text: str) -> list[str]:
    return [t for t in "".join(
        c.lower() if c.isalnum() else " " for c in text
    ).split() if len(t) > 1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(target_id: str = "target-001") -> FilterContext:
    return FilterContext(
        target_id=target_id,
        target_version="0.1.0",
        filter_id="dedupe-001",
    )


def _signal(
    *,
    title: str = "",
    url: str = "",
    summary: str = "",
    body: str = "",
    source_id: str = "src-001",
    external_id: str | None = None,
    target_id: str = "target-001",
) -> Signal:
    # Source-first pivot: Signal is source-owned. ``target_id`` is no longer
    # a Signal field (it moved to the FilterContext / derived target outputs).
    # The param is kept on this helper for call-site compatibility but is no
    # longer forwarded to the model. See migration the source-first pivot.
    ext = external_id if external_id is not None else (url or title or str(uuid4()))
    payload: dict[str, Any] = {
        "title": title,
        "summary": summary,
        "body": body,
        "external_id": ext,
        "url": url,
        "link": url,
    }
    return Signal(
        source_id=source_id,
        payload=payload,
        canonical_url=url or None,
        content_hash="",
    )


def _config(
    *,
    tier1: bool = True,
    tier2: bool = True,
    tier3: bool = True,
    tier4: bool = True,
    tier3_threshold: float = 0.92,
    tier4_distance_threshold: float = 0.15,
    tier4_window_hours: int = 24,
    embedding_dim: int = 1024,
) -> Dedupe4TierConfig:
    return Dedupe4TierConfig(
        tier1=TierToggle(enabled=tier1),
        tier2=TierToggle(enabled=tier2),
        tier3=Tier3Config(
            enabled=tier3,
            threshold=tier3_threshold,
            embedding_dim=embedding_dim,
        ),
        tier4=Tier4Config(
            enabled=tier4,
            window_hours=tier4_window_hours,
            distance_threshold=tier4_distance_threshold,
        ),
    )


# =============================================================================
# Pure-unit tests — no I/O
# =============================================================================


def test_canonical_url_strips_fragment_and_sorts_query():
    canonical = Dedupe4TierHandler.canonical_url(
        "HTTPS://Example.COM/Path?b=2&a=1#frag"
    )
    assert canonical == "https://example.com/Path?a=1&b=2"


def test_canonical_url_strips_default_port():
    assert (
        Dedupe4TierHandler.canonical_url("http://example.com:80/x")
        == "http://example.com/x"
    )
    assert (
        Dedupe4TierHandler.canonical_url("https://example.com:443/x")
        == "https://example.com/x"
    )
    assert (
        Dedupe4TierHandler.canonical_url("http://example.com:8080/x")
        == "http://example.com:8080/x"
    )


def test_canonical_url_drops_empty_query_values():
    assert (
        Dedupe4TierHandler.canonical_url("https://example.com/x?a=&b=2")
        == "https://example.com/x?b=2"
    )


def test_canonical_url_idempotent():
    once = Dedupe4TierHandler.canonical_url(
        "HTTPS://EXAMPLE.com/Path?z=9&a=1#x"
    )
    twice = Dedupe4TierHandler.canonical_url(once)
    assert once == twice


def test_canonical_url_empty():
    assert Dedupe4TierHandler.canonical_url("") == ""


def test_canonical_url_preserves_path_case():
    """Paths are case-sensitive per RFC 3986."""
    assert "/Path/Sub" in Dedupe4TierHandler.canonical_url(
        "https://example.com/Path/Sub"
    )


def test_normalized_content_strips_html():
    sig = _signal(
        title="Title",
        body="<p>Hello <b>world</b></p>  ",
    )
    text = Dedupe4TierHandler.normalized_content(sig)
    assert text == "title hello world"
    assert "<" not in text and ">" not in text


def test_normalized_content_collapses_whitespace():
    sig = _signal(title="A  B\t\tC", summary="\n\nD")
    text = Dedupe4TierHandler.normalized_content(sig)
    assert "  " not in text
    assert text == "a b c d"


def test_normalized_content_lowercase():
    sig = _signal(title="HELLO World")
    text = Dedupe4TierHandler.normalized_content(sig)
    assert text == "hello world"


def test_normalized_title_strips_html_and_lowercases():
    assert Dedupe4TierHandler.normalized_title("  Hello <b>World</b>  ") == "hello world"
    assert Dedupe4TierHandler.normalized_title("") == ""


def test_levenshtein_identical():
    assert _normalized_levenshtein("hello world", "hello world") == 0.0


def test_levenshtein_one_char_diff():
    # "hello" vs "hellp" — 1 substitution out of 5 chars => 0.2
    d = _normalized_levenshtein("hello", "hellp")
    assert 0.19 < d < 0.21


def test_levenshtein_completely_different():
    # No char overlap => max distance == 1.0
    d = _normalized_levenshtein("abc", "xyz")
    assert d == 1.0


def test_levenshtein_empty():
    assert _normalized_levenshtein("", "") == 0.0
    assert _normalized_levenshtein("hello", "") == 1.0
    assert _normalized_levenshtein("", "world") == 1.0


def test_safe_name_replaces_non_alnum():
    assert _safe_name("foo/bar:baz") == "foo_bar_baz"
    assert _safe_name("hello.world") == "hello_world"
    assert _safe_name("") == "unknown"
    assert _safe_name("a-b_c") == "a-b_c"  # hyphens + underscores preserved


# =============================================================================
# Tier-isolation behavioral tests (fakes; no containers)
# =============================================================================


@pytest.mark.asyncio
async def test_tier1_url_exact_match():
    """Same canonicalized URL → Tier 1 hit."""
    redis = FakeRedis()
    handler = Dedupe4TierHandler(
        _config(tier2=False, tier3=False, tier4=False),
        redis=redis,
    )
    ctx = _ctx()

    s1 = _signal(title="A", url="https://example.com/x?a=1&b=2", external_id="ext-1")
    s2 = _signal(
        # Different fragment / query order / case — same canonical URL.
        title="A different title",
        url="HTTPS://Example.com/x?b=2&a=1#frag",
        external_id="ext-2",
    )

    r1 = await handler.transform(s1, ctx)
    assert r1 is not None
    assert "dedupe_tier" not in r1.payload

    r2 = await handler.transform(s2, ctx)
    assert r2 is not None
    assert r2.payload.get("dedupe_tier") == 1
    assert r2.payload.get("duplicate_of") == "ext-1"


@pytest.mark.asyncio
async def test_tier1_no_match_on_distinct_urls():
    redis = FakeRedis()
    handler = Dedupe4TierHandler(
        _config(tier2=False, tier3=False, tier4=False),
        redis=redis,
    )
    ctx = _ctx()

    s1 = _signal(url="https://a.example/x")
    s2 = _signal(url="https://b.example/x")

    await handler.transform(s1, ctx)
    out = await handler.transform(s2, ctx)
    assert out is not None
    assert "dedupe_tier" not in out.payload


@pytest.mark.asyncio
async def test_tier1_disabled_skips_url_check():
    redis = FakeRedis()
    handler = Dedupe4TierHandler(
        _config(tier1=False, tier2=False, tier3=False, tier4=False),
        redis=redis,
    )
    ctx = _ctx()
    s1 = _signal(url="https://example.com/x")
    s2 = _signal(url="https://example.com/x", external_id="ext-2", title="b")
    await handler.transform(s1, ctx)
    out = await handler.transform(s2, ctx)
    assert "dedupe_tier" not in out.payload


@pytest.mark.asyncio
async def test_tier2_content_exact_match():
    """Identical normalized body text → Tier 2 hit (URL differs)."""
    redis = FakeRedis()
    handler = Dedupe4TierHandler(
        _config(tier3=False, tier4=False),
        redis=redis,
    )
    ctx = _ctx()

    body = "<p>Earthquake magnitude 6.2 strikes off the coast of Chile</p>"
    s1 = _signal(
        title="Quake hits Chile",
        body=body,
        url="https://a.example/news/1",
        external_id="ext-1",
    )
    s2 = _signal(
        title="QUAKE HITS CHILE",      # same after normalization
        body=body.upper(),              # same content after lowercase
        url="https://b.example/news/2", # different URL — Tier 1 misses
        external_id="ext-2",
    )
    await handler.transform(s1, ctx)
    out = await handler.transform(s2, ctx)
    assert out.payload.get("dedupe_tier") == 2
    assert out.payload.get("duplicate_of") == "ext-1"


@pytest.mark.asyncio
async def test_tier2_no_match_on_distinct_content():
    redis = FakeRedis()
    handler = Dedupe4TierHandler(
        _config(tier3=False, tier4=False), redis=redis,
    )
    ctx = _ctx()
    s1 = _signal(title="Eruption in Iceland", body="lava flow continues")
    s2 = _signal(
        title="Solar flare disrupts comms",
        body="X-class solar flare detected",
        url="https://x.example/y",
    )
    await handler.transform(s1, ctx)
    out = await handler.transform(s2, ctx)
    assert "dedupe_tier" not in out.payload


@pytest.mark.asyncio
async def test_tier3_semantic_match_via_fake_embedder():
    """Tier 1 + 2 miss; Tier 3 hits via vector cosine on a fake embedder."""
    redis = FakeRedis()
    qdrant = FakeQdrant()
    embedder = DeterministicEmbedder(dim=512)
    handler = Dedupe4TierHandler(
        _config(tier4=False, embedding_dim=512, tier3_threshold=0.6),
        redis=redis,
        qdrant=qdrant,
        embedder=embedder,
    )
    ctx = _ctx()

    s1 = _signal(
        title="Hurricane Lima makes landfall in Florida",
        summary="Category 4 storm with sustained winds of 145 mph",
        url="https://a.example/lima",
        external_id="ext-1",
    )
    # Sharing-most-tokens variant — same canonical URL is different,
    # different normalized body, but token-bag overlaps strongly.
    s2 = _signal(
        title="Hurricane Lima landfall Florida category 4",
        summary="storm with sustained winds 145 mph reaches the coast",
        url="https://b.example/lima2",
        external_id="ext-2",
    )

    await handler.transform(s1, ctx)
    out = await handler.transform(s2, ctx)
    assert out.payload.get("dedupe_tier") == 3
    assert out.payload.get("duplicate_of") == "ext-1"


@pytest.mark.asyncio
async def test_tier3_no_match_below_threshold():
    redis = FakeRedis()
    qdrant = FakeQdrant()
    embedder = DeterministicEmbedder(dim=512)
    handler = Dedupe4TierHandler(
        _config(tier4=False, embedding_dim=512, tier3_threshold=0.99),
        redis=redis,
        qdrant=qdrant,
        embedder=embedder,
    )
    ctx = _ctx()
    s1 = _signal(title="apple banana cherry", url="https://a/1", external_id="ext-1")
    s2 = _signal(title="dog elephant frog", url="https://a/2", external_id="ext-2")
    await handler.transform(s1, ctx)
    out = await handler.transform(s2, ctx)
    assert "dedupe_tier" not in out.payload


@pytest.mark.asyncio
async def test_tier3_disabled_when_no_embedder_or_qdrant():
    """Tier 3 silently skipped when embedder or qdrant not supplied."""
    redis = FakeRedis()
    handler = Dedupe4TierHandler(
        _config(tier4=False), redis=redis,  # no qdrant + embedder
    )
    ctx = _ctx()
    s1 = _signal(title="x", url="https://a/1")
    s2 = _signal(title="y", url="https://a/2")
    await handler.transform(s1, ctx)
    out = await handler.transform(s2, ctx)
    # No tier annotation since 3 is skipped (missing deps).
    assert "dedupe_tier" not in out.payload


@pytest.mark.asyncio
async def test_tier4_temporal_match_same_source_similar_title():
    """Same source, near-identical title, within window → Tier 4 hit."""
    redis = FakeRedis()
    fake_time = [1_700_000_000.0]
    handler = Dedupe4TierHandler(
        _config(
            tier1=False, tier2=False, tier3=False,
            tier4_window_hours=6,
            tier4_distance_threshold=0.15,
        ),
        redis=redis,
        clock=lambda: fake_time[0],
    )
    ctx = _ctx()

    s1 = _signal(
        title="Tropical storm Alpha approaches Cuba",
        source_id="news-src-1",
        external_id="ext-1",
    )
    # Same source, 1-char-different title, 2 hours later.
    s2 = _signal(
        title="Tropical storm Alpha approaches Cuba.",  # period appended
        source_id="news-src-1",
        external_id="ext-2",
        url="https://different.example/x",  # Tier 1 must not catch
    )

    await handler.transform(s1, ctx)
    fake_time[0] += 2 * 3600.0
    out = await handler.transform(s2, ctx)
    assert out.payload.get("dedupe_tier") == 4
    assert out.payload.get("duplicate_of") == "ext-1"


@pytest.mark.asyncio
async def test_tier4_outside_window_no_match():
    redis = FakeRedis()
    fake_time = [1_700_000_000.0]
    handler = Dedupe4TierHandler(
        _config(
            tier1=False, tier2=False, tier3=False,
            tier4_window_hours=6,
        ),
        redis=redis,
        clock=lambda: fake_time[0],
    )
    ctx = _ctx()
    s1 = _signal(title="Quake felt in Tokyo", source_id="src", external_id="ext-1")
    await handler.transform(s1, ctx)
    fake_time[0] += 7 * 3600.0  # 7h > 6h window
    s2 = _signal(title="Quake felt in Tokyo.", source_id="src",
                 external_id="ext-2", url="https://x/y")
    out = await handler.transform(s2, ctx)
    assert "dedupe_tier" not in out.payload


@pytest.mark.asyncio
async def test_tier4_different_source_no_match():
    redis = FakeRedis()
    handler = Dedupe4TierHandler(
        _config(tier1=False, tier2=False, tier3=False),
        redis=redis,
    )
    ctx = _ctx()
    s1 = _signal(title="Aurora visible over Norway", source_id="src-a", external_id="ext-1")
    s2 = _signal(
        title="Aurora visible over Norway",
        source_id="src-b",                # different source — bypasses Tier 4
        external_id="ext-2",
        url="https://different/x",
    )
    await handler.transform(s1, ctx)
    out = await handler.transform(s2, ctx)
    assert "dedupe_tier" not in out.payload


@pytest.mark.asyncio
async def test_tiers_short_circuit_on_first_match():
    """A URL match must not trigger a Tier 3 embedding call."""

    calls = {"embed": 0}

    class CountingEmbedder:
        async def embed(self, text: str) -> list[float]:
            calls["embed"] += 1
            return [1.0] * 512

    redis = FakeRedis()
    qdrant = FakeQdrant()
    handler = Dedupe4TierHandler(
        _config(embedding_dim=512),
        redis=redis,
        qdrant=qdrant,
        embedder=CountingEmbedder(),
    )
    ctx = _ctx()

    s1 = _signal(url="https://example.com/a", title="t", external_id="ext-1")
    await handler.transform(s1, ctx)
    initial = calls["embed"]

    s2 = _signal(url="https://example.com/a", title="t2", external_id="ext-2")
    out = await handler.transform(s2, ctx)
    assert out.payload.get("dedupe_tier") == 1
    # Embedder must NOT be invoked when Tier 1 matched.
    assert calls["embed"] == initial


@pytest.mark.asyncio
async def test_handler_returns_signal_for_unique_payload():
    redis = FakeRedis()
    handler = Dedupe4TierHandler(_config(tier3=False, tier4=False), redis=redis)
    ctx = _ctx()
    s1 = _signal(title="unique title", body="unique body", url="https://u/1")
    out = await handler.transform(s1, ctx)
    assert out is not None
    assert "dedupe_tier" not in out.payload
    assert "duplicate_of" not in out.payload


@pytest.mark.asyncio
async def test_health_check_reports_state_and_counters():
    redis = FakeRedis()
    handler = Dedupe4TierHandler(_config(tier3=False, tier4=False), redis=redis)
    ctx = _ctx()
    await handler.transform(_signal(url="https://a/1"), ctx)
    await handler.transform(_signal(url="https://a/1", external_id="ext-2"), ctx)
    health = await handler.health_check(ctx)
    assert health.state in {"healthy", "degraded"}
    assert health.signals_in_24h == 2
    assert health.detail["tier_hits"][1] == 1


@pytest.mark.asyncio
async def test_handler_satisfies_stream_handler_protocol():
    """Structural-typing check: handler implements StreamHandler shape."""
    from legba.data.filters._contract import StreamHandler

    redis = FakeRedis()
    h = Dedupe4TierHandler(_config(), redis=redis)
    # runtime_checkable Protocol — structural check.
    assert isinstance(h, StreamHandler)


@pytest.mark.asyncio
async def test_lifecycle_hooks_are_callable():
    redis = FakeRedis()
    h = Dedupe4TierHandler(_config(), redis=redis)
    ctx = _ctx()
    await h.on_configure(ctx)
    await h.on_activate(ctx)
    await h.on_pause(ctx)
    await h.on_resume(ctx)
    await h.on_retire(ctx)


# =============================================================================
# Integration — real Redis + real Qdrant containers + deterministic embedder
# =============================================================================


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


REDIS_AVAILABLE = _port_open("127.0.0.1", 6379)
QDRANT_AVAILABLE = _port_open("127.0.0.1", 6333)


@pytest_asyncio.fixture
async def real_redis():
    from redis.asyncio import Redis
    client = Redis(
        host="127.0.0.1",
        port=6379,
        db=0,
        decode_responses=False,
    )
    # Wipe any leftover dedupe keys from a previous test run.
    keys = await client.keys("legba:dedup-test:*")
    if keys:
        await client.delete(*keys)
    yield client
    keys = await client.keys("legba:dedup-test:*")
    if keys:
        await client.delete(*keys)
    await client.aclose()


@pytest_asyncio.fixture
async def real_qdrant():
    from qdrant_client import AsyncQdrantClient
    client = AsyncQdrantClient(host="127.0.0.1", port=6333)
    yield client
    # Drop test collections.
    cols = await client.get_collections()
    for c in cols.collections:
        if c.name.startswith("legba_dedup_test__"):
            try:
                await client.delete_collection(collection_name=c.name)
            except Exception:                                # pragma: no cover
                pass
    await client.close()


@pytest.mark.integration
@pytest.mark.skipif(not REDIS_AVAILABLE, reason="redis container not reachable on 6379")
@pytest.mark.asyncio
async def test_integration_tier1_real_redis(real_redis):
    """End-to-end Tier 1 against a real Redis."""
    cfg = _config(tier2=False, tier3=False, tier4=False)
    cfg = Dedupe4TierConfig(
        **{**cfg.model_dump(), "redis_key_prefix": "legba:dedup-test"},
    )
    handler = Dedupe4TierHandler(cfg, redis=real_redis)
    ctx = _ctx(target_id="target-integ-1")

    s1 = _signal(title="A", url="https://example.com/x?b=2&a=1", external_id="ext-1")
    s2 = _signal(
        title="Different title same URL",
        url="HTTPS://EXAMPLE.COM/x?a=1&b=2#hash",
        external_id="ext-2",
    )
    await handler.transform(s1, ctx)
    out = await handler.transform(s2, ctx)
    assert out.payload.get("dedupe_tier") == 1
    assert out.payload.get("duplicate_of") == "ext-1"


@pytest.mark.integration
@pytest.mark.skipif(
    not (REDIS_AVAILABLE and QDRANT_AVAILABLE),
    reason="redis or qdrant container not reachable",
)
@pytest.mark.asyncio
async def test_integration_all_four_tiers_end_to_end(real_redis, real_qdrant):
    """5-signal scenario: original + URL-dup + content-near-dup +
    semantic-dup + temporal-dup → assert dedupe_tier 1/2/3/4 in order."""
    fake_time = [1_700_000_000.0]
    cfg = Dedupe4TierConfig(
        tier1=TierToggle(enabled=True),
        tier2=TierToggle(enabled=True),
        tier3=Tier3Config(
            enabled=True,
            threshold=0.55,            # tuned for the token-bag embedder
            embedding_dim=512,
            collection_prefix="legba_dedup_test",
        ),
        tier4=Tier4Config(
            enabled=True,
            window_hours=6,
            distance_threshold=0.15,
        ),
        set_ttl_seconds=300,
        redis_key_prefix="legba:dedup-test",
    )
    handler = Dedupe4TierHandler(
        cfg,
        redis=real_redis,
        qdrant=real_qdrant,
        embedder=DeterministicEmbedder(dim=512),
        clock=lambda: fake_time[0],
    )
    ctx = _ctx(target_id="target-integ-all-tiers")

    # --- Original
    original = _signal(
        title="Wildfire spreads through California national park",
        summary="Thousands evacuated as crews battle blaze in Sequoia region",
        body="<p>Smoke visible from 200 miles away. Air quality drops to 'hazardous'.</p>",
        url="https://news.example.com/wildfire-ca-2026?lang=en&utm_source=feed",
        source_id="news-org-A",
        external_id="orig-1",
    )
    r0 = await handler.transform(original, ctx)
    assert "dedupe_tier" not in r0.payload, "original must not be a duplicate"

    # --- URL dup — same canonical URL, different fragment/query order/case.
    url_dup = _signal(
        title="A wholly different headline",
        body="Wholly different body text that should not match content tier.",
        url="HTTPS://News.Example.com/wildfire-ca-2026?utm_source=feed&lang=en#anchor",
        source_id="other-source",   # different source so Tier 4 doesn't catch
        external_id="url-dup-1",
    )
    r1 = await handler.transform(url_dup, ctx)
    assert r1.payload.get("dedupe_tier") == 1, (
        f"expected Tier 1 URL hit, got tier={r1.payload.get('dedupe_tier')}"
    )
    assert r1.payload.get("duplicate_of") == "orig-1"

    # --- Content-near-dup — identical normalized content, distinct URL,
    # different source, different title (to avoid Tier 4).
    content_dup = _signal(
        title=" Wildfire spreads THROUGH california national PARK ",  # same after norm
        summary="thousands EVACUATED as crews battle blaze in sequoia region",
        body="<div>Smoke   visible   from   200 MILES away. Air quality drops to 'HAZARDOUS'.</div>",
        url="https://aggregator.example/article/12345",
        source_id="aggregator-B",
        external_id="content-dup-1",
    )
    r2 = await handler.transform(content_dup, ctx)
    assert r2.payload.get("dedupe_tier") == 2, (
        f"expected Tier 2 content hit, got tier={r2.payload.get('dedupe_tier')}"
    )
    assert r2.payload.get("duplicate_of") == "orig-1"

    # --- Semantic dup — token-overlap heavy paraphrase; URL + body differ.
    semantic_dup = _signal(
        title="California wildfire forces evacuation Sequoia",
        summary="Smoke from spreads battle blaze crews thousands national park",
        body="<p>Different prose. Sequoia California wildfire battle crews national.</p>",
        url="https://otherwire.example/foo/2026/wildfire-update",
        source_id="wire-C",
        external_id="semantic-dup-1",
    )
    r3 = await handler.transform(semantic_dup, ctx)
    assert r3.payload.get("dedupe_tier") == 3, (
        f"expected Tier 3 semantic hit, got tier={r3.payload.get('dedupe_tier')}"
    )
    assert r3.payload.get("duplicate_of") == "orig-1"

    # --- Temporal dup — same source as original, near-identical title, 2h later.
    fake_time[0] += 2 * 3600.0
    temporal_dup = _signal(
        title="Wildfire spreads through California national park.",  # 1-char diff
        summary="Wholly different summary content unrelated to original.",
        body="<p>Wholly different body unrelated.</p>",
        url="https://news.example.com/wildfire-ca-2026-followup",  # different URL
        source_id="news-org-A",                                     # same source
        external_id="temporal-dup-1",
    )
    r4 = await handler.transform(temporal_dup, ctx)
    assert r4.payload.get("dedupe_tier") == 4, (
        f"expected Tier 4 temporal hit, got tier={r4.payload.get('dedupe_tier')}"
    )
    assert r4.payload.get("duplicate_of") == "orig-1"

    # Health probe surfaces the counters.
    health = await handler.health_check(ctx)
    assert health.signals_in_24h == 5
    tier_hits = health.detail["tier_hits"]
    assert tier_hits[1] == 1
    assert tier_hits[2] == 1
    assert tier_hits[3] == 1
    assert tier_hits[4] == 1
