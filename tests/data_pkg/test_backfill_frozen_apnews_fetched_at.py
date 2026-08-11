# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""B-1 backfill — migration 0141 repairs the 5 frozen AP feeds and NOTHING else.

The 08-03 incident (MASTER_PLAN §32.7) left 201 signals from five rsshub AP-topic
feeds carrying an advanced ``fetched_at``: content fetched once on 07-28, re-served
unchanged on every poll for six days, and bumped forward each time by the S-4
collapse. The code fix stops the bump going forward; migration 0141 repairs the
rows already carrying the lie.

The migration's two risky properties are its SCOPE (it must not touch any other
source's history — the same predicate applied table-wide would rewrite ~620MB of
rows nobody has evidence about) and its IDEMPOTENCE (a re-applied migration must
not rewind anything a second time). Both are asserted here against the real
migration file executed on a real Postgres at schema head, because both are the
kind of property that reads as obviously-correct in review and is only actually
correct if the predicate says what you think it says.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig


_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src" / "legba" / "data" / "migrations"
    / "0141_backfill_frozen_apnews_fetched_at.sql"
)

#: The five feeds the incident named. Exactly these are in scope.
_FROZEN = (
    "source.rsshub.apnews.north_korea",
    "source.rsshub.apnews.taiwan",
    "source.rsshub.apnews.niger",
    "source.rsshub.apnews.haiti",
    "source.rsshub.apnews.drcongo",
)

#: A 07-28 fetch (real) re-serve-bumped forward to 08-03 (the lie).
_FETCH = datetime(2026, 7, 28, 8, 43, tzinfo=timezone.utc)
_BUMPED = datetime(2026, 8, 3, 2, 43, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
    )
    try:
        yield c
    finally:
        await c.close()


async def _seed(conn, source_id: str, *, fetched_at, created_at) -> str:
    sid = uuid4()
    await conn.execute(
        "INSERT INTO signals (id, source_id, owner_tenant, modality, payload, "
        "content_hash, fetched_at, created_at, last_seen_at) "
        "VALUES ($1,$2,$3,'text','{}'::jsonb,$4,$5,$6,NULL)",
        sid, source_id, "b1_backfill", f"h_{uuid4().hex}", fetched_at, created_at,
    )
    return sid


async def _row(conn, sid):
    return await conn.fetchrow(
        "SELECT fetched_at, created_at, last_seen_at FROM signals WHERE id=$1", sid
    )


@pytest.mark.asyncio
async def test_backfill_rewinds_only_the_frozen_feeds(conn):
    """Bumped AP rows are rewound to created_at and keep the re-serve time;
    an unbumped AP row and a bumped OTHER-source row are both left alone."""
    sql = _MIGRATION.read_text()

    bumped = {s: await _seed(conn, s, fetched_at=_BUMPED, created_at=_FETCH)
              for s in _FROZEN}
    # Same feed family, never bumped (fetched_at == created_at) — must be a no-op
    # target, not merely harmless: the predicate should not match it at all.
    untouched_ap = await _seed(
        conn, "source.rsshub.apnews.haiti",
        fetched_at=_FETCH, created_at=_FETCH,
    )
    # A DIFFERENT source with a genuinely bumped row. Every hazard feed in the
    # fleet looks like this; none of them are in scope for this repair.
    other = await _seed(
        conn, "source.rsshub.other.topic",
        fetched_at=_BUMPED, created_at=_FETCH,
    )

    try:
        await conn.execute(sql)

        for source_id, sid in bumped.items():
            row = await _row(conn, sid)
            assert row["fetched_at"] == _FETCH, source_id       # the repair
            assert row["last_seen_at"] == _BUMPED, source_id    # ... losing nothing

        row = await _row(conn, untouched_ap)
        assert row["fetched_at"] == _FETCH
        assert row["last_seen_at"] is None   # never re-served → still no record

        row = await _row(conn, other)
        assert row["fetched_at"] == _BUMPED  # out of scope, untouched
        assert row["last_seen_at"] is None
    finally:
        await conn.execute(
            "DELETE FROM signals WHERE owner_tenant = 'b1_backfill'")


@pytest.mark.asyncio
async def test_backfill_is_idempotent(conn):
    """Re-applying must be a no-op — the predicate is self-extinguishing, so a
    second run cannot rewind an already-repaired row to something older still."""
    sql = _MIGRATION.read_text()
    sid = await _seed(
        conn, "source.rsshub.apnews.niger", fetched_at=_BUMPED, created_at=_FETCH,
    )
    try:
        await conn.execute(sql)
        first = await _row(conn, sid)
        await conn.execute(sql)
        await conn.execute(sql)
        assert await _row(conn, sid) == first
    finally:
        await conn.execute(
            "DELETE FROM signals WHERE owner_tenant = 'b1_backfill'")


@pytest.mark.asyncio
async def test_repaired_rows_drop_out_of_a_fresh_slice_window(conn):
    """The point of the repair, stated as the desks experience it.

    The substrate slice admits rows on ``fetched_at > NOW() - INTERVAL 'N hours'``
    (actor_substrate_slice.py:330). Before the repair a six-day-old frozen story
    passed that filter every time; after it, it ages out — which is the whole
    reason the Korea/Taiwan/Niger/Haiti/DRC desks were re-reading a 07-28
    snapshot as current."""
    sql = _MIGRATION.read_text()
    now = datetime.now(timezone.utc)
    sid = await _seed(
        conn, "source.rsshub.apnews.taiwan",
        fetched_at=now,                       # the lie: "fetched just now"
        created_at=now - timedelta(days=6),   # the truth: fetched six days ago
    )
    try:
        in_window = (
            "SELECT count(*) FROM signals WHERE id=$1 "
            "AND fetched_at > NOW() - INTERVAL '72 hours'"
        )
        assert await conn.fetchval(in_window, sid) == 1   # tops every 72h slice
        await conn.execute(sql)
        assert await conn.fetchval(in_window, sid) == 0   # ... and now it doesn't
    finally:
        await conn.execute(
            "DELETE FROM signals WHERE owner_tenant = 'b1_backfill'")
