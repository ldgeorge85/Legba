# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-G1 step 3 — the `entity_edges` DUAL-WRITE.

Every producer that writes a nexus writes the id-keyed edge too, in the same
transaction. These tests traverse the REAL binding path — ``write_nexus`` as the
reifier, proposed_edge_governance and the seed adapters all call it — rather than
the mirror function in isolation. A parallel write nothing invokes is worth
nothing.

What is asserted:
  * PARITY — a nexus write produces BOTH rows, atomically;
  * the nexus write is UNCHANGED: same row, same columns, and an unresolvable
    endpoint does not cost the legacy row (readers are still on `nexuses`);
  * the four-tier `edge_family` map, per producer;
  * unresolvable and ambiguous endpoints PARK with a reason and are counted —
    never guessed, never dropped;
  * re-observation lifts confidence, unions evidence and increments
    `observed_count`, which is what makes decay evidential rather than row-age;
  * a polarity flip supersedes the prior edge, matching the nexus contract.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.provenance import AnalystContext, NexusPayload, write_nexus
from legba.data.provenance.entity_edge_writes import COUNTERS, edge_family_for


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest.fixture(autouse=True)
def _fresh_counters():
    COUNTERS.reset()
    yield
    COUNTERS.reset()


def _ctx(analyst_id: str = "relationship_reifier") -> AnalystContext:
    return AnalystContext(
        analyst_id=analyst_id, analyst_version="test", run_id=uuid4(),
        target_id=None, target_version=None)


async def _seed(conn: Any, name: str, *, cls: str = "organization") -> str:
    eid = str(uuid4())
    await conn.execute(
        """INSERT INTO entity_profiles
             (id, canonical_name, entity_class, entity_type, data)
           VALUES ($1::uuid, $2, $3, $3, '{}'::jsonb)""",
        eid, name, cls)
    return eid


async def _write(conn: Any, subject: str, object_: str, *,
                 rel_type: str = "allied with", polarity: int = 1,
                 confidence: float = 0.7, analyst_id: str = "relationship_reifier",
                 source_type: str | None = None, data: dict | None = None,
                 sigs: list[UUID] | None = None, **kw: Any):
    payload = NexusPayload(
        subject=subject, object=object_, rel_type=rel_type,
        label=f"{subject} {rel_type} {object_}", polarity=polarity,
        intent=kw.get("intent", ""), channel=kw.get("channel", "direct"),
        confidence=confidence,
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        data=data or {},
    )
    extra: dict[str, Any] = {}
    if source_type is not None:
        extra["source_type"] = source_type
    return await write_nexus(
        conn, analyst_ctx=_ctx(analyst_id), payload=payload,
        derived_from=[], source_signal_ids=sigs or [], **extra)


async def _edges(conn: Any, src: str) -> list[asyncpg.Record]:
    return await conn.fetch(
        "SELECT * FROM entity_edges WHERE src_id=$1::uuid ORDER BY created_at",
        src)


# ---------------------------------------------------------------------------
# The family map — a pure function, exhaustive over the live producer set
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "source_type,analyst_id,rel_type,expected",
    [
        # every (analyst_id, source_type, rel_type) combination measured live
        ("agent", "relationship_reifier", "allied with", "relation"),
        ("agent", "relationship_reifier", "co occurs with", "cooccurrence"),
        ("agent", "proposed_edge_governance", "co occurs with", "cooccurrence"),
        ("seed", "seed.wikidata_leaders", "member of", "reference"),
        ("seed", "seed.world_baseline", "in active conflict with", "reference"),
        ("seed", "seed.sipri_arms_transfers", "arms transfer to", "reference"),
        ("manual", "seed.manual_batch", "hostile to", "reference"),
        # the ACLED adapter stamps 'backfill' but is still an import
        ("backfill", "seed.acled_conflict", "hostile to", "reference"),
        # env-gated proxy chains are Legba's own derivation
        ("inferred", "graph_mining", "proxy_hostility", "relation"),
    ],
)
def test_edge_family_map_covers_every_live_producer(
        source_type, analyst_id, rel_type, expected):
    assert edge_family_for(source_type, analyst_id, rel_type) == expected


# ---------------------------------------------------------------------------
# Parity
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_nexus_write_produces_BOTH_rows(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a_name, b_name = f"Zzdw Alpha {tag}", f"Zzdw Beta {tag}"
        a = await _seed(conn, a_name)
        b = await _seed(conn, b_name)
        sig = uuid4()

        out, dlq = await _write(conn, a_name, b_name, sigs=[sig])
        assert dlq is None and out is not None

        nex = await conn.fetchrow(
            "SELECT * FROM nexuses WHERE lower(subject)=lower($1)", a_name)
        edges = await _edges(conn, a)

    assert nex is not None, "the legacy row is unchanged and still written"
    assert len(edges) == 1, "and the id-keyed mirror landed beside it"
    e = edges[0]
    assert str(e["dst_id"]) == b
    assert e["edge_type"] == "allied with" == nex["rel_type"]
    assert e["edge_family"] == "relation"
    assert e["polarity"] == nex["polarity"] == 1
    assert e["confidence"] == pytest.approx(nex["confidence"])
    assert sig in e["source_signal_ids"], "the citation handle travels"
    assert COUNTERS.written == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_both_rows_are_ONE_transaction(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a_name, b_name = f"Zzatom2 Alpha {tag}", f"Zzatom2 Beta {tag}"
        await _seed(conn, a_name)
        await _seed(conn, b_name)

        tx = conn.transaction()
        await tx.start()
        await _write(conn, a_name, b_name)
        await tx.rollback()

        assert await conn.fetchval(
            "SELECT count(*) FROM nexuses WHERE lower(subject)=lower($1)",
            a_name) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM entity_edges e JOIN entity_profiles p "
            "  ON p.id=e.src_id WHERE lower(p.canonical_name)=lower($1)",
            a_name) == 0, "neither row survives — they roll back together"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_producer_lands_in_the_reference_tier(pg_pool):
    """The split that stops structural balance measuring UN co-membership."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a_name, b_name = f"Zzref Country {tag}", f"Zzref Igo {tag}"
        a = await _seed(conn, a_name, cls="country")
        await _seed(conn, b_name)

        await _write(conn, a_name, b_name, rel_type="member of",
                     analyst_id="seed.wikidata_leaders", source_type="seed")
        edges = await _edges(conn, a)

    assert len(edges) == 1
    assert edges[0]["edge_family"] == "reference"
    assert edges[0]["source_type"] == "seed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cooccurrence_producer_lands_in_the_cooccurrence_tier(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a_name, b_name = f"Zzcoo Alpha {tag}", f"Zzcoo Beta {tag}"
        a = await _seed(conn, a_name)
        await _seed(conn, b_name)

        await _write(conn, a_name, b_name, rel_type="CoOccursWith", polarity=0,
                     analyst_id="proposed_edge_governance",
                     data={"promoted_from_proposed_edge": str(uuid4()),
                           "evidence_text": "seen in the same dispatch"})
        edges = await _edges(conn, a)

    assert len(edges) == 1
    e = edges[0]
    assert e["edge_family"] == "cooccurrence"
    assert e["edge_type"] == "co occurs with", (
        "the edge shares the nexus's canonicalized rel_type, so the two tables "
        "cannot disagree about the triple")
    ev = json.loads(e["evidence_set"]) if isinstance(e["evidence_set"], str) \
        else e["evidence_set"]
    assert ev["evidence_text"] == "seen in the same dispatch", \
        "a promoted candidate's evidence becomes the edge's citation"


# ---------------------------------------------------------------------------
# Park, never guess
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_unresolvable_endpoint_parks_and_the_nexus_still_lands(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a_name = f"Zzunres Alpha {tag}"
        ghost = f"Zzunres Ghost {tag}"          # never seeded
        await _seed(conn, a_name)

        out, dlq = await _write(conn, a_name, ghost)
        assert dlq is None and out is not None

        nex = await conn.fetchval(
            "SELECT count(*) FROM nexuses WHERE lower(object)=lower($1)", ghost)
        park = await conn.fetchrow(
            "SELECT * FROM entity_edges_unresolved WHERE lower(dst_text)=lower($1)",
            ghost)

    assert nex == 1, (
        "readers are still on `nexuses` — an unresolvable endpoint must never "
        "cost the legacy row")
    assert park is not None, "the residue is recorded, not dropped"
    assert park["reason"] == "dst_unresolved"
    assert park["edge_family"] == "relation"
    assert COUNTERS.parked == 1 and COUNTERS.written == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ambiguous_endpoint_parks_rather_than_picking_one(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        amb = f"Zzamb2 Both {tag}"
        b_name = f"Zzamb2 Beta {tag}"
        await _seed(conn, amb, cls="organization")
        await _seed(conn, amb, cls="person")     # same lowered name, two ids
        await _seed(conn, b_name)

        await _write(conn, amb, b_name)
        park = await conn.fetchrow(
            "SELECT * FROM entity_edges_unresolved WHERE lower(src_text)=lower($1)",
            amb)
        made = await conn.fetchval(
            "SELECT count(*) FROM entity_edges e JOIN entity_profiles p "
            "  ON p.id=e.src_id WHERE lower(p.canonical_name)=lower($1)", amb)

    assert park is not None and park["reason"] == "ambiguous"
    assert made == 0, "no edge is invented for an ambiguous name"
    assert COUNTERS.by_reason == {"ambiguous": 1}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repeated_unresolvable_pair_does_not_inflate_the_park(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a_name, ghost = f"Zzpk Alpha {tag}", f"Zzpk Ghost {tag}"
        await _seed(conn, a_name)
        for _ in range(3):
            await _write(conn, a_name, ghost)
        n = await conn.fetchval(
            "SELECT count(*) FROM entity_edges_unresolved "
            " WHERE lower(dst_text)=lower($1)", ghost)
    assert n == 1, "the park is a measurement of the residue, not of retries"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_endpoints_that_merged_together_skip_rather_than_park(pg_pool):
    """Both names resolve to the same entity after a merge. Not an error, not a
    park — an entity is simply not related to itself."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a_name, b_name = f"Zzmrg Alpha {tag}", f"Zzmrg Beta {tag}"
        a = await _seed(conn, a_name)
        b = await _seed(conn, b_name)
        await conn.execute(
            "UPDATE entity_profiles SET merged_into=$2::uuid WHERE id=$1::uuid",
            b, a)

        out, _ = await _write(conn, a_name, b_name)
        assert out is not None, "the nexus still records the assertion"
        made = await conn.fetchval(
            "SELECT count(*) FROM entity_edges WHERE src_id=$1::uuid", a)
        parked = await conn.fetchval(
            "SELECT count(*) FROM entity_edges_unresolved "
            " WHERE lower(src_text)=lower($1)", a_name)

    assert made == 0 and parked == 0
    assert COUNTERS.self_edges == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_edge_naming_a_tombstone_lands_on_the_keeper(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        keeper_n, loser_n = f"Zztomb Keeper {tag}", f"Zztomb Loser {tag}"
        other_n = f"Zztomb Other {tag}"
        keeper = await _seed(conn, keeper_n)
        loser = await _seed(conn, loser_n)
        await _seed(conn, other_n)
        await conn.execute(
            "UPDATE entity_profiles SET merged_into=$2::uuid WHERE id=$1::uuid",
            loser, keeper)

        await _write(conn, loser_n, other_n)
        edges = await _edges(conn, keeper)

    assert len(edges) == 1, (
        "a write naming the merged loser resolves onto its keeper instead of "
        "creating a row that names a tombstone — which is the whole defect")


# ---------------------------------------------------------------------------
# Re-observation and supersession
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_re_observation_counts_a_sighting_and_unions_evidence(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a_name, b_name = f"Zzobs Alpha {tag}", f"Zzobs Beta {tag}"
        a = await _seed(conn, a_name)
        await _seed(conn, b_name)
        s1, s2 = uuid4(), uuid4()

        await _write(conn, a_name, b_name, confidence=0.4, sigs=[s1])
        await _write(conn, a_name, b_name, confidence=0.8, sigs=[s2])
        edges = await _edges(conn, a)

    assert len(edges) == 1, "the same triple is one open edge"
    e = edges[0]
    assert e["observed_count"] == 2, (
        "decay must be EVIDENTIAL — an edge ages because nobody reported it "
        "again, not because the row is old")
    assert e["confidence"] == pytest.approx(0.8), "confidence lifts to the max"
    assert {s1, s2} <= set(e["source_signal_ids"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_polarity_flip_supersedes_the_prior_edge(pg_pool):
    """Matching the nexus contract: a re-type with a different sign closes the
    prior open row rather than mutating it, so the history stays auditable."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a_name, b_name = f"Zzflip Alpha {tag}", f"Zzflip Beta {tag}"
        a = await _seed(conn, a_name)
        await _seed(conn, b_name)

        await _write(conn, a_name, b_name, rel_type="allied with", polarity=1)
        await _write(conn, a_name, b_name, rel_type="allied with", polarity=-1)
        rows = await _edges(conn, a)

    assert len(rows) == 2
    closed = [r for r in rows if r["valid_until"] is not None]
    open_ = [r for r in rows if r["valid_until"] is None]
    assert len(closed) == 1 and len(open_) == 1
    assert closed[0]["polarity"] == 1 and open_[0]["polarity"] == -1
    assert str(closed[0]["superseded_by"]) == str(open_[0]["id"])
