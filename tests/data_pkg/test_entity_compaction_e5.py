# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""E5 — compaction (re-point merged-loser edges) + the tombstone read-filter.

E4c merge_pair sets entity_profiles.merged_into but leaves nexus/fact endpoint
STRING columns holding the loser surface. E5's _compact_merged_edges (folded
into entity_gc) re-points those to the keeper, closing collisions/self-loops
instead of violating the open-triple unique index; a merged loser is filtered
from the entity reads (E5a). Covered:

  * re-point a nexus endpoint loser->keeper (no collision);
  * a collision (keeper triple already open) CLOSES the loser row, no duplicate;
  * a re-pointed self-loop is closed;
  * a fact subject is re-pointed;
  * the sweep is idempotent (compacted_at marker) + marks the loser compacted;
  * inspect_entity excludes a merged-loser tombstone (E5a).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers.entity_gc import _compact_merged_edges
from legba.data.config import PostgresConfig


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


async def _seed_entity(conn, name, *, cls="organization", merged_into=None) -> str:
    eid = str(uuid4())
    data: dict[str, Any] = {}
    if merged_into is not None:
        # a merged loser tombstone (NOT yet compacted).
        data = {"gc_status": "merged", "merge": {"into": merged_into}}
    await conn.execute(
        "INSERT INTO entity_profiles (id, canonical_name, entity_class, entity_type,"
        " merged_into, data) VALUES ($1::uuid,$2,$3,$3,$4::uuid,$5::jsonb)",
        eid, name, cls, merged_into, json.dumps(data))
    return eid


async def _seed_nexus(conn, subject, obj, rel="CoOccursWith") -> str:
    nid = str(uuid4())
    await conn.execute(
        "INSERT INTO nexuses (id, subject, object, rel_type, source_type) "
        "VALUES ($1::uuid,$2,$3,$4,'agent')", nid, subject, obj, rel)
    return nid


async def _seed_fact(conn, subject, predicate, value) -> str:
    fid = str(uuid4())
    await conn.execute(
        "INSERT INTO facts (id, subject, predicate, value, source_type) "
        "VALUES ($1::uuid,$2,$3,$4,'agent')", fid, subject, predicate, value)
    return fid


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repoint_nexus_no_collision(pg_pool):
    async with pg_pool.acquire() as conn:
        keeper = await _seed_entity(conn, "Zzc Keeper Org")
        loser = await _seed_entity(conn, "the Zzc Keeper Org", merged_into=keeper)
        nx = await _seed_nexus(conn, "the Zzc Keeper Org", "Zzc Other")
    n = await _compact_merged_edges(pg_pool)
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT subject FROM nexuses WHERE id=$1::uuid", nx)
    assert n >= 1
    assert row["subject"] == "Zzc Keeper Org"  # re-pointed to keeper surface


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repoint_nexus_collision_closes_loser(pg_pool):
    async with pg_pool.acquire() as conn:
        keeper = await _seed_entity(conn, "Zzcol Keeper")
        loser = await _seed_entity(conn, "the Zzcol Keeper", merged_into=keeper)
        # an existing OPEN keeper triple + the loser triple that would collide
        keep_nx = await _seed_nexus(conn, "Zzcol Keeper", "Zzcol X", "AlliedWith")
        lose_nx = await _seed_nexus(conn, "the Zzcol Keeper", "Zzcol X", "AlliedWith")
    await _compact_merged_edges(pg_pool)
    async with pg_pool.acquire() as conn:
        keep = await conn.fetchrow(
            "SELECT valid_until, superseded_by FROM nexuses WHERE id=$1::uuid", keep_nx)
        lose = await conn.fetchrow(
            "SELECT valid_until, superseded_by FROM nexuses WHERE id=$1::uuid", lose_nx)
        open_cnt = await conn.fetchval(
            "SELECT count(*) FROM nexuses WHERE lower(subject)='zzcol keeper' "
            "AND lower(object)='zzcol x' AND valid_until IS NULL AND superseded_by IS NULL")
    assert keep["valid_until"] is None  # the keeper row stays open
    assert lose["valid_until"] is not None  # the loser row was closed
    assert str(lose["superseded_by"]) == keep_nx  # superseded by the survivor
    assert open_cnt == 1  # exactly one open triple (no unique-index violation)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repoint_self_loop_closes(pg_pool):
    async with pg_pool.acquire() as conn:
        keeper = await _seed_entity(conn, "Zzsl Keeper")
        loser = await _seed_entity(conn, "the Zzsl Keeper", merged_into=keeper)
        # Loser -> Keeper re-points to Keeper -> Keeper (a self-loop) -> closed.
        nx = await _seed_nexus(conn, "the Zzsl Keeper", "Zzsl Keeper")
    await _compact_merged_edges(pg_pool)
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT valid_until FROM nexuses WHERE id=$1::uuid", nx)
    assert row["valid_until"] is not None  # self-loop closed


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repoint_fact_subject(pg_pool):
    async with pg_pool.acquire() as conn:
        keeper = await _seed_entity(conn, "Zzf Keeper")
        loser = await _seed_entity(conn, "the Zzf Keeper", merged_into=keeper)
        f = await _seed_fact(conn, "the Zzf Keeper", "has_capital", "Zzville")
    await _compact_merged_edges(pg_pool)
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT subject FROM facts WHERE id=$1::uuid", f)
    assert row["subject"] == "Zzf Keeper"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_compaction_idempotent_and_marks(pg_pool):
    async with pg_pool.acquire() as conn:
        keeper = await _seed_entity(conn, "Zzidem Keeper")
        loser = await _seed_entity(conn, "the Zzidem Keeper", merged_into=keeper)
        await _seed_nexus(conn, "the Zzidem Keeper", "Zzidem Y")
    first = await _compact_merged_edges(pg_pool)
    second = await _compact_merged_edges(pg_pool)
    async with pg_pool.acquire() as conn:
        marked = await conn.fetchval(
            "SELECT data->'merge' ? 'compacted_at' FROM entity_profiles "
            "WHERE id=$1::uuid", loser)
    assert first >= 1
    assert second == 0  # already compacted -> no work
    assert marked is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chained_merge_repoints_to_terminal(pg_pool):
    # Review HIGH: a chain L -> K -> K2 must re-point L's edges onto the TERMINAL
    # survivor K2, never the intermediate tombstone K — even when K was compacted
    # first (its data.merge already flagged), which used to strand L forever.
    async with pg_pool.acquire() as conn:
        k2 = await _seed_entity(conn, "Zzchain Terminal")
        k = await _seed_entity(conn, "Zzchain Middle", merged_into=k2)
        loser = await _seed_entity(conn, "Zzchain Loser", merged_into=k)
        nx = await _seed_nexus(conn, "Zzchain Loser", "Zzchain X")
        # pre-flag the intermediate K as already compacted (the stranding order).
        await conn.execute(
            "UPDATE entity_profiles SET data = jsonb_set(data,'{merge,compacted_at}',"
            "to_jsonb(now()::text),true) WHERE id=$1::uuid", k)
    await _compact_merged_edges(pg_pool)
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT subject FROM nexuses WHERE id=$1::uuid", nx)
    assert row["subject"] == "Zzchain Terminal"  # terminal survivor, NOT "Middle"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_inspect_entity_hides_tombstone(pg_pool):
    from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort
    async with pg_pool.acquire() as conn:
        keeper = await _seed_entity(conn, "Zzins Keeper", cls="person")
        await _seed_entity(conn, "Zzins Loser Tombstone", cls="person",
                           merged_into=keeper)
    port = PostgresQdrantSubstrateQueryPort(pg_pool=pg_pool, qdrant_client=None)
    res = await port.inspect_entity(name="Zzins Loser Tombstone")
    assert res["found"] is False  # a merged tombstone is not a live entity
