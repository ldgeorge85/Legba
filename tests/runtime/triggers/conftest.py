# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixtures for the P-10 coalescing-trigger integration tests (live dev rig).

No mocks. Each fixture connects the real dev-rig Postgres + NATS:

  * ``trig_pg``    — a connected :class:`PostgresStore` on a FRESH migrated
                     ``legba_trig_test_<uuid>`` DB (full 0001-0024 chain, so the
                     source-first ``signals`` + ``signal_aliases`` tables exist)
                     with the P-10 ``trigger_state`` ledger ensured. Per-test DB
                     keeps runs isolated + repeatable.
  * ``trig_state`` — a :class:`TriggerStateStore` over ``trig_pg``.
  * ``trig_nats``  — a connected :class:`NatsStore` on the dev-rig NATS.
"""

from __future__ import annotations

import os
import socket
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

os.environ.setdefault("LEGBA_DATA_PG_HOST", "127.0.0.1")
os.environ.setdefault("LEGBA_DATA_PG_PORT", "5432")
os.environ.setdefault("LEGBA_DATA_PG_USER", "legba")
os.environ.setdefault("LEGBA_DATA_PG_PASSWORD", "legba")
os.environ.setdefault("LEGBA_DATA_NATS_URL", "nats://127.0.0.1:4222")

from legba.data.config import NatsConfig, PostgresConfig
from legba.data.migrate import apply_primary_migrations
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.runtime.triggers.state import TriggerStateStore

ADMIN_DSN = "postgresql://legba:legba@127.0.0.1:5432/postgres"


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest_asyncio.fixture
async def trig_pg():
    if not _port_open("127.0.0.1", 5432):
        pytest.skip("dev-rig Postgres not reachable on 127.0.0.1:5432")
    db_name = f"legba_trig_test_{uuid4().hex[:10]}"
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()

    cfg = PostgresConfig(
        host="127.0.0.1", port=5432, user="legba", password="legba", database=db_name
    )
    applied = await apply_primary_migrations(cfg)
    assert applied, "expected migrations to apply"

    store = PostgresStore(cfg)
    await store.connect()
    # P-10 owns the trigger_state ledger (additive, not in the frozen 0024).
    await TriggerStateStore(store.pool).ensure_schema()
    try:
        yield store
    finally:
        await store.close()
        conn = await asyncpg.connect(ADMIN_DSN)
        try:
            await conn.execute(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname='{db_name}' AND pid <> pg_backend_pid()"
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            await conn.close()


@pytest_asyncio.fixture
async def trig_state(trig_pg: PostgresStore):
    return TriggerStateStore(trig_pg.pool)


@pytest_asyncio.fixture
async def trig_nats():
    if not _port_open("127.0.0.1", 4222):
        pytest.skip("dev-rig NATS not reachable on 127.0.0.1:4222")
    store = NatsStore(NatsConfig.from_env())
    await store.connect()
    yield store
    await store.close()
