# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-G1 — ``fold_entity_edges()``, the merge fold.

A merge tombstones the loser and redirects it; the loser's EDGES have to move
with it or the merge strands them. The legacy name-keyed sweep
(``_compact_merged_edges``) does this after the fact, 200 losers per run, behind
``except Exception: warning`` — which is why stale endpoints are still
outstanding. The fold runs INSIDE the merge transaction instead, so a merge
either moves its edges or does not happen.

Two schema invariants make the naive "repoint, then dedupe" order fail outright,
and both are asserted here:

  * ``uq_entity_edges_open`` is a plain (non-deferrable) unique index enforced at
    the end of EACH statement, so a repoint that collides a loser edge with a
    keeper edge would raise before any dedupe could run;
  * ``entity_edges_no_self`` forbids src = dst always, so a loser->keeper edge
    cannot be repointed at all — it must be closed in place.

Also covered: evidence is summed/unioned rather than dropped, chained merges
land on the TERMINAL survivor, and the fold is reversible (duplicates are
superseded with a pointer, not deleted).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


async def _seed(conn: Any, name: str, *, cls: str = "organization") -> str:
    eid = str(uuid4())
    await conn.execute(
        """INSERT INTO entity_profiles
             (id, canonical_name, entity_class, entity_type, data)
           VALUES ($1::uuid, $2, $3, $3, '{}'::jsonb)""",
        eid, name, cls)
    return eid


def _at(month: int) -> datetime:
    return datetime(2026, month, 1, tzinfo=timezone.utc)


async def _edge(conn: Any, src: str, dst: str, *, etype: str = "allied with",
                obs: int = 1, conf: float = 0.5, seen: datetime | None = None,
                sigs: list[str] | None = None) -> str:
    eid = str(uuid4())
    await conn.execute(
        """INSERT INTO entity_edges
             (id, src_id, dst_id, edge_type, edge_family, observed_count,
              confidence, first_seen_at, source_signal_ids)
           VALUES ($1::uuid, $2::uuid, $3::uuid, $4, 'relation', $5, $6,
                   $7::timestamptz, $8::uuid[])""",
        eid, src, dst, etype, obs, conf, seen or _at(1), sigs or [])
    return eid


async def _tombstone(conn: Any, loser: str, keeper: str) -> None:
    await conn.execute(
        "UPDATE entity_profiles SET merged_into=$2::uuid WHERE id=$1::uuid",
        loser, keeper)


async def _fold(conn: Any, loser: str) -> asyncpg.Record:
    return await conn.fetchrow(
        "SELECT * FROM fold_entity_edges($1::uuid)", loser)


async def _open_edges(conn: Any, entity: str) -> list[asyncpg.Record]:
    return await conn.fetch(
        """SELECT * FROM entity_edges
            WHERE (src_id=$1::uuid OR dst_id=$1::uuid)
              AND valid_until IS NULL AND superseded_by IS NULL""",
        entity)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fold_repoints_both_directions(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        keeper = await _seed(conn, f"Zzfold Keeper {tag}")
        loser = await _seed(conn, f"Zzfold Loser {tag}")
        out = await _seed(conn, f"Zzfold Out {tag}")
        inn = await _seed(conn, f"Zzfold In {tag}")

        e_out = await _edge(conn, loser, out)          # loser -> X
        e_in = await _edge(conn, inn, loser)           # Y -> loser

        await _tombstone(conn, loser, keeper)
        rep = await _fold(conn, loser)
        assert rep["repointed"] == 2
        assert rep["superseded"] == 0 and rep["self_closed"] == 0

        row_out = await conn.fetchrow(
            "SELECT src_id, dst_id FROM entity_edges WHERE id=$1::uuid", e_out)
        row_in = await conn.fetchrow(
            "SELECT src_id, dst_id FROM entity_edges WHERE id=$1::uuid", e_in)
        assert str(row_out["src_id"]) == keeper
        assert str(row_in["dst_id"]) == keeper
        assert not await _open_edges(conn, loser), \
            "no open edge may still name the tombstone"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fold_coalesces_duplicates_and_never_loses_evidence(pg_pool):
    """The collision the naive repoint-first order would have raised on."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        keeper = await _seed(conn, f"Zzdup Keeper {tag}")
        loser = await _seed(conn, f"Zzdup Loser {tag}")
        other = await _seed(conn, f"Zzdup Other {tag}")
        s1, s2 = str(uuid4()), str(uuid4())

        older = await _edge(conn, keeper, other, obs=2, conf=0.4,
                            seen=_at(1), sigs=[s1])
        newer = await _edge(conn, loser, other, obs=3, conf=0.9,
                            seen=_at(2), sigs=[s2])

        await _tombstone(conn, loser, keeper)
        rep = await _fold(conn, loser)
        assert rep["superseded"] == 1

        surv = await _open_edges(conn, keeper)
        assert len(surv) == 1, "the two edges collapse to exactly one open edge"
        row = surv[0]
        assert str(row["id"]) == older, "the OLDEST row of the group is kept"
        assert row["observed_count"] == 5, "sightings SUM (2+3), never overwrite"
        assert row["confidence"] == pytest.approx(0.9), "confidence takes the max"
        assert {str(s) for s in row["source_signal_ids"]} == {s1, s2}, \
            "evidence unions — a fold must not drop a citation"

        # reversible: the duplicate is superseded WITH A POINTER, not deleted
        dead = await conn.fetchrow(
            "SELECT superseded_by, valid_until FROM entity_edges WHERE id=$1::uuid",
            newer)
        assert str(dead["superseded_by"]) == older
        assert dead["valid_until"] is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fold_closes_would_be_self_edges_instead_of_repointing(pg_pool):
    """A loser->keeper edge cannot be repointed: the CHECK forbids src = dst."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        keeper = await _seed(conn, f"Zzslf Keeper {tag}")
        loser = await _seed(conn, f"Zzslf Loser {tag}")
        eid = await _edge(conn, loser, keeper)

        await _tombstone(conn, loser, keeper)
        rep = await _fold(conn, loser)
        assert rep["self_closed"] == 1

        row = await conn.fetchrow(
            "SELECT src_id, dst_id, valid_until FROM entity_edges WHERE id=$1::uuid",
            eid)
    assert row["valid_until"] is not None, "closed in place"
    assert str(row["src_id"]) == loser, (
        "its endpoints stay on the tombstone — the row is closed history and "
        "the FK still holds because a merge tombstones rather than deletes")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_double_merge_chain_lands_on_the_terminal_survivor(pg_pool):
    """A -> B -> C: folding A must reach C, not stop at the dead middle."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a = await _seed(conn, f"Zzchain A {tag}")
        b = await _seed(conn, f"Zzchain B {tag}")
        c = await _seed(conn, f"Zzchain C {tag}")
        other = await _seed(conn, f"Zzchain Other {tag}")
        e_a = await _edge(conn, a, other, etype="allied with")
        e_b = await _edge(conn, b, other, etype="hostile to")

        await _tombstone(conn, a, b)
        await _tombstone(conn, b, c)

        await _fold(conn, a)
        await _fold(conn, b)

        for eid in (e_a, e_b):
            src = await conn.fetchval(
                "SELECT src_id FROM entity_edges WHERE id=$1::uuid", eid)
            assert str(src) == c, "edges land on the TERMINAL survivor"
        assert not await _open_edges(conn, a)
        assert not await _open_edges(conn, b)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fold_of_a_non_tombstone_is_a_no_op(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a = await _seed(conn, f"Zznop A {tag}")
        b = await _seed(conn, f"Zznop B {tag}")
        await _edge(conn, a, b)
        rep = await _fold(conn, a)
    assert (rep["repointed"], rep["superseded"], rep["self_closed"]) == (0, 0, 0)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fold_is_idempotent_and_strands_nothing_at_scale(pg_pool):
    """Re-running the fold changes nothing, and a many-edge merge with a mix of
    colliding and non-colliding edges leaves no open edge on the tombstone and
    no duplicate open triple."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        keeper = await _seed(conn, f"Zzscale Keeper {tag}")
        loser = await _seed(conn, f"Zzscale Loser {tag}")
        peers = [await _seed(conn, f"Zzscale Peer{i} {tag}") for i in range(6)]

        for i, p in enumerate(peers):
            await _edge(conn, loser, p, etype=f"rel {i}")
            if i % 2 == 0:                       # half of them collide
                await _edge(conn, keeper, p, etype=f"rel {i}")
        await _edge(conn, loser, keeper)         # becomes a self-edge

        await _tombstone(conn, loser, keeper)
        first = await _fold(conn, loser)
        assert first["self_closed"] == 1
        assert first["superseded"] == 3

        again = await _fold(conn, loser)
        assert (again["repointed"], again["superseded"], again["self_closed"]) \
            == (0, 0, 0), "a replay of the fold is a no-op"

        assert not await _open_edges(conn, loser)
        dupes = await conn.fetchval(
            """SELECT count(*) FROM (
                 SELECT src_id, dst_id, edge_type FROM entity_edges
                  WHERE valid_until IS NULL AND superseded_by IS NULL
                    AND (src_id=$1::uuid OR dst_id=$1::uuid)
                  GROUP BY 1,2,3 HAVING count(*) > 1) d""",
            keeper)
    assert dupes == 0, "a fold never duplicates an open triple"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fold_handles_a_collision_reachable_only_via_the_intermediary(pg_pool):
    """The intermediary is part of the unique key. Two edges between the SAME
    pair, one "via the loser" and one "via the keeper", become the same triple
    the moment the fold repoints — even though neither names the loser as an
    endpoint. Miss this and the repoint hits the unique index."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        keeper = await _seed(conn, f"Zzvia Keeper {tag}")
        loser = await _seed(conn, f"Zzvia Loser {tag}")
        a = await _seed(conn, f"Zzvia A {tag}")
        b = await _seed(conn, f"Zzvia B {tag}")

        async def _via(src, dst, via, when):
            eid = str(uuid4())
            await conn.execute(
                """INSERT INTO entity_edges
                     (id, src_id, dst_id, intermediary_id, edge_type,
                      edge_family, first_seen_at)
                   VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid,
                           'proxy hostility', 'relation', $5::timestamptz)""",
                eid, src, dst, via, when)
            return eid

        old = await _via(a, b, keeper, _at(1))
        new = await _via(a, b, loser, _at(2))

        await _tombstone(conn, loser, keeper)
        rep = await _fold(conn, loser)
        assert rep["superseded"] == 1, "the via-collision was caught"

        open_rows = await conn.fetch(
            """SELECT id, intermediary_id FROM entity_edges
                WHERE src_id=$1::uuid AND valid_until IS NULL
                  AND superseded_by IS NULL""", a)
        dead = await conn.fetchval(
            "SELECT superseded_by FROM entity_edges WHERE id=$1::uuid", new)

    assert len(open_rows) == 1
    assert str(open_rows[0]["id"]) == old
    assert str(open_rows[0]["intermediary_id"]) == keeper
    assert str(dead) == old
