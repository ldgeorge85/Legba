# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-G1 step 4 — the backfills (migrations 0144, 0145).

The migrations run once against a fresh test DB before these tests see it, and
on that DB there is nothing to backfill. So these tests re-execute the SAME SQL
bodies against seeded fixtures: the file is read from disk and applied, which
means the assertions are about the shipped statements, not a paraphrase of them.
That also proves the property the deploy depends on — the backfills are
IDEMPOTENT, because here they are provably running a second time.

What is asserted:
  * every tier is classified correctly (the `edge_family` map must match
    `edge_family_for()` in the write path, or the backfill and the dual-write
    disagree about the same edge);
  * a row naming a merged tombstone lands on the KEEPER — the repair;
  * several name triples collapsing onto one id triple SUM their sightings and
    UNION their evidence rather than raising or dropping;
  * unresolvable and ambiguous endpoints park with a reason and an origin id;
  * `nexuses` and `proposed_edges` are not modified;
  * 0145's `co_occurs` converges with 0144's `co occurs with` instead of
    shadowing it;
  * resolve RATES on a fixture with a known mix.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.migrations import MIGRATIONS_DIR

BACKFILL_NEXUS = "0144_backfill_entity_edges_from_nexuses.sql"
BACKFILL_PROMOTED = "0145_backfill_entity_edges_from_promoted_edges.sql"


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


def _sql(name: str) -> str:
    return (Path(MIGRATIONS_DIR) / name).read_text()


async def _run(conn: Any, name: str) -> None:
    """Apply a shipped backfill body. The DO block creates ON COMMIT DROP temp
    tables, so it must run inside a transaction."""
    async with conn.transaction():
        await conn.execute(_sql(name))


async def _seed(conn: Any, name: str, *, cls: str = "organization") -> str:
    eid = str(uuid4())
    await conn.execute(
        """INSERT INTO entity_profiles
             (id, canonical_name, entity_class, entity_type, data)
           VALUES ($1::uuid, $2, $3, $3, '{}'::jsonb)""",
        eid, name, cls)
    return eid


async def _nexus(conn: Any, subject: str, object_: str, *,
                 rel_type: str = "allied with", polarity: int = 1,
                 analyst_id: str = "relationship_reifier",
                 source_type: str = "agent", confidence: float = 0.6,
                 sigs: list[Any] | None = None) -> str:
    nid = str(uuid4())
    await conn.execute(
        """INSERT INTO nexuses
             (id, subject, object, rel_type, label, polarity, confidence,
              valid_from, analyst_id, source_type, source_signal_ids)
           VALUES ($1::uuid, $2, $3, $4, '', $5, $6, $7::timestamptz, $8, $9,
                   $10::uuid[])""",
        nid, subject, object_, rel_type, polarity, confidence,
        datetime(2026, 1, 1, tzinfo=timezone.utc), analyst_id, source_type,
        sigs or [])
    return nid


async def _proposed(conn: Any, src: str, dst: str, *, status: str = "promoted",
                    rel: str = "co_occurs", confidence: float = 0.7,
                    evidence: str = "") -> str:
    pid = str(uuid4())
    await conn.execute(
        """INSERT INTO proposed_edges
             (id, source_entity, target_entity, relationship_type, confidence,
              evidence_text, status)
           VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)""",
        pid, src, dst, rel, confidence, evidence, status)
    return pid


async def _open_edges(conn: Any, src: str) -> list[asyncpg.Record]:
    return await conn.fetch(
        """SELECT * FROM entity_edges
            WHERE src_id=$1::uuid AND valid_until IS NULL
              AND superseded_by IS NULL""", src)


# ---------------------------------------------------------------------------
# 0144 — nexuses
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_0144_classifies_every_tier(pg_pool):
    """The tier map must agree with the write path's, or the backfill and the
    dual-write file the same edge two different ways."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        names = {k: f"Zzbf {k} {tag}" for k in
                 ("rel", "ref", "coo", "peer")}
        ids = {k: await _seed(conn, v) for k, v in names.items()}

        await _nexus(conn, names["rel"], names["peer"], rel_type="allied with",
                     analyst_id="relationship_reifier", source_type="agent")
        await _nexus(conn, names["ref"], names["peer"], rel_type="member of",
                     analyst_id="seed.wikidata_leaders", source_type="seed")
        await _nexus(conn, names["coo"], names["peer"],
                     rel_type="co occurs with", polarity=0,
                     analyst_id="proposed_edge_governance", source_type="agent")

        await _run(conn, BACKFILL_NEXUS)

        fams = {}
        for k in ("rel", "ref", "coo"):
            rows = await _open_edges(conn, ids[k])
            assert len(rows) == 1, f"{k} produced {len(rows)} edges"
            fams[k] = rows[0]["edge_family"]

    assert fams == {"rel": "relation", "ref": "reference",
                    "coo": "cooccurrence"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_0144_repoints_a_nexus_naming_a_tombstone(pg_pool):
    """The repair: 464 open nexus rows name a merged loser and there is no path
    that fixes them in place. The projection resolves through the redirect."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        keeper_n, loser_n = f"Zzbft Keeper {tag}", f"Zzbft Loser {tag}"
        peer_n = f"Zzbft Peer {tag}"
        keeper = await _seed(conn, keeper_n)
        loser = await _seed(conn, loser_n)
        await _seed(conn, peer_n)
        await conn.execute(
            "UPDATE entity_profiles SET merged_into=$2::uuid WHERE id=$1::uuid",
            loser, keeper)

        nid = await _nexus(conn, loser_n, peer_n)
        await _run(conn, BACKFILL_NEXUS)

        rows = await _open_edges(conn, keeper)
        stranded = await _open_edges(conn, loser)
        nex = await conn.fetchrow(
            "SELECT subject FROM nexuses WHERE id=$1::uuid", nid)

    assert len(rows) == 1 and not stranded
    assert nex["subject"] == loser_n, (
        "`nexuses` is the row of record and is NOT rewritten — the edge "
        "projection resolves, the text column stays the text it was")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_0144_collapses_name_triples_onto_one_id_triple(pg_pool):
    """Two nexus rows, two names, one entity after a merge — one edge, with the
    sightings summed and the evidence unioned."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        keeper_n, loser_n = f"Zzcol Keeper {tag}", f"Zzcol Loser {tag}"
        peer_n = f"Zzcol Peer {tag}"
        keeper = await _seed(conn, keeper_n)
        loser = await _seed(conn, loser_n)
        await _seed(conn, peer_n)
        await conn.execute(
            "UPDATE entity_profiles SET merged_into=$2::uuid WHERE id=$1::uuid",
            loser, keeper)
        s1, s2 = uuid4(), uuid4()
        n1 = await _nexus(conn, keeper_n, peer_n, confidence=0.4, sigs=[s1])
        n2 = await _nexus(conn, loser_n, peer_n, confidence=0.9, sigs=[s2])

        await _run(conn, BACKFILL_NEXUS)
        rows = await _open_edges(conn, keeper)

    assert len(rows) == 1, "the collapse is the point"
    e = rows[0]
    assert e["observed_count"] == 2, "both sightings counted"
    assert e["confidence"] == pytest.approx(0.9), "confidence takes the max"
    assert {s1, s2} <= set(e["source_signal_ids"]), "evidence unions"
    assert {UUID(n1), UUID(n2)} <= set(e["derived_from"]), \
        "each contributing nexus id joins the lineage"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_0144_parks_unresolved_and_ambiguous_with_a_reason(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        known = f"Zzpark Known {tag}"
        ghost = f"Zzpark Ghost {tag}"
        amb = f"Zzpark Amb {tag}"
        await _seed(conn, known)
        await _seed(conn, amb, cls="organization")
        await _seed(conn, amb, cls="person")

        n_unres = await _nexus(conn, known, ghost)
        n_amb = await _nexus(conn, amb, known)

        await _run(conn, BACKFILL_NEXUS)

        parks = {str(r["origin_id"]): r for r in await conn.fetch(
            "SELECT * FROM entity_edges_unresolved WHERE origin_table='nexuses'")}

    assert parks[n_unres]["reason"] == "dst_unresolved"
    assert parks[n_amb]["reason"] == "ambiguous"
    assert parks[n_unres]["edge_family"] == "relation"
    payload = parks[n_amb]["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else payload
    assert payload["src_matches"] == 2, "the park records WHY, measurably"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_0144_is_idempotent(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a_n, b_n = f"Zzidem Alpha {tag}", f"Zzidem Beta {tag}"
        ghost = f"Zzidem Ghost {tag}"
        a = await _seed(conn, a_n)
        await _seed(conn, b_n)
        await _nexus(conn, a_n, b_n)
        await _nexus(conn, a_n, ghost)

        await _run(conn, BACKFILL_NEXUS)
        first = await _open_edges(conn, a)
        parks_1 = await conn.fetchval(
            "SELECT count(*) FROM entity_edges_unresolved "
            " WHERE lower(dst_text)=lower($1)", ghost)

        await _run(conn, BACKFILL_NEXUS)
        second = await _open_edges(conn, a)
        parks_2 = await conn.fetchval(
            "SELECT count(*) FROM entity_edges_unresolved "
            " WHERE lower(dst_text)=lower($1)", ghost)

    assert len(first) == len(second) == 1, "no duplicate edge on re-run"
    assert parks_1 == parks_2 == 1, "no duplicate park on re-run"
    assert second[0]["observed_count"] == 2, (
        "a re-run is an honest re-observation of the same source row, not a "
        "silent no-op — the count says the projection ran twice")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_0144_skips_closed_nexuses(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a_n, b_n = f"Zzclosed Alpha {tag}", f"Zzclosed Beta {tag}"
        a = await _seed(conn, a_n)
        await _seed(conn, b_n)
        nid = await _nexus(conn, a_n, b_n)
        await conn.execute(
            "UPDATE nexuses SET valid_until=now() WHERE id=$1::uuid", nid)

        await _run(conn, BACKFILL_NEXUS)
        rows = await _open_edges(conn, a)
    assert not rows, "only what holds NOW is projected"


# ---------------------------------------------------------------------------
# 0145 — promoted candidates
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_0145_promotes_only_promoted(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a_n = f"Zzpe Alpha {tag}"
        a = await _seed(conn, a_n)
        for st in ("promoted", "pending", "rejected", "orphaned"):
            peer = f"Zzpe {st} {tag}"
            await _seed(conn, peer)
            await _proposed(conn, a_n, peer, status=st)

        await _run(conn, BACKFILL_PROMOTED)
        rows = await _open_edges(conn, a)

    assert len(rows) == 1, (
        "the candidate queue is not a graph — 73% of it is permanently pending "
        "and absorbing it recreates the co-mention hairball")
    assert rows[0]["edge_family"] == "cooccurrence"
    assert rows[0]["polarity"] == 0, "bare co-occurrence is neutral"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_0145_converges_with_the_governance_nexus_rather_than_shadowing(pg_pool):
    """Governance promotes a candidate BY writing a nexus, so 0144 and 0145 are
    two projections of the same edge. `co_occurs` must canonicalize onto the
    same key or the store carries the edge twice under two spellings."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a_n, b_n = f"Zzconv Alpha {tag}", f"Zzconv Beta {tag}"
        a = await _seed(conn, a_n)
        await _seed(conn, b_n)
        await _nexus(conn, a_n, b_n, rel_type="co occurs with", polarity=0,
                     analyst_id="proposed_edge_governance", confidence=0.5)
        await _proposed(conn, a_n, b_n, evidence="same dispatch",
                        confidence=0.8)

        await _run(conn, BACKFILL_NEXUS)
        await _run(conn, BACKFILL_PROMOTED)
        rows = await _open_edges(conn, a)

    assert len(rows) == 1, "one edge, two projections of it"
    e = rows[0]
    assert e["edge_type"] == "co occurs with"
    assert e["observed_count"] == 2, "seen by the candidate AND its promotion"
    assert e["confidence"] == pytest.approx(0.8)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_0145_parks_and_carries_evidence(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a_n, b_n = f"Zzpev Alpha {tag}", f"Zzpev Beta {tag}"
        ghost = f"Zzpev Ghost {tag}"
        a = await _seed(conn, a_n)
        await _seed(conn, b_n)
        pid = await _proposed(conn, a_n, b_n, evidence="cited together")
        p_bad = await _proposed(conn, a_n, ghost)

        await _run(conn, BACKFILL_PROMOTED)
        rows = await _open_edges(conn, a)
        park = await conn.fetchrow(
            "SELECT * FROM entity_edges_unresolved "
            " WHERE origin_table='proposed_edges' AND origin_id=$1::uuid", p_bad)
        untouched = await conn.fetchval(
            "SELECT status FROM proposed_edges WHERE id=$1::uuid", p_bad)

    assert len(rows) == 1
    ev = rows[0]["evidence_set"]
    ev = json.loads(ev) if isinstance(ev, str) else ev
    assert ev["evidence_text"] == "cited together", "every edge becomes citable"
    assert ev["promoted_from_proposed_edge"] == pid
    assert park is not None and park["reason"] == "dst_unresolved"
    assert untouched == "promoted", "`proposed_edges` is not modified"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_0145_is_idempotent(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a_n, b_n = f"Zzpidem Alpha {tag}", f"Zzpidem Beta {tag}"
        a = await _seed(conn, a_n)
        await _seed(conn, b_n)
        await _proposed(conn, a_n, b_n)

        await _run(conn, BACKFILL_PROMOTED)
        await _run(conn, BACKFILL_PROMOTED)
        rows = await _open_edges(conn, a)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Resolve rate on a known mix
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_rate_on_a_known_mix(pg_pool):
    """Eight nexus rows: five clean, one tombstoned (still clean — it resolves
    to the keeper), one unresolvable, one ambiguous. 6/8 = 75% clean, and every
    one of the other two is accounted for rather than lost."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        hub = f"Zzrate Hub {tag}"
        await _seed(conn, hub)
        peers = []
        for i in range(5):
            p = f"Zzrate Peer{i} {tag}"
            await _seed(conn, p)
            peers.append(p)
            await _nexus(conn, hub, p, rel_type=f"rel {i}")

        keeper_n, loser_n = f"Zzrate K {tag}", f"Zzrate L {tag}"
        keeper = await _seed(conn, keeper_n)
        loser = await _seed(conn, loser_n)
        await conn.execute(
            "UPDATE entity_profiles SET merged_into=$2::uuid WHERE id=$1::uuid",
            loser, keeper)
        await _nexus(conn, hub, loser_n, rel_type="rel tomb")

        await _nexus(conn, hub, f"Zzrate Ghost {tag}", rel_type="rel ghost")

        amb = f"Zzrate Amb {tag}"
        await _seed(conn, amb, cls="organization")
        await _seed(conn, amb, cls="person")
        await _nexus(conn, hub, amb, rel_type="rel amb")

        await _run(conn, BACKFILL_NEXUS)

        hub_id = await conn.fetchval(
            "SELECT id FROM entity_profiles WHERE canonical_name=$1", hub)
        made = await conn.fetchval(
            """SELECT count(*) FROM entity_edges WHERE src_id=$1::uuid
                 AND valid_until IS NULL AND superseded_by IS NULL""", hub_id)
        parked = await conn.fetch(
            """SELECT reason FROM entity_edges_unresolved
                WHERE origin_table='nexuses' AND lower(src_text)=lower($1)""",
            hub)
        landed_on_keeper = await conn.fetchval(
            """SELECT count(*) FROM entity_edges
                WHERE src_id=$1::uuid AND dst_id=$2::uuid""", hub_id, keeper)

    assert made == 6, "5 clean + the tombstoned one, resolved onto its keeper"
    assert landed_on_keeper == 1
    assert sorted(r["reason"] for r in parked) == ["ambiguous", "dst_unresolved"]
    assert made + len(parked) == 8, "every source row is accounted for"
