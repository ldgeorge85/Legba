# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W3-A step 1 — the FACTS backfill (migration 0180).

0144 took `nexuses` and 0145 took the promoted `proposed_edges`; 0180 takes the
surface both of them missed. `facts` is a subject-predicate-value store that is
mostly attributive, but the `fact_extractor` `relation` backend writes
ENTITY-TO-ENTITY triples into it — 10,912 open rows over 18 predicates on the
live substrate, invisible to every graph reader until now.

As with `test_entity_edges_backfill.py`, these tests re-execute the SHIPPED SQL
body read from disk rather than a paraphrase of it, so the assertions are about
the statements that will actually deploy — and because the migration already ran
once against the fresh test DB, every one of these also proves it is IDEMPOTENT.

What is asserted:
  * only RELATIONAL facts cross over — an attributive fact is not an edge;
  * the two tiers land in the right `edge_family` (derived -> relation,
    seed -> reference), which must agree with 0144/0145 or the same edge gets
    filed two ways;
  * polarity is derived from the predicate by the POLARITY table's rules, and
    a structural predicate signs 0 rather than guessing a side;
  * a fact naming a merged tombstone lands on the KEEPER;
  * several fact rows collapsing onto one id triple SUM their sightings and
    UNION their lineage;
  * a fact coalescing onto an edge a nexus already minted does NOT overwrite
    that edge's family or its richer evidence;
  * unresolvable and ambiguous endpoints park with a reason and an origin id;
  * `facts` is not modified.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.migrations import MIGRATIONS_DIR

BACKFILL_FACTS = "0180_backfill_entity_edges_from_facts.sql"


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


async def _run(conn: Any) -> None:
    """Apply the shipped backfill body. The DO block creates ON COMMIT DROP temp
    tables, so it must run inside a transaction."""
    async with conn.transaction():
        await conn.execute((Path(MIGRATIONS_DIR) / BACKFILL_FACTS).read_text())


async def _seed(conn: Any, name: str, *, cls: str = "organization") -> str:
    eid = str(uuid4())
    await conn.execute(
        """INSERT INTO entity_profiles
             (id, canonical_name, entity_class, entity_type, data)
           VALUES ($1::uuid, $2, $3, $3, '{}'::jsonb)""",
        eid, name, cls)
    return eid


async def _fact(conn: Any, subject: str, value: str, *,
                predicate: str = "member of", relation: bool = True,
                source_type: str = "ingestion", confidence: float = 0.6,
                analyst_id: str = "fact_extractor", day: int = 1) -> str:
    """Insert one fact. ``relation=True`` stamps the extractor's structural
    marker (``data.backend='relation'``) — the exact selector 0180 uses for the
    derived tier.

    ``day`` moves ``valid_from``: `facts` carries a UNIQUE index on
    (lower(subject), lower(predicate), lower(value), valid_from) over open rows,
    so the same triple can only recur at a different validity instant. That is
    itself worth knowing — it means the many-facts-to-one-edge collapse arises
    from merges and re-assertion over time, not from duplicate rows.
    """
    fid = str(uuid4())
    data = '{"backend": "relation", "extractor": "fact_extractor"}' \
        if relation else '{}'
    await conn.execute(
        """INSERT INTO facts
             (id, subject, predicate, value, confidence, source_type,
              analyst_id, valid_from, data)
           VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8::timestamptz,
                   $9::jsonb)""",
        fid, subject, predicate, value, confidence, source_type, analyst_id,
        datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day), data)
    return fid


def _jsonb(value: Any) -> Any:
    """asyncpg hands jsonb back as text unless a codec is registered."""
    return json.loads(value) if isinstance(value, str) else value


async def _open_edges(conn: Any, src: str) -> list[asyncpg.Record]:
    return await conn.fetch(
        """SELECT * FROM entity_edges
            WHERE src_id=$1::uuid AND valid_until IS NULL
              AND superseded_by IS NULL""", src)


# ---------------------------------------------------------------------------
# Source selection — what is an edge and what is not
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_attributive_facts_do_not_become_edges(pg_pool):
    """The whole selection question. A fact whose `value` is a literal is not an
    edge, and a seed predicate outside the closed relational vocabulary is not
    an edge either — even when both endpoint names happen to resolve."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a, b = f"Zzfb A {tag}", f"Zzfb B {tag}"
        a_id = await _seed(conn, a)
        await _seed(conn, b)

        # attributive: no relation backend, predicate not relational
        await _fact(conn, a, b, predicate="population", relation=False,
                    source_type="seed")
        # seed + relational predicate IS an edge (asserted separately below)
        await _run(conn)

        rows = await _open_edges(conn, a_id)

    assert rows == [], (
        "an attributive fact must not become an edge just because its value "
        "string happens to name an entity")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tiers_land_in_the_right_family(pg_pool):
    """Derived -> `relation`, seed -> `reference`. This map must agree with
    0144/0145's or the same edge is filed two different ways."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        drv, ref, peer = f"Zzft D {tag}", f"Zzft R {tag}", f"Zzft P {tag}"
        drv_id, ref_id = await _seed(conn, drv), await _seed(conn, ref)
        await _seed(conn, peer)

        await _fact(conn, drv, peer, predicate="conflict with")
        await _fact(conn, ref, peer, predicate="head of state",
                    relation=False, source_type="seed",
                    analyst_id="seed.wikidata_leaders")

        await _run(conn)

        drv_rows = await _open_edges(conn, drv_id)
        ref_rows = await _open_edges(conn, ref_id)

    assert len(drv_rows) == 1 and drv_rows[0]["edge_family"] == "relation"
    assert len(ref_rows) == 1 and ref_rows[0]["edge_family"] == "reference"


# ---------------------------------------------------------------------------
# Polarity — derived from the predicate, conservative by default
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("predicate,expected", [
    ("ally of", 1),
    ("member of", 1),
    ("head of government", 1),
    ("conflict with", -1),
    ("sanctioned by", -1),
    # structural / historical — NOT an alignment claim, so it signs 0 and the
    # consumer excludes it from balance rather than counting a lattice as a side
    ("located in", 0),
    ("border with", 0),
    ("capital of", 0),
    ("controls", 0),
    ("founded by", 0),
])
async def test_polarity_is_derived_from_the_predicate(pg_pool, predicate,
                                                      expected):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a, b = f"Zzfp A {tag}", f"Zzfp B {tag}"
        a_id = await _seed(conn, a)
        await _seed(conn, b)
        await _fact(conn, a, b, predicate=predicate)

        await _run(conn)
        rows = await _open_edges(conn, a_id)

    assert len(rows) == 1
    assert rows[0]["polarity"] == expected, (
        f"{predicate!r} signed {rows[0]['polarity']}, expected {expected}")


# ---------------------------------------------------------------------------
# Resolution — the repair, the park, the collapse
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_fact_naming_a_tombstone_lands_on_the_keeper(pg_pool):
    """The repair. A fifth of the relational fact population names an entity the
    GC has since merged away; the projection resolves through the redirect."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        keeper_n, loser_n, peer_n = (f"Zzfk K {tag}", f"Zzfk L {tag}",
                                     f"Zzfk P {tag}")
        keeper, loser = await _seed(conn, keeper_n), await _seed(conn, loser_n)
        await _seed(conn, peer_n)
        await conn.execute(
            "UPDATE entity_profiles SET merged_into=$2::uuid WHERE id=$1::uuid",
            loser, keeper)

        fid = await _fact(conn, loser_n, peer_n)
        await _run(conn)

        landed = await _open_edges(conn, keeper)
        stranded = await _open_edges(conn, loser)
        fact = await conn.fetchrow(
            "SELECT subject, valid_until FROM facts WHERE id=$1::uuid", fid)

    assert len(landed) == 1 and not stranded
    assert fact["subject"] == loser_n and fact["valid_until"] is None, (
        "`facts` is the row of record and is NOT rewritten or closed")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unresolvable_and_ambiguous_endpoints_park(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        known = f"Zzfu Known {tag}"
        await _seed(conn, known)
        ghost = f"Zzfu Ghost {tag}"
        # ambiguous: one lowered name on two profiles of different class, both
        # live, so it reaches two terminal ids
        amb = f"Zzfu Amb {tag}"
        await _seed(conn, amb, cls="location")
        await _seed(conn, amb, cls="person")

        dead_id = await _fact(conn, ghost, known)
        amb_id = await _fact(conn, amb, known)

        await _run(conn)

        parks = {str(r["origin_id"]): r for r in await conn.fetch(
            "SELECT * FROM entity_edges_unresolved WHERE origin_table='facts'"
            " AND origin_id = ANY($1::uuid[])", [dead_id, amb_id])}

    assert parks[dead_id]["reason"] == "src_unresolved"
    assert parks[amb_id]["reason"] == "ambiguous"
    assert _jsonb(parks[dead_id]["payload"])["backfill"] == "0180"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_several_facts_collapse_onto_one_edge_summing_sightings(pg_pool):
    """Repeated extraction and merges both map many fact rows onto one id
    triple. The collapse must SUM the sightings and UNION the lineage, never
    raise 'cannot affect row a second time' and never drop a citation."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        keeper_n, loser_n, peer_n = (f"Zzfc K {tag}", f"Zzfc L {tag}",
                                     f"Zzfc P {tag}")
        keeper, loser = await _seed(conn, keeper_n), await _seed(conn, loser_n)
        await _seed(conn, peer_n)
        await conn.execute(
            "UPDATE entity_profiles SET merged_into=$2::uuid WHERE id=$1::uuid",
            loser, keeper)

        f1 = await _fact(conn, keeper_n, peer_n, confidence=0.4, day=1)
        f2 = await _fact(conn, loser_n, peer_n, confidence=0.9, day=1)
        f3 = await _fact(conn, keeper_n, peer_n, confidence=0.5, day=2)

        await _run(conn)
        rows = await _open_edges(conn, keeper)

    assert len(rows) == 1, "three name triples must collapse to ONE id triple"
    assert rows[0]["observed_count"] == 3
    assert rows[0]["confidence"] == pytest.approx(0.9)
    assert set(rows[0]["derived_from"]) >= {UUID(x) for x in (f1, f2, f3)}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coalescing_onto_a_nexus_edge_preserves_its_family(pg_pool):
    """A fact landing on a triple `nexuses` already minted must ADD a sighting,
    never re-tier the edge. `edge_family` is not in the ON CONFLICT SET list and
    this is the test that keeps it out."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a, b = f"Zzfx A {tag}", f"Zzfx B {tag}"
        a_id = await _seed(conn, a)
        b_id = await _seed(conn, b)

        # a pre-existing REFERENCE edge with rich evidence, as 0145 would leave
        await conn.execute(
            """INSERT INTO entity_edges
                 (src_id, dst_id, edge_type, edge_family, polarity, confidence,
                  observed_count, evidence_set)
               VALUES ($1::uuid, $2::uuid, 'member of', 'reference', 1, 0.8, 1,
                       '{"evidence_text": "keep me"}'::jsonb)""",
            a_id, b_id)

        await _fact(conn, a, b, predicate="member of", confidence=0.3)
        await _run(conn)
        rows = await _open_edges(conn, a_id)

    assert len(rows) == 1
    assert rows[0]["edge_family"] == "reference", "the existing tier stands"
    assert rows[0]["observed_count"] == 2, "the fact is a second sighting"
    assert rows[0]["confidence"] == pytest.approx(0.8), "max, not clobber"
    assert _jsonb(rows[0]["evidence_set"])["evidence_text"] == "keep me", (
        "a thinner projection marker must never overwrite richer evidence")
