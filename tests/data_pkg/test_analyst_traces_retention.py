# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""analyst_traces TTL retention purge (S-6 — disk-creep remediation).

Mirrors the ``signals_retention`` precedent. Two layers:
  * Unit (no DB) — the handler is a no-op when disabled (ttl_days<=0, the
    default) or when deps is None; the summary is honest in both states.
  * Integration (real PG) — migration 0101's purge-scan index exists; the purge
    deletes aged traces + CASCADE-drops their linked critiques (no orphans),
    keeps recent traces, and honestly reports what it removed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers import analyst_traces_retention
from legba.data.config import PostgresConfig


# ---------------------------------------------------------------------------
# Unit — disabled-by-default + no-deps no-op + honesty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_by_default_no_purge():
    """ttl_days defaults to 0 → the job is inert (no pool touch)."""

    class _BoomPool:
        def acquire(self):  # pragma: no cover - must never be called
            raise AssertionError("disabled purge must not touch the pool")

    class _Deps:
        pg_pool = _BoomPool()

    res = await analyst_traces_retention.handle([], {}, _Deps())
    assert res.finding.data["ttl_days"] == 0
    assert res.finding.data["traces_purged"] == 0
    assert res.finding.data["critiques_cascaded"] == 0
    assert "disabled" in res.finding.title.lower()
    # Zero token usage — pure maintenance.
    assert res.usage["prompt_tokens"] == 0


@pytest.mark.asyncio
async def test_no_deps_is_zeroed_run():
    """A positive ttl but no pool (unit path) yields a zeroed, honest run."""
    res = await analyst_traces_retention.handle([], {"ttl_days": 30}, None)
    assert res.finding.data["traces_purged"] == 0
    assert res.finding.data["ttl_days"] == 30
    # Non-disabled title reflects the (zero) purge honestly.
    assert "disabled" not in res.finding.title.lower()
    assert "purged 0 trace" in res.finding.title.lower()


@pytest.mark.asyncio
async def test_disabled_summary_is_honest_about_ttl_and_zero():
    res = await analyst_traces_retention.handle([], {"ttl_days": 0}, None)
    d = res.finding.data
    assert d == {
        "sub_handler": "analyst_traces_retention",
        "ttl_days": 0,
        "traces_purged": 0,
        "critiques_cascaded": 0,
    }
    # Never a false "purged" tag on a no-op run.
    assert "traces_purged" not in res.finding.tags


# ---------------------------------------------------------------------------
# Unit — LEGBA_ANALYST_TRACES_TTL_DAYS env fallback (ff65f78)
# ---------------------------------------------------------------------------
#
# The ff65f78 fix (cadence fires carry ONLY ``sub_handler``; ``method.options``
# is schema-forbidden, so the env var is the real opt-in lever) shipped without
# direct coverage — these lock the pattern, mirrored by the
# ``signals_retention`` parity tests.


@pytest.mark.asyncio
async def test_env_fallback_resolves_ttl_on_cadence_shaped_call(monkeypatch):
    monkeypatch.setenv("LEGBA_ANALYST_TRACES_TTL_DAYS", "30")
    res = await analyst_traces_retention.handle(
        [], {"sub_handler": "analyst_traces_retention"}, None
    )
    assert res.finding.data["ttl_days"] == 30


@pytest.mark.asyncio
async def test_options_ttl_takes_precedence_over_env(monkeypatch):
    monkeypatch.setenv("LEGBA_ANALYST_TRACES_TTL_DAYS", "30")
    res = await analyst_traces_retention.handle([], {"ttl_days": 0}, None)
    assert res.finding.data["ttl_days"] == 0


@pytest.mark.asyncio
async def test_env_unset_stays_disabled(monkeypatch):
    monkeypatch.delenv("LEGBA_ANALYST_TRACES_TTL_DAYS", raising=False)
    res = await analyst_traces_retention.handle([], {}, None)
    assert res.finding.data["ttl_days"] == 0


# ---------------------------------------------------------------------------
# Integration — real purge against migrated PG (ephemeral-DB pattern)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


async def _insert_trace(conn, run_id, started_at, *, status="success"):
    await conn.execute(
        """
        INSERT INTO analyst_traces
            (run_id, analyst_id, analyst_version, cadence_trigger, status,
             run_started_at, receipt_hash)
        VALUES ($1, 'unit_x', '0000000000000001', 'cadence', $2, $3, $4)
        """,
        run_id, status, started_at, f"rh_{run_id.hex[:12]}",
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_0101_purge_index_exists(pg_pool):
    async with pg_pool.acquire() as conn:
        idx = await conn.fetchval(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'idx_analyst_traces_retention_run_started_at'"
        )
    assert idx is not None, "migration 0101 purge-scan index missing"
    assert "run_started_at" in idx.lower()
    assert "analyst_traces" in idx.lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_purge_truth_table_deletes_aged_keeps_recent(pg_pool):
    """Deletes strictly older than the TTL; keeps rows at/under the TTL."""
    from legba.runtime.deps import StandardDeps

    now = datetime.now(tz=timezone.utc)
    old = now - timedelta(days=120)      # aged → DELETED
    boundary = now - timedelta(days=2)   # within TTL → KEPT
    recent = now - timedelta(hours=1)    # within TTL → KEPT

    old_id, boundary_id, recent_id = uuid4(), uuid4(), uuid4()

    async with pg_pool.acquire() as conn:
        await _insert_trace(conn, old_id, old)
        await _insert_trace(conn, boundary_id, boundary)
        await _insert_trace(conn, recent_id, recent)

    deps = StandardDeps(pg_pool=pg_pool)
    try:
        res = await analyst_traces_retention.handle(
            [], {"ttl_days": 30, "batch_limit": 1_000_000}, deps
        )
        d = res.finding.data
        assert d["traces_purged"] >= 1
        assert d["ttl_days"] == 30
        assert "traces_purged" in res.finding.tags

        async with pg_pool.acquire() as conn:
            # Aged trace gone.
            assert await conn.fetchval(
                "SELECT 1 FROM analyst_traces WHERE run_id=$1", old_id) is None
            # Recent traces (within TTL) survive.
            assert await conn.fetchval(
                "SELECT 1 FROM analyst_traces WHERE run_id=$1", boundary_id) == 1
            assert await conn.fetchval(
                "SELECT 1 FROM analyst_traces WHERE run_id=$1", recent_id) == 1
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM analyst_traces WHERE run_id = ANY($1::uuid[])",
                [old_id, boundary_id, recent_id],
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disabled_ttl_is_a_live_noop(pg_pool):
    """ttl_days<=0 with a real pool still purges nothing (default posture)."""
    from legba.runtime.deps import StandardDeps

    old_id = uuid4()
    old = datetime.now(tz=timezone.utc) - timedelta(days=365)
    async with pg_pool.acquire() as conn:
        await _insert_trace(conn, old_id, old)

    deps = StandardDeps(pg_pool=pg_pool)
    try:
        res = await analyst_traces_retention.handle([], {"ttl_days": 0}, deps)
        assert res.finding.data["traces_purged"] == 0
        async with pg_pool.acquire() as conn:
            # Ancient row untouched — disabled means disabled.
            assert await conn.fetchval(
                "SELECT 1 FROM analyst_traces WHERE run_id=$1", old_id) == 1
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM analyst_traces WHERE run_id=$1", old_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_purge_cascades_linked_critique_and_reports_it(pg_pool):
    """An aged trace's linked critique is CASCADE-deleted (no orphan) and the
    summary honestly counts it."""
    from legba.runtime.deps import StandardDeps

    old_id = uuid4()
    old = datetime.now(tz=timezone.utc) - timedelta(days=200)
    async with pg_pool.acquire() as conn:
        await _insert_trace(conn, old_id, old)
        await conn.execute(
            """
            INSERT INTO analyst_critiques
                (trace_id, judge_analyst_id, judge_analyst_version, rubric_uri)
            VALUES ($1, 'critic_x', '0000000000000001', 'rubric://x/1')
            """,
            old_id,
        )

    deps = StandardDeps(pg_pool=pg_pool)
    try:
        res = await analyst_traces_retention.handle(
            [], {"ttl_days": 30, "batch_limit": 1_000_000}, deps
        )
        assert res.finding.data["traces_purged"] >= 1
        assert res.finding.data["critiques_cascaded"] >= 1
        async with pg_pool.acquire() as conn:
            # Both the trace and its cascaded critique are gone — no orphan.
            assert await conn.fetchval(
                "SELECT 1 FROM analyst_traces WHERE run_id=$1", old_id) is None
            assert await conn.fetchval(
                "SELECT 1 FROM analyst_critiques WHERE trace_id=$1", old_id) is None
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM analyst_critiques WHERE trace_id=$1", old_id)
            await conn.execute("DELETE FROM analyst_traces WHERE run_id=$1", old_id)
