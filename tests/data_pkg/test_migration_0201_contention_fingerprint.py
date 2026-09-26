# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0201 — ``fact_contention.input_fingerprint``, against real SQL.

The column is how the hourly arbiter tells a dispute whose inputs moved from one
whose inputs did not, which is what stopped it holding its actor's turn for 9-16
minutes an hour (see fact_contention_pass.py for the measurement).

Everything asserted here is a property of the DATABASE — the column exists, it
is nullable with no default, a second apply is a no-op, and the arbiter's own
finalize write round-trips through it. A test that mocked the connection would
assert only that the SQL text contains the words.

The nullability is load-bearing, not incidental. Every row that existed before
this migration reads NULL, ``unchanged()`` refuses NULL, and so the whole
standing corpus is recomputed once and fills the column in by itself — bounded
by the per-pass budget. A DEFAULT would have asserted a fingerprint nobody
computed, and every pre-existing group would have been skipped on a hash that
matched nothing it was decided from.
"""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.migrations import MIGRATIONS_DIR

MIGRATION_NAME = "0201_fact_contention_input_fingerprint.sql"
COLUMN = "input_fingerprint"
_SUBJECT_TAG = "fcmig"


def _migration_sql() -> str:
    return (MIGRATIONS_DIR / MIGRATION_NAME).read_text(encoding="utf-8")


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    yield c
    await c.execute(
        "DELETE FROM fact_contention WHERE subject_key LIKE $1", f"{_SUBJECT_TAG} %"
    )
    await c.close()


@pytest.mark.asyncio
async def test_the_column_exists_and_is_nullable_text(conn):
    row = await conn.fetchrow(
        """
        SELECT data_type, is_nullable, column_default
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'fact_contention'
           AND column_name = $1
        """,
        COLUMN,
    )
    assert row is not None, "migration 0201 did not apply"
    assert row["data_type"] == "text"
    assert row["is_nullable"] == "YES"
    assert row["column_default"] is None, (
        "a default would claim a fingerprint nobody computed, and every "
        "pre-existing group would skip on a hash matching nothing"
    )


@pytest.mark.asyncio
async def test_reapplying_the_migration_is_a_no_op(conn):
    """The runner re-globs every file on every boot, so the second apply is the
    normal case, not the exception. ``ADD COLUMN IF NOT EXISTS`` is what makes
    it survive rather than raise 42701."""
    sql = _migration_sql()
    assert "IF NOT EXISTS" in sql
    await conn.execute(sql)
    await conn.execute(sql)
    still_there = await conn.fetchval(
        """
        SELECT count(*) FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'fact_contention'
           AND column_name = $1
        """,
        COLUMN,
    )
    assert still_there == 1


@pytest.mark.asyncio
async def test_a_fingerprint_round_trips_and_starts_null(conn):
    subject = f"{_SUBJECT_TAG} {uuid4().hex[:8]}"
    cid = await conn.fetchval(
        "INSERT INTO fact_contention (subject_key, predicate_key, status) "
        "VALUES ($1, 'located in', 'contested') RETURNING id",
        subject,
    )
    assert await conn.fetchval(
        "SELECT input_fingerprint FROM fact_contention WHERE id = $1", cid
    ) is None, "a group nobody has decided yet carries no fingerprint"

    digest = "f" * 64
    await conn.execute(
        "UPDATE fact_contention SET input_fingerprint = $2 WHERE id = $1",
        cid, digest,
    )
    assert await conn.fetchval(
        "SELECT input_fingerprint FROM fact_contention WHERE id = $1", cid
    ) == digest
