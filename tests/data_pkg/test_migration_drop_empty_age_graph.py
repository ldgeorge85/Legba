# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""20261115T1829_drop_empty_age_graph - drop the AGE graph only if it is empty.

The migration's whole value is its GUARD: it drops
the graph when every label table holds zero rows, and REFUSES (loudly, with
nothing dropped and no ledger row) when any row exists. These run the SHIPPED
SQL against a scratch database migrated to everything except this file.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.migrate import apply_files
from legba.data.migrations import MIGRATIONS_DIR, ordered_migration_files

NAME = "20261115T1829_drop_empty_age_graph.sql"
MIGRATION = Path(MIGRATIONS_DIR) / NAME
_ADMIN_DSN = "postgresql://legba:legba@127.0.0.1:5432/postgres"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Static guards (no database)
# ---------------------------------------------------------------------------


def test_it_is_time_named_and_ordered_last():
    assert MIGRATION.is_file()
    assert ordered_migration_files()[-1].name >= NAME


def test_it_checks_before_it_drops_and_never_writes_or_dumps():
    sql = _sql()
    assert "RAISE EXCEPTION" in sql and "REFUSED" in sql
    assert sql.index("RAISE EXCEPTION") < sql.index("drop_graph")
    upper = sql.upper()
    for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE", "COPY ", "DROP EXTENSION", "DROP SCHEMA AG_CATALOG"):
        assert forbidden not in upper, forbidden


# ---------------------------------------------------------------------------
# Behaviour - a scratch database where the graph exists
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def scratch():
    """A database migrated to everything BEFORE the drop, so the (empty) graph
    from the baseline is still there. Yields an open connection."""
    from tests._pg_test_server_guard import assert_test_server

    await assert_test_server()
    name = f"legba_agedrop_{uuid4().hex[:10]}"
    admin = await asyncpg.connect(_ADMIN_DSN)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    cfg = PostgresConfig(
        host="127.0.0.1", port=5432, user="legba", password="legba", database=name
    )
    conn = await asyncpg.connect(cfg.dsn)
    try:
        before = [p for p in ordered_migration_files() if p.name != NAME]
        await apply_files(conn, before)
        yield conn
    finally:
        await conn.close()
        admin = await asyncpg.connect(_ADMIN_DSN)
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname='{name}' AND pid <> pg_backend_pid()"
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()


async def _graph_present(conn) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT count(*) FROM ag_catalog.ag_graph WHERE name='legba_graph'"
        )
    )


async def _applied(conn) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT count(*) FROM legba_data_migrations WHERE name=$1", NAME
        )
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_refuses_a_nonempty_graph_then_drops_it_once_empty(scratch):
    conn = scratch
    await conn.execute("LOAD 'age'")
    await conn.execute('SET search_path = ag_catalog, "$user", public')
    assert await _graph_present(conn), "the baseline should have created the graph"
    await conn.execute(
        "SELECT * FROM cypher('legba_graph', $$ CREATE (:Entity {id: 'x'}) $$) AS (v agtype)"
    )

    with pytest.raises(asyncpg.PostgresError, match="REFUSED"):
        await apply_files(conn, [MIGRATION])
    # Nothing dropped, and the ledger does not record the file, so it retries.
    assert await _graph_present(conn)
    assert await conn.fetchval("SELECT count(*) FROM legba_graph.\"Entity\"") == 1
    assert not await _applied(conn)

    await conn.execute("DELETE FROM legba_graph.\"Entity\"")
    applied = await apply_files(conn, [MIGRATION])
    assert applied == [NAME]
    assert not await _graph_present(conn)
    assert await conn.fetchval("SELECT to_regnamespace('legba_graph')") is None
    # The extension and ag_catalog stay.
    assert await conn.fetchval("SELECT 1 FROM pg_extension WHERE extname='age'") == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_empty_graph_drops_and_a_second_run_is_a_no_op(scratch):
    conn = scratch
    assert await _graph_present(conn)
    assert await apply_files(conn, [MIGRATION]) == [NAME]
    assert not await _graph_present(conn)
    # Re-applying by ledger name applies nothing; running the SQL itself again
    # on a database without the graph is a NOTICE, not an error.
    assert await apply_files(conn, [MIGRATION]) == []
    await conn.execute("LOAD 'age'")
    await conn.execute(_sql())
    assert not await _graph_present(conn)
