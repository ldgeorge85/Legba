# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Re-run the Wave-0 DB-backed tests (off-chain enforcement + write_journal /
supersede_prior_consolidation) against the DISPOSABLE container.

The Wave-0 suites live under tests/data_pkg, whose conftest hardwires the live
127.0.0.1:5432 port. To honor the Wave-1 isolation rule (NEVER the live
``legba`` / ``legba-postgres-1``) we re-import their test functions here, where
the journal_w1 conftest supplies a ``migrated_pg`` / ``pg_conn`` pointed at the
disposable 5544 instance. The asserted behavior is IDENTICAL — this module only
redirects the DB the same tests run against, proving Wave 1 did not regress the
off-chain invariant or the consolidation supersession machinery.
"""

from __future__ import annotations

import asyncpg
import pytest_asyncio

from legba.data.config import PostgresConfig

# Off-chain enforcement (§3.5) — the gating test must stay green.
from tests.data_pkg.test_journal_off_chain import (  # noqa: F401
    test_downstream_walk_never_returns_journal,
    test_journal_not_a_valid_lineage_root,
    test_journal_table_not_in_lineage_catalog,
)

# write_journal / supersede_prior_consolidation (§8).
from tests.data_pkg.test_writes_journal import (  # noqa: F401
    test_consolidation_supersession_bootstrap_and_close,
    test_entries_never_supersede,
    test_journal_payload_validates_and_claims,
    test_output_kind_journal_registered,
    test_supersede_prior_consolidation_idempotent_on_replay,
    test_write_journal_routes_to_table_and_derived_from_empty,
)


@pytest_asyncio.fixture
async def pg_conn(migrated_pg: PostgresConfig):
    """A connection to the disposable DB (the Wave-0 tests use this fixture name)."""
    conn = await asyncpg.connect(migrated_pg.dsn)
    yield conn
    await conn.close()
