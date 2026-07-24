# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""E4c — the entity_researcher MERGE EXECUTOR (tombstone + redirect).

The graph-mutating step, exercised against the 0086 ``merged_into`` /
``resolve_entity`` contract. Covered:

  * elect_keeper — class-priority (country beats person);
  * merge_pair — loser tombstoned (merged_into + gc_status='merged'),
    resolve_entity(loser)=keeper, and the loser surface folded into the
    keeper's merged_aliases (the E1 synergy);
  * idempotent re-merge is a no-op; a same-terminal / cycle attempt is skipped;
  * unmerge reverses it (merged_into NULL, resolve_entity(loser)=loser again);
  * execute_merges applies 'same'@conf + the auto_merge band, skips 'not_same',
    and dry_run mutates nothing.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data._entity_candidates import CandidatePair
from legba.data.analysts.entity_researcher import (
    Verdict,
    elect_keeper,
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


async def _seed(conn: Any, name: str, *, cls: str = "person",
                aliases: list[str] | None = None) -> str:
    eid = str(uuid4())
    data: dict[str, Any] = {}
    if aliases:
        data["merged_aliases"] = aliases
    await conn.execute(
        """INSERT INTO entity_profiles
             (id, canonical_name, entity_class, entity_type, data)
           VALUES ($1::uuid, $2, $3, $3, $4::jsonb)""",
        eid, name, cls, json.dumps(data))
    return eid


def _pair(a_id, a_name, b_id, b_name, *, a_cls="person", b_cls="person",
          band="gray", block_key="") -> CandidatePair:
    return CandidatePair(
        left_id=a_id, left_name=a_name, left_class=a_cls,
        right_id=b_id, right_name=b_name, right_class=b_cls,
        band=band, score=0.9, signals=("exact_block_key",), block_key=block_key)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_elect_keeper_by_class_priority(pg_pool):
    async with pg_pool.acquire() as conn:
        person = await _seed(conn, "Zzelect Person", cls="person")
        country = await _seed(conn, "Zzelect Country", cls="country")
        elected = await elect_keeper(conn, person, country)
    assert elected == (country, person), "country outranks person as keeper"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_merge_pair_tombstones_and_folds_alias(pg_pool):
    async with pg_pool.acquire() as conn:
        keeper = await _seed(conn, "Supreme Zzcouncil", cls="organization")
        loser = await _seed(conn, "Zzcouncil Frag", cls="organization",
                            aliases=["ZzcAcronym"])
        assert await merge_pair(conn, keeper, loser, reason="test") is True
        # loser is a tombstone redirecting to keeper
        row = await conn.fetchrow(
            "SELECT merged_into, data->>'gc_status' AS gc FROM entity_profiles "
            "WHERE id=$1::uuid", loser)
        assert str(row["merged_into"]) == keeper
        assert row["gc"] == "merged"
        assert str(await conn.fetchval("SELECT resolve_entity($1::uuid)", loser)) \
            == keeper
        # the loser surface + its own alias folded into the keeper (E1 synergy)
        kdata = await conn.fetchval(
            "SELECT data->'merged_aliases' FROM entity_profiles WHERE id=$1::uuid",
            keeper)
        folded = set(json.loads(kdata))
        assert "Zzcouncil Frag" in folded and "ZzcAcronym" in folded


@pytest.mark.integration
@pytest.mark.asyncio
async def test_merge_idempotent_and_cycle_guarded(pg_pool):
    async with pg_pool.acquire() as conn:
        a = await _seed(conn, "Zzcycle A", cls="organization")
        b = await _seed(conn, "Zzcycle B", cls="organization")
        assert await merge_pair(conn, a, b, reason="1") is True
        # re-merge same direction -> already same terminal -> no-op
        assert await merge_pair(conn, a, b, reason="2") is False
        # reverse direction -> would cycle (both resolve to a) -> skipped
        assert await merge_pair(conn, b, a, reason="3") is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unmerge_reverses(pg_pool):
    async with pg_pool.acquire() as conn:
        keeper = await _seed(conn, "Zzun Keeper", cls="person")
        loser = await _seed(conn, "Zzun Loser", cls="person")
        await merge_pair(conn, keeper, loser, reason="x")
        assert await unmerge(conn, loser) is True
        row = await conn.fetchrow(
            "SELECT merged_into, data->>'gc_status' AS gc FROM entity_profiles "
            "WHERE id=$1::uuid", loser)
        assert row["merged_into"] is None and row["gc"] is None
        assert str(await conn.fetchval("SELECT resolve_entity($1::uuid)", loser)) \
            == loser
        # reversing a non-tombstone is a no-op
        assert await unmerge(conn, loser) is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execute_merges_bands_and_dry_run(pg_pool):
    async with pg_pool.acquire() as conn:
        # 'same' @ high conf -> merge
        k1 = await _seed(conn, "Zzex Keeper1", cls="person")
        l1 = await _seed(conn, "Zzex Loser1", cls="person")
        p1 = _pair(k1, "Zzex Keeper1", l1, "Zzex Loser1", band="gray")
        v1 = Verdict(p1.pair_key, k1, l1, "same", 0.9, "variant")
        # 'not_same' -> skip
        a2 = await _seed(conn, "Zzex Distinct2a", cls="person")
        b2 = await _seed(conn, "Zzex Distinct2b", cls="person")
        p2 = _pair(a2, "Zzex Distinct2a", b2, "Zzex Distinct2b", band="gray")
        v2 = Verdict(p2.pair_key, a2, b2, "not_same", 0.9, "distinct")
        # auto_merge band, no verdict -> merge deterministically
        k3 = await _seed(conn, "Zzex Keeper3", cls="organization")
        l3 = await _seed(conn, "Zzex Loser3", cls="organization")
        p3 = _pair(k3, "Zzex Keeper3", l3, "Zzex Loser3", band="auto_merge")

        pairs = [p1, p2, p3]
        verdicts = [v1, v2]

        # dry-run mutates nothing
        dry = await execute_merges(conn, verdicts, pairs, dry_run=True)
        assert dry.merged == 2  # p1 (same) + p3 (auto_merge); p2 excluded
        assert await conn.fetchval(
            "SELECT count(*) FROM entity_profiles WHERE merged_into IS NOT NULL "
            "AND id = ANY($1::uuid[])", [l1, b2, l3]) == 0

        rep = await execute_merges(conn, verdicts, pairs)
    assert rep.merged == 2 and rep.skipped == 0
    async with pg_pool.acquire() as conn:
        # p2's endpoints stay independent
        assert await conn.fetchval(
            "SELECT count(*) FILTER (WHERE merged_into IS NOT NULL) "
            "FROM entity_profiles WHERE id = ANY($1::uuid[])", [a2, b2]) == 0
        # p1 + p3 losers tombstoned
        assert await conn.fetchval(
            "SELECT count(*) FILTER (WHERE merged_into IS NOT NULL) "
            "FROM entity_profiles WHERE id = ANY($1::uuid[])", [l1, l3]) == 2
