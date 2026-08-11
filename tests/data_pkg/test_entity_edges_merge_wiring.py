# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-G1 — the fold runs inside the REAL merge transaction, not beside it.

``fold_entity_edges()`` being correct (test_entity_edges_fold.py) proves nothing
about whether anything calls it. This file traverses the production binding path:
``merge_pair`` / ``execute_merges`` as the researcher actually invokes them, and
asserts the edges moved and the counts reached the receipt.

The atomicity claim is the one worth testing hardest: the fold is inside the same
``conn.transaction()`` that sets ``merged_into``, so a rollback must leave BOTH
the tombstone and the edge exactly as they were. A fold that ran in its own
transaction would leave repointed edges behind a merge that never happened.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data._entity_candidates import CandidatePair
from legba.data.analysts.entity_researcher import (
    Verdict,
    execute_merges,
    merge_pair,
    unmerge,
)
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


async def _edge(conn: Any, src: str, dst: str, *,
                etype: str = "allied with") -> str:
    eid = str(uuid4())
    await conn.execute(
        """INSERT INTO entity_edges
             (id, src_id, dst_id, edge_type, edge_family)
           VALUES ($1::uuid, $2::uuid, $3::uuid, $4, 'relation')""",
        eid, src, dst, etype)
    return eid


@pytest.mark.integration
@pytest.mark.asyncio
async def test_merge_pair_folds_edges_and_reports_the_counts(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        keeper = await _seed(conn, f"Zzwire Keeper {tag}")
        loser = await _seed(conn, f"Zzwire Loser {tag}")
        peer = await _seed(conn, f"Zzwire Peer {tag}")
        moved = await _edge(conn, loser, peer)

        counts: dict[str, int] = {}
        assert await merge_pair(conn, keeper, loser, reason="test",
                                edge_fold=counts) is True

        assert counts == {"repointed": 1, "superseded": 0, "self_closed": 0}
        src = await conn.fetchval(
            "SELECT src_id FROM entity_edges WHERE id=$1::uuid", moved)
        assert str(src) == keeper, "the merge moved the edge, not a later sweep"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_merge_pair_without_an_accumulator_still_folds(pg_pool):
    """The accumulator is for the receipt; the fold is not optional."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        keeper = await _seed(conn, f"Zzopt Keeper {tag}")
        loser = await _seed(conn, f"Zzopt Loser {tag}")
        peer = await _seed(conn, f"Zzopt Peer {tag}")
        moved = await _edge(conn, loser, peer)

        assert await merge_pair(conn, keeper, loser) is True
        src = await conn.fetchval(
            "SELECT src_id FROM entity_edges WHERE id=$1::uuid", moved)
    assert str(src) == keeper


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fold_and_tombstone_are_ONE_transaction(pg_pool):
    """A rollback must undo both, or a merge that never happened leaves
    repointed edges behind it."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        keeper = await _seed(conn, f"Zzatom Keeper {tag}")
        loser = await _seed(conn, f"Zzatom Loser {tag}")
        peer = await _seed(conn, f"Zzatom Peer {tag}")
        moved = await _edge(conn, loser, peer)

        tx = conn.transaction()
        await tx.start()
        assert await merge_pair(conn, keeper, loser, reason="doomed") is True
        await tx.rollback()

        row = await conn.fetchrow(
            """SELECT ep.merged_into, e.src_id
                 FROM entity_profiles ep, entity_edges e
                WHERE ep.id=$1::uuid AND e.id=$2::uuid""",
            loser, moved)
    assert row["merged_into"] is None, "the tombstone rolled back"
    assert str(row["src_id"]) == loser, (
        "and so did the edge repoint — a fold in its own transaction would "
        "have left this edge on the keeper of a merge that never happened")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execute_merges_surfaces_the_fold_in_its_report(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        # country outranks person, so the country is elected keeper
        keeper = await _seed(conn, f"Zzexec Keeper {tag}", cls="country")
        loser = await _seed(conn, f"Zzexec Loser {tag}", cls="person")
        peer = await _seed(conn, f"Zzexec Peer {tag}")
        await _edge(conn, loser, peer, etype="allied with")
        await _edge(conn, keeper, peer, etype="allied with")   # collides
        await _edge(conn, loser, keeper, etype="hostile to")   # self after fold

        pair = CandidatePair(
            left_id=keeper, left_name=f"Zzexec Keeper {tag}", left_class="country",
            right_id=loser, right_name=f"Zzexec Loser {tag}", right_class="person",
            band="gray", score=0.95, signals=("exact_block_key",), block_key="zz")
        verdict = Verdict(pair_key=pair.pair_key, entity_a=keeper,
                          entity_b=loser, verdict="same", confidence=0.99,
                          justification="test")

        report = await execute_merges(conn, [verdict], [pair], dry_run=False)

    assert report.merged == 1
    assert report.edges_superseded == 1, "the colliding pair coalesced"
    assert report.edges_self_closed == 1, "the loser->keeper edge closed in place"
    assert report.edges_repointed == 1, (
        "the surviving allied-with row moves to the keeper. Whichever of the "
        "colliding pair was elected, exactly one row still named the loser "
        "after the collapse — the repoint covers CLOSED rows too, so superseded "
        "history points at the survivor rather than at a tombstone. The "
        "loser->keeper row is the only one skipped, because the no-self CHECK "
        "forbids repointing it at all")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dry_run_folds_nothing(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        keeper = await _seed(conn, f"Zzdry Keeper {tag}", cls="country")
        loser = await _seed(conn, f"Zzdry Loser {tag}", cls="person")
        peer = await _seed(conn, f"Zzdry Peer {tag}")
        moved = await _edge(conn, loser, peer)

        pair = CandidatePair(
            left_id=keeper, left_name=f"Zzdry Keeper {tag}", left_class="country",
            right_id=loser, right_name=f"Zzdry Loser {tag}", right_class="person",
            band="gray", score=0.95, signals=("exact_block_key",), block_key="zz")
        verdict = Verdict(pair_key=pair.pair_key, entity_a=keeper,
                          entity_b=loser, verdict="same", confidence=0.99,
                          justification="test")

        report = await execute_merges(conn, [verdict], [pair], dry_run=True)
        src = await conn.fetchval(
            "SELECT src_id FROM entity_edges WHERE id=$1::uuid", moved)

    assert (report.edges_repointed, report.edges_superseded,
            report.edges_self_closed) == (0, 0, 0)
    assert str(src) == loser, "a dry run mutates nothing, edges included"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unmerge_leaves_the_superseded_pointer_walkable(pg_pool):
    """The fold is reversible BY CONSTRUCTION: duplicates keep a
    ``superseded_by`` pointer, so the pre-merge shape is still derivable. A
    string rewrite could not offer this — the original string is gone."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        keeper = await _seed(conn, f"Zzrev Keeper {tag}")
        loser = await _seed(conn, f"Zzrev Loser {tag}")
        peer = await _seed(conn, f"Zzrev Peer {tag}")
        kept = await _edge(conn, keeper, peer)
        dup = await _edge(conn, loser, peer)

        await merge_pair(conn, keeper, loser)
        assert await unmerge(conn, loser) is True

        row = await conn.fetchrow(
            "SELECT superseded_by, valid_until FROM entity_edges WHERE id=$1::uuid",
            dup)
    assert str(row["superseded_by"]) == kept
    assert row["valid_until"] is not None
