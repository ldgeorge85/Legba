# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Conftest for the Journal Assessor Wave 1 instrument-tool tests.

ISOLATION (per the Wave-1 brief): these tests run against a DISPOSABLE throwaway
Postgres+AGE container the operator/CI spins up — NEVER the live ``legba`` /
``legba-postgres-1``. The container host:port is configurable via
``LEGBA_W1_PG_HOST`` / ``LEGBA_W1_PG_PORT`` (defaults 127.0.0.1:5544 — a port the
live stack does NOT use; 5432 is the live db). A fresh ``legba_w1_test_<uuid>``
DB is created per session inside that disposable instance and dropped at teardown,
so even within the disposable container the tests never collide.

This deliberately does NOT import the data_pkg conftest (which hardwires
127.0.0.1:5432, the live port). Run with::

    PYTHONPATH=src LEGBA_W1_PG_PORT=5544 pytest tests/journal_w1 -q
"""

from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.migrate import apply_primary_migrations

_HOST = os.environ.get("LEGBA_W1_PG_HOST", "127.0.0.1")
_PORT = int(os.environ.get("LEGBA_W1_PG_PORT", "5544"))
_USER = os.environ.get("LEGBA_W1_PG_USER", "legba")
_PASSWORD = os.environ.get("LEGBA_W1_PG_PASSWORD", "legba")
_ADMIN_DB = os.environ.get("LEGBA_W1_PG_ADMIN_DB", "legba_w1")


def _admin_dsn() -> str:
    return f"postgresql://{_USER}:{_PASSWORD}@{_HOST}:{_PORT}/{_ADMIN_DB}"


@pytest_asyncio.fixture(scope="session")
async def w1_pg_config() -> PostgresConfig:
    """Create a fresh disposable test DB, migrate it, drop it at session end.

    Skips (does NOT error) if the disposable container is unreachable, so the
    suite degrades cleanly when nobody started it.
    """
    try:
        conn = await asyncpg.connect(_admin_dsn())
    except Exception as exc:  # disposable container not up — skip, never touch live
        pytest.skip(
            f"disposable Postgres at {_HOST}:{_PORT} unreachable ({exc}); "
            "start it (apache/age) before running tests/journal_w1"
        )
    db_name = f"legba_w1_test_{uuid4().hex[:10]}"
    try:
        await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()

    cfg = PostgresConfig(
        host=_HOST, port=_PORT, user=_USER, password=_PASSWORD, database=db_name,
    )
    await apply_primary_migrations(cfg)
    yield cfg

    conn = await asyncpg.connect(_admin_dsn())
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname='{db_name}' AND pid <> pg_backend_pid()"
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="session")
async def migrated_pg(w1_pg_config: PostgresConfig) -> PostgresConfig:
    """Alias of ``w1_pg_config`` so the Wave-0 DB tests (off-chain / write_journal)
    can be re-run against the DISPOSABLE container without their data_pkg conftest
    (which hardwires the live 5432 port). Same fixture name → same injection."""
    return w1_pg_config


@pytest_asyncio.fixture
async def pg_pool(w1_pg_config: PostgresConfig):
    pool = await asyncpg.create_pool(w1_pg_config.dsn, min_size=1, max_size=4)
    try:
        yield pool
    finally:
        await pool.close()


# Tables the instrument reads touch — truncated before each test so a row seeded
# by one test never leaks into another's "latest row" / "no data" assertions
# (the session DB is shared across the module for speed).
_TRUNCATE_TABLES = (
    "analyst_outputs",
    "graph_metrics",
    "analyst_traces",
    "source_poll_outcomes",
    "source_descriptors",
    "budget_ledger",
    "budget_demotion_events",
    "journal_entries",
    "situations",
    "nexuses",
    "facts",
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE " + ", ".join(_TRUNCATE_TABLES) + " RESTART IDENTITY CASCADE"
        )
    yield


@pytest_asyncio.fixture
async def port(pg_pool):
    """The production SubstrateQueryPort over the disposable pool (qdrant=None —
    the instrument reads never touch Qdrant)."""
    from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort

    return PostgresQdrantSubstrateQueryPort(pg_pool=pg_pool, qdrant_client=None)
