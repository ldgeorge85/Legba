# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""0226 — ``source_poll_outcomes.keyword_filtered`` and the ``gated`` outcome,
on the REAL driver (W3-F8).

* the migration file is idempotent: applied twice (the runner applies it once;
  a hand re-run must be a no-op) the column is one nullable integer and the
  CHECK accepts ``gated`` and still refuses a made-up outcome;
* ``write_poll_outcome`` persists the gate's drop count and stores NULL (not 0)
  for a source that is not gated -- read back with SQL;
* ``SourceCore.pull_once`` over a source whose gate drops every entry leaves a
  ``gated`` row carrying the count, and no signal.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.schemas.source import FilterStage
from legba.runtime.deps import StandardDeps
from legba.runtime.source_actor import SourceCore, SourceDeps
from legba.runtime.source_poll_outcome import write_poll_outcome
from legba.runtime.state import SCHEMA as STATE_SCHEMA
from tests.runtime.test_keyword_gate import _descriptor, _entry, _StubHandler

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src" / "legba" / "data" / "migrations" / "0226_poll_outcome_gated.sql"
)
_PREFIX = "source.test.m0226_"


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
async def test_migration_is_idempotent_and_widens_the_outcome_check(pg_pool) -> None:
    sql = _MIGRATION.read_text()
    assert "BEGIN;" not in sql.upper() and "COMMIT;" not in sql.upper()  # the runner owns the txn
    async with pg_pool.acquire() as conn:
        await conn.execute(sql)
        await conn.execute(sql)                                # twice: a no-op
        cols = await conn.fetch(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'source_poll_outcomes' AND column_name = 'keyword_filtered'"
        )
        assert len(cols) == 1
        assert cols[0]["data_type"] == "integer"
        assert cols[0]["is_nullable"] == "YES" and cols[0]["column_default"] is None
        checks = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conname = 'source_poll_outcomes_outcome_chk'"
        )
        assert checks == 1
        sid = _PREFIX + uuid4().hex[:8]
        for ok in ("empty", "error", "success", "gated"):
            await conn.execute(
                "INSERT INTO public.source_poll_outcomes (source_id, outcome) VALUES ($1, $2)",
                sid, ok,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO public.source_poll_outcomes (source_id, outcome) VALUES ($1, 'bogus')",
                sid,
            )


@pytest.mark.asyncio
async def test_the_writer_persists_the_count_and_null_for_not_gated(pg_pool) -> None:
    sid = _PREFIX + uuid4().hex[:8]
    async with pg_pool.acquire() as conn:
        await write_poll_outcome(
            conn, source_id=sid, source_version="v1", owner_tenant="acme",
            outcome="gated", health_state="healthy", capped=False,
            signals_written=0, error=None, keyword_filtered=7,
        )
        await write_poll_outcome(
            conn, source_id=sid, source_version="v1", owner_tenant="acme",
            outcome="empty", health_state="healthy", capped=False,
            signals_written=0, error=None,                       # not gated
        )
        await write_poll_outcome(
            conn, source_id=sid, source_version="v1", owner_tenant="acme",
            outcome="empty", health_state="healthy", capped=False,
            signals_written=0, error=None, keyword_filtered=0,   # gated, none dropped
        )
        rows = await conn.fetch(
            "SELECT outcome, keyword_filtered FROM public.source_poll_outcomes "
            "WHERE source_id = $1 ORDER BY occurred_at, id", sid,
        )
    assert [(r["outcome"], r["keyword_filtered"]) for r in rows] == [
        ("gated", 7), ("empty", None), ("empty", 0),
    ]


@pytest.mark.asyncio
async def test_pull_once_over_a_fully_gated_poll_leaves_a_gated_row(pg_pool) -> None:
    async with pg_pool.acquire() as c:
        await c.execute(STATE_SCHEMA)
    sid = _PREFIX + uuid4().hex[:8]
    sd = _descriptor(sid, ingestion_filters=[
        FilterStage(kind="keyword_gate", config={"include": ["sanctions"]}),
    ])
    core = SourceCore(
        f"source::{sid}::m0226",
        SourceDeps(descriptor=sd, deps=StandardDeps(pg_pool=pg_pool, nats_publish=None)),
    )
    base = datetime.now(tz=timezone.utc) - timedelta(minutes=10)
    core.sd.handler = _StubHandler([
        _entry("Weather", base), _entry("Football", base + timedelta(minutes=1)),
    ])
    try:
        result = await core.pull_once()
        assert result["signals_written"] == 0 and result["keyword_filtered"] == 2
        async with pg_pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT outcome, keyword_filtered, signals_written "
                "FROM public.source_poll_outcomes WHERE source_id = $1", sid,
            )
            n_signals = await c.fetchval("SELECT count(*) FROM signals WHERE source_id = $1", sid)
        assert dict(row) == {"outcome": "gated", "keyword_filtered": 2, "signals_written": 0}
        assert n_signals == 0
    finally:
        async with pg_pool.acquire() as c:
            await c.execute(
                "DELETE FROM public.actor_filter_state WHERE filter_id = $1", f"source:{sid}",
            )
