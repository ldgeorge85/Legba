# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Conftest for the Journal Assessor Wave 4 (propose-and-gate) DB tests.

ISOLATION (mandatory): these tests run against a DISPOSABLE throwaway
Postgres+AGE container the operator/CI spins up — NEVER the live ``legba`` /
``legba-postgres-1`` (port 5432). The container host:port is configurable via
``LEGBA_W4_PG_HOST`` / ``LEGBA_W4_PG_PORT`` (defaults 127.0.0.1:5544 — the same
disposable port the Wave-1 conftest uses; 5432 is the LIVE db and is never
touched). A fresh ``legba_w4_test_<uuid>`` DB is created per session inside that
disposable instance and dropped at teardown.

Run with::

    PYTHONPATH=src LEGBA_W4_PG_PORT=5544 pytest tests/journal_w4 -q
"""

from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.migrate import apply_primary_migrations

_HOST = os.environ.get("LEGBA_W4_PG_HOST", os.environ.get("LEGBA_W1_PG_HOST", "127.0.0.1"))
_PORT = int(os.environ.get("LEGBA_W4_PG_PORT", os.environ.get("LEGBA_W1_PG_PORT", "5544")))
_USER = os.environ.get("LEGBA_W4_PG_USER", "legba")
_PASSWORD = os.environ.get("LEGBA_W4_PG_PASSWORD", "legba")
_ADMIN_DB = os.environ.get("LEGBA_W4_PG_ADMIN_DB", os.environ.get("LEGBA_W1_PG_ADMIN_DB", "legba_w1"))


def _admin_dsn() -> str:
    return f"postgresql://{_USER}:{_PASSWORD}@{_HOST}:{_PORT}/{_ADMIN_DB}"


@pytest_asyncio.fixture(scope="session")
async def w4_pg_config() -> PostgresConfig:
    """Create a fresh disposable test DB, migrate it (head 0048 → journal_proposals
    + journal_entries exist), drop it at session end. Skips (never errors) when the
    disposable container is unreachable, so the suite degrades cleanly."""
    try:
        conn = await asyncpg.connect(_admin_dsn())
    except Exception as exc:  # disposable container not up — skip, never touch live
        pytest.skip(
            f"disposable Postgres at {_HOST}:{_PORT} unreachable ({exc}); "
            "start it (apache/age) before running tests/journal_w4"
        )
    db_name = f"legba_w4_test_{uuid4().hex[:10]}"
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


@pytest_asyncio.fixture
async def pg_pool(w4_pg_config: PostgresConfig):
    pool = await asyncpg.create_pool(w4_pg_config.dsn, min_size=1, max_size=4)
    try:
        yield pool
    finally:
        await pool.close()


# The live tables a propose_* call must NEVER write, plus the journal tables the
# tests assert on. Truncated before each test so a row from one test never leaks.
_TRUNCATE_TABLES = (
    "journal_proposals",
    "journal_entries",
    "facts",
    "hypotheses",
    "nexuses",
    "situations",
    "analyst_outputs",
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE " + ", ".join(_TRUNCATE_TABLES) + " RESTART IDENTITY CASCADE"
        )
    yield
