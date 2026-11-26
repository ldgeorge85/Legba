# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""0268 — the desk-rows shadow ledger on the REAL driver.

* the migration is idempotent and creates the table with its two indexes;
* the shadow's own writer lands one row whose ``counts`` and ``record`` are
  jsonb objects (counts only in ``counts``; the full shadow in ``record``).
"""

from __future__ import annotations

import json
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from legba.data import desk_rows_shadow as shadow_mod
from legba.data.config import PostgresConfig
from legba.data.desk_rows_contract import CONTRACT_VERSION
from legba.data.desk_rows_shadow import LEDGER_TABLE, ShadowRecord

_MIGRATION = (Path(__file__).resolve().parents[2] / "src" / "legba" / "data" / "migrations"
              / "0268_desk_rows_shadow.sql")
_ANALYST = "test.m0268_desk"


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=2)
    yield pool
    async with pool.acquire() as c:
        await c.execute(f"DELETE FROM public.{LEDGER_TABLE} WHERE analyst_id = $1", _ANALYST)
    await pool.close()


@pytest.mark.asyncio
async def test_migration_is_idempotent(pg_pool) -> None:
    sql = _MIGRATION.read_text()
    async with pg_pool.acquire() as conn:
        await conn.execute(sql)
        await conn.execute(sql)
        idx = await conn.fetch("SELECT indexname FROM pg_indexes WHERE tablename = $1", LEDGER_TABLE)
    assert {r["indexname"] for r in idx} >= {"desk_rows_shadow_analyst_occurred", "desk_rows_shadow_run"}


@pytest.mark.asyncio
async def test_the_writer_lands_one_row(pg_pool) -> None:
    rec = ShadowRecord(_ANALYST, "desk_x", "run-1", status="written",
                       final={"by_outcome": {"established_eligible": 2}}, body="*As of x.*")
    await shadow_mod._write(pg_pool, rec)
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT * FROM public.{LEDGER_TABLE} WHERE analyst_id = $1", _ANALYST)
    counts = row["counts"] if isinstance(row["counts"], dict) else json.loads(row["counts"])
    record = row["record"] if isinstance(row["record"], dict) else json.loads(row["record"])
    assert (row["run_id"], row["target_id"], row["status"], row["contract_version"]) == \
        ("run-1", "desk_x", "written", CONTRACT_VERSION)  # Desk_rows.v2.1
    assert counts["final"]["by_outcome"]["established_eligible"] == 2 and "body" not in counts
    assert record["body"] == "*As of x.*" and record["gate_version"] == "statement_gate.v4"
