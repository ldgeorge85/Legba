# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-S — migration 0247 and ``scripts/backfill_signal_url_hash.py``.

The migration builds the two indexes only on a small table; on the live table
the script builds them CONCURRENTLY. So the script's definitions must be the
migration's, byte for byte (normalised whitespace), and both must produce the
same ``pg_indexes`` rows. The backfill is dry-run by default, fills exactly
the pending rows in bounded batches, never overwrites a row whose URL changed
under it, and ``--verify`` / ``--recompute`` find and fix stale keys.
"""

from __future__ import annotations

import importlib.util
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from legba.data._url_canon import stored_canonical_url_hash
from legba.data.config import PostgresConfig
from legba.data.migrations import MIGRATIONS_DIR

REPO = Path(__file__).resolve().parents[2]
MIGRATION = MIGRATIONS_DIR / "0247_signals_canonical_url_hash.sql"
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _script():
    spec = importlib.util.spec_from_file_location(
        "backfill_signal_url_hash", REPO / "scripts" / "backfill_signal_url_hash.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def test_script_index_definitions_are_the_migrations() -> None:
    text = _norm(MIGRATION.read_text())
    for name, definition in _script().INDEXES.items():
        assert _norm(f"CREATE INDEX IF NOT EXISTS {name} {definition};") in text, name


def test_migration_is_additive_and_bounded() -> None:
    body = MIGRATION.read_text()
    code = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("--"))
    assert "ADD COLUMN IF NOT EXISTS canonical_url_hash TEXT;" in code
    assert "DEFAULT" not in code.upper()  # catalog-only add: nullable, no default
    assert "lock_timeout" in code
    assert not re.search(r"\b(DROP|UPDATE|DELETE|COMMIT)\b", code)
    assert not re.search(r"^\s*BEGIN\s*;", code, re.M)  # the DO block's BEGIN is not a txn


@pytest.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    try:
        yield c
    finally:
        await c.close()


async def _indexdefs(conn) -> dict[str, str]:
    return {r["indexname"]: r["indexdef"] for r in await conn.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'signals' "
        "AND indexname LIKE 'idx_signals_canonical_url_%'")}


async def test_migration_built_both_indexes_on_a_small_table(conn) -> None:
    assert await conn.fetchval(
        "SELECT data_type FROM information_schema.columns WHERE table_name = 'signals' "
        "AND column_name = 'canonical_url_hash'") == "text"
    defs = await _indexdefs(conn)
    assert set(defs) == set(_script().INDEXES)
    states = await _script().index_state(conn)
    assert set(states.values()) == {"valid"}


async def test_create_indexes_concurrently_rebuilds_identical_indexes(conn) -> None:
    mod = _script()
    before = await _indexdefs(conn)
    for name in mod.INDEXES:
        await conn.execute(f"DROP INDEX IF EXISTS public.{name}")
    assert set((await mod.index_state(conn)).values()) == {"missing"}
    states = await mod.create_indexes(conn, log=lambda *_: None)
    assert set(states.values()) == {"valid"}
    assert await _indexdefs(conn) == before
    # Idempotent.
    assert set((await mod.create_indexes(conn, log=lambda *_: None)).values()) == {"valid"}


async def test_backfill_dry_run_apply_verify_recompute(conn) -> None:
    mod = _script()
    tenant = f"ns_bf_{uuid4().hex[:8]}"
    urls = [f"https://www.Example.org/{i}?b=2&a=1#f" for i in range(57)] + ["   "]
    ids = [uuid4() for _ in urls]
    try:
        await conn.executemany(
            """INSERT INTO signals (id, source_id, owner_tenant, payload, canonical_url, fetched_at)
               VALUES ($1, 's', $2, '{}'::jsonb, $3, $4)""",
            [(i, tenant, u, T0 + timedelta(seconds=n % 5)) for n, (i, u) in enumerate(zip(ids, urls))]
            + [(uuid4(), tenant, None, T0), (uuid4(), tenant, "", T0)])
        mine = "owner_tenant = $1"
        pending = await conn.fetchval(
            f"SELECT count(*) FROM signals WHERE {mine} AND canonical_url_hash IS NULL "
            "AND canonical_url <> ''", tenant)
        assert pending == len(urls)

        # Dry run writes nothing.
        assert await mod.dry_run(conn, batch_size=10, log=lambda *_: None) == 0
        assert await conn.fetchval(
            f"SELECT count(*) FROM signals WHERE {mine} AND canonical_url_hash IS NOT NULL", tenant) == 0

        # A concurrent writer changes one URL after the batch was read: that
        # row is skipped by the guard, not overwritten with a stale key.
        batch = await mod.next_batch(conn, 10)
        moved = batch[0]["id"]
        await conn.execute("UPDATE signals SET canonical_url = 'https://moved.example/' WHERE id = $1", moved)
        assert await mod.apply_batch(conn, batch) == len(batch) - 1
        assert await conn.fetchval("SELECT canonical_url_hash FROM signals WHERE id = $1", moved) is None

        out = await mod.backfill(conn, batch_size=10, log=lambda *_: None)
        assert out["batches"] >= 5
        rows = await conn.fetch(
            f"SELECT canonical_url, canonical_url_hash FROM signals WHERE {mine}", tenant)
        for r in rows:
            assert r["canonical_url_hash"] == stored_canonical_url_hash(r["canonical_url"])
        v = await mod.verify(conn)
        assert v["stale"] == 0 and v["pending"] == 0

        # A stale key (as after a rule change) is found and recomputed.
        await conn.execute("UPDATE signals SET canonical_url_hash = 'stale' WHERE id = $1", ids[3])
        assert (await mod.verify(conn))["stale"] == 1
        out = await mod.backfill(conn, batch_size=1000, recompute=True, log=lambda *_: None)
        assert out["updated"] == 1
        assert (await mod.verify(conn))["stale"] == 0
    finally:
        await conn.execute("DELETE FROM signals WHERE owner_tenant = $1", tenant)
