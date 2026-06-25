# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for L-248 per-descriptor dedupe tier selection.

Covers the new ``tiers: list[int]`` field on :class:`Dedupe4TierConfig` and
the resulting handler gating. Three flavors:

  * Pure-unit pydantic validation tests on the schema field.
  * In-process fakes — assert which tiers run / don't run, and (crucially)
    that no Qdrant collection is touched when tier 3 is opted out.
  * Real Redis + real Qdrant integration tests — same assertions against
    the substrate the descriptor would actually hit in production.

The brief (L-248) mandates real Qdrant interactions, so the tier-3-active
vs tier-3-skipped substrate behavior is asserted with real clients when
the containers are reachable.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError

from legba.data.filters._contract import FilterContext
from legba.data.filters.dedupe import (
    Dedupe4TierConfig,
    Dedupe4TierHandler,
    Tier3Config,
    Tier4Config,
    TierToggle,
)
from legba.data.sources._contract import Signal

# Reuse the in-process fakes + helpers from the sibling 4-tier test file —
# they're stable and lifting a copy would diverge over time.
from .test_filter_dedupe import (
    DeterministicEmbedder,
    FakeQdrant,
    FakeRedis,
    _ctx,
    _signal,
)


# =============================================================================
# Schema validation
# =============================================================================


def test_tiers_default_is_all_four():
    cfg = Dedupe4TierConfig()
    assert cfg.tiers == [1, 2, 3, 4]


def test_tiers_accepts_subset_one_two():
    cfg = Dedupe4TierConfig(tiers=[1, 2])
    assert cfg.tiers == [1, 2]


def test_tiers_accepts_subset_one_two_three():
    cfg = Dedupe4TierConfig(tiers=[1, 2, 3])
    assert cfg.tiers == [1, 2, 3]


def test_tiers_accepts_single_tier():
    cfg = Dedupe4TierConfig(tiers=[1])
    assert cfg.tiers == [1]


def test_tiers_rejects_empty():
    with pytest.raises(ValidationError) as excinfo:
        Dedupe4TierConfig(tiers=[])
    assert "non-empty" in str(excinfo.value)


def test_tiers_rejects_unknown_tier():
    with pytest.raises(ValidationError) as excinfo:
        Dedupe4TierConfig(tiers=[1, 5])
    assert "invalid" in str(excinfo.value)


def test_tiers_rejects_zero_or_negative():
    with pytest.raises(ValidationError):
        Dedupe4TierConfig(tiers=[0, 1])
    with pytest.raises(ValidationError):
        Dedupe4TierConfig(tiers=[-1, 1])


def test_tiers_rejects_duplicates():
    with pytest.raises(ValidationError) as excinfo:
        Dedupe4TierConfig(tiers=[1, 1, 2])
    assert "duplicates" in str(excinfo.value)


def test_tiers_rejects_unsorted():
    with pytest.raises(ValidationError) as excinfo:
        Dedupe4TierConfig(tiers=[2, 1])
    assert "sorted ascending" in str(excinfo.value)
    with pytest.raises(ValidationError):
        Dedupe4TierConfig(tiers=[1, 3, 2])


# =============================================================================
# Behavioral — tier gating via the new `tiers` selector (fakes)
# =============================================================================


@pytest.mark.asyncio
async def test_tiers_one_two_runs_only_cheap_tiers():
    """With ``tiers=[1, 2]`` configured, tiers 3+4 must not execute.

    Concrete assertions:
      * No call to the embedder (a counting embedder would see zero).
      * Tier 4's Redis sorted-set must remain empty (the temporal insert
        is skipped).
      * The signal still flows through and is annotated by tier 1 / 2 hits.
    """
    embed_calls = {"n": 0}

    class CountingEmbedder:
        async def embed(self, text: str) -> list[float]:
            embed_calls["n"] += 1
            return [1.0] * 512

    redis = FakeRedis()
    qdrant = FakeQdrant()

    cfg = Dedupe4TierConfig(
        tiers=[1, 2],
        tier3=Tier3Config(embedding_dim=512),  # still default-enabled toggle
        tier4=Tier4Config(),                   # still default-enabled toggle
    )
    handler = Dedupe4TierHandler(
        cfg, redis=redis, qdrant=qdrant, embedder=CountingEmbedder(),
    )
    ctx = _ctx()

    s1 = _signal(
        title="A unique title here",
        body="some unique body text",
        url="https://a.example/x",
        source_id="src-1",
        external_id="ext-1",
    )
    await handler.transform(s1, ctx)

    # Tier 1 hit on a duplicate URL.
    s2 = _signal(
        title="totally different title",
        body="completely unrelated body",
        url="https://a.example/x",            # same canonical URL
        source_id="src-1",
        external_id="ext-2",
    )
    out = await handler.transform(s2, ctx)
    assert out.payload.get("dedupe_tier") == 1

    # No embedding work happened — tier 3 was opted out via the selector.
    assert embed_calls["n"] == 0
    # No Qdrant collection was created either.
    cols = await qdrant.get_collections()
    assert cols.collections == [] or all(
        not getattr(c, "name", "").startswith("legba_dedup__")
        for c in cols.collections
    )
    # No temporal sorted-set was written — tier 4 was opted out.
    assert not any(
        "temporal" in key for key in redis._zset.keys()
    ), f"temporal zset should be empty; got: {list(redis._zset.keys())}"


@pytest.mark.asyncio
async def test_tiers_one_two_three_runs_first_three_skips_tier4():
    """``tiers=[1, 2, 3]`` runs tiers 1+2+3 but not tier 4."""
    redis = FakeRedis()
    qdrant = FakeQdrant()
    embedder = DeterministicEmbedder(dim=512)

    cfg = Dedupe4TierConfig(
        tiers=[1, 2, 3],
        tier3=Tier3Config(embedding_dim=512, threshold=0.55),
        tier4=Tier4Config(),
    )
    handler = Dedupe4TierHandler(
        cfg, redis=redis, qdrant=qdrant, embedder=embedder,
    )
    ctx = _ctx()

    s1 = _signal(
        title="Wildfire spreads through California national park",
        summary="Crews battle blaze in Sequoia region",
        body="<p>Smoke visible from 200 miles away.</p>",
        url="https://news.example.com/wildfire-1",
        source_id="news-a",
        external_id="orig-1",
    )
    r0 = await handler.transform(s1, ctx)
    assert "dedupe_tier" not in r0.payload

    # Tier 3 hit — token-overlap heavy paraphrase, distinct URL/source/title.
    s2 = _signal(
        title="California wildfire forces evacuation Sequoia battle crews",
        summary="Smoke spreads battle blaze crews national park visible",
        body="<p>Sequoia California wildfire battle crews national.</p>",
        url="https://other.example/foo",
        source_id="news-b",
        external_id="paraphrase-1",
    )
    out = await handler.transform(s2, ctx)
    assert out.payload.get("dedupe_tier") == 3

    # Qdrant collection WAS created (tier 3 active).
    cols = await qdrant.get_collections()
    names = [getattr(c, "name", "") for c in cols.collections]
    assert any(n.startswith("legba_dedup__") for n in names), (
        f"expected a legba_dedup__* collection; got: {names}"
    )

    # Tier 4 was NOT written — temporal zset is empty.
    assert not any(
        "temporal" in key for key in redis._zset.keys()
    ), f"temporal zset should be empty; got: {list(redis._zset.keys())}"


@pytest.mark.asyncio
async def test_tiers_default_all_four_unchanged_behavior():
    """Default ``tiers=[1, 2, 3, 4]`` preserves the existing 4-tier flow."""
    redis = FakeRedis()
    qdrant = FakeQdrant()
    embedder = DeterministicEmbedder(dim=512)

    cfg = Dedupe4TierConfig(
        # tiers not specified -> default [1, 2, 3, 4].
        tier3=Tier3Config(embedding_dim=512, threshold=0.55),
    )
    handler = Dedupe4TierHandler(
        cfg, redis=redis, qdrant=qdrant, embedder=embedder,
    )
    ctx = _ctx()

    assert handler.is_tier_active(1)
    assert handler.is_tier_active(2)
    assert handler.is_tier_active(3)
    assert handler.is_tier_active(4)

    s1 = _signal(
        title="Eruption felt in southern Iceland village",
        body="<p>Lava flow seen from satellite</p>",
        url="https://x.example/1",
        source_id="src-1",
        external_id="ext-1",
    )
    await handler.transform(s1, ctx)

    # Tier 4 should record the title in the per-source temporal cache.
    assert any(
        "temporal" in key for key in redis._zset.keys()
    ), "default config should populate the temporal zset on tier-4-active runs"


@pytest.mark.asyncio
async def test_tier3_skipped_no_qdrant_collection_created():
    """Sentinel test: with ``tiers=[1, 2]`` plus a real-looking qdrant fake
    handed in, the handler still must NOT touch qdrant. This is the L-248
    storage-cost win — per-target Qdrant collections grow linearly across
    hundreds of targets; opting tier 3 out has to actually skip creation.
    """
    redis = FakeRedis()

    class TripwireQdrant:
        """Any call to this fake raises — proves the handler didn't reach Qdrant."""

        async def get_collections(self):  # pragma: no cover - tripwire
            raise AssertionError("qdrant.get_collections must not be called when tier 3 is opted out")

        async def create_collection(self, **kw):  # pragma: no cover - tripwire
            raise AssertionError("qdrant.create_collection must not be called when tier 3 is opted out")

        async def query_points(self, **kw):  # pragma: no cover - tripwire
            raise AssertionError("qdrant.query_points must not be called when tier 3 is opted out")

        async def upsert(self, **kw):  # pragma: no cover - tripwire
            raise AssertionError("qdrant.upsert must not be called when tier 3 is opted out")

    class TripwireEmbedder:
        async def embed(self, text):  # pragma: no cover - tripwire
            raise AssertionError("embedder.embed must not be called when tier 3 is opted out")

    cfg = Dedupe4TierConfig(tiers=[1, 2])
    handler = Dedupe4TierHandler(
        cfg,
        redis=redis,
        qdrant=TripwireQdrant(),       # type: ignore[arg-type]
        embedder=TripwireEmbedder(),   # type: ignore[arg-type]
    )
    ctx = _ctx()

    # Several signals — none should hit the qdrant tripwires.
    for i in range(3):
        s = _signal(
            title=f"news story {i}",
            body=f"body content number {i}",
            url=f"https://x.example/article-{i}",
            external_id=f"ext-{i}",
        )
        await handler.transform(s, ctx)


@pytest.mark.asyncio
async def test_tiers_only_one_runs_only_url_tier():
    """``tiers=[1]`` runs only tier 1; even tier 2 (cheap) is skipped."""
    redis = FakeRedis()
    handler = Dedupe4TierHandler(
        Dedupe4TierConfig(tiers=[1]),
        redis=redis,
    )
    ctx = _ctx()

    s1 = _signal(
        title="identical title",
        body="<p>identical body</p>",
        url="https://one.example/a",
        external_id="ext-1",
    )
    s2 = _signal(
        title="identical title",                 # would trigger tier 2 if active
        body="<p>identical body</p>",
        url="https://two.example/b",             # distinct URL
        external_id="ext-2",
    )
    await handler.transform(s1, ctx)
    out = await handler.transform(s2, ctx)
    # Tier 2 is opted out via the selector — no dedupe annotation expected.
    assert "dedupe_tier" not in out.payload


@pytest.mark.asyncio
async def test_health_check_surfaces_resolved_tiers():
    """The handler health probe exposes the resolved tier set."""
    redis = FakeRedis()
    cfg = Dedupe4TierConfig(tiers=[1, 2])
    handler = Dedupe4TierHandler(cfg, redis=redis)
    ctx = _ctx()

    health = await handler.health_check(ctx)
    assert health.detail["tiers_configured"] == [1, 2]
    assert health.detail["tiers_active"] == [1, 2]
    assert health.detail["tier1_enabled"] is True
    assert health.detail["tier2_enabled"] is True
    assert health.detail["tier3_enabled"] is False
    assert health.detail["tier4_enabled"] is False


@pytest.mark.asyncio
async def test_tier_selector_intersects_with_per_tier_enabled_flag():
    """If a tier is listed in ``tiers`` but disabled via ``tierN.enabled=False``,
    it doesn't run.

    The AND semantic is documented on Dedupe4TierConfig and matters because
    the Phase 5a pipeline runner still constructs configs with per-tier
    disabled flags. The two opt-down paths must agree.
    """
    redis = FakeRedis()
    cfg = Dedupe4TierConfig(
        tiers=[1, 2, 3, 4],
        tier4=Tier4Config(enabled=False),    # opt out via per-tier flag
    )
    handler = Dedupe4TierHandler(cfg, redis=redis)
    assert handler.is_tier_active(1)
    assert handler.is_tier_active(2)
    # Tier 3 inactive because no qdrant/embedder supplied (port gate).
    assert not handler.is_tier_active(3)
    # Tier 4 inactive because per-tier toggle is False even though it's in tiers.
    assert not handler.is_tier_active(4)


# =============================================================================
# Integration — real Redis + real Qdrant
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
        host="127.0.0.1", port=6379, db=0, decode_responses=False,
    )
    keys = await client.keys("legba:dedup-tiers-test:*")
    if keys:
        await client.delete(*keys)
    yield client
    keys = await client.keys("legba:dedup-tiers-test:*")
    if keys:
        await client.delete(*keys)
    await client.aclose()


@pytest_asyncio.fixture
async def real_qdrant():
    from qdrant_client import AsyncQdrantClient
    client = AsyncQdrantClient(host="127.0.0.1", port=6333)
    yield client
    cols = await client.get_collections()
    for c in cols.collections:
        if c.name.startswith("legba_dedup_tiers_test__"):
            try:
                await client.delete_collection(collection_name=c.name)
            except Exception:                                # pragma: no cover
                pass
    await client.close()


@pytest.mark.integration
@pytest.mark.skipif(
    not (REDIS_AVAILABLE and QDRANT_AVAILABLE),
    reason="redis or qdrant container not reachable",
)
@pytest.mark.asyncio
async def test_integration_tiers_one_two_does_not_create_qdrant_collection(
    real_redis, real_qdrant,
):
    """L-248 storage-cost win — assert against a *real* Qdrant.

    Construct a handler with ``tiers=[1, 2]`` against a real Qdrant client
    and process signals. List collections after — no
    ``legba_dedup_tiers_test__*`` collection must be created.
    """
    target_id = f"l248_t12_{uuid4().hex[:8]}"
    cfg = Dedupe4TierConfig(
        tiers=[1, 2],
        tier3=Tier3Config(
            embedding_dim=512,
            collection_prefix="legba_dedup_tiers_test",
        ),
        redis_key_prefix="legba:dedup-tiers-test",
    )
    handler = Dedupe4TierHandler(
        cfg,
        redis=real_redis,
        qdrant=real_qdrant,
        embedder=DeterministicEmbedder(dim=512),
    )
    ctx = _ctx(target_id=target_id)

    # Push a few signals through.
    for i in range(3):
        s = _signal(
            title=f"news item {i}",
            body=f"body for item {i}",
            url=f"https://l248.example/{i}",
            external_id=f"l248-ext-{i}",
            target_id=target_id,
        )
        await handler.transform(s, ctx)

    # The per-target Qdrant collection MUST NOT exist.
    cols = await real_qdrant.get_collections()
    target_marker = target_id  # _safe_name keeps lowercase alnum + _
    matching = [
        c.name for c in cols.collections
        if c.name.startswith("legba_dedup_tiers_test__")
        and target_marker in c.name
    ]
    assert matching == [], (
        f"tiers=[1,2] must not create per-target Qdrant collection; "
        f"found: {matching}"
    )


@pytest.mark.integration
@pytest.mark.skipif(
    not (REDIS_AVAILABLE and QDRANT_AVAILABLE),
    reason="redis or qdrant container not reachable",
)
@pytest.mark.asyncio
async def test_integration_tiers_with_three_creates_qdrant_collection(
    real_redis, real_qdrant,
):
    """Counter-test: with tier 3 active, the Qdrant collection IS created.

    Same shape as the previous test but with ``tiers=[1, 2, 3]`` — proves
    the gating, not just an unrelated bug masking creation in the opt-out
    case.
    """
    target_id = f"l248_t123_{uuid4().hex[:8]}"
    cfg = Dedupe4TierConfig(
        tiers=[1, 2, 3],
        tier3=Tier3Config(
            embedding_dim=512,
            threshold=0.95,
            collection_prefix="legba_dedup_tiers_test",
        ),
        redis_key_prefix="legba:dedup-tiers-test",
    )
    handler = Dedupe4TierHandler(
        cfg,
        redis=real_redis,
        qdrant=real_qdrant,
        embedder=DeterministicEmbedder(dim=512),
    )
    ctx = _ctx(target_id=target_id)

    s = _signal(
        title="A first story for tier 3 inserts",
        body="With enough text to embed and write to qdrant.",
        url="https://l248tier3.example/first",
        external_id="l248-t3-ext-1",
        target_id=target_id,
    )
    await handler.transform(s, ctx)

    cols = await real_qdrant.get_collections()
    matching = [
        c.name for c in cols.collections
        if c.name.startswith("legba_dedup_tiers_test__")
        and target_id in c.name
    ]
    assert len(matching) == 1, (
        f"tiers=[1,2,3] must create exactly one per-target Qdrant "
        f"collection; found: {matching}"
    )


@pytest.mark.integration
@pytest.mark.skipif(
    not REDIS_AVAILABLE,
    reason="redis container not reachable",
)
@pytest.mark.asyncio
async def test_integration_tiers_one_two_real_redis_tier1_hit(real_redis):
    """End-to-end Tier-1 dedupe against real Redis when tiers=[1, 2]."""
    target_id = f"l248_real_{uuid4().hex[:8]}"
    cfg = Dedupe4TierConfig(
        tiers=[1, 2],
        redis_key_prefix="legba:dedup-tiers-test",
    )
    handler = Dedupe4TierHandler(cfg, redis=real_redis)
    ctx = _ctx(target_id=target_id)

    s1 = _signal(
        title="A", url="https://example.com/x?b=2&a=1", external_id="ext-1",
        target_id=target_id,
    )
    s2 = _signal(
        title="Different title same URL",
        url="HTTPS://EXAMPLE.COM/x?a=1&b=2#hash",
        external_id="ext-2",
        target_id=target_id,
    )
    await handler.transform(s1, ctx)
    out = await handler.transform(s2, ctx)
    assert out.payload.get("dedupe_tier") == 1
    assert out.payload.get("duplicate_of") == "ext-1"
