# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""0254 — ``source_poll_outcomes.age_skipped`` on the REAL driver.

* the migration file is idempotent: applied twice the column is one nullable
  integer with no default;
* ``write_poll_outcome`` persists the count, a 0, and NULL when the cap did
  not apply — read back with SQL (the ``$N::integer`` cast binds on asyncpg,
  alone and after a JSONB optional column).
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.runtime.source_poll_outcome import write_poll_outcome

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src" / "legba" / "data" / "migrations" / "0254_poll_outcome_age_skipped.sql"
)
_PREFIX = "source.test.m0254_"


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    async with pool.acquire() as c:
        await c.execute(
            "DELETE FROM public.source_poll_outcomes WHERE source_id LIKE $1", _PREFIX + "%",
        )
    await pool.close()


@pytest.mark.asyncio
async def test_migration_is_idempotent(pg_pool) -> None:
    sql = _MIGRATION.read_text()
    assert "BEGIN;" not in sql.upper() and "COMMIT;" not in sql.upper()
    async with pg_pool.acquire() as conn:
        await conn.execute(sql)
        await conn.execute(sql)
        cols = await conn.fetch(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'source_poll_outcomes' AND column_name = 'age_skipped'"
        )
    assert len(cols) == 1
    assert cols[0]["data_type"] == "integer"
    assert cols[0]["is_nullable"] == "YES" and cols[0]["column_default"] is None


@pytest.mark.asyncio
async def test_the_writer_persists_the_count_zero_and_null(pg_pool) -> None:
    sid = _PREFIX + uuid4().hex[:8]
    base = dict(source_id=sid, source_version="v1", owner_tenant="acme", outcome="empty",
                health_state="healthy", capped=False, signals_written=0, error=None)
    async with pg_pool.acquire() as conn:
        await write_poll_outcome(conn, **base, age_skipped=38)
        await write_poll_outcome(conn, **base, age_skipped=0)
        await write_poll_outcome(conn, **base)
        await write_poll_outcome(conn, **base, age_skipped=5, fetch_protocol={"version": "HTTP/1.1"})
        rows = await conn.fetch(
            "SELECT age_skipped, fetch_protocol IS NOT NULL AS fp FROM public.source_poll_outcomes "
            "WHERE source_id = $1 ORDER BY occurred_at, id", sid,
        )
    assert [(r["age_skipped"], r["fp"]) for r in rows] == [
        (38, False), (0, False), (None, False), (5, True),
    ]
