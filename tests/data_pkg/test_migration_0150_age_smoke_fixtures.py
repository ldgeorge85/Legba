# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0150 — delete the 27 June-17 smoke fixtures from ``legba_graph``.

`legba_graph` never held a production row. Everything in it was written by the
2026-06-17 AGE smoke suite, and an empty graph that *answers* is worse than one
that errors: `/api/v1/graph/path` walked those fixtures, found nothing, and
rendered a confident `detail="no path"` (graph-debate JUDGE_SYNTHESIS §4.3
item 2, live defect #2).

The migration's whole value is its GUARD, so that is what these tests exercise:
it must delete the fixture cohort and it must REFUSE — loudly, without deleting
anything — the moment the graph holds a vertex it does not recognise, because
by then the graph is being fed and a blind wipe destroys real knowledge.

These run the SHIPPED SQL against the pivot substrate inside a transaction that
is always rolled back, so they cannot pass against a migration that says
something different from the one that will run.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from legba.data import migrate as _migrate_mod

MIGRATION = (
    Path(_migrate_mod.__file__).parent / "migrations" / "0150_drop_age_smoke_fixtures.sql"
)

# The signature the migration matches on, mirrored here so a drift in either
# direction is caught rather than silently widening the blast radius.
FIXTURE_TS_PREFIX = "2026-06-17T18:2"
UNDATED_FIXTURE = '{"name": "FromCypher", "origin": "test"}'


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Static guards
# ---------------------------------------------------------------------------


def test_migration_exists_and_is_data_only():
    assert MIGRATION.is_file(), f"{MIGRATION} missing"
    sql = _sql().upper()
    # It removes DATA. It must never remove the graph or a label definition —
    # dropping the graph would take the 11 vlabels / 21 elabels from 0001+0037
    # with it and turn a data cleanup into a schema migration.
    assert "DROP_GRAPH" not in sql
    assert "DROP GRAPH" not in sql
    assert "CREATE_GRAPH" not in sql
    assert "TRUNCATE" not in sql
    assert "DELETE FROM LEGBA_GRAPH._AG_LABEL_VERTEX" in sql


def test_migration_refuses_rather_than_deletes_when_unsure():
    """The guard is the point: an unrecognised vertex must RAISE, not be spared."""
    sql = _sql()
    assert "RAISE EXCEPTION" in sql
    assert "v_total <> v_fixtures" in sql


def test_migration_signature_covers_both_fixture_shapes():
    sql = _sql()
    assert FIXTURE_TS_PREFIX in sql, "the dated smoke cohort is not matched"
    assert UNDATED_FIXTURE in sql, "the one undated smoke vertex is not matched"


# ---------------------------------------------------------------------------
# Behaviour — the shipped SQL against a real AGE substrate
# ---------------------------------------------------------------------------

_PIVOT_DB = {
    "host": os.environ.get("LEGBA_PIVOT_PG_HOST", "127.0.0.1"),
    "port": int(os.environ.get("LEGBA_PIVOT_PG_PORT", "5432")),
    "user": os.environ.get("LEGBA_PIVOT_PG_USER", "legba"),
    "password": os.environ.get("LEGBA_PIVOT_PG_PASSWORD", "legba"),
    "database": os.environ.get("LEGBA_PIVOT_PG_DB", "legba_pivot_test"),
}

_FIXTURE_VERTICES = (
    ("Organization", "SearchOrg1"),
    ("Person", "Boss"),
    ("Person", "Worker"),
    ("Country", "CountryA"),
    ("Concept", "TestMerge"),
)


@pytest.fixture
async def age_conn():
    asyncpg = pytest.importorskip("asyncpg")
    try:
        conn = await asyncpg.connect(**_PIVOT_DB)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"legba_pivot_test unreachable: {exc}")
    try:
        await conn.execute("LOAD 'age'")
        await conn.execute('SET search_path = ag_catalog, "$user", public')
        exists = await conn.fetchval(
            "SELECT count(*) FROM ag_catalog.ag_graph WHERE name = 'legba_graph'"
        )
        if not exists:
            pytest.skip("legba_graph absent on the pivot substrate")
        yield conn
    finally:
        await conn.close()


async def _seed_fixtures(conn) -> None:
    """Recreate the June-17 smoke shape: dated vertices, one undated, one edge."""
    await conn.execute("DELETE FROM legba_graph._ag_label_edge")
    await conn.execute("DELETE FROM legba_graph._ag_label_vertex")
    for label, name in _FIXTURE_VERTICES:
        await conn.execute(
            f"""SELECT * FROM cypher('legba_graph', $$
                CREATE (:{label} {{name: '{name}',
                    entity_id: '00000000-0000-4000-8000-000000000001',
                    created_at: '{FIXTURE_TS_PREFIX}6:58.758806+00:00',
                    updated_at: '{FIXTURE_TS_PREFIX}6:58.758899+00:00'}})
                $$) AS (v agtype)"""
        )
    await conn.execute(
        """SELECT * FROM cypher('legba_graph', $$
            CREATE (:CypherCreated {name: 'FromCypher', origin: 'test'}) $$)
           AS (v agtype)"""
    )
    await conn.execute(
        """SELECT * FROM cypher('legba_graph', $$
            MATCH (a:Person {name:'Worker'}), (b:Person {name:'Boss'})
            CREATE (a)-[:ReportsTo]->(b) $$) AS (v agtype)"""
    )


async def test_migration_deletes_the_smoke_cohort(age_conn):
    tr = age_conn.transaction()
    await tr.start()
    try:
        await _seed_fixtures(age_conn)
        assert await age_conn.fetchval(
            "SELECT count(*) FROM legba_graph._ag_label_vertex"
        ) == len(_FIXTURE_VERTICES) + 1
        assert await age_conn.fetchval(
            "SELECT count(*) FROM legba_graph._ag_label_edge"
        ) == 1

        await age_conn.execute(_sql())

        assert await age_conn.fetchval(
            "SELECT count(*) FROM legba_graph._ag_label_vertex"
        ) == 0, "fixture vertices survived the migration"
        assert await age_conn.fetchval(
            "SELECT count(*) FROM legba_graph._ag_label_edge"
        ) == 0, "fixture edges survived the migration"
        # The labels themselves are untouched — this is a data migration.
        assert await age_conn.fetchval(
            "SELECT count(*) FROM ag_catalog.ag_label WHERE graph = "
            "(SELECT graphid FROM ag_catalog.ag_graph WHERE name='legba_graph')"
        ) > 10
    finally:
        await tr.rollback()


async def test_migration_is_a_noop_on_an_already_empty_graph(age_conn):
    tr = age_conn.transaction()
    await tr.start()
    try:
        await age_conn.execute("DELETE FROM legba_graph._ag_label_edge")
        await age_conn.execute("DELETE FROM legba_graph._ag_label_vertex")
        await age_conn.execute(_sql())  # must not raise
        await age_conn.execute(_sql())  # and must stay idempotent
    finally:
        await tr.rollback()


async def test_migration_refuses_when_the_graph_holds_production_data(age_conn):
    """One id-keyed vertex is enough to stop the whole delete. That is the guard."""
    asyncpg = pytest.importorskip("asyncpg")
    tr = age_conn.transaction()
    await tr.start()
    try:
        await _seed_fixtures(age_conn)
        await age_conn.execute(
            """SELECT * FROM cypher('legba_graph', $$
                CREATE (:Country {id: '11111111-1111-4111-8111-111111111111',
                                  name: 'Iran'}) $$) AS (v agtype)"""
        )
        before = await age_conn.fetchval(
            "SELECT count(*) FROM legba_graph._ag_label_vertex"
        )
        with pytest.raises(asyncpg.PostgresError, match="refusing to delete"):
            await age_conn.execute(_sql())
    finally:
        await tr.rollback()

    # Nothing was deleted — the exception aborted the whole DO block.
    assert before == len(_FIXTURE_VERTICES) + 2
