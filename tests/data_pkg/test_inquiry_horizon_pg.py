# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SW-10 on the REAL driver: the yield's keyset page SQL and the one-off void
script, both run against the disposable test Postgres (``migrated_pg``), never
the live database.

  * ``inquiry_yield.fetch_finding_refs`` — the page SQL's row-value keyset
    predicate ``(produced_at, id) > ($3, $4)`` and its NULL first-page cursor
    are Postgres semantics a fake connection cannot prove. 3x the old
    ``LIMIT 5000`` cap plus one, with page boundaries inside runs of equal
    ``produced_at``, must come back whole and once each.
  * ``scripts/void_inquiry_hypotheses_no_horizon.sql`` — closes exactly the
    open hypotheses with no horizon (as ``withdrawn``, the close stamped the
    way ``ledger_close`` stamps one), leaves every other row alone, and a
    second run closes nothing.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers import inquiry_yield as iy
from legba.data.config import PostgresConfig

pytestmark = pytest.mark.asyncio

_REPO = Path(__file__).resolve().parents[2]
_VOID_SCRIPT = _REPO / "scripts" / "void_inquiry_hypotheses_no_horizon.sql"
_TAG = "sw10void"
_ANALYST = "sw10_yield_paging"
# A window no other test writes into — the read is window-scoped, so the
# assertion is exact even on a shared test database.
_WIN_END = datetime(2001, 3, 1, tzinfo=timezone.utc)
_WIN_START = _WIN_END - timedelta(days=7)


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    try:
        yield c
    finally:
        await c.execute("DELETE FROM analyst_outputs WHERE analyst_id = $1", _ANALYST)
        await c.execute(
            "DELETE FROM inquiry_ledger WHERE descriptor_id LIKE $1", f"{_TAG}_%",
        )
        await c.close()


# ---------------------------------------------------------------------------
# The yield's keyset page read
# ---------------------------------------------------------------------------


async def test_keyset_read_over_3x_the_old_cap_returns_the_whole_week(conn):
    n = 3 * 5_000 + 1
    # every 7 consecutive rows share one produced_at: page boundaries (every
    # 5,000 rows) land inside a run of equal timestamps, where only the `id`
    # tie-break keeps a row from being skipped or repeated.
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (kind, title, body, confidence, data, schema_uri, analyst_id,
             produced_at, created_at, derived_from)
        SELECT 'finding', 'sw10 paging ' || g, '', 0.5, '{}'::jsonb,
               'iglu:legba/analyst_output/jsonschema/1-0-0', $1,
               $2::timestamptz - make_interval(secs => 1 + g / 7),
               $2::timestamptz,
               ARRAY[gen_random_uuid()]
          FROM generate_series(0, $3 - 1) AS g
        """,
        _ANALYST, _WIN_END, n,
    )
    # one row OUTSIDE the window and one with an empty derived_from: neither
    # may come back.
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (kind, title, body, confidence, data, schema_uri, analyst_id,
             produced_at, derived_from)
        VALUES ('finding', 'sw10 out of window', '', 0.5, '{}'::jsonb,
                'iglu:legba/analyst_output/jsonschema/1-0-0', $1, $2,
                ARRAY[gen_random_uuid()]),
               ('finding', 'sw10 no refs', '', 0.5, '{}'::jsonb,
                'iglu:legba/analyst_output/jsonschema/1-0-0', $1, $3, '{}')
        """,
        _ANALYST, _WIN_END, _WIN_END - timedelta(hours=1),
    )

    rows, capped = await iy.fetch_finding_refs(conn, _WIN_START, _WIN_END)

    assert capped is False
    assert len(rows) == n
    assert len({r["id"] for r in rows}) == n, "a page boundary repeated a row"
    keys = [(r["produced_at"], r["id"]) for r in rows]
    assert keys == sorted(keys)


async def test_keyset_read_ceiling_flags_capped_only_when_rows_remain(conn):
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (kind, title, body, confidence, data, schema_uri, analyst_id,
             produced_at, derived_from)
        SELECT 'finding', 'sw10 ceiling ' || g, '', 0.5, '{}'::jsonb,
               'iglu:legba/analyst_output/jsonschema/1-0-0', $1,
               $2::timestamptz - make_interval(secs => 1 + g / 3),
               ARRAY[gen_random_uuid()]
          FROM generate_series(0, 19) AS g
        """,
        _ANALYST, _WIN_END,
    )
    rows, capped = await iy.fetch_finding_refs(
        conn, _WIN_START, _WIN_END, page_rows=6, max_rows=20,
    )
    assert (len(rows), capped) == (20, False)
    rows, capped = await iy.fetch_finding_refs(
        conn, _WIN_START, _WIN_END, page_rows=6, max_rows=19,
    )
    assert (len(rows), capped) == (19, True)


# ---------------------------------------------------------------------------
# The one-off void script
# ---------------------------------------------------------------------------


def _script_sql() -> str:
    """The script minus its psql meta-commands (``\\set``), which asyncpg's
    simple-query path cannot run; everything else is executed verbatim."""
    return "\n".join(
        line for line in _VOID_SCRIPT.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("\\")
    )


async def _insert(conn, descriptor_id, kind, *, test=None, resolves_by=None,
                  status="open"):
    closed_at = None if status == "open" else datetime.now(timezone.utc)
    return await conn.fetchval(
        """
        INSERT INTO inquiry_ledger
            (descriptor_id, kind, text, status, resolution_test, resolves_by,
             cited_refs, closed_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
        RETURNING id
        """,
        descriptor_id, kind, f"{kind} row", status, test, resolves_by,
        json.dumps([str(uuid4())]), closed_at,
    )


async def test_void_script_closes_only_open_hypotheses_with_no_horizon(conn):
    d = f"{_TAG}_pilot"
    horizon = datetime(2099, 12, 31, tzinfo=timezone.utc)
    void_a = await _insert(conn, d, "hypothesis", test="t")
    void_b = await _insert(conn, f"{_TAG}_crossroads", "hypothesis", test="t")
    keep_horizon = await _insert(conn, d, "hypothesis", test="t", resolves_by=horizon)
    keep_question = await _insert(conn, d, "question")
    keep_observation = await _insert(conn, d, "observation")
    keep_closed = await _insert(conn, d, "hypothesis", test="t", status="refuted")
    before = {
        r["id"]: dict(r)
        for r in await conn.fetch(
            "SELECT * FROM inquiry_ledger WHERE descriptor_id LIKE $1", f"{_TAG}_%",
        )
    }

    await conn.execute(_script_sql())

    after = {
        r["id"]: dict(r)
        for r in await conn.fetch(
            "SELECT * FROM inquiry_ledger WHERE descriptor_id LIKE $1", f"{_TAG}_%",
        )
    }
    for rid in (void_a, void_b):
        row = after[rid]
        assert row["status"] == "withdrawn"
        assert row["closed_at"] is not None
        assert row["updated_at"] > before[rid]["updated_at"]
        assert row["text"].startswith(before[rid]["text"])
        assert "\n\n[closed -> withdrawn] void_no_horizon:" in row["text"]
        assert row["cited_refs"] == before[rid]["cited_refs"]  # left as opened
        assert row["resolution_test"] == "t"
    for rid in (keep_horizon, keep_question, keep_observation, keep_closed):
        assert after[rid] == before[rid], f"row {rid} must be untouched"

    # IDEMPOTENT: a second run closes nothing and re-stamps nothing.
    await conn.execute(_script_sql())
    again = {
        r["id"]: dict(r)
        for r in await conn.fetch(
            "SELECT * FROM inquiry_ledger WHERE descriptor_id LIKE $1", f"{_TAG}_%",
        )
    }
    assert again == after
