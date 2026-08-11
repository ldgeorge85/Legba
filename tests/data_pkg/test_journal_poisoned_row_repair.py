# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0120 — soft-close the poisoned `journal_entries` rows.

Two junk classes accumulated in the journal and neither was ever remediated:
tool-JSON envelopes (a gather-timeout left NARRATE emitting a raw tool-call
envelope, persisted verbatim where prose belongs) and empty stubs (bodies that
are a bare "(empty ...)" placeholder). Live at review time: 7 rows of 116.

What these pin:

  * the CONTENT MARKERS select the poison and nothing else — a normal journal
    body that merely mentions a tool, or contains a brace, is not touched;
  * SOFT-close, never delete: the row, its body and its lineage survive, only
    `valid_until` + a queryable `data.closed_by` cohort stamp change;
  * an already-superseded row keeps its ORIGINAL close time (3 of the 7 were
    already closed by the daily consolidation chain — that history is real);
  * the migration is idempotent (re-running stamps nothing new).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.migrations import MIGRATIONS_DIR
from legba.data.config import PostgresConfig

_MIGRATION_SQL = (
    MIGRATIONS_DIR / "0120_soft_close_poisoned_journal_rows.sql"
).read_text(encoding="utf-8")

_T0 = datetime(2026, 7, 31, 2, 7, tzinfo=timezone.utc)

# The live poison, verbatim in shape.
_TOOL_JSON_CONSOLIDATION = (
    '{"tool":"get_source_health","call":{"filter":{"source_id":"source.rss.x"}}}'
)
_TOOL_JSON_ENTRY = '{\n  "tool": "graph_insights",\n  "args": {\n    "metrics"\n'


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    try:
        await c.execute("DELETE FROM journal_entries")
        yield c
    finally:
        await c.execute("DELETE FROM journal_entries")
        await c.close()


async def _insert(
    conn, *, entry_kind, body, title="Journal entry", valid_until=None, produced_at=_T0,
):
    row_id = uuid4()
    await conn.execute(
        """
        INSERT INTO journal_entries
            (id, entry_kind, title, body, period_start, period_end,
             produced_at, valid_until)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        row_id, entry_kind, title, body,
        produced_at - timedelta(hours=12), produced_at, produced_at, valid_until,
    )
    return row_id


async def _state(conn, row_id):
    return await conn.fetchrow(
        "SELECT valid_until, body, data->>'closed_by' AS closed_by "
        "FROM journal_entries WHERE id = $1",
        row_id,
    )


@pytest.mark.asyncio
async def test_poison_is_closed_and_clean_rows_are_untouched(conn):
    # `uq_journal_single_open_consolidation` permits exactly one OPEN
    # consolidation, and live the poisoned ones are already superseded by the
    # daily chain — so they go in closed, as they actually are.
    closed_at = _T0 - timedelta(days=1)
    poisoned = {
        "tool_json_consolidation": await _insert(
            conn, entry_kind="consolidation", body=_TOOL_JSON_CONSOLIDATION,
            title=_TOOL_JSON_CONSOLIDATION[:60], valid_until=closed_at,
        ),
        "tool_json_entry": await _insert(
            conn, entry_kind="entry", body=_TOOL_JSON_ENTRY,
        ),
        "empty_consolidation": await _insert(
            conn, entry_kind="consolidation", body="(empty consolidation)",
            valid_until=closed_at,
        ),
        "empty_lens": await _insert(conn, entry_kind="lens", body="(empty lens read)"),
        "empty_lens_diff": await _insert(
            conn, entry_kind="lens_diff", body="(empty chorus diff)",
        ),
        "empty_entry": await _insert(conn, entry_kind="entry", body="(empty entry)"),
    }
    # Real prose that must SURVIVE — including the adversarial neighbours: a body
    # that talks about tools, one that quotes JSON mid-paragraph, and one whose
    # first word is "(empty" but which is a genuine sentence.
    clean = {
        "prose": await _insert(
            conn, entry_kind="entry",
            body="The collection posture held. Four sources degraded overnight.",
        ),
        "mentions_tool": await _insert(
            conn, entry_kind="entry",
            body='I called the "tool" get_source_health and it answered honestly.',
        ),
        "quotes_json_inline": await _insert(
            conn, entry_kind="consolidation",
            body='The handler returned {"tool": "graph_insights"} and I read it.',
        ),
        "empty_prefix_prose": await _insert(
            conn, entry_kind="entry",
            body="(empty of incident, but not of meaning) the week stayed quiet.",
        ),
    }

    await conn.execute(_MIGRATION_SQL)

    for name, row_id in poisoned.items():
        st = await _state(conn, row_id)
        assert st["valid_until"] is not None, f"{name} should be soft-closed"
        assert st["closed_by"] == "mig_0120_poisoned_journal_rows", name
        # SOFT close — the body is preserved, never blanked or deleted.
        assert st["body"], name

    for name, row_id in clean.items():
        st = await _state(conn, row_id)
        assert st["valid_until"] is None, f"{name} must stay open"
        assert st["closed_by"] is None, name

    # Nothing was deleted: every row inserted is still there.
    assert await conn.fetchval("SELECT count(*) FROM journal_entries") == len(
        poisoned
    ) + len(clean)


@pytest.mark.asyncio
async def test_already_superseded_row_keeps_its_original_close_time(conn):
    """3 of the 7 live rows were already closed by the consolidation chain."""
    original = _T0 - timedelta(days=3)
    row_id = await _insert(
        conn, entry_kind="consolidation", body=_TOOL_JSON_CONSOLIDATION,
        valid_until=original,
    )

    await conn.execute(_MIGRATION_SQL)

    st = await _state(conn, row_id)
    # COALESCE — the real close time is history and is not overwritten...
    assert st["valid_until"] == original
    # ...but the row still joins the queryable cohort.
    assert st["closed_by"] == "mig_0120_poisoned_journal_rows"


@pytest.mark.asyncio
async def test_migration_is_idempotent(conn):
    row_id = await _insert(conn, entry_kind="entry", body="(empty entry)")

    await conn.execute(_MIGRATION_SQL)
    first = await _state(conn, row_id)
    await conn.execute(_MIGRATION_SQL)
    second = await _state(conn, row_id)

    # Second pass is a no-op: the close time does not drift forward.
    assert first["valid_until"] == second["valid_until"]
    assert second["closed_by"] == "mig_0120_poisoned_journal_rows"


@pytest.mark.asyncio
async def test_soft_close_removes_the_row_from_the_journal_memory_read(conn):
    """The repair must BITE. The journal reads its own recent entries back to
    itself each window; a closed envelope has to leave that corpus, else the
    migration is cosmetic and the narrator keeps eating tool JSON."""
    poisoned = await _insert(
        conn, entry_kind="entry", body=_TOOL_JSON_ENTRY,
        produced_at=_T0 + timedelta(hours=1),  # NEWEST — would win the read
    )
    good = await _insert(
        conn, entry_kind="entry", body="Real prose about a real window.",
    )

    await conn.execute(_MIGRATION_SQL)

    # The exact predicate the substrate journal-slice reads now carry.
    rows = await conn.fetch(
        "SELECT id FROM journal_entries WHERE entry_kind = 'entry' "
        "  AND valid_until IS NULL "
        "ORDER BY period_end DESC, produced_at DESC"
    )
    ids = [r["id"] for r in rows]
    assert poisoned not in ids
    assert ids == [good]
