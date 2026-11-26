# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""indicator_tracker on the REAL driver — migration 0237, the stream watermark,
the once-only flip record and the ``debounced_flips`` reader.

Every assertion reads the persisted rows back through asyncpg:

  * a stream that oscillates 11 times writes 11 ``raw`` rows and ONE
    ``debounced`` row, and the receipt (the feed row) carries the debounced
    flip only;
  * a re-run on identical inputs writes NOTHING: no flip row, no watermark
    rewrite, and the run is trace-only (no feed row);
  * a stream's first sweep records its history as ``backfill`` and feeds none
    of it;
  * ``debounced_flips(target, since)`` returns debounced rows only, scoped to
    the desk and the window.

The sweep is global over ``analyst_outputs`` (a META analyst), so every
assertion is scoped to this file's own desks or compares global counts across
a re-run.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers import indicator_tracker as it
from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore

pytestmark = pytest.mark.integration

N, T = "not_observed", "triggered"


@pytest_asyncio.fixture
async def pg(migrated_pg: PostgresConfig):
    store = PostgresStore(migrated_pg)
    await store.connect()
    async with store.pool.acquire() as conn:
        # This file owns the tracker's record and its watermark class.
        await conn.execute("DELETE FROM indicator_flips")
        await conn.execute(
            "DELETE FROM alert_trigger_watermarks WHERE trigger_class = 'indicator_tracker'"
        )
    yield store
    await store.close()


def _target() -> str:
    return f"country_watch_db{uuid4().hex[:8]}"


async def _insert_run(conn, *, target: str, analyst: str, produced_at: datetime,
                      status: str, slug: str = "punitive-trade-measure") -> str:
    rid = uuid4()
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, kind, title, body, schema_uri, analyst_id, target_id,
             produced_at, data)
        VALUES ($1, 'finding', 'unit read', 'body', 'legba/finding/1.0.0',
                $2, $3, $4, $5::jsonb)
        """,
        rid, analyst, target, produced_at,
        json.dumps({"data": {"indicators": [
            {"id": slug, "statement": "Punitive trade measure announced",
             "status": status, "citations": [1]},
        ]}}),
    )
    return str(rid)


def _times(n: int) -> list[datetime]:
    start = datetime.now(timezone.utc) - timedelta(hours=11 * n + 1)
    return [start + timedelta(hours=11 * i) for i in range(n)]


async def _flips(conn, target: str) -> list[dict]:
    rows = await conn.fetch(
        "SELECT * FROM indicator_flips WHERE target_id = $1 ORDER BY id", target,
    )
    return [dict(r) for r in rows]


async def _watermark(conn, target: str, analyst: str):
    return await conn.fetchrow(
        "SELECT state, updated_at FROM alert_trigger_watermarks "
        "WHERE trigger_class = 'indicator_tracker' AND watermark_key = $1",
        f"{analyst}|{target}",
    )


async def test_oscillating_stream_writes_eleven_raw_one_debounced_and_reruns_write_nothing(pg):
    target, analyst = _target(), "economic_coercion"
    statuses = [N, N] + [T, N] * 5 + [T, T]
    times = _times(len(statuses))
    deps = SimpleNamespace(pg_pool=pg.pool)

    # 1) The stream's first two runs: a first sight — the baseline, no flip.
    async with pg.pool.acquire() as conn:
        ids = [await _insert_run(conn, target=target, analyst=analyst,
                                 produced_at=times[i], status=statuses[i]) for i in range(2)]
    first = await it.handle([], {}, deps)
    async with pg.pool.acquire() as conn:
        assert await _flips(conn, target) == []
        wm = await _watermark(conn, target, analyst)
    assert wm is not None
    state = wm["state"] if isinstance(wm["state"], dict) else json.loads(wm["state"])
    assert state["last_finding_id"] == ids[1]
    assert state["lineages"][0]["confirmed"] == N

    # 2) The next twelve runs land: eleven toggles, then a held triggered.
    async with pg.pool.acquire() as conn:
        for i in range(2, len(statuses)):
            ids.append(await _insert_run(conn, target=target, analyst=analyst,
                                         produced_at=times[i], status=statuses[i]))
    second = await it.handle([], {}, deps)
    async with pg.pool.acquire() as conn:
        rows = await _flips(conn, target)
        total_rows = await conn.fetchval("SELECT count(*) FROM indicator_flips")
        wm2 = await _watermark(conn, target, analyst)
    raw = [r for r in rows if r["grade"] == "raw"]
    deb = [r for r in rows if r["grade"] == "debounced"]
    assert len(raw) == 11 and len(deb) == 1
    d = deb[0]
    assert (d["from_status"], d["to_status"]) == (N, T)
    assert d["activation"] is True and d["backfill"] is False
    assert str(d["evidence_finding_id"]) == ids[12]     # onset: the first T of the held stretch
    assert str(d["confirmed_finding_id"]) == ids[13]    # the run that made it hold
    assert str(d["prev_finding_id"]) == ids[11]         # the last run in the old status
    assert d["confirmed_by"] == "runs" and d["held_runs"] == 2
    assert d["raw_flips_between"] == 11
    assert d["method_version"] == it.METHOD_VERSION
    assert all(not r["backfill"] for r in raw)

    # The feed row carries this desk's flip, debounced only.
    assert second.force_trace_only is False
    mine = [f for f in second.finding.data["flips"] if f["target_id"] == target]
    assert len(mine) == 1 and mine[0]["raw_flips_between"] == 11
    assert second.finding.data["flip_grade"] == "debounced"
    assert all(f["target_id"] != target for f in first.finding.data["flips"])

    # 3) A re-run on identical inputs writes nothing at all.
    third = await it.handle([], {}, deps)
    async with pg.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM indicator_flips") == total_rows
        wm3 = await _watermark(conn, target, analyst)
    assert wm3["updated_at"] == wm2["updated_at"]
    assert third.force_trace_only is True
    assert third.finding.data["flip_count"] == 0
    assert third.finding.data["raw_flips_recorded"] == 0

    # 4) The reader: debounced only, scoped to the desk and the window.
    async with pg.pool.acquire() as conn:
        got = await it.debounced_flips(conn, target, times[0] - timedelta(hours=1))
        before = await it.debounced_flips(
            conn, target, times[0] - timedelta(hours=1), until=times[12],
        )
        other = await it.debounced_flips(conn, _target(), times[0] - timedelta(hours=1))
    assert len(got) == 1
    assert got[0]["to_status"] == T and got[0]["raw_flips_between"] == 11
    assert got[0]["confirmed_finding_id"] == ids[13]
    assert before == []  # confirmed after the as-of cut: not visible as of times[12]
    assert other == []


async def test_first_sweep_backfills_history_and_feeds_none_of_it(pg):
    target, analyst = _target(), "energy_security"
    statuses = [N, N, T, T, N, N]
    times = _times(len(statuses))
    async with pg.pool.acquire() as conn:
        for s, at in zip(statuses, times):
            await _insert_run(conn, target=target, analyst=analyst, produced_at=at, status=s)
    deps = SimpleNamespace(pg_pool=pg.pool)
    result = await it.handle([], {}, deps)
    async with pg.pool.acquire() as conn:
        rows = await _flips(conn, target)
        got = await it.debounced_flips(conn, target, times[0] - timedelta(hours=1))
    assert [(r["grade"], r["from_status"], r["to_status"]) for r in rows if r["grade"] == "debounced"] == [
        ("debounced", N, T), ("debounced", T, N),
    ]
    assert all(r["backfill"] for r in rows)
    assert [f for f in result.finding.data["flips"] if f["target_id"] == target] == []
    # The reader still serves the history (arm E reads windows in the past).
    assert [(g["from_status"], g["to_status"], g["backfill"]) for g in got] == [
        (N, T, True), (T, N, True),
    ]
    again = await it.handle([], {}, deps)
    async with pg.pool.acquire() as conn:
        assert len(await _flips(conn, target)) == len(rows)
    assert again.force_trace_only is True


async def test_reader_never_returns_raw_and_none_target_reads_every_desk(pg):
    a, b = _target(), _target()
    deps = SimpleNamespace(pg_pool=pg.pool)
    times = _times(4)
    async with pg.pool.acquire() as conn:
        for tgt in (a, b):
            await _insert_run(conn, target=tgt, analyst="escalation",
                              produced_at=times[0], status=N)
    await it.handle([], {}, deps)
    async with pg.pool.acquire() as conn:
        for tgt in (a, b):
            for at, s in zip(times[1:], (N, T, T)):
                await _insert_run(conn, target=tgt, analyst="escalation",
                                  produced_at=at, status=s)
        # A third desk that only toggles: raw rows, never a debounced one.
        c = _target()
        for at, s in zip(times, (N, N, T, N)):
            await _insert_run(conn, target=c, analyst="escalation", produced_at=at, status=s)
    await it.handle([], {}, deps)
    since = times[0] - timedelta(hours=1)
    async with pg.pool.acquire() as conn:
        everyone = await it.debounced_flips(conn, None, since)
        raw_c = await conn.fetchval(
            "SELECT count(*) FROM indicator_flips WHERE target_id = $1 AND grade = 'raw'", c,
        )
        only_c = await it.debounced_flips(conn, c, since)
    assert {g["target_id"] for g in everyone} >= {a, b}
    assert c not in {g["target_id"] for g in everyone}
    assert raw_c == 2 and only_c == []
    assert all("grade" not in g for g in everyone)  # the reader has no raw grade to leak
