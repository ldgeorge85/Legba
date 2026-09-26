# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0197 — ``unit_correctness.reference_age_days``, against real SQL.

0196 required a reference whose window CONTAINED the as-of stamp. A reference's
window ends at its own T0 and every read it grades lands after that, so the
first live forced run — a reference for 09-02T19:30Z → 09-16T19:30Z, read at
22:32Z the same day — matched nothing and graded nothing. The grace that fixes
it (``LEGBA_GRADER_REFERENCE_GRACE_DAYS``, default 7) means a published number
can now rest on a reference whose window has closed, and this column is how far.

Everything asserted here is a property of the DATABASE — the column exists and
is nullable, the CHECK makes a negative age unrepresentable, a second apply is a
no-op, and 0196's own guarantees survive the ALTER. A test that mocked the
connection would assert only that the SQL text contains the words.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers._correctness_rubric import (
    RUBRIC_SHA256,
)
from legba.data.config import PostgresConfig
from legba.data.migrations import MIGRATIONS_DIR

MIGRATION_NAME = "0197_unit_correctness_reference_age.sql"
COLUMN = "reference_age_days"

_REF_SQL = """
INSERT INTO unit_references (
    id, target_id, window_start, window_end, built_at, builder, ref_json,
    span_verified_rate, thin_dimensions, sha256
) VALUES ($1,$2,$3,$4,$5,'pytest-lane',$6::jsonb,NULL,'{}'::text[],$7)
RETURNING id
"""

_UC_SQL = """
INSERT INTO unit_correctness (
    id, analyst_id, target_id, head_id, as_of, reference_id, rubric_sha, grain,
    n_claims, n_contains, n_contradicts, n_silent, n_split, n_unparseable,
    n_single_family, correctness_share, coverage_share, families, cost_usd,
    reference_age_days
) VALUES ($1,$2,$3,$4,$5,$6,$7,'desk',2,1,1,0,0,0,2,$8,$9,'{}'::jsonb,0,$10)
RETURNING id
"""


def _migration_sql() -> str:
    return (MIGRATIONS_DIR / MIGRATION_NAME).read_text(encoding="utf-8")


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    yield c
    await c.close()


@pytest_asyncio.fixture
async def scratch(conn):
    target = f"country_test_{uuid4().hex[:8]}"

    async def _wipe():
        await conn.execute(
            "DELETE FROM unit_correctness WHERE target_id = $1", target
        )
        await conn.execute(
            "DELETE FROM unit_references WHERE target_id = $1", target
        )

    await _wipe()
    yield target
    await _wipe()


async def _ref(conn, target: str):
    now = datetime.now(timezone.utc)
    return await conn.fetchval(
        _REF_SQL, uuid4(), target, now - timedelta(days=14), now, now,
        json.dumps({"ref_developments": []}), uuid4().hex + uuid4().hex,
    )


async def _unit(conn, target: str, ref_id, age):
    return await conn.fetchval(
        _UC_SQL, uuid4(), "internal_stability", target, uuid4(),
        datetime.now(timezone.utc), ref_id, RUBRIC_SHA256,
        Decimal("0.5"), Decimal("1.0"), age,
    )


# ---------------------------------------------------------------------------
# The migration itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_column_exists_and_is_a_nullable_numeric(conn):
    row = await conn.fetchrow(
        """
        SELECT data_type, is_nullable, column_default
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'unit_correctness'
           AND column_name = $1
        """,
        COLUMN,
    )
    assert row is not None, "migration 0197 did not apply"
    assert row["data_type"] == "numeric"
    # NULLABLE, and NULL is not zero: every row written before this migration
    # measured against a contained window but never recorded the distance, and
    # a DEFAULT 0 would assert a freshness nobody measured.
    assert row["is_nullable"] == "YES"
    assert row["column_default"] is None


@pytest.mark.asyncio
async def test_reapplying_the_migration_is_a_no_op(conn):
    """The runner re-globs every file, and ``ADD CONSTRAINT`` has no
    ``IF NOT EXISTS`` — the guarded DO block is what makes the second apply
    survive rather than raise 42710."""
    sql = _migration_sql()
    await conn.execute(sql)
    await conn.execute(sql)
    assert await conn.fetchval(
        """
        SELECT count(*) FROM information_schema.columns
         WHERE table_name = 'unit_correctness' AND column_name = $1
        """,
        COLUMN,
    ) == 1
    assert await conn.fetchval(
        """
        SELECT count(*) FROM pg_constraint
         WHERE conname = 'ck_unit_correctness_reference_age'
        """
    ) == 1


@pytest.mark.asyncio
async def test_the_staleness_index_exists(conn):
    indexes = {
        r["indexname"] for r in await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = "
            "'unit_correctness'"
        )
    }
    assert "idx_unit_correctness_reference_age" in indexes
    # 0196's guarantees survive the ALTER.
    assert "uq_unit_correctness_run" in indexes


# ---------------------------------------------------------------------------
# What the column may hold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fractional_age_round_trips(conn, scratch):
    """0.126701 days is 3h02m — the exact distance of the live defect. Whole
    days would have rounded it to nothing or to a day, and the difference
    between "hours old" and "a day old" is the whole point of the column."""
    ref_id = await _ref(conn, scratch)
    await _unit(conn, scratch, ref_id, Decimal("0.126701"))
    value = await conn.fetchval(
        "SELECT reference_age_days FROM unit_correctness WHERE target_id = $1",
        scratch,
    )
    assert value == Decimal("0.126701")


@pytest.mark.asyncio
async def test_a_null_age_is_allowed_and_is_not_zero(conn, scratch):
    ref_id = await _ref(conn, scratch)
    await _unit(conn, scratch, ref_id, None)
    value = await conn.fetchval(
        "SELECT reference_age_days FROM unit_correctness WHERE target_id = $1",
        scratch,
    )
    assert value is None


@pytest.mark.asyncio
async def test_a_negative_age_is_unrepresentable(conn, scratch):
    """An age is a distance backwards to a window that had already closed.
    Negative is not a stale reference read early — it is a bug, and the
    database refuses it rather than storing it for someone to average."""
    ref_id = await _ref(conn, scratch)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await _unit(conn, scratch, ref_id, Decimal("-1"))


@pytest.mark.asyncio
async def test_zero_is_legal_and_means_the_stamp_was_inside_the_window(
    conn, scratch
):
    ref_id = await _ref(conn, scratch)
    assert await _unit(conn, scratch, ref_id, Decimal("0")) is not None
