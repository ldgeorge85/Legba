# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the connection wrappers (qdrant, redis, nats)."""

from __future__ import annotations

import pytest

from legba.data.config import (
    NatsConfig,
    QdrantConfig,
    RedisConfig,
)


# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_qdrant_signals_collection_lifecycle():
    from legba.data.qdrant import DORMANT_COLLECTIONS, QdrantStore

    store = QdrantStore(QdrantConfig.from_env())
    await store.connect()
    try:
        # Make sure stale dormant collections (if any) are dropped first.
        await store.retire_dormant_collections()

        created = await store.ensure_signals_collection()
        # Idempotent: second call no-ops.
        created_again = await store.ensure_signals_collection()
        assert created_again is False

        names = await store.list_collections()
        assert store.cfg.signals_collection in names
        for d in DORMANT_COLLECTIONS:
            assert d not in names, f"dormant collection {d} should be retired"

        info = await store.collection_info(store.cfg.signals_collection)
        assert info is not None
    finally:
        await store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_qdrant_per_target_collection():
    from legba.data.qdrant import QdrantStore

    store = QdrantStore(QdrantConfig.from_env())
    await store.connect()
    try:
        name = await store.ensure_target_collection("smoke_target_a")
        assert name == "legba_target__smoke_target_a"
        info = await store.collection_info(name)
        assert info is not None
        # Cleanup the test-created per-target collection.
        await store.client.delete_collection(collection_name=name)
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_redis_ping_and_embed_cache_ttl():
    from legba.data.redis import RedisStore

    store = RedisStore(RedisConfig.from_env())
    await store.connect()
    try:
        assert await store.ping()
        await store.cache_embed("legba_data_test:embed:abc", b"vector-bytes")
        ttl = await store.client.ttl("legba_data_test:embed:abc")
        # ttl <= cfg.embed_cache_ttl_seconds (defaults to 86400)
        assert 0 < int(ttl) <= store.cfg.embed_cache_ttl_seconds
        await store.client.delete("legba_data_test:embed:abc")
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# NATS
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nats_jetstream_stream_lifecycle():
    from legba.data.nats import NatsStore

    store = NatsStore(NatsConfig.from_env())
    await store.connect()
    try:
        created = await store.ensure_stream(
            "LEGBA_DATA_TEST",
            subjects=["legba_data_test.>"],
        )
        # Idempotent
        created_again = await store.ensure_stream(
            "LEGBA_DATA_TEST",
            subjects=["legba_data_test.>"],
        )
        assert created is True
        assert created_again is False
        # Drop the stream so the test is self-cleaning.
        await store.js.delete_stream("LEGBA_DATA_TEST")
    finally:
        await store.close()
