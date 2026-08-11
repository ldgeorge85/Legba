# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W3-A step 5 — the `entity_edges` dual-write parity loop, registered with S-1.

WHY THIS IS GAUGED AT ALL. `entity_edges` is written by ONE choke point
(`write_entity_edge_for_nexus`, called from the single `INSERT INTO nexuses` in
the codebase) inside the nexus transaction. That design makes divergence
impossible *while it runs* — and therefore makes a regression completely
INVISIBLE: the nexus rows keep landing, every reader cut over on this train
keeps returning answers, and those answers quietly narrow to whatever the last
backfill left behind. There is no exception to log and no empty result to
notice. It is the exact shape S-1 exists for.

THE INVARIANT: every OPEN nexus whose two endpoint names resolve to two
DISTINCT entities has a matching open `entity_edges` row. Rows failing the
resolve are excluded — an unresolvable endpoint is the documented park outcome
(0143), not a dual-write failure, and counting it would manufacture a permanent
~2.75% deficit nobody could ever clear.

This file also pays a debt the `BacklogDrain` docstring had already promised
and nothing delivered: "both are executed against the live schema by a test, so
a column rename breaks the suite instead of silently zeroing the gauge." Only
`BACKLOG_DRAINS[0]` was ever executed. Now every declared drain is.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.registry import production_gauge as pg

PARITY_ID = "entity_edges_dual_write_parity"


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield p
    await p.close()


def _drain(backlog_id: str) -> pg.BacklogDrain:
    found = next(
        (d for d in pg.BACKLOG_DRAINS if d.backlog_id == backlog_id), None)
    assert found is not None, f"{backlog_id} is not declared"
    return found


async def _entity(conn: Any, name: str) -> str:
    return await conn.fetchval(
        """INSERT INTO entity_profiles (canonical_name, entity_class,
             entity_type, data)
           VALUES ($1, 'organization', 'organization', '{}'::jsonb)
           RETURNING id""", name)


async def _nexus(conn: Any, subject: str, object_: str, *,
                 rel_type: str = "allied with", age_hours: int = 24) -> str:
    """A nexus row written WITHOUT its dual-write mirror — i.e. the drift."""
    nid = await conn.fetchval(
        """INSERT INTO nexuses (subject, object, rel_type, label, polarity,
             confidence) VALUES ($1, $2, $3, '', 1, 0.7) RETURNING id""",
        subject, object_, rel_type)
    await conn.execute(
        "UPDATE nexuses SET created_at = now() - ($2 || ' hours')::interval "
        "WHERE id = $1", nid, str(age_hours))
    return nid


async def _overdue(conn: Any) -> int:
    row = await conn.fetchrow(_drain(PARITY_ID).overdue_sql)
    return int(row["overdue"])


# ---------------------------------------------------------------------------
# The declaration, and the debt the docstring owed
# ---------------------------------------------------------------------------

def test_the_parity_loop_is_declared():
    d = _drain(PARITY_ID)
    assert d.unit == "edge"
    assert d.owner_analyst_id and d.label


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("backlog_id", [d.backlog_id for d in pg.BACKLOG_DRAINS])
async def test_every_declared_drain_sql_executes_against_the_live_schema(
    pool, backlog_id,
):
    """The BacklogDrain docstring promises this and only drain[0] delivered it.

    A gauge whose query silently errors reads as "no overdue work" — the
    failure mode is a CLEAN BILL OF HEALTH, which is worse than a red one.
    """
    d = _drain(backlog_id)
    window_start = datetime.now(tz=timezone.utc) - timedelta(days=21)
    async with pool.acquire() as conn:
        overdue = await conn.fetchrow(d.overdue_sql)
        resolved = await conn.fetchrow(d.resolved_sql, window_start)

    assert overdue is not None and "overdue" in overdue
    assert "oldest_due_at" in overdue, (
        "the gauge reads oldest_due_at to age a deficit")
    assert resolved is not None and "resolved" in resolved
    assert int(overdue["overdue"]) >= 0
    assert int(resolved["resolved"]) >= 0


# ---------------------------------------------------------------------------
# The invariant itself
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_nexus_without_its_edge_is_overdue(pool):
    """The drift, made visible. This is the ONLY way the regression surfaces."""
    async with pool.acquire() as conn:
        before = await _overdue(conn)
        tag = uuid4().hex[:8]
        await _entity(conn, f"Par A {tag}")
        await _entity(conn, f"Par B {tag}")
        await _nexus(conn, f"Par A {tag}", f"Par B {tag}")
        after = await _overdue(conn)

    assert after == before + 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_nexus_with_its_edge_is_not_overdue(pool):
    async with pool.acquire() as conn:
        before = await _overdue(conn)
        tag = uuid4().hex[:8]
        a = await _entity(conn, f"Pao A {tag}")
        b = await _entity(conn, f"Pao B {tag}")
        await _nexus(conn, f"Pao A {tag}", f"Pao B {tag}")
        await conn.execute(
            """INSERT INTO entity_edges (src_id, dst_id, edge_type,
                 edge_family, polarity, confidence)
               VALUES ($1, $2, 'allied with', 'relation', 1, 0.7)""", a, b)
        after = await _overdue(conn)

    assert after == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unresolvable_endpoint_is_not_counted_as_drift(pool):
    """The trap this guard exists to avoid. ~2.75% of open nexus rows name
    something that resolves to no entity; that is the documented PARK outcome,
    not a dual-write failure. Counting it would manufacture a permanent deficit
    nobody could clear — and a gauge that cries wolf forever gets muted, which
    costs the real signal."""
    async with pool.acquire() as conn:
        before = await _overdue(conn)
        tag = uuid4().hex[:8]
        await _entity(conn, f"Pau Known {tag}")
        await _nexus(conn, f"Pau Ghost {tag}", f"Pau Known {tag}")
        after = await _overdue(conn)

    assert after == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_ambiguous_endpoint_is_not_counted_as_drift(pool):
    async with pool.acquire() as conn:
        before = await _overdue(conn)
        tag = uuid4().hex[:8]
        name = f"Pam Ambig {tag}"
        await conn.execute(
            """INSERT INTO entity_profiles (canonical_name, entity_class,
                 entity_type, data)
               VALUES ($1, 'location', 'location', '{}'::jsonb),
                      ($1, 'person', 'person', '{}'::jsonb)""", name)
        await _entity(conn, f"Pam Peer {tag}")
        await _nexus(conn, name, f"Pam Peer {tag}")
        after = await _overdue(conn)

    assert after == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_self_referencing_pair_is_not_counted_as_drift(pool):
    """Both endpoints resolve to ONE entity after a merge. The dual-write
    returns SELF_EDGE and writes nothing, by design — an entity is not related
    to itself."""
    async with pool.acquire() as conn:
        before = await _overdue(conn)
        tag = uuid4().hex[:8]
        keeper = await _entity(conn, f"Pas K {tag}")
        loser = await _entity(conn, f"Pas L {tag}")
        await conn.execute(
            "UPDATE entity_profiles SET merged_into=$2::uuid WHERE id=$1::uuid",
            loser, keeper)
        await _nexus(conn, f"Pas K {tag}", f"Pas L {tag}")
        after = await _overdue(conn)

    assert after == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_closed_nexus_is_not_counted_as_drift(pool):
    async with pool.acquire() as conn:
        before = await _overdue(conn)
        tag = uuid4().hex[:8]
        await _entity(conn, f"Pac A {tag}")
        await _entity(conn, f"Pac B {tag}")
        nid = await _nexus(conn, f"Pac A {tag}", f"Pac B {tag}")
        await conn.execute(
            "UPDATE nexuses SET valid_until = now() WHERE id = $1", nid)
        after = await _overdue(conn)

    assert after == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_just_written_nexus_gets_the_grace_window(pool):
    """The write is same-transaction and needs no grace in principle; the hour
    means a scan racing a long ingest batch cannot report a deficit that
    resolves itself a second later."""
    async with pool.acquire() as conn:
        before = await _overdue(conn)
        tag = uuid4().hex[:8]
        await _entity(conn, f"Pag A {tag}")
        await _entity(conn, f"Pag B {tag}")
        await _nexus(conn, f"Pag A {tag}", f"Pag B {tag}", age_hours=0)
        after = await _overdue(conn)

    assert after == before
