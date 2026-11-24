# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The journal correction against a REAL Postgres (asyncpg), not a fake.

The lane's own tests drove ``journal_correction`` through a recording fake, so
``_CLOSE_SQL`` using ``$1`` both as a uuid and as text passed them and failed on
the live database ("inconsistent types deduced for parameter $1"). These run the
write and the close through the real driver on the migrated test database.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.provenance.journal_correction import (
    close_rows_by_correction,
    write_correction_entry,
)

_T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    mine: list = []
    try:
        yield c, mine
    finally:
        if mine:
            await c.execute("DELETE FROM journal_entries WHERE id = ANY($1::uuid[])", mine)
        await c.close()


async def _insert(c, mine, *, entry_kind="lens", body="An entry citing nothing real."):
    rid = uuid4()
    await c.execute(
        """
        INSERT INTO journal_entries
            (id, entry_kind, title, body, period_start, period_end, produced_at)
        VALUES ($1, $2, 'Journal entry', $3, $4, $5, $5)
        """,
        rid, entry_kind, body, _T0 - timedelta(hours=12), _T0,
    )
    mine.append(rid)
    return rid


async def test_write_and_close_run_on_the_real_driver(conn) -> None:
    c, mine = conn
    a = await _insert(c, mine)
    b = await _insert(c, mine, entry_kind="entry")
    before = await c.fetch(
        "SELECT id, title, body, claims, cited_substrate_refs FROM journal_entries "
        "WHERE id = ANY($1::uuid[]) ORDER BY id", [a, b],
    )
    corrects = [
        {"id": str(a), "entry_kind": "lens", "period_start": _T0 - timedelta(hours=12),
         "period_end": _T0, "reason": "no_refs", "detail": "cites nothing"},
        {"id": str(b), "entry_kind": "entry", "period_start": _T0 - timedelta(hours=12),
         "period_end": _T0, "reason": "refs_unresolved", "detail": "0/3 resolve"},
    ]
    key = f"real-pg-{uuid4()}"
    cid, created = await write_correction_entry(
        c, correction_key=key, corrects=corrects, reason="test reason",
    )
    mine.append(cid)
    assert created

    closed = await close_rows_by_correction(c, correction_id=cid, ids=[a, b])
    assert sorted(closed) == sorted([str(a), str(b)])

    rows = await c.fetch(
        "SELECT id, title, body, claims, cited_substrate_refs, superseded_by, valid_until, "
        "data->>'corrected_by' AS corrected_by, data->>'closed_by' AS closed_by "
        "FROM journal_entries WHERE id = ANY($1::uuid[]) ORDER BY id", [a, b],
    )
    for r, old in zip(rows, before):
        assert r["superseded_by"] == cid
        assert r["valid_until"] is not None
        assert r["corrected_by"] == str(cid)
        assert r["closed_by"] == "correction"
        for col in ("title", "body", "claims", "cited_substrate_refs"):
            assert r[col] == old[col]

    cited = await c.fetchval(
        "SELECT cited_substrate_refs FROM journal_entries WHERE id = $1", cid,
    )
    assert sorted(cited) == sorted([a, b])

    # Idempotent on the real driver too: a second close touches nothing, and the
    # key finds the same correction row.
    assert await close_rows_by_correction(c, correction_id=cid, ids=[a, b]) == []
    again, created2 = await write_correction_entry(
        c, correction_key=key, corrects=corrects, reason="test reason",
    )
    assert (again, created2) == (cid, False)
