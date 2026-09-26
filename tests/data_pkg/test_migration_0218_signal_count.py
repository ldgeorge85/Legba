# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0218 — the ``events.signal_count`` correction, against real SQL.

Every property here is a property of the DATABASE and of the migration file
itself, never of a transcription of it: the tests read
``0218_events_signal_count_correction.sql`` off disk and execute THAT, so a
divergence between the file the reviewer applies at the roll and the
statement proven here cannot happen.

What is pinned: the correction lands the link table's own membership on a
drifted row (including the fan-out shape that caused the live drift — a row
carrying ``n_links * n_actor_links``), zeroes a row that claims members it
has none of, is idempotent (a second execution changes nothing), and leaves
``idx_events_analyst_updated`` behind. Plus the runner constraint that picked
the index form: the migration body runs inside a transaction, so
``CONCURRENTLY`` is not available to it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.migrations import MIGRATIONS_DIR

MIGRATION_NAME = "0218_events_signal_count_correction.sql"

#: Every row this file writes carries this producer prefix, so the fixture can
#: clean up after itself on the session-scoped shared ``migrated_pg`` DB.
_TAG = "analyst.mig0218"


def _migration_sql() -> str:
    return (MIGRATIONS_DIR / MIGRATION_NAME).read_text(encoding="utf-8")


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    yield c
    await c.execute("DELETE FROM events WHERE analyst_id LIKE $1", f"{_TAG}%")
    await c.close()


async def _event(
    c: asyncpg.Connection,
    *,
    claimed: int,
    members: int,
    actors: int = 0,
    sources: int = 1,
    claimed_sources: int | None = None,
) -> UUID:
    """One event row claiming ``claimed`` members while linking ``members``.

    ``actors`` entity links reproduce the multiplier that made the live number
    what it was — the correction must ignore them entirely.
    """
    analyst = f"{_TAG}_{uuid4().hex[:8]}"
    event_id = await c.fetchval(
        "INSERT INTO events (event_signature, analyst_id, title,"
        " signal_count, distinct_source_count)"
        " VALUES ($1, $2, 'drifted', $3, $4) RETURNING id",
        f"evt:mig0218-{uuid4().hex[:8]}#evt:ir",
        analyst,
        claimed,
        claimed if claimed_sources is None else claimed_sources,
    )
    now = datetime.now(tz=timezone.utc)
    for i in range(members):
        await c.execute(
            "INSERT INTO signal_event_links (signal_id, event_id, linked_at,"
            " source_id) VALUES ($1, $2, $3, $4)",
            uuid4(), event_id, now - timedelta(minutes=i),
            f"src_{i % sources}",
        )
    for _ in range(actors):
        entity_id = await c.fetchval(
            "INSERT INTO entity_profiles (data, canonical_name)"
            " VALUES ('{}'::jsonb, $1) RETURNING id",
            f"Mig0218 {uuid4().hex[:8]}",
        )
        await c.execute(
            "INSERT INTO event_entity_links (event_id, entity_id, role)"
            " VALUES ($1, $2, 'actor')",
            event_id, entity_id,
        )
    return event_id


async def _rollups(c: asyncpg.Connection, event_id: UUID) -> tuple[int, int]:
    row = await c.fetchrow(
        "SELECT signal_count, distinct_source_count FROM events WHERE id = $1",
        event_id,
    )
    return int(row["signal_count"]), int(row["distinct_source_count"])


# ---------------------------------------------------------------------------
# The correction
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_correction_lands_the_link_tables_own_membership(conn) -> None:
    """A row carrying the fan-out product comes back to its real link count."""
    # 7 members x 23 actors = 161, the live shape: the number is a product of
    # the two link tables, and 7 is the truth.
    event_id = await _event(conn, claimed=161, members=7, actors=23, sources=3)
    assert await _rollups(conn, event_id) == (161, 161)

    await conn.execute(_migration_sql())

    assert await _rollups(conn, event_id) == (7, 3)
    # The actor links are untouched — the correction reads one table.
    assert await conn.fetchval(
        "SELECT count(*) FROM event_entity_links WHERE event_id = $1", event_id
    ) == 23


@pytest.mark.integration
@pytest.mark.asyncio
async def test_correction_zeroes_an_event_with_no_links(conn) -> None:
    """A row claiming members it has none of is corrected, not skipped."""
    event_id = await _event(conn, claimed=4, members=0)
    await conn.execute(_migration_sql())
    assert await _rollups(conn, event_id) == (0, 0)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_correction_is_idempotent(conn) -> None:
    """Twice over the same rows is the same result — and the second pass
    corrects nothing, because the first left nothing to correct."""
    drifted = await _event(conn, claimed=99, members=5, actors=2, sources=2)
    already_right = await _event(
        conn, claimed=3, members=3, sources=3, claimed_sources=3
    )

    sql = _migration_sql()
    await conn.execute(sql)
    first = (await _rollups(conn, drifted), await _rollups(conn, already_right))
    await conn.execute(sql)
    second = (await _rollups(conn, drifted), await _rollups(conn, already_right))

    assert first == ((5, 2), (3, 3))
    assert second == first


# ---------------------------------------------------------------------------
# The index, and the runner constraint that chose its form
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_analyst_updated_index_exists_after_migration(conn) -> None:
    indexes = {
        r["indexname"]
        for r in await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'events'"
        )
    }
    assert "idx_events_analyst_updated" in indexes
    definition = await conn.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE indexname = $1",
        "idx_events_analyst_updated",
    )
    # The open-event read's own shape: the producer, then the ORDER BY it
    # pages on, then the tiebreak.
    assert "analyst_id" in definition
    assert "updated_at DESC" in definition


def test_migration_builds_the_index_inside_the_transaction() -> None:
    """``migrate._apply_file`` runs the whole body in one transaction, and
    ``CREATE INDEX CONCURRENTLY`` cannot run in a transaction block — a future
    edit reaching for it would abort the migration, not speed it up."""
    sql = _migration_sql()
    assert "CREATE INDEX IF NOT EXISTS idx_events_analyst_updated" in sql
    # The banner names CONCURRENTLY to explain why it is absent — check the
    # EXECUTABLE half, not the prose.
    body = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    assert "CONCURRENTLY" not in body.upper()
