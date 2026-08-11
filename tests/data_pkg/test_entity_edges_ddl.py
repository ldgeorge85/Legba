# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-G1 migration 0143 — the id-keyed edge store's SCHEMA contract.

The table is worth building only for the invariants it makes unrepresentable,
so this file asserts the invariants rather than the column list:

  * the migration lands in the ledger (the runner globs ``*.sql`` in sorted
    filename order — a misnamed file is skipped SILENTLY, never failed);
  * endpoints are real FKs: deleting a profile CASCADEs its edges away (the
    26k-row orphan class becomes structurally impossible) while the weaker
    intermediary claim degrades to NULL instead of taking the edge with it;
  * ``edge_family`` is a closed four-value vocabulary — the seed lattice is
    separable from Legba's own derived relations, which is the whole reason
    structural balance stops measuring UN co-membership;
  * an entity is not related to itself, and there is at most ONE open edge per
    (src, dst, type, intermediary) — while CLOSED history over the same triple
    is unconstrained, so an edge may recur across time;
  * ``resolve_entity_name`` chases tombstones but NEVER guesses: an unknown or
    ambiguous name returns NULL so the caller parks it.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig

MIGRATION_NAME = "0143_entity_edges.sql"


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


async def _edge(conn: Any, src: str, dst: str, *, etype: str = "allied with",
                family: str = "relation", **kw: Any) -> str:
    eid = str(uuid4())
    await conn.execute(
        """INSERT INTO entity_edges
             (id, src_id, dst_id, intermediary_id, edge_type, edge_family)
           VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5, $6)""",
        eid, src, dst, kw.get("intermediary"), etype, family)
    return eid


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_0143_applies_on_a_fresh_substrate(pg_pool):
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name, sha256 FROM legba_data_migrations WHERE name = $1",
            MIGRATION_NAME)
    assert row is not None, (
        f"{MIGRATION_NAME} is not in the ledger — the runner globs *.sql and "
        "applies in sorted filename order, so a misnamed file is skipped "
        "silently rather than failing")
    assert len(row["sha256"]) == 64


@pytest.mark.integration
@pytest.mark.asyncio
async def test_endpoints_are_real_foreign_keys(pg_pool):
    """The whole point of the table: an edge cannot outlive its endpoint."""
    async with pg_pool.acquire() as conn:
        a = await _seed(conn, f"Zzfk Alpha {uuid4().hex[:8]}")
        b = await _seed(conn, f"Zzfk Beta {uuid4().hex[:8]}")
        eid = await _edge(conn, a, b)

        # a name that resolves to no profile cannot be an endpoint at all
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await _edge(conn, a, str(uuid4()))

        await conn.execute("DELETE FROM entity_profiles WHERE id=$1::uuid", b)
        assert await conn.fetchval(
            "SELECT count(*) FROM entity_edges WHERE id=$1::uuid", eid) == 0, \
            "deleting an endpoint must CASCADE the edge away, never orphan it"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_intermediary_degrades_to_null_rather_than_cascading(pg_pool):
    async with pg_pool.acquire() as conn:
        a = await _seed(conn, f"Zzim Alpha {uuid4().hex[:8]}")
        b = await _seed(conn, f"Zzim Beta {uuid4().hex[:8]}")
        via = await _seed(conn, f"Zzim Via {uuid4().hex[:8]}")
        eid = await _edge(conn, a, b, intermediary=via)

        await conn.execute("DELETE FROM entity_profiles WHERE id=$1::uuid", via)
        row = await conn.fetchrow(
            "SELECT intermediary_id FROM entity_edges WHERE id=$1::uuid", eid)
    assert row is not None, "the edge survives losing its intermediary"
    assert row["intermediary_id"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_edge_family_is_a_closed_four_value_vocabulary(pg_pool):
    async with pg_pool.acquire() as conn:
        a = await _seed(conn, f"Zzfam Alpha {uuid4().hex[:8]}")
        b = await _seed(conn, f"Zzfam Beta {uuid4().hex[:8]}")
        for fam in ("relation", "reference", "cooccurrence", "structural"):
            await _edge(conn, a, b, etype=f"t_{fam}", family=fam)
        with pytest.raises(asyncpg.CheckViolationError):
            await _edge(conn, a, b, etype="t_bogus", family="derived")

        registered = await conn.fetch(
            "SELECT value FROM vocabulary_entries WHERE family='edge_family'")
    assert {r["value"] for r in registered} == {
        "relation", "reference", "cooccurrence", "structural"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_self_edges(pg_pool):
    async with pg_pool.acquire() as conn:
        a = await _seed(conn, f"Zzself Alpha {uuid4().hex[:8]}")
        with pytest.raises(asyncpg.CheckViolationError):
            await _edge(conn, a, a)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_open_edge_per_triple_but_closed_history_is_free(pg_pool):
    async with pg_pool.acquire() as conn:
        a = await _seed(conn, f"Zzuq Alpha {uuid4().hex[:8]}")
        b = await _seed(conn, f"Zzuq Beta {uuid4().hex[:8]}")
        first = await _edge(conn, a, b, etype="allied with")

        with pytest.raises(asyncpg.UniqueViolationError):
            await _edge(conn, a, b, etype="allied with")

        # a different type on the same pair is a different edge
        await _edge(conn, a, b, etype="hostile to")

        # closing the first frees the triple: the relation may recur later
        await conn.execute(
            "UPDATE entity_edges SET valid_until=now() WHERE id=$1::uuid", first)
        await _edge(conn, a, b, etype="allied with")
        assert await conn.fetchval(
            "SELECT count(*) FROM entity_edges "
            " WHERE src_id=$1::uuid AND edge_type='allied with'", a) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_entity_name_chases_tombstones(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        keeper = await _seed(conn, f"Zzrn Keeper {tag}")
        loser = await _seed(conn, f"Zzrn Loser {tag}")
        await conn.execute(
            "UPDATE entity_profiles SET merged_into=$2::uuid WHERE id=$1::uuid",
            loser, keeper)

        assert str(await conn.fetchval(
            "SELECT resolve_entity_name($1)", f"Zzrn Keeper {tag}")) == keeper
        # the point: an edge naming the MERGED loser lands on the keeper
        assert str(await conn.fetchval(
            "SELECT resolve_entity_name($1)", f"Zzrn Loser {tag}")) == keeper
        # case- and whitespace-insensitive, like every other name surface
        assert str(await conn.fetchval(
            "SELECT resolve_entity_name($1)", f"  zzrn loser {tag}  ")) == keeper


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_entity_name_parks_rather_than_guesses(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        assert await conn.fetchval(
            "SELECT resolve_entity_name($1)", f"Zznope {tag}") is None

        # SAME lowered name, two classes -> two distinct terminal ids. The
        # unique index is (lower(canonical_name), entity_class), so a name is
        # not a key; picking one would manufacture an edge nobody asserted.
        await _seed(conn, f"Zzamb {tag}", cls="organization")
        await _seed(conn, f"Zzamb {tag}", cls="person")
        assert await conn.fetchval(
            "SELECT resolve_entity_name($1)", f"Zzamb {tag}") is None, \
            "an ambiguous name must resolve to NULL so the caller parks it"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unresolved_park_is_idempotent_per_origin_row(pg_pool):
    async with pg_pool.acquire() as conn:
        origin = str(uuid4())
        for _ in range(2):
            await conn.execute(
                """INSERT INTO entity_edges_unresolved
                     (src_text, dst_text, edge_type, edge_family, reason,
                      origin_table, origin_id)
                   VALUES ('Zzpark A','Zzpark B','allied with','relation',
                           'src_unresolved','nexuses',$1::uuid)
                   ON CONFLICT (origin_table, origin_id)
                     WHERE origin_id IS NOT NULL DO NOTHING""",
                origin)
        assert await conn.fetchval(
            "SELECT count(*) FROM entity_edges_unresolved WHERE origin_id=$1::uuid",
            origin) == 1, "a re-run refreshes the park, it does not inflate it"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_redundant_nexus_index_dropped(pg_pool):
    """Debate finding #21: a strict 3-col prefix of the 4-col UNIQUE index over
    the identical predicate, paying a second write per nexus insert for nothing.
    """
    async with pg_pool.acquire() as conn:
        names = {r["indexname"] for r in await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename='nexuses'")}
    assert "idx_nexuses_open_triple" not in names
    assert "idx_nexuses_triple_open" in names, "the UNIQUE index must survive"
