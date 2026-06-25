# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Signals TTL retention purge (graph-and-data Wave-1b, item 3 / D4).

Two layers:
  * Unit (no DB) — the handler is a no-op when disabled (ttl_days<=0, the
    default) or when deps is None.
  * Integration (real PG) — migration 0036's purge-scan index exists; the purge
    deletes aged signals + their value-referenced children (no orphans), keeps
    fresh signals, and never purges a `retain_always` / `evidence_hold` signal.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers import signals_retention
from legba.data.config import PostgresConfig


# ---------------------------------------------------------------------------
# Unit — disabled-by-default + no-deps no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_by_default_no_purge():
    """ttl_days defaults to 0 → the job is inert (no pool touch)."""

    class _BoomPool:
        def acquire(self):  # pragma: no cover - must never be called
            raise AssertionError("disabled purge must not touch the pool")

    class _Deps:
        pg_pool = _BoomPool()

    res = await signals_retention.handle([], {}, _Deps())
    assert res.finding.data["ttl_days"] == 0
    assert res.finding.data["signals_purged"] == 0
    assert "disabled" in res.finding.title.lower()


@pytest.mark.asyncio
async def test_no_deps_is_zeroed_run():
    res = await signals_retention.handle([], {"ttl_days": 30}, None)
    assert res.finding.data["signals_purged"] == 0


# ---------------------------------------------------------------------------
# Integration — real purge against migrated PG
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_0036_purge_index_exists(pg_pool):
    async with pg_pool.acquire() as conn:
        idx = await conn.fetchval(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'idx_signals_retention_fetched_at'"
        )
    assert idx is not None, "migration 0036 purge-scan index missing"
    assert "retention_class" in idx.lower()
    assert "fetched_at" in idx.lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_purge_deletes_aged_and_keeps_fresh_and_held(pg_pool):
    from legba.runtime.deps import StandardDeps

    tenant = f"ret_{uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)
    old = now - timedelta(days=120)
    fresh = now - timedelta(days=1)

    old_sig = uuid4()        # aged, purgeable class → DELETED
    old_held = uuid4()       # aged but retain_always → KEPT
    fresh_sig = uuid4()      # within TTL → KEPT
    ent = uuid4()

    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO entity_profiles (id, canonical_name, entity_type, "
            "entity_class, data, completeness_score) "
            "VALUES ($1,$2,'location','location','{}'::jsonb,0.3)",
            ent, f"Place_{tenant}",
        )
        for sid, ts, rc in [
            (old_sig, old, "reference_only"),
            (old_held, old, "retain_always"),
            (fresh_sig, fresh, "reference_only"),
        ]:
            await conn.execute(
                "INSERT INTO signals (id, source_id, owner_tenant, modality, "
                "payload, fetched_at, retention_class) "
                "VALUES ($1,$2,$3,'text',$4::jsonb,$5,$6)",
                sid, "src", tenant, json.dumps({"title": "x"}), ts, rc,
            )
        # Children of the to-be-purged old signal (no FK to signals → the job
        # must clean these explicitly so nothing orphans).
        await conn.execute(
            "INSERT INTO signal_entity_links (signal_id, entity_id, role, confidence) "
            "VALUES ($1,$2,'mentioned',0.8)",
            old_sig, ent,
        )
        await conn.execute(
            "INSERT INTO signal_aliases (alias_signal_id, canonical_signal_id) "
            "VALUES ($1,$2)",
            old_sig, fresh_sig,
        )

    deps = StandardDeps(pg_pool=pg_pool)
    try:
        res = await signals_retention.handle(
            [], {"ttl_days": 30, "batch_limit": 1_000_000}, deps
        )
        d = res.finding.data
        assert d["signals_purged"] >= 1
        assert d["entity_links_purged"] >= 1
        assert d["aliases_purged"] >= 1

        async with pg_pool.acquire() as conn:
            # Aged purgeable signal gone; its children gone (no orphans).
            assert await conn.fetchval(
                "SELECT 1 FROM signals WHERE id=$1", old_sig) is None
            assert await conn.fetchval(
                "SELECT 1 FROM signal_entity_links WHERE signal_id=$1", old_sig
            ) is None
            assert await conn.fetchval(
                "SELECT 1 FROM signal_aliases WHERE alias_signal_id=$1", old_sig
            ) is None
            # Held + fresh signals survive.
            assert await conn.fetchval(
                "SELECT 1 FROM signals WHERE id=$1", old_held) == 1
            assert await conn.fetchval(
                "SELECT 1 FROM signals WHERE id=$1", fresh_sig) == 1
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signal_entity_links WHERE entity_id=$1", ent)
            await conn.execute(
                "DELETE FROM signal_aliases WHERE canonical_signal_id=$1", fresh_sig)
            await conn.execute("DELETE FROM signals WHERE owner_tenant=$1", tenant)
            await conn.execute("DELETE FROM entity_profiles WHERE id=$1", ent)
