# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression guard for fact_decay against the real `facts` schema (anchor §0.3).

fact_decay UPDATEs reference valid_until / superseded_by / confidence_components.
Migration 0032_facts_decay_columns adds them (ADD COLUMN IF NOT EXISTS). This
test is the guard that would have caught the silent UndefinedColumn no-op: it
seeds a stale row and asserts both UPDATEs run WITHOUT error and actually
mutate the row.

Also asserts the three columns are present on the facts table (the schema-fix
acceptance).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers.fact_decay import (
    _decay_stale_confidence,
    _expire_past_valid_until,
)
from legba.data.config import PostgresConfig


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


# ISOLATION: the migrated DB is session-shared across the whole suite. The
# facts these tests seed (notably the OPEN 'FDGUARD_Subj' row) would otherwise
# leak into every later test that scans/aggregates facts globally (the
# fact_decay_scan sidecar counts were the bitten case) — so every row this
# module inserts carries the FDGUARD_ subject prefix and is deleted again
# after each test.
@pytest_asyncio.fixture(autouse=True)
async def _fdguard_cleanup(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM facts WHERE subject LIKE 'FDGUARD_%'")
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM facts WHERE subject LIKE 'FDGUARD_%'")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_facts_decay_columns_present(pg_pool):
    async with pg_pool.acquire() as conn:
        cols = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='facts'"
            )
        }
    assert {"valid_until", "superseded_by", "confidence_components"} <= cols


@pytest.mark.integration
@pytest.mark.asyncio
async def test_decay_stale_confidence_runs_and_decrements(pg_pool):
    fact_id = uuid4()
    stale = datetime.now(tz=timezone.utc) - timedelta(days=60)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO facts (id, subject, predicate, value, confidence,
                               source_type, data, updated_at)
            VALUES ($1, 'FDGUARD_Subj', 'pred', 'Val', 0.8, 'ingestion',
                    '{}'::jsonb, $2)
            """,
            fact_id, stale,
        )

    # Must NOT raise UndefinedColumn — the regression this guards.
    decayed = await _decay_stale_confidence(pg_pool)
    assert decayed >= 1

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT confidence, data->>'last_confidence_decay' AS lcd, "
            "confidence_components->>'decay' AS decay FROM facts WHERE id=$1",
            fact_id,
        )
    assert row["confidence"] < 0.8
    assert row["lcd"] is not None
    assert row["decay"] is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expire_past_valid_until_runs(pg_pool):
    fact_id = uuid4()
    past = datetime.now(tz=timezone.utc) - timedelta(days=1)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO facts (id, subject, predicate, value, confidence,
                               source_type, data, valid_until)
            VALUES ($1, 'FDGUARD_ExpSubj', 'pred', 'Val', 1.0, 'ingestion',
                    '{}'::jsonb, $2)
            """,
            fact_id, past,
        )

    expired = await _expire_past_valid_until(pg_pool)
    assert expired >= 1

    async with pg_pool.acquire() as conn:
        expired_flag = await conn.fetchval(
            "SELECT data->>'expired' FROM facts WHERE id=$1", fact_id
        )
    assert expired_flag == "true"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_superseded_rows_are_not_decayed_or_expired(pg_pool):
    """A superseded row (closed by PIECE B auto-supersession: valid_until set +
    superseded_by stamped) is history — fact_decay must NOT re-expire it
    (superseded_by IS NULL guard) nor decay its confidence (it is no longer the
    canonical open row). This pins the interaction between supersession and the
    decay sweep."""
    superseded_id = uuid4()
    successor_id = uuid4()
    stale = datetime.now(tz=timezone.utc) - timedelta(days=60)
    closed = datetime.now(tz=timezone.utc) - timedelta(days=1)
    async with pg_pool.acquire() as conn:
        # A stale, CLOSED + CHAINED row (the superseded prior).
        await conn.execute(
            """
            INSERT INTO facts (id, subject, predicate, value, confidence,
                               source_type, data, updated_at,
                               valid_until, superseded_by)
            VALUES ($1, 'FDGUARD_Acmestan_fd', 'led by', 'Alice', 0.8, 'ingestion',
                    '{}'::jsonb, $2, $3, $4)
            """,
            superseded_id, stale, closed, successor_id,
        )

    expired = await _expire_past_valid_until(pg_pool)
    decayed = await _decay_stale_confidence(pg_pool)

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT confidence, data->>'expired' AS expired FROM facts WHERE id=$1",
            superseded_id,
        )
    # Confidence untouched (still 0.8) and not marked expired — it is chained
    # history, owned by the supersession pointer, not by the decay sweep.
    assert row["confidence"] == pytest.approx(0.8)
    assert row["expired"] is None
    # Sanity: the sweep ran (returned ints), it simply skipped this row.
    assert isinstance(expired, int) and isinstance(decayed, int)
