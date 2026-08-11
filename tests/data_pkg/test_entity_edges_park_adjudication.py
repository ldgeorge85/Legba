# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W3-A step 4 — adjudicating `entity_edges_unresolved` (migration 0182).

0143 built the park on the principle that an unresolvable endpoint is a
MEASUREMENT that stays adjudicable later. 0182 is the "later": it makes the
park SELF-DRAINING and retires rows that are no longer adjudicable, while
REFUSING to guess at the two classes that are entity-resolution work.

Live adjudication of the 1,286 parked rows (2026-08-03, read-only):

    resolves exactly now          0   mint + drain
    ambiguous                   480   residual — cross-class duplicate profiles
    dead name, punctuation       29   residual — NOT auto-resolved (W3-C: 68.3%)
    dead name, no near match    777   residual

The mechanism ships even though it drains zero rows today: a park row becomes
resolvable the moment an entity is minted or merged under its name, and without
this the park only ever grows.

The tests that matter most here are the NEGATIVE ones — the two classes 0182
must leave alone. Guessing an endpoint mints an edge nobody asserted, which is
worse than a park row that says so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.migrations import MIGRATIONS_DIR

ADJUDICATE = "0182_adjudicate_parked_edge_endpoints.sql"


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


async def _run(conn: Any) -> None:
    async with conn.transaction():
        await conn.execute((Path(MIGRATIONS_DIR) / ADJUDICATE).read_text())


async def _entity(conn: Any, name: str, *, cls: str = "organization") -> str:
    return await conn.fetchval(
        """INSERT INTO entity_profiles (canonical_name, entity_class,
             entity_type, data) VALUES ($1, $2, $2, '{}'::jsonb) RETURNING id""",
        name, cls)


async def _park(conn: Any, src: str, dst: str, *, reason: str = "src_unresolved",
                origin_table: str = "nexuses", origin_id: Any = None,
                edge_type: str = "allied with",
                family: str = "relation") -> str:
    return await conn.fetchval(
        """INSERT INTO entity_edges_unresolved
             (src_text, dst_text, edge_type, edge_family, reason, origin_table,
              origin_id, payload)
           VALUES ($1, $2, $3, $4, $5, $6, $7, '{}'::jsonb) RETURNING id""",
        src, dst, edge_type, family, reason, origin_table, origin_id)


async def _parked(conn: Any, pid: str) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM entity_edges_unresolved WHERE id=$1::uuid", pid)


# ---------------------------------------------------------------------------
# The self-draining mechanism
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_park_row_that_now_resolves_is_minted_and_drained(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a = await _entity(conn, f"Pk A {tag}")
        b = await _entity(conn, f"Pk B {tag}")
        pid = await _park(conn, f"Pk A {tag}", f"Pk B {tag}")

        await _run(conn)

        edge = await conn.fetchrow(
            """SELECT * FROM entity_edges
                WHERE src_id=$1::uuid AND dst_id=$2::uuid
                  AND valid_until IS NULL AND superseded_by IS NULL""", a, b)
        still = await _parked(conn, pid)

    assert edge is not None, "the endpoints resolve now — mint the edge"
    assert edge["edge_family"] == "relation", (
        "the producer's classification is carried, not re-derived")
    assert still == 0, "and the park row is drained"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_pair_that_merged_into_one_entity_is_drained_not_minted(pg_pool):
    """Both endpoints now resolve to the SAME entity — settled, not pending.
    Leaving it parked would overstate the residue forever."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        keeper = await _entity(conn, f"Pks K {tag}")
        loser = await _entity(conn, f"Pks L {tag}")
        await conn.execute(
            "UPDATE entity_profiles SET merged_into=$2::uuid WHERE id=$1::uuid",
            loser, keeper)
        pid = await _park(conn, f"Pks K {tag}", f"Pks L {tag}")

        before = await conn.fetchval("SELECT count(*) FROM entity_edges")
        await _run(conn)
        after = await conn.fetchval("SELECT count(*) FROM entity_edges")
        still = await _parked(conn, pid)

    assert still == 0
    assert after == before, "an entity is not related to itself"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_adjudication_is_idempotent(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a = await _entity(conn, f"Pki A {tag}")
        b = await _entity(conn, f"Pki B {tag}")
        await _park(conn, f"Pki A {tag}", f"Pki B {tag}")

        await _run(conn)
        first = await conn.fetchval(
            """SELECT observed_count FROM entity_edges
                WHERE src_id=$1::uuid AND dst_id=$2::uuid""", a, b)
        await _run(conn)
        second = await conn.fetchval(
            """SELECT count(*) FROM entity_edges
                WHERE src_id=$1::uuid AND dst_id=$2::uuid""", a, b)

    assert first == 1
    assert second == 1, "a re-run coalesces through the same conflict key"


# ---------------------------------------------------------------------------
# THE NEGATIVE TESTS — the classes 0182 must refuse to touch
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_ambiguous_endpoint_is_left_parked_and_never_guessed(pg_pool):
    """480 of the 1,286 live park rows are this class, and almost none is a
    genuine two-referents collision — they are cross-class duplicate PROFILES
    (the V-G6 defect). The fix is an entity merge; picking a side here would
    manufacture an edge nobody asserted."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        name = f"Pka Ambig {tag}"
        await _entity(conn, name, cls="location")
        await _entity(conn, name, cls="person")
        await _entity(conn, f"Pka Peer {tag}")
        pid = await _park(conn, name, f"Pka Peer {tag}", reason="ambiguous")

        await _run(conn)
        still = await _parked(conn, pid)
        minted = await conn.fetchval(
            """SELECT count(*) FROM entity_edges e
                JOIN entity_profiles p ON p.id = e.src_id
               WHERE p.canonical_name = $1""", name)

    assert still == 1, "still parked — it is adjudicable, just not by a guess"
    assert minted == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_punctuation_class_is_counted_not_applied(pg_pool):
    """`Choe Son - hui` vs `choe son-hui` differ only by the NER tokenizer's
    hyphen spacing, and 29 live park rows would resolve if that were normalized
    away. They are NOT resolved: W3-C measured 68.3% precision on this class, so
    a bulk apply mints a wrong edge roughly one time in three."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        await _entity(conn, f"Pkp Choe Son - hui {tag}")
        await _entity(conn, f"Pkp Peer {tag}")
        pid = await _park(conn, f"Pkp Choe Son-hui {tag}", f"Pkp Peer {tag}")

        await _run(conn)
        still = await _parked(conn, pid)

    assert still == 1, (
        "the near-match is REPORTED for the entity-resolution train, never "
        "auto-applied — an edge minted wrongly is worse than one parked "
        "honestly")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_genuinely_dead_name_stays_parked(pg_pool):
    """A park row whose origin assertion still stands is still a measurement of
    resolution quality. It is not deleted just because it is inconvenient."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        await _entity(conn, f"Pkd Peer {tag}")
        nid = await conn.fetchval(
            """INSERT INTO nexuses (subject, object, rel_type, label, polarity,
                 confidence) VALUES ($1, $2, 'allied with', '', 1, 0.5)
               RETURNING id""", f"Pkd Ghost {tag}", f"Pkd Peer {tag}")
        pid = await _park(conn, f"Pkd Ghost {tag}", f"Pkd Peer {tag}",
                          origin_id=nid)

        await _run(conn)
        still = await _parked(conn, pid)

    assert still == 1


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_park_row_whose_origin_was_withdrawn_is_retired(pg_pool):
    """The park records a claim; when the substrate stops making the claim the
    row records nothing."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        await _entity(conn, f"Pkw Peer {tag}")
        nid = await conn.fetchval(
            """INSERT INTO nexuses (subject, object, rel_type, label, polarity,
                 confidence, valid_until)
               VALUES ($1, $2, 'allied with', '', 1, 0.5, now())
               RETURNING id""", f"Pkw Ghost {tag}", f"Pkw Peer {tag}")
        pid = await _park(conn, f"Pkw Ghost {tag}", f"Pkw Peer {tag}",
                          origin_id=nid)

        await _run(conn)
        still = await _parked(conn, pid)

    assert still == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_park_row_whose_origin_vanished_is_retired(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        await _entity(conn, f"Pkv Peer {tag}")
        pid = await _park(conn, f"Pkv Ghost {tag}", f"Pkv Peer {tag}",
                          origin_table="facts", origin_id=uuid4())

        await _run(conn)
        still = await _parked(conn, pid)

    assert still == 0, "nothing left to adjudicate"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dual_write_parks_are_not_retired_by_this_migration(pg_pool):
    """origin_id IS NULL rows dedupe on the name triple and refresh created_at,
    so age is the right signal — and a TTL is an operator decision, registered
    in retention_policies and OFF by default."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        await _entity(conn, f"Pkn Peer {tag}")
        pid = await _park(conn, f"Pkn Ghost {tag}", f"Pkn Peer {tag}")

        await _run(conn)
        still = await _parked(conn, pid)
        policy = await conn.fetchrow(
            "SELECT * FROM retention_policies "
            " WHERE policy_name='entity_edges_unresolved_retention'")

    assert still == 1
    assert policy is not None, "the TTL is registered, not hard-coded"
    assert policy["enabled"] is False, (
        "the park measures resolution quality — deleting it hides the thing it "
        "exists to show, so an operator turns it on deliberately")
