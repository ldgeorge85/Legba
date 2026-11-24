# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""0256 — the subscribe acquisition's tables on the REAL driver (lane I-4).

* the migration is idempotent: ``source_poll_outcomes.stream`` is one
  nullable jsonb, ``source_stream_gaps`` exists with its kind check;
* a window row persists its ``stream`` JSONB through ``write_poll_outcome``
  (alone and beside the other optional columns); a poll row leaves it NULL;
* :meth:`StreamCore.write_gap` lands a declared gap; an undeclared kind is
  refused by the table;
* :class:`PgAdvisoryLease` holds a source's stream for ONE holder: a second
  lease on the same source is refused until the first is released.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data.config import PostgresConfig
from legba.runtime.source_poll_outcome import write_poll_outcome

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src" / "legba" / "data" / "migrations" / "0256_source_subscribe.sql"
)
_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ingestion" / "source_test_subscribe_sse.yaml"
_PREFIX = "source.test.m0256_"


def _j(v):
    """A jsonb value as a dict, whether or not a codec decoded it."""
    return v if isinstance(v, dict) else json.loads(v)


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    async with pool.acquire() as c:
        await c.execute("DELETE FROM public.source_poll_outcomes WHERE source_id LIKE $1", _PREFIX + "%")
        await c.execute("DELETE FROM public.source_stream_gaps WHERE source_id LIKE $1", _PREFIX + "%")
    await pool.close()


@pytest.mark.asyncio
async def test_migration_is_idempotent(pg_pool) -> None:
    sql = _MIGRATION.read_text()
    assert "BEGIN;" not in sql.upper() and "COMMIT;" not in sql.upper()
    async with pg_pool.acquire() as conn:
        await conn.execute(sql)
        await conn.execute(sql)
        col = await conn.fetchrow(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'source_poll_outcomes' AND column_name = 'stream'"
        )
        gaps = await conn.fetchval("SELECT to_regclass('public.source_stream_gaps') IS NOT NULL")
    assert col["data_type"] == "jsonb" and col["is_nullable"] == "YES"
    assert gaps is True


@pytest.mark.asyncio
async def test_a_window_row_keeps_its_stream_and_a_poll_row_leaves_it_null(pg_pool) -> None:
    sid = _PREFIX + uuid4().hex[:8]
    base = dict(source_id=sid, source_version="v1", owner_tenant="acme", outcome="empty",
                health_state="healthy", capped=False, signals_written=0, error=None)
    window = {"state": "quiet", "transport": "sse", "events": 0, "heartbeats": 12, "gaps": 0}
    async with pg_pool.acquire() as conn:
        await write_poll_outcome(conn, **base, stream=window)
        await write_poll_outcome(conn, **base, stream={**window, "state": "connected", "events": 4},
                                 age_skipped=3, sink_counts={"observation": {"written": 1}})
        await write_poll_outcome(conn, **base)
        rows = await conn.fetch(
            "SELECT stream, age_skipped, sink_counts IS NOT NULL AS sc FROM public.source_poll_outcomes "
            "WHERE source_id = $1 ORDER BY occurred_at, id", sid,
        )
    streams = [None if r["stream"] is None else _j(r["stream"]) for r in rows]
    assert streams[0]["state"] == "quiet" and streams[0]["heartbeats"] == 12
    assert streams[1]["state"] == "connected" and rows[1]["age_skipped"] == 3 and rows[1]["sc"]
    assert streams[2] is None


def _core(pool):
    from legba.data.schemas.source import SourceDescriptor
    from legba.runtime.deps import StandardDeps
    from legba.runtime.source_actor import SourceDeps
    from legba.runtime.stream_ingest import StreamCore

    body = yaml.safe_load(_FIXTURE.read_text())
    body["identity"]["id"] = _PREFIX + uuid4().hex[:8]
    desc = SourceDescriptor.model_validate(body, strict=False)
    return StreamCore(f"source::{desc.identity.id}::{desc.identity.version[:16]}",
                      SourceDeps(descriptor=desc, deps=StandardDeps(pg_pool=pool)))


@pytest.mark.asyncio
async def test_a_declared_gap_lands_and_an_undeclared_kind_is_refused(pg_pool) -> None:
    from legba.runtime.stream_ingest import StreamGap

    core = _core(pg_pool)
    t0 = datetime.now(tz=timezone.utc)
    await core.write_gap(StreamGap("overflow", opened_at=t0, closed_at=t0 + timedelta(seconds=2),
                                   after_event_id="41", first_dropped_id="42", last_dropped_id="57",
                                   dropped=16, detail={"queue_max": 2}))
    await core.write_gap(StreamGap("no_resume", opened_at=t0, closed_at=t0, detail={"reason": "x"}))
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT gap_kind, dropped, after_event_id, first_dropped_id, last_dropped_id, transport, detail "
            "FROM public.source_stream_gaps WHERE source_id = $1 ORDER BY id", core.descriptor.identity.id)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO public.source_stream_gaps (source_id, transport, gap_kind, opened_at) "
                "VALUES ($1, 'sse', 'silent_loss', now())", core.descriptor.identity.id)
    assert [(r["gap_kind"], r["dropped"]) for r in rows] == [("overflow", 16), ("no_resume", None)]
    assert (rows[0]["after_event_id"], rows[0]["first_dropped_id"], rows[0]["last_dropped_id"]) == ("41", "42", "57")
    assert rows[0]["transport"] == "sse" and _j(rows[0]["detail"]) == {"queue_max": 2}


@pytest.mark.asyncio
async def test_the_advisory_lease_has_one_holder(pg_pool) -> None:
    from legba.runtime.stream_connector import PgAdvisoryLease

    sid = _PREFIX + uuid4().hex[:8]
    a, b = PgAdvisoryLease(pg_pool, sid), PgAdvisoryLease(pg_pool, sid)
    assert await a.acquire() is True
    assert await a.alive() is True
    assert await b.acquire() is False          # another holder: stand by
    await a.release()
    assert await a.alive() is False
    assert await b.acquire() is True           # released: the next replica takes it
    await b.release()


@pytest.mark.asyncio
async def test_a_window_through_the_stream_core_lands_on_the_real_table(pg_pool) -> None:
    from legba.runtime.stream_ingest import WindowStats

    core = _core(pg_pool)
    t0 = datetime.now(tz=timezone.utc)
    stats = WindowStats(opened_at=t0 - timedelta(seconds=300), state="stalled", stalls=1,
                        last_error="no bytes for 90s")
    assert await core.write_window(stats, closed_at=t0) == "error"
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT outcome, health_state, error, stream FROM public.source_poll_outcomes WHERE source_id = $1",
            core.descriptor.identity.id)
    assert (row["outcome"], row["health_state"]) == ("error", "unhealthy")
    assert row["error"].startswith("stream_stalled") and _j(row["stream"])["stalls"] == 1
