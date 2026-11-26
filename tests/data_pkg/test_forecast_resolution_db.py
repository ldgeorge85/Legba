# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""On a migrated test DB — the 0244 guards the fakes cannot prove.

  * the migration re-applies with zero change;
  * the real store writes a proposal (idempotent on the fingerprint), a
    confirmation, an override (with its override_log row) and a correction
    through asyncpg, and the question's status moves under the 0242 seal;
  * UPDATE and DELETE on a record are refused; the CHECKs refuse an act
    without a resolution, an override without a reason;
  * the record leaves and the latest-records read run on the real schema.

Container suite (``migrated_pg``); the fake-only twin is
``test_forecast_resolution.py``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from legba.data import forecast_ledger as fl
from legba.data import forecast_resolution as fr
from legba.data import forecast_resolvers as rules
from legba.data.config import PostgresConfig

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATIONS = Path(__file__).resolve().parents[2] / "src/legba/data/migrations"
TABLES = (
    "forecast_resolution_records", "forecast_resolutions", "forecast_resolution_proposals",
    "forecast_numbers", "forecast_questions", "scorecard_entries", "override_log",
)
SEALED = datetime(2026, 3, 1, tzinfo=timezone.utc)
DUE = datetime(2026, 3, 20, 23, 59, 59, tzinfo=timezone.utc)
SPENT = DUE + timedelta(hours=73)


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig, clean_tables):
    await clean_tables(*TABLES)
    c = await asyncpg.connect(migrated_pg.dsn)
    try:
        yield c
    finally:
        await c.close()


async def _sealed(conn) -> dict:
    q = fl.ForecastQuestion(
        slug="FX1-db", question="A fictional exchange is carried out.", resolver_kind="event_reported",
        resolver_spec={"class": "prisoner exchange", "actors": ["RU", "UA"],
                       "window": ["2026-03-01", "2026-03-20"]},
        due_at=DUE, window_start=SEALED, window_end=DUE,
        void_rules="Neither government states anything and no outlet reports on an exchange by due + grace.",
    )
    _, qid = await fl.upsert_question(conn, q)
    await fl.state_number(conn, qid, fl.ForecastNumber("operator", "operator", 0.6, SEALED))
    await fl.seal_question(conn, qid, at=SEALED)
    row = dict(await conn.fetchrow(fl._SELECT_QUESTION_BY_ID, qid))
    row["resolver_spec"] = fl._json_field(row["resolver_spec"])
    return row


def _proposal(q: dict, outcome: str = "needs_judgement") -> rules.Proposal:
    return rules.evaluate(q, {"main": []}, rules.ResolverContext(as_of=SPENT)) if outcome == "needs_judgement" \
        else rules.Proposal(outcome=outcome, resolver_kind="event_reported", as_of=SPENT,
                            evidence=[{"ref": "signal:00000000-0000-0000-0000-000000000001",
                                       "url": "https://example.org/a"}])


async def test_0244_reapplies_with_zero_change(conn):
    body = (MIGRATIONS / "0244_forecast_resolution_records.sql").read_text()
    before = await conn.fetchval("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal") == before


async def test_the_record_lifecycle_on_the_real_schema(conn):
    q = await _sealed(conn)
    p = _proposal(q)
    assert p.outcome == "needs_judgement"
    status, rec = await fr.write_proposal(conn, q, p)
    assert status == "written"
    assert (await fr.write_proposal(conn, q, p))[0] == "unchanged"
    oid = await fr.override_resolution(conn, q["id"], outcome="no", actor="operator:1",
                                       reason="quiet no-show is a no", judgements=["main:quiet_no_show"],
                                       basis_record_id=rec, now=SPENT)
    assert await conn.fetchval("SELECT status FROM forecast_questions WHERE id = $1::uuid", q["id"]) == "resolved"
    assert await conn.fetchval("SELECT count(*) FROM override_log") == 1
    cid = await fr.correct_resolution(conn, q["id"], outcome="void", actor="operator:1",
                                      reason="premise failed", now=SPENT)
    assert await conn.fetchval(
        "SELECT supersedes_id::text FROM forecast_resolution_records WHERE id = $1::uuid", cid) == oid
    assert await conn.fetchval("SELECT status FROM forecast_questions WHERE id = $1::uuid", q["id"]) == "void"
    leaves = await fr.record_leaf_rows(conn)
    assert len(leaves) == 3 and {r["kind"] for r in leaves} == {"forecast_resolution_record"}
    latest = await fr.latest_records(conn, [q["id"]])
    assert latest[q["id"]]["proposal"]["record_id"] == rec
    assert latest[q["id"]]["act"]["record_kind"] == "correction"


async def test_records_refuse_update_delete_and_unsound_acts(conn):
    q = await _sealed(conn)
    _, rec = await fr.write_proposal(conn, q, _proposal(q))
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute("UPDATE forecast_resolution_records SET outcome = 'yes' WHERE id = $1::uuid", rec)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute("DELETE FROM forecast_resolution_records WHERE id = $1::uuid", rec)
    args = fr._record_args(q["id"], q["content_sha256"], record_kind="confirmation", outcome="yes",
                           resolver_kind="event_reported", resolver_version="v", as_of=SPENT,
                           actor="operator:1", fingerprint="a" * 64)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):   # an act without its resolution
        await conn.fetchrow(fr.INSERT_RECORD_SQL, *args)
    args = fr._record_args(q["id"], q["content_sha256"], record_kind="proposal", outcome="maybe",
                           resolver_kind="event_reported", resolver_version="v", as_of=SPENT,
                           actor="x", fingerprint="b" * 64)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.fetchrow(fr.INSERT_RECORD_SQL, *args)
