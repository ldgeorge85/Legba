# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compressed trace bulk against a migrated Postgres.

* the migration: nullable pointer columns, the bundle table, the NOT VALID
  foreign key, the pending index, and a re-apply that changes nothing;
* the job: batches, the byte-for-byte check, resume after a budget stop, a
  failed check rolling the batch back, a row held by another session skipped
  without waiting, and inserts running beside a batch;
* every reader of the moved columns, through its real route or function,
  answering the same before and after compaction.

Each test uses its own analyst ids and passes them to ``compact`` so it never
compacts rows other files in this shared database rely on.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data import trace_bulk, trace_bulk_compact
from legba.data.config import PostgresConfig
from legba.data.migrations import MIGRATIONS_DIR
from legba.data.postgres import PostgresStore
from legba.data.provenance.receipts import RuntimeReceiptChain
from legba.data.registry.api import API_TOKEN_ENV
from legba.data.trace_bulk_compact import CompactSettings, compact

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_MIGRATION = MIGRATIONS_DIR / "20261125T0844_trace_bulk_bundles.sql"
_NOW = datetime.now(timezone.utc)
_OLD = _NOW - timedelta(days=90)
_ON = CompactSettings(enabled=True, age_days=30, bundle_size=4, max_seconds=120)


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=6)
    yield p
    await p.close()


@pytest_asyncio.fixture
async def store(migrated_pg: PostgresConfig):
    """The registry's own pool: jsonb comes back as Python values."""
    s = PostgresStore(migrated_pg)
    await s.connect()
    yield s
    await s.close()


def _aid(tag: str) -> str:
    return f"zz_tb_{tag}_{uuid4().hex[:8]}"


async def _record(pool, analyst_id: str, i: int, *, started: datetime, **over: Any) -> UUID:
    run_id = uuid4()
    chain = RuntimeReceiptChain(pool)
    prompt = over.pop("prompt", f"[SYSTEM] Ünïcödé analyst {analyst_id} — 中文 ✓\n" * 40 + f"run {i}")
    await chain.record(
        run_id=run_id, analyst_id=analyst_id, analyst_version="v1",
        cadence_trigger="cadence", target_id="br",
        input_row_refs=[uuid4()],
        input_payload=over.pop("input_payload", {"prompt_tail": {"text": f"tail {i}", "total_chars": 9000 + i}}),
        prompt_module_hash="pmod", prompt_rendered=prompt,
        output_row_refs=over.pop("output_row_refs", [uuid4()]),
        output_payload=over.pop("output_payload", {"data": {"i": i, "x": [1, 2.5, None, "é"]}, "big": "y" * 3000}),
        run_started_at=started + timedelta(minutes=i), run_ended_at=started + timedelta(minutes=i, seconds=5),
        llm_calls=[{"model": "m", "tokens": 10}], tool_calls=[{"name": "t"}],
        intermediate_steps=[{"kind": "llm_call", "tokens": 10}],
        prompt_sha256="p" * 64,
    )
    return run_id


_BULK_SQL = """
SELECT run_id, prompt_rendered, input_payload::text AS input_payload,
       output_payload::text AS output_payload, bulk_bundle_id, bulk_columns
  FROM analyst_traces WHERE analyst_id = $1 ORDER BY run_started_at
"""


async def _bulk(conn, analyst_id: str) -> dict[UUID, dict[str, Any]]:
    return {r["run_id"]: dict(r) for r in await conn.fetch(_BULK_SQL, analyst_id)}


async def _read_back(conn, analyst_id: str) -> dict[UUID, dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT run_id, prompt_rendered, input_payload, output_payload, bulk_bundle_id, bulk_columns"
        " FROM analyst_traces WHERE analyst_id = $1", analyst_id)
    return {r["run_id"]: r for r in await trace_bulk.hydrate_traces(conn, rows)}


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


async def test_migration_shape_and_reapply(pool) -> None:
    async with pool.acquire() as conn:
        cols = {r["column_name"]: r for r in await conn.fetch(
            "SELECT column_name, is_nullable, column_default FROM information_schema.columns"
            " WHERE table_name = 'analyst_traces' AND column_name LIKE 'bulk_%'")}
        assert set(cols) == {"bulk_bundle_id", "bulk_columns"}
        assert all(c["is_nullable"] == "YES" and c["column_default"] is None for c in cols.values())
        assert await conn.fetchval(
            "SELECT attstorage::text FROM pg_attribute WHERE attrelid = 'analyst_trace_bundles'::regclass"
            " AND attname = 'body'") == "e"
        fk = await conn.fetchrow(
            "SELECT convalidated FROM pg_constraint WHERE conname = 'analyst_traces_bulk_bundle_id_fkey'")
        assert fk is not None and fk["convalidated"] is False
        assert await conn.fetchval(
            "SELECT count(*) FROM pg_indexes WHERE indexname = 'idx_analyst_traces_bulk_pending'") == 1
        async with conn.transaction():
            await conn.execute(_MIGRATION.read_text())
        assert await conn.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE conname = 'analyst_traces_bulk_bundle_id_fkey'") == 1


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


async def test_batches_verify_and_resume(pool, store) -> None:
    aid = _aid("resume")
    old = [await _record(pool, aid, i, started=_OLD) for i in range(10)]
    old.append(await _record(pool, aid, 10, started=_OLD, prompt=None))
    recent = [await _record(pool, aid, i, started=_NOW - timedelta(days=1)) for i in range(2)]
    async with pool.acquire() as conn:
        before = await _bulk(conn, aid)
    async with store.acquire() as conn:
        before_py = await _read_back(conn, aid)

    first = await compact(pool, _ON, max_bundles=1, analyst_ids=[aid])
    assert (first.bundles, first.traces, first.stopped_on_budget) == (1, 4, True)
    second = await compact(pool, _ON, analyst_ids=[aid])
    assert (second.bundles, second.traces, second.failures) == (2, 7, [])
    assert second.stored_bytes * 5 < second.raw_bytes
    assert (await compact(pool, _ON, analyst_ids=[aid])).traces == 0

    async with pool.acquire() as conn:
        after = await _bulk(conn, aid)
        for rid in old:
            row = after[rid]
            assert row["bulk_bundle_id"] is not None
            assert row["prompt_rendered"] is None and row["input_payload"] is None
            assert row["output_payload"] is None
        assert after[old[-1]]["bulk_columns"] == ["input_payload", "output_payload"]
        for rid in recent:
            assert after[rid]["bulk_bundle_id"] is None and after[rid] == before[rid]
        # byte-identical text on a plain connection
        got = await _read_back(conn, aid)
        for rid, row in before.items():
            for c in trace_bulk.BULK_COLUMNS:
                assert got[rid][c] == row[c], (rid, c)
        # what is left on the row is untouched
        kept = await conn.fetch(
            "SELECT llm_calls::text, tool_calls::text, intermediate_steps::text, receipt_hash,"
            " prompt_sha256 FROM analyst_traces WHERE run_id = ANY($1::uuid[])", old)
        assert all(k["llm_calls"] != "[]" and k["prompt_sha256"] == "p" * 64 for k in kept)
    # the same Python values on the json-decoding pool
    async with store.acquire() as conn:
        assert await _read_back(conn, aid) == before_py


async def test_a_short_tail_waits_for_a_full_bundle(pool) -> None:
    aid = _aid("tail")
    for i in range(2):
        await _record(pool, aid, i, started=_NOW - timedelta(days=33))
    stats = await compact(pool, _ON, analyst_ids=[aid])
    assert stats.traces == 0
    later = await compact(pool, _ON, now=_NOW + timedelta(days=10), analyst_ids=[aid])
    assert later.traces == 2


async def test_a_failed_check_rolls_the_batch_back(pool, monkeypatch) -> None:
    aid = _aid("verify")
    for i in range(4):
        await _record(pool, aid, i, started=_OLD)
    async with pool.acquire() as conn:
        before = await _bulk(conn, aid)

    real = trace_bulk_compact.decode_bundle

    def _tamper(body):
        out = real(body)
        first = next(iter(out.values()))
        first["output_payload"] = first["output_payload"].replace("é", "e")
        return out

    monkeypatch.setattr(trace_bulk_compact, "decode_bundle", _tamper)
    stats = await compact(pool, _ON, analyst_ids=[aid])
    assert stats.traces == 0 and len(stats.failures) == 1 and "differ" in stats.failures[0]
    async with pool.acquire() as conn:
        assert await _bulk(conn, aid) == before
        assert await conn.fetchval(
            "SELECT count(*) FROM analyst_trace_bundles WHERE analyst_id = $1", aid) == 0
    monkeypatch.setattr(trace_bulk_compact, "decode_bundle", real)
    assert (await compact(pool, _ON, analyst_ids=[aid])).traces == 4


async def test_a_row_held_elsewhere_is_skipped_not_waited_on(pool) -> None:
    aid = _aid("held")
    ids = [await _record(pool, aid, i, started=_OLD) for i in range(5)]
    holder = await pool.acquire()
    try:
        tx = holder.transaction()
        await tx.start()
        await holder.execute("SELECT 1 FROM analyst_traces WHERE run_id = $1 FOR UPDATE", ids[0])
        stats = await asyncio.wait_for(compact(pool, _ON, analyst_ids=[aid]), timeout=30)
        assert stats.traces == 4
        await tx.rollback()
    finally:
        await pool.release(holder)
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT bulk_bundle_id FROM analyst_traces WHERE run_id = $1", ids[0]) is None
    assert (await compact(pool, _ON, now=_NOW + timedelta(days=30), analyst_ids=[aid])).traces == 1


async def test_inserts_run_beside_a_batch(pool) -> None:
    aid = _aid("concurrent")
    for i in range(16):
        await _record(pool, aid, i, started=_OLD)
    settings = CompactSettings(enabled=True, age_days=30, bundle_size=2, max_seconds=120)

    async def _writer() -> list[UUID]:
        out = []
        for i in range(12):
            started = _OLD if i % 2 else _NOW
            out.append(await _record(pool, aid, 100 + i, started=started))
            await asyncio.sleep(0)
        return out

    stats, written = await asyncio.wait_for(
        asyncio.gather(compact(pool, settings, analyst_ids=[aid]), _writer()), timeout=60)
    assert stats.failures == [] and len(written) == 12
    await compact(pool, settings, analyst_ids=[aid])
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT run_id, run_started_at, bulk_bundle_id FROM analyst_traces WHERE analyst_id = $1", aid)
        assert len(rows) == 28
        cutoff = _NOW - timedelta(days=30)
        assert all((r["bulk_bundle_id"] is not None) == (r["run_started_at"] < cutoff) for r in rows)
        got = await _read_back(conn, aid)
        assert all(isinstance(got[r["run_id"]]["prompt_rendered"], str) for r in rows)


# ---------------------------------------------------------------------------
# Readers, before and after compaction
# ---------------------------------------------------------------------------


def _deps(store):
    return SimpleNamespace(descriptor_registry=SimpleNamespace(pg=store))


async def _client(router, prefix):
    app = FastAPI()
    app.include_router(router, prefix=prefix)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def test_lineage_and_record_sheet_receipts_on_compacted_runs(pool, store, monkeypatch) -> None:
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    from legba.data.record_sheet_reads import run_integrity
    from legba.data.registry.lineage_api import build_lineage_router

    signal_id, finding_id, situation_id = uuid4(), uuid4(), uuid4()
    f_aid, s_aid = _aid("lin_f"), _aid("lin_s")
    async with store.acquire() as conn:
        await conn.execute(
            "INSERT INTO signals (id, source_id, source_version, produced_by_kind, fetched_at, modality,"
            " payload, content_hash, derived_from, schema_uri) VALUES ($1, 'rss_main', '', 'source', NOW(),"
            " 'text', $2::jsonb, '', '{}'::uuid[], 'iglu:legba/signal/jsonschema/3-0-0')",
            signal_id, json.dumps({"title": "sig"}))
    f_run = await _record(pool, f_aid, 0, started=_OLD, output_row_refs=[finding_id])
    s_run = await _record(pool, s_aid, 0, started=_OLD, output_row_refs=[situation_id])
    async with store.acquire() as conn:
        await conn.execute(
            "INSERT INTO analyst_outputs (id, kind, title, body, analyst_id, analyst_version, produced_at,"
            " derived_from, schema_uri, run_id) VALUES ($1, 'finding', 'f', '', $2, 'v1', NOW(), $3::uuid[],"
            " 'iglu:legba/finding/jsonschema/1-0-0', $4)", finding_id, f_aid, [signal_id], f_run)
        await conn.execute(
            "INSERT INTO situations (id, data, name, target_id, target_version, analyst_id, analyst_version,"
            " produced_at, derived_from, schema_uri, run_id) VALUES ($1, '{}'::jsonb, 's', 'br', 'tv', $2,"
            " 'v1', NOW(), $3::uuid[], 'iglu:legba/situation/jsonschema/2-0-0', $4)",
            situation_id, s_aid, [finding_id], s_run)

    async def _read():
        async with await _client(build_lineage_router(_deps(store)), "/api/v1") as c:
            r = await c.get(f"/api/v1/lineage/situation/{situation_id}",
                            params={"direction": "upstream", "depth": 3})
        assert r.status_code == 200, r.text
        body = r.json()
        nodes = {n["id"]: n["receipt"] for n in body["nodes"]}
        async with store.acquire() as conn:
            sheet = await run_integrity(conn, str(f_run))
        return body["root"]["receipt"], nodes[str(finding_id)], sheet["receipt"]

    before = await _read()
    assert before[0]["chain_consistent"] is True and before[1]["chain_consistent"] is True
    assert before[2]["state"] == "verified"
    assert (await compact(pool, _ON, analyst_ids=[f_aid, s_aid])).traces == 2
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM analyst_traces WHERE run_id = ANY($1::uuid[])"
            " AND bulk_bundle_id IS NOT NULL AND prompt_rendered IS NULL AND output_payload IS NULL",
            [f_run, s_run]) == 2
    assert await _read() == before


async def test_flow_routes_on_a_compacted_run(pool, store, monkeypatch) -> None:
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    from legba.data.registry import flow_api

    aid = _aid("flow")
    rid = await _record(pool, aid, 0, started=_OLD, prompt="P" * 5000 + "...[TRUNCATED: x]")

    async def _read():
        async with await _client(flow_api.build_flow_router(_deps(store)), "/api/v1/v3") as c:
            detail = (await c.get(f"/api/v1/v3/flow/run/{rid}")).json()
            parts = {p: (await c.get(f"/api/v1/v3/flow/run/{rid}/body", params={"part": p})).json()
                     for p in ("prompt", "prompt_tail", "input_payload", "output_payload")}
        # the node route's latest-prompt read (the node route itself needs a
        # registered descriptor in the graph; this is its exact query and helper)
        async with store.acquire() as conn:
            latest = await flow_api._hydrated_row(conn, await conn.fetchrow(flow_api.LATEST_PROMPT_SQL, aid))
        latest["prompt_tail"] = flow_api._prompt_tail(latest.pop("input_payload", None))
        return detail, parts, flow_api._prompt_block({}, latest)["rendered"]

    before = await _read()
    assert before[0]["prompt_chars"] == 5000 + len("...[TRUNCATED: x]")
    assert before[0]["output_payload_bytes"] > 3000 and before[2]["truncated"] is True
    assert before[2]["total_chars"] == 9000
    assert (await compact(pool, _ON, now=_NOW + timedelta(days=30), analyst_ids=[aid])).traces == 1
    assert await _read() == before


async def test_layers_route_on_a_compacted_receipt(pool, store, monkeypatch) -> None:
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    from legba.data.registry import layers_api

    aid = layers_api.ANALYST_ID
    async with store.acquire() as conn:
        await conn.execute("DELETE FROM analyst_traces WHERE analyst_id = $1", aid)
    payload = {"data": {"targets": [{"target_id": "zz_desk", "country": "ZZ", "layers": {}}],
                        "as_of": "2026-08-01"}}
    try:
        await _record(pool, aid, 0, started=_OLD, output_payload=payload)

        async def _read():
            async with await _client(layers_api.build_layers_router(_deps(store)), "/api/v1/v3") as c:
                r = await c.get("/api/v1/v3/layers/divergence")
            assert r.status_code == 200, r.text
            body = r.json()
            body.pop("generated_at", None)
            return body

        before = await _read()
        assert before["receipt_run_id"] is not None and before["desks"]
        assert (await compact(pool, _ON, now=_NOW + timedelta(days=30), analyst_ids=[aid])).traces == 1
        assert await _read() == before
    finally:
        async with store.acquire() as conn:
            await conn.execute("DELETE FROM analyst_traces WHERE analyst_id = $1", aid)


async def test_optimizer_training_rows_on_compacted_runs(pool, store) -> None:
    from legba.data.analysts.optimizer import read_traces_and_critiques

    aid = _aid("opt")
    for i in range(5):
        await _record(pool, aid, i, started=_OLD)

    async def _read():
        async with store.acquire() as conn:
            return await read_traces_and_critiques(conn, analyzed_analyst_id=aid, read_window_days=3650)

    before = await _read()
    assert len(before) == 5 and all(isinstance(r["input"], str) and r["gold"] for r in before)
    assert (await compact(pool, _ON, now=_NOW + timedelta(days=30), analyst_ids=[aid])).traces == 5
    assert await _read() == before


async def test_the_daily_retention_run_compacts_when_switched_on(pool, monkeypatch) -> None:
    from legba.data.analysts.deterministic_handlers import analyst_traces_retention
    from legba.runtime.deps import StandardDeps

    aid = _aid("daily")
    for i in range(3):
        await _record(pool, aid, i, started=_OLD)
    real = trace_bulk_compact.compact

    async def _only_this_analyst(p, settings, **kw):
        assert settings.enabled and settings.age_days == 30
        return await real(p, settings, analyst_ids=[aid], **kw)

    monkeypatch.setattr(trace_bulk_compact, "compact", _only_this_analyst)
    monkeypatch.setenv("LEGBA_TRACE_COMPRESS", "1")
    res = await analyst_traces_retention.handle([], {"ttl_days": 0}, StandardDeps(pg_pool=pool))
    d = res.finding.data
    assert d["compress_traces"] == 3 and d["compress_bundles"] == 1 and d["compress_failures"] == 0
    assert d["traces_purged"] == 0 and "traces_compressed" in res.finding.tags
    assert "compress_traces=3" in res.finding.body
