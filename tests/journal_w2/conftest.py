# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Conftest for the Journal Wave 2 (consolidation tier) tests.

ISOLATION (same rule as Wave 1): these tests run against a DISPOSABLE throwaway
Postgres+AGE container — NEVER the live ``legba`` / ``legba-postgres-1``. We
REUSE the Wave-1 disposable harness verbatim (same 5544 default, same fresh
per-session ``legba_w1_test_<uuid>`` DB created + dropped), so there is one
disposable fixture, not two. Run with::

    PYTHONPATH=src LEGBA_W1_PG_PORT=5544 pytest tests/journal_w2 -q
"""

from __future__ import annotations

import asyncpg
import pytest_asyncio

from legba.data.config import PostgresConfig

# Re-export the Wave-1 disposable fixtures (session DB create/migrate/drop +
# pg_pool + per-test truncate) so this package binds to the SAME disposable
# instance, never the live port.
from tests.journal_w1.conftest import (  # noqa: F401
    _clean_tables,
    migrated_pg,
    pg_pool,
    w1_pg_config,
)


@pytest_asyncio.fixture
async def pg_conn(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    yield conn
    await conn.close()
