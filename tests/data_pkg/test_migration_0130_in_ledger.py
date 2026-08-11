# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0130 is picked up by the runner on a FRESH substrate.

The runner globs ``*.sql`` and applies in sorted filename order, so a migration
that is misnamed is not an error — it is simply never applied, and nothing says
so. That is the same silent-absence shape the rest of this train exists to fix,
so it gets a test rather than an assumption.
"""

from __future__ import annotations

import asyncpg
import pytest

from legba.data.config import PostgresConfig

MIGRATION_NAME = "0130_quarantine_subfloor_embeddings.sql"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_0130_applies_on_a_fresh_substrate(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        row = await conn.fetchrow(
            "SELECT name, sha256 FROM legba_data_migrations WHERE name = $1",
            MIGRATION_NAME,
        )
        assert row is not None, (
            f"{MIGRATION_NAME} is not in the ledger — the runner globs *.sql and "
            "applies in sorted filename order, so a misnamed file is skipped "
            "silently rather than failing"
        )
        assert len(row["sha256"]) == 64
    finally:
        await conn.close()
