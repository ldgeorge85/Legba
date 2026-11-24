# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W10-F3 - an adversarial flag lands on a NULL-data signal row, on the REAL driver.

``jsonb_set(NULL, ...)`` is NULL, so before the fix the UPDATE matched the
row, reported ``UPDATE 1`` and wrote NULL back: the flag never landed on any
row whose ``data`` was NULL (every row stamped before TAX-2). The flag is read
back off the persisted row.
"""

from __future__ import annotations

import json
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers.adversarial_signals import (
    _flag_signals,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_SOURCE = f"source.advnull_{uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def pg_pool(migrated_pg):
    pool = await asyncpg.create_pool(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
        min_size=1, max_size=2,
    )
    yield pool
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM signals WHERE source_id = $1", _SOURCE)
    await pool.close()


async def _insert(conn, data: str | None) -> str:
    sid = uuid4()
    await conn.execute(
        "INSERT INTO signals (id, source_id, source_version, payload, "
        "content_hash, fetched_at, created_at, updated_at, schema_uri, data) "
        "VALUES ($1, $2, 'v1', '{}'::jsonb, $3, now(), now(), now(), "
        "'iglu:legba/signal/jsonschema/1-0-0', $4::jsonb)",
        sid, _SOURCE, f"h-{sid}", data,
    )
    return str(sid)


async def _data(conn, sid: str):
    raw = await conn.fetchval("SELECT data FROM signals WHERE id = $1::uuid", sid)
    return json.loads(raw) if isinstance(raw, str) else raw


async def test_flag_lands_on_a_null_data_row(pg_pool):
    async with pg_pool.acquire() as conn:
        sid = await _insert(conn, None)
        assert await conn.fetchval(
            "SELECT data IS NULL FROM signals WHERE id = $1::uuid", sid)
    n = await _flag_signals(
        pg_pool, [sid], "echo_chamber",
        {"severity": "high", "description": "test echo"},
    )
    assert n == 1
    async with pg_pool.acquire() as conn:
        data = await _data(conn, sid)
    assert data is not None
    flags = data["adversarial_flags"]
    assert len(flags) == 1
    assert flags[0]["type"] == "echo_chamber"
    assert flags[0]["severity"] == "high"


async def test_flag_appends_and_keeps_existing_keys(pg_pool):
    existing = json.dumps({
        "classifications": ["src:wire"],
        "adversarial_flags": [{"type": "earlier"}],
    })
    async with pg_pool.acquire() as conn:
        sid = await _insert(conn, existing)
    assert await _flag_signals(
        pg_pool, [sid], "provenance_cluster", {"description": "d"}) == 1
    async with pg_pool.acquire() as conn:
        data = await _data(conn, sid)
    assert data["classifications"] == ["src:wire"]
    assert [f["type"] for f in data["adversarial_flags"]] == [
        "earlier", "provenance_cluster"]
