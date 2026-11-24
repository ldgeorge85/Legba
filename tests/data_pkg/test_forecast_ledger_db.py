# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ST0-A on a migrated test DB — the 0242 guards the store's fakes cannot prove.

  * the migration body re-applies with zero catalog change;
  * the real store round-trips (upsert, state, seal, resolve) through asyncpg;
  * a sealed number refuses UPDATE, a sealed question refuses every change but
    ``status``, a sealed question refuses a new number, DELETE is refused on
    every ledger table, a resolution allows only ``active`` true → false;
  * ``forecast_scores`` computes (p − o)²;
  * the anchor's leaf query and the Ledger read run on the real schema.

Container suite (``migrated_pg``); the fake-only twin is
``test_forecast_ledger.py``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from legba.data import forecast_ledger as fl
from legba.data.config import PostgresConfig

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src/legba/data/migrations/0242_forecast_ledger.sql"
)
TABLES = (
    "forecast_resolutions", "forecast_resolution_proposals", "forecast_numbers",
    "forecast_questions", "scorecard_entries", "override_log",
)
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 12, 15, 23, 59, 59, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig, clean_tables):
    await clean_tables(*TABLES)
    c = await asyncpg.connect(migrated_pg.dsn)
    try:
        yield c
    finally:
        await c.close()


def _q(slug: str = "FX1-db") -> fl.ForecastQuestion:
    return fl.ForecastQuestion(
        slug=slug,
        question="A fictional series closes at or above 100 on 2026-12-15",
        resolver_kind="series_threshold",
        resolver_spec={"source": "FICT", "series": "X", "op": ">=", "value": 100, "on": "2026-12-15"},
        due_at=DUE,
    )


async def _sealed(conn) -> str:
    _, qid = await fl.upsert_question(conn, _q())
    await fl.state_number(conn, qid, fl.ForecastNumber("operator", "operator", 0.25, NOW))
    await fl.state_number(conn, qid, fl.ForecastNumber("clef_arm", "llm.decide.fict", 0.6, NOW))
    await fl.seal_question(conn, qid, at=NOW)
    return qid


_CATALOG_SQL = """
SELECT (SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
         WHERE NOT t.tgisinternal AND c.relname = ANY($1::text[])) AS triggers,
       (SELECT count(*) FROM pg_indexes WHERE tablename = ANY($1::text[])) AS indexes,
       (SELECT count(*) FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid
         WHERE c.relname = ANY($1::text[])) AS constraints
"""


async def test_the_migration_reapplies_with_no_change(conn):
    before = dict(await conn.fetchrow(_CATALOG_SQL, list(TABLES)))
    assert before["triggers"] == 6
    await conn.execute(MIGRATION.read_text(encoding="utf-8"))
    after = dict(await conn.fetchrow(_CATALOG_SQL, list(TABLES)))
    assert after == before


async def test_the_store_round_trips_on_the_real_schema(conn):
    qid = await _sealed(conn)
    rows = await fl.fetch_ledger(conn, now=NOW)
    (row,) = rows
    assert row["sealed_at"] is not None
    assert [(n["issuer_kind"], n["p"]) for n in row["numbers"]] == [("operator", 0.25), ("clef_arm", 0.6)]
    assert all(n["sealed"] for n in row["numbers"])
    assert row["resolver_spec"]["series"] == "X"
    leaves = await conn.fetch(fl.LEDGER_LEAVES_SQL)
    assert [r["kind"] for r in leaves] == ["forecast_number", "forecast_number", "forecast_question"]
    rid = await fl.resolve(conn, qid, outcome="yes", resolved_by="resolver:test",
                           now=DUE + timedelta(hours=1))
    scores = {r["issuer_kind"]: float(r["brier"]) for r in await conn.fetch(
        "SELECT issuer_kind, brier FROM forecast_scores WHERE question_id = $1::uuid", qid)}
    assert scores == pytest.approx({"operator": 0.5625, "clef_arm": 0.16})
    assert await conn.fetchval("SELECT status FROM forecast_questions WHERE id = $1::uuid", qid) == "resolved"
    assert rid


async def test_a_sealed_number_refuses_update_and_a_sealed_question_a_new_number(conn):
    qid = await _sealed(conn)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute("UPDATE forecast_numbers SET p = 0.5 WHERE question_id = $1::uuid", qid)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute(
            "INSERT INTO forecast_numbers (question_id, issuer_kind, issuer_ref, p, as_of, content_sha256) "
            "VALUES ($1::uuid, 'consult', 'late', 0.5, now(), repeat('a', 64))", qid)


async def test_a_sealed_question_changes_only_its_status(conn):
    qid = await _sealed(conn)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute("UPDATE forecast_questions SET question = 'rewritten' WHERE id = $1::uuid", qid)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute("UPDATE forecast_questions SET sealed_at = NULL WHERE id = $1::uuid", qid)
    await conn.execute("UPDATE forecast_questions SET status = 'void' WHERE id = $1::uuid", qid)


async def test_an_unsealed_number_may_be_restated(conn):
    _, qid = await fl.upsert_question(conn, _q("FX2-open"))
    await fl.state_number(conn, qid, fl.ForecastNumber("operator", "operator", 0.25, NOW))
    out, _ = await fl.state_number(conn, qid, fl.ForecastNumber("operator", "operator", 0.3, NOW))
    assert out == "updated"


async def test_delete_is_refused_on_every_ledger_table(conn):
    qid = await _sealed(conn)
    await fl.resolve(conn, qid, outcome="no", resolved_by="resolver:test", now=DUE + timedelta(hours=1))
    for table in ("forecast_numbers", "forecast_questions", "forecast_resolutions"):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(f"DELETE FROM {table}")


async def test_a_resolution_only_retires(conn):
    qid = await _sealed(conn)
    await fl.resolve(conn, qid, outcome="no", resolved_by="resolver:test", now=DUE + timedelta(hours=1))
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute("UPDATE forecast_resolutions SET outcome = 'yes'")
    await fl.resolve(conn, qid, outcome="yes", resolved_by="operator:1",
                     now=DUE + timedelta(hours=2), correction_reason="late statement")
    assert await conn.fetchval("SELECT count(*) FROM forecast_resolutions WHERE NOT active") == 1
    assert await conn.fetchval("SELECT count(*) FROM override_log") == 1
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute("UPDATE forecast_resolutions SET active = true WHERE NOT active")
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute("DELETE FROM override_log")
