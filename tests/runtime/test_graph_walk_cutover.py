# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W3-A cutover 2 — the path / proxy / broker walks moved onto `entity_edges`.

The three walks (`query_paths`, `find_proxy_chains`, `query_brokers`) used to
join `nexuses` on `lower(subject) = lower(object)` text. Two consequences, and
the second is the one that changed the ANSWERS:

  * **Identity.** A merge severed a chain — its two halves named the loser and
    the keeper and no text join bridged them — while one name borne by two
    profiles fused two actors into a single hop. The walk now traverses
    `src_id`/`dst_id` foreign keys.
  * **A co-mention is not a path.** 8,635 of 12,732 open nexus rows are
    `co occurs with`, so the old walk's "chains" were overwhelmingly two nouns
    appearing in the same document twice in a row. `families` now defaults to
    the ASSERTING tiers.

Plus the fail-loud contract: on an id-keyed walk an endpoint that resolves to
no entity is indistinguishable from "not connected" unless it is said out loud.
"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio

from legba.runtime.substrate_query_port import (
    _ASSERTING_FAMILIES,
    PostgresQdrantSubstrateQueryPort,
    _walk_families,
)


@pytest_asyncio.fixture
async def pg_pool(migrated_pg):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def port(pg_pool):
    return PostgresQdrantSubstrateQueryPort(
        pg_pool=pg_pool, qdrant_client=None,
        signals_collection="legba_test_walk__signals")


async def _entity(conn, name: str, *, cls: str = "organization"):
    return await conn.fetchval(
        """INSERT INTO entity_profiles (canonical_name, entity_class,
             entity_type, data) VALUES ($1, $2, $2, '{}'::jsonb) RETURNING id""",
        name, cls)


async def _edge(conn, src, dst, *, rel="supports", polarity=1,
                family="relation", via=None, confidence=0.9):
    return await conn.fetchval(
        """INSERT INTO entity_edges
             (src_id, dst_id, intermediary_id, edge_type, edge_family,
              polarity, confidence)
           VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
        src, dst, via, rel, family, polarity, confidence)


# ---------------------------------------------------------------------------
# The family default
# ---------------------------------------------------------------------------

def test_asserting_families_exclude_cooccurrence():
    assert "cooccurrence" not in _ASSERTING_FAMILIES
    assert set(_walk_families(None)) == set(_ASSERTING_FAMILIES)


def test_unknown_families_degrade_to_the_default_rather_than_raising():
    """These args arrive from LLM tool-use; a hallucinated family must not fail
    a whole consult turn."""
    assert _walk_families(["nonsense"]) == list(_ASSERTING_FAMILIES)
    assert _walk_families(["cooccurrence", "nonsense"]) == ["cooccurrence"]


@pytest.mark.asyncio
async def test_a_cooccurrence_chain_is_not_a_path_by_default(pg_pool, port):
    """The hairball. Two entities appearing in one document is not a
    relationship, so it must not connect them in a path walk unless asked."""
    async with pg_pool.acquire() as conn:
        a = await _entity(conn, "Wlk CoA")
        b = await _entity(conn, "Wlk CoB")
        await _edge(conn, a, b, rel="co occurs with", polarity=0,
                    family="cooccurrence")

    default = await port.query_paths(subject="Wlk CoA", obj="Wlk CoB")
    assert default["paths"] == []
    assert default["edge_families"] == list(_ASSERTING_FAMILIES)

    opted_in = await port.query_paths(
        subject="Wlk CoA", obj="Wlk CoB", families=["cooccurrence"])
    assert len(opted_in["paths"]) == 1


# ---------------------------------------------------------------------------
# Identity — what the text join got wrong
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_merge_no_longer_severs_a_chain(pg_pool, port):
    """A -> L and K -> B where L merged into K is ONE two-hop chain. The text
    join saw two disconnected fragments."""
    async with pg_pool.acquire() as conn:
        a = await _entity(conn, "Wlk MA")
        k = await _entity(conn, "Wlk MK")
        loser = await _entity(conn, "Wlk ML")
        b = await _entity(conn, "Wlk MB")
        # the edge was minted against the loser, then the merge folded it
        await _edge(conn, a, loser)
        await _edge(conn, k, b)
        await conn.execute(
            "UPDATE entity_profiles SET merged_into=$2 WHERE id=$1", loser, k)
        await conn.execute("SELECT public.fold_entity_edges($1)", loser)

    out = await port.query_paths(subject="Wlk MA", obj="Wlk MB")
    assert len(out["paths"]) == 1
    assert out["paths"][0]["hops"] == 2
    assert out["paths"][0]["nodes"] == ["Wlk MA", "Wlk MK", "Wlk MB"]


@pytest.mark.asyncio
async def test_paths_carry_both_names_and_ids(pg_pool, port):
    async with pg_pool.acquire() as conn:
        a = await _entity(conn, "Wlk IdA")
        b = await _entity(conn, "Wlk IdB")
        await _edge(conn, a, b)

    out = await port.query_paths(subject="Wlk IdA", obj="Wlk IdB")
    p = out["paths"][0]
    assert p["nodes"] == ["Wlk IdA", "Wlk IdB"]
    assert p["node_ids"] == [str(a), str(b)]


# ---------------------------------------------------------------------------
# Fail loud — never a confidently empty answer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unresolvable_endpoint_warns_instead_of_returning_empty(port):
    out = await port.query_paths(subject="Wlk Nobody Here", obj="Wlk Nor Here")
    assert out["paths"] == []
    assert len(out["warnings"]) == 2
    assert "subject_unresolved" in out["warnings"][0]
    assert "object_unresolved" in out["warnings"][1]


@pytest.mark.asyncio
async def test_an_ambiguous_endpoint_warns_rather_than_picking(pg_pool, port):
    """`resolve_entity_name` returns NULL for a name reaching two terminal ids.
    Picking one would manufacture a path nobody asserted."""
    async with pg_pool.acquire() as conn:
        await _entity(conn, "Wlk Ambig", cls="location")
        await _entity(conn, "Wlk Ambig", cls="person")
        b = await _entity(conn, "Wlk AmbigPeer")

    out = await port.query_paths(subject="Wlk Ambig", obj="Wlk AmbigPeer")
    assert out["paths"] == []
    assert any("ambiguous" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_proxy_chains_propagate_the_miss(port):
    out = await port.find_proxy_chains(subject="Wlk Ghost A", obj="Wlk Ghost B")
    assert out["chains"] == []
    assert out["warnings"], "a miss must not read as 'no proxy chains'"


# ---------------------------------------------------------------------------
# Brokers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_broker_tally_keys_on_identity_not_surface(pg_pool, port):
    """Two surfaces of one actor must not be counted as two brokers each with
    half the path count — that demotes the very node the measure exists to
    find."""
    async with pg_pool.acquire() as conn:
        a1 = await _entity(conn, "Wlk BA1")
        a2 = await _entity(conn, "Wlk BA2")
        mid = await _entity(conn, "Wlk BMid")
        alias = await _entity(conn, "Wlk BMidAlias")
        b = await _entity(conn, "Wlk BB")
        await _edge(conn, a1, mid)
        await _edge(conn, a2, alias)
        await _edge(conn, mid, b)
        await _edge(conn, alias, b)
        # the alias is the same actor
        await conn.execute(
            "UPDATE entity_profiles SET merged_into=$2 WHERE id=$1", alias, mid)
        await conn.execute("SELECT public.fold_entity_edges($1)", alias)

    out = await port.query_brokers(
        camp_a=["Wlk BA1", "Wlk BA2"], camp_b=["Wlk BB"])
    assert len(out["brokers"]) == 1
    assert out["brokers"][0]["entity"] == "Wlk BMid"
    assert out["brokers"][0]["path_count"] == 2, (
        "both routes credit the ONE broker")
    assert out["brokers"][0]["entity_id"] == str(mid)


@pytest.mark.asyncio
async def test_an_unresolvable_camp_member_is_named_not_dropped(pg_pool, port):
    async with pg_pool.acquire() as conn:
        a = await _entity(conn, "Wlk CampA")
        mid = await _entity(conn, "Wlk CampMid")
        b = await _entity(conn, "Wlk CampB")
        await _edge(conn, a, mid)
        await _edge(conn, mid, b)

    out = await port.query_brokers(
        camp_a=["Wlk CampA", "Wlk Not An Entity"], camp_b=["Wlk CampB"])
    assert [x["entity"] for x in out["brokers"]] == ["Wlk CampMid"]
    assert any("Wlk Not An Entity" in w for w in out["warnings"]), (
        "a ranking over half a camp is a different answer from one over all "
        "of it — the caller has to be able to tell")


@pytest.mark.asyncio
async def test_brokers_return_empty_with_a_warning_when_no_camp_resolves(port):
    out = await port.query_brokers(camp_a=["Wlk Nope"], camp_b=["Wlk Nada"])
    assert out["brokers"] == []
    assert out["warnings"]


# ---------------------------------------------------------------------------
# Proxy cut-outs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_reified_cutout_is_named_by_join(pg_pool, port):
    """The intermediary is an entity ID now, so it is NAMED rather than
    returned as whatever string the producer happened to write."""
    async with pg_pool.acquire() as conn:
        a = await _entity(conn, "Wlk PxA")
        b = await _entity(conn, "Wlk PxB")
        via = await _entity(conn, "Wlk PxVia")
        await _edge(conn, a, b, rel="proxy_hostility", polarity=-1, via=via)

    out = await port.find_proxy_chains(subject="Wlk PxA", obj="Wlk PxB")
    assert len(out["chains"]) == 1
    assert out["chains"][0]["intermediary"] == "Wlk PxVia"


@pytest.mark.asyncio
async def test_a_bare_direct_edge_is_not_a_proxy_chain(pg_pool, port):
    async with pg_pool.acquire() as conn:
        a = await _entity(conn, "Wlk DxA")
        b = await _entity(conn, "Wlk DxB")
        await _edge(conn, a, b)

    out = await port.find_proxy_chains(subject="Wlk DxA", obj="Wlk DxB")
    assert out["chains"] == []
