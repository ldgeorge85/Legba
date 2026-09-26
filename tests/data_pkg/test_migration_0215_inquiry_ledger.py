# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0215 — the inquiry ledger, against real SQL.

Program 5 lane 1 (planning/PROGRAM5_INQUIRY_DESIGN_2026-09-24.md §3). Every
property asserted here is a property of the DATABASE, not of the pack code
that sits on top of it — the table exists with its named constraints, the
sealed-ledger discipline (a hypothesis with no resolution_test is refused)
holds even against a bare INSERT that never goes through
``inquiry_state.ledger_write``, and a second apply of the migration is a
no-op.
"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.migrations import MIGRATIONS_DIR

MIGRATION_NAME = "0215_inquiry_ledger.sql"
_TAG = "inqmig"


def _migration_sql() -> str:
    return (MIGRATIONS_DIR / MIGRATION_NAME).read_text(encoding="utf-8")


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    yield c
    await c.execute(
        "DELETE FROM inquiry_ledger WHERE descriptor_id LIKE $1", f"{_TAG}_%"
    )
    await c.close()


def _names(rows) -> set[str]:
    return {r["conname"] for r in rows}


async def _constraints(conn) -> list:
    return await conn.fetch(
        """
        SELECT conname FROM pg_constraint
         WHERE conrelid = 'public.inquiry_ledger'::regclass
        """
    )


# ---------------------------------------------------------------------------
# The table + its named constraints exist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_table_exists_with_the_expected_columns(conn):
    rows = await conn.fetch(
        """
        SELECT column_name, data_type, is_nullable
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'inquiry_ledger'
        """
    )
    by_name = {r["column_name"]: r for r in rows}
    expected = {
        "id", "descriptor_id", "kind", "text", "status", "resolution_test",
        "resolves_by", "dispatched_to", "cited_refs", "created_at",
        "updated_at", "closed_at", "closed_by_entry",
    }
    assert expected <= set(by_name), f"missing columns: {expected - set(by_name)}"
    assert by_name["descriptor_id"]["is_nullable"] == "NO"
    assert by_name["kind"]["is_nullable"] == "NO"
    assert by_name["text"]["is_nullable"] == "NO"
    assert by_name["status"]["is_nullable"] == "NO"
    assert by_name["resolution_test"]["is_nullable"] == "YES"
    assert by_name["closed_by_entry"]["is_nullable"] == "YES"
    assert by_name["cited_refs"]["data_type"] == "jsonb"


@pytest.mark.asyncio
async def test_named_constraints_exist(conn):
    names = _names(await _constraints(conn))
    for expected in (
        "inquiry_ledger_kind_vocab",
        "inquiry_ledger_status_vocab",
        "inquiry_ledger_hypothesis_needs_test",
        "inquiry_ledger_closed_at_matches_status",
    ):
        assert expected in names, f"missing constraint {expected}"


@pytest.mark.asyncio
async def test_descriptor_status_index_exists(conn):
    indexes = {
        r["indexname"]
        for r in await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'inquiry_ledger'"
        )
    }
    assert "idx_inquiry_ledger_descriptor_status" in indexes


@pytest.mark.asyncio
async def test_reapplying_the_migration_is_a_no_op(conn):
    sql = _migration_sql()
    assert "IF NOT EXISTS" in sql
    await conn.execute(sql)
    await conn.execute(sql)
    names = _names(await _constraints(conn))
    assert "inquiry_ledger_hypothesis_needs_test" in names


# ---------------------------------------------------------------------------
# THE SEALED-LEDGER DISCIPLINE — a hypothesis with no resolution_test is
# refused at the DATABASE, not merely by the pack's own validation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hypothesis_without_resolution_test_is_refused_at_the_db(conn):
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            """
            INSERT INTO inquiry_ledger (descriptor_id, kind, text)
            VALUES ($1, 'hypothesis', 'a hypothesis with no test')
            """,
            f"{_TAG}_hyp_bare",
        )


@pytest.mark.asyncio
async def test_hypothesis_with_resolution_test_is_accepted(conn):
    row = await conn.fetchrow(
        """
        INSERT INTO inquiry_ledger (descriptor_id, kind, text, resolution_test)
        VALUES ($1, 'hypothesis', 'a hypothesis with a test', 'the test')
        RETURNING id, status, closed_at
        """,
        f"{_TAG}_hyp_ok",
    )
    assert row["status"] == "open"
    assert row["closed_at"] is None


@pytest.mark.asyncio
async def test_non_hypothesis_kinds_never_need_a_resolution_test(conn):
    for kind in ("question", "observation", "expectation"):
        row = await conn.fetchrow(
            """
            INSERT INTO inquiry_ledger (descriptor_id, kind, text)
            VALUES ($1, $2, 'no test needed')
            RETURNING id
            """,
            f"{_TAG}_{kind}", kind,
        )
        assert row["id"] is not None


@pytest.mark.asyncio
async def test_bad_kind_is_refused(conn):
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            """
            INSERT INTO inquiry_ledger (descriptor_id, kind, text)
            VALUES ($1, 'not_a_real_kind', 'x')
            """,
            f"{_TAG}_badkind",
        )


@pytest.mark.asyncio
async def test_bad_status_is_refused(conn):
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            """
            INSERT INTO inquiry_ledger (descriptor_id, kind, text, status)
            VALUES ($1, 'observation', 'x', 'not_a_real_status')
            """,
            f"{_TAG}_badstatus",
        )


@pytest.mark.asyncio
async def test_open_status_must_have_no_closed_at(conn):
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            """
            INSERT INTO inquiry_ledger (descriptor_id, kind, text, status, closed_at)
            VALUES ($1, 'observation', 'x', 'open', now())
            """,
            f"{_TAG}_openclosed",
        )


@pytest.mark.asyncio
async def test_terminal_status_must_have_closed_at(conn):
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            """
            INSERT INTO inquiry_ledger (descriptor_id, kind, text, status)
            VALUES ($1, 'observation', 'x', 'confirmed')
            """,
            f"{_TAG}_confirmed_no_ts",
        )


@pytest.mark.asyncio
async def test_terminal_status_with_closed_at_is_accepted(conn):
    row = await conn.fetchrow(
        """
        INSERT INTO inquiry_ledger (descriptor_id, kind, text, status, closed_at)
        VALUES ($1, 'observation', 'x', 'confirmed', now())
        RETURNING id
        """,
        f"{_TAG}_confirmed_ts",
    )
    assert row["id"] is not None
