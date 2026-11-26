# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""0260 — the published evaluation record on the REAL driver.

* the migration is idempotent: applied twice, two tables, the guard and the
  indexes, and no column that could hold a question or an answer;
* the store round-trips through asyncpg: publish (jsonb lists, the seal),
  a correction as version 2 superseding version 1, the read model;
* the guard: an UPDATE of a published run is refused, DELETE is refused on
  both tables, an arm is never updated, an arm cannot be added after its
  run's publication, and the withdrawal goes through ONCE.

Invented rows only, removed afterwards with the guard disabled for the
cleanup alone (the test database only).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data import eval_runs as er
from legba.data.config import PostgresConfig

from ._eval_runs_fakes import arm, run

_MIGRATION = (
    Path(__file__).resolve().parents[2] / "src" / "legba" / "data" / "migrations" / "0260_eval_runs.sql"
)
ACTOR = "user:0b4c2e0e-1111-4222-8333-944455556666"


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    async with pool.acquire() as c:
        await c.execute(_MIGRATION.read_text())
    keys: list[str] = []
    yield SimpleNamespace(pool=pool, keys=keys, acquire=pool.acquire)
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("ALTER TABLE public.eval_run_arms DISABLE TRIGGER trg_eval_run_arms_guard")
            await c.execute("ALTER TABLE public.eval_runs DISABLE TRIGGER trg_eval_runs_guard")
            await c.execute(
                "DELETE FROM public.eval_run_arms WHERE run_id IN "
                "(SELECT id FROM public.eval_runs WHERE run_key = ANY($1::text[]))", keys)
            # newest first: a later version references the one it supersedes
            await c.execute(
                "DELETE FROM public.eval_runs WHERE run_key = ANY($1::text[]) AND supersedes_id IS NOT NULL", keys)
            await c.execute("DELETE FROM public.eval_runs WHERE run_key = ANY($1::text[])", keys)
            await c.execute("ALTER TABLE public.eval_runs ENABLE TRIGGER trg_eval_runs_guard")
            await c.execute("ALTER TABLE public.eval_run_arms ENABLE TRIGGER trg_eval_run_arms_guard")
    await pool.close()


def _key(pg) -> str:
    k = "m0260-" + uuid4().hex[:10]
    pg.keys.append(k)
    return k


async def test_migration_is_idempotent_and_has_no_text_column_for_questions(pg_pool) -> None:
    sql = _MIGRATION.read_text()
    assert "BEGIN;" not in sql.upper() and "COMMIT;" not in sql.upper()
    async with pg_pool.acquire() as conn:
        await conn.execute(sql)
        await conn.execute(sql)
        cols = await conn.fetch(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name IN ('eval_runs', 'eval_run_arms')"
        )
        trig = await conn.fetch(
            "SELECT tgname FROM pg_trigger WHERE tgrelid IN "
            "('public.eval_runs'::regclass, 'public.eval_run_arms'::regclass) AND NOT tgisinternal"
        )
        idx = await conn.fetch("SELECT indexname FROM pg_indexes WHERE tablename IN ('eval_runs', 'eval_run_arms')")
    names = {c["column_name"] for c in cols}
    assert {"run_key", "version", "supersedes_id", "withdrawn_at", "withdrawn_reason", "published_by",
            "score", "ci_low", "ci_high", "n", "by_class", "sample_index"} <= names
    assert not any(w in n for n in names for w in ("answer", "question_text", "gold_text", "prompt"))
    assert {t["tgname"] for t in trig} == {
        "trg_eval_runs_guard", "trg_eval_run_arms_guard", "trg_eval_run_arms_same_transaction"}
    assert {"uq_eval_runs_key_version", "uq_eval_runs_supersedes", "idx_eval_runs_as_of",
            "uq_eval_run_arms_run_key", "uq_eval_run_arms_run_position"} <= {i["indexname"] for i in idx}


async def test_store_round_trip_correction_and_read_model(pg_pool) -> None:
    key = _key(pg_pool)
    async with pg_pool.acquire() as conn:
        v1 = await er.publish(conn, run(key), actor=ACTOR)
        same = await er.publish(conn, run(key), actor=ACTOR)
        assert same["written"] is False and same["id"] == v1["id"]
        v2 = await er.publish(conn, run(key, arms=(arm("A", 0.4), arm("B", 0.5))), actor=ACTOR,
                              correction_reason="arm B corrected")
        rows = [r for r in await er.fetch_runs(conn, limit=500) if r["run_key"] == key]
        stored = await conn.fetchrow("SELECT published_by, content_sha256 FROM public.eval_runs WHERE id = $1::uuid",
                                     v2["id"])
    assert [(r["version"], r["status"]) for r in rows] == [(2, "current"), (1, "superseded")]
    assert rows[1]["superseded_by"] == v2["id"] and rows[0]["supersedes"] == v1["id"]
    assert rows[0]["caveats"] == ["An invented caveat."] and rows[0]["gold_versions"] == ["gold v1"]
    assert rows[0]["results"][1]["by_class"][0] == {"class": "ALPHA", "score": 0.5, "n": 6}
    assert stored["published_by"] == ACTOR and stored["content_sha256"] == v2["content_sha256"]


async def test_the_guard(pg_pool) -> None:
    key = _key(pg_pool)
    async with pg_pool.acquire() as conn:
        pub = await er.publish(conn, run(key), actor=ACTOR)
        rid = pub["id"]
        with pytest.raises(asyncpg.CheckViolationError, match="append-only"):
            await conn.execute("UPDATE public.eval_runs SET title = 'changed' WHERE id = $1::uuid", rid)
        with pytest.raises(asyncpg.CheckViolationError, match="append-only"):
            # a withdrawal that also changes the content is not a withdrawal
            await conn.execute(
                "UPDATE public.eval_runs SET withdrawn_at = now(), withdrawn_by = 'x', withdrawn_reason = 'r', "
                "title = 'changed' WHERE id = $1::uuid", rid)
        with pytest.raises(asyncpg.CheckViolationError, match="append-only"):
            await conn.execute("DELETE FROM public.eval_runs WHERE id = $1::uuid", rid)
        with pytest.raises(asyncpg.CheckViolationError, match="append-only"):
            await conn.execute("UPDATE public.eval_run_arms SET score = 0.99 WHERE run_id = $1::uuid", rid)
        with pytest.raises(asyncpg.CheckViolationError, match="append-only"):
            await conn.execute("DELETE FROM public.eval_run_arms WHERE run_id = $1::uuid", rid)
        with pytest.raises(asyncpg.CheckViolationError, match="only with its run"):
            await conn.execute(er._INSERT_ARM, rid, 9, "Z", "Late arm", "Added after publication.",
                               None, None, 0.9, None, None, 3, "[]")
        with pytest.raises(asyncpg.CheckViolationError):
            # withdrawn needs all three, with a reason
            await conn.execute("UPDATE public.eval_runs SET withdrawn_at = now() WHERE id = $1::uuid", rid)
        w = await er.withdraw(conn, key, actor=ACTOR, reason="invented withdrawal")
        assert w["withdrawn_at"] is not None
        with pytest.raises(asyncpg.CheckViolationError, match="append-only"):
            await conn.execute("UPDATE public.eval_runs SET withdrawn_reason = 'rewritten' WHERE id = $1::uuid", rid)
        row = await conn.fetchrow(
            "SELECT title, withdrawn_by, withdrawn_reason FROM public.eval_runs WHERE id = $1::uuid", rid)
    assert dict(row) == {"title": "Invented evaluation", "withdrawn_by": ACTOR, "withdrawn_reason": "invented withdrawal"}
