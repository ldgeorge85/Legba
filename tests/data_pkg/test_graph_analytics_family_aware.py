# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W3-A cutover 3 — `graph_mining` + `structural_balance` became family-aware.

Both handlers read `entity_edges` now, but the load-bearing change is the
FAMILY filter, not the table. Neither had any provenance predicate before —
`structural_balance` did not even SELECT `source_type` — so neither could tell
an imported lattice edge from a derived one.

THE DEFECT, from 0143's header: 86% of the open SIGNED edge set is imported
Wikidata country->IGO membership at polarity +1. Three such +1 legs form a
"balanced" triad, so `balance_ratio` was overwhelmingly a statement about which
countries co-belong to the UN, Interpol and the OPCW — not about world-state
alignment.

The two handlers get DIFFERENT defaults on purpose, and that asymmetry is the
thing to protect:

  * `structural_balance` counts `relation` + `structural` only. A `reference`
    edge is TRUE and STATIC; it is not evidence that two actors are aligned.
  * `graph_mining` ALSO walks `reference`, because for BROKERAGE an IGO
    membership is a genuine structural conduit — "who sits between these two
    blocs" legitimately runs through the UN. Same table, different question.

Neither walks `cooccurrence`.
"""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers import (
    graph_mining,
    structural_balance,
)
from legba.data.analysts.deterministic_handlers.graph_mining import (
    MINING_FAMILIES,
)
from legba.data.analysts.deterministic_handlers.structural_balance import (
    BALANCE_FAMILIES,
)
from legba.data.analysts.handler_options import resolve_handler_options
from legba.data.config import PostgresConfig


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


class _PoolDeps:
    def __init__(self, pool):
        self.pg_pool = pool


async def _entity(conn, name: str):
    return await conn.fetchval(
        """INSERT INTO entity_profiles (canonical_name, entity_class,
             entity_type, data) VALUES ($1, 'country', 'country', '{}'::jsonb)
           RETURNING id""", name)


async def _edge(conn, src, dst, *, rel, polarity, family):
    await conn.execute(
        """INSERT INTO entity_edges
             (src_id, dst_id, edge_type, edge_family, polarity, confidence,
              valid_from)
           VALUES ($1, $2, $3, $4, $5, 0.9, now())""",
        src, dst, rel, family, polarity)


async def _igo_triangle(conn, tag: str, family: str):
    """Three mutually +1 members of one IGO — a 'balanced' triad if counted."""
    a = await _entity(conn, f"Fam {tag} A")
    b = await _entity(conn, f"Fam {tag} B")
    c = await _entity(conn, f"Fam {tag} C")
    for x, y in ((a, b), (b, c), (a, c)):
        await _edge(conn, x, y, rel="member of", polarity=1, family=family)
    return a, b, c


# ---------------------------------------------------------------------------
# The defaults themselves
# ---------------------------------------------------------------------------

def test_the_two_defaults_differ_and_neither_walks_cooccurrence():
    assert "cooccurrence" not in BALANCE_FAMILIES
    assert "cooccurrence" not in MINING_FAMILIES
    assert "reference" not in BALANCE_FAMILIES, (
        "the imported lattice is not evidence of alignment")
    assert "reference" in MINING_FAMILIES, (
        "for brokerage an IGO membership IS a conduit")


@pytest.mark.parametrize("handler", ["graph_mining", "structural_balance"])
def test_edge_families_is_a_declared_knob_bound_to_the_vocabulary(handler):
    ok = resolve_handler_options(handler, {"edge_families": ["relation"]})
    assert ok.accepted["edge_families"] == ["relation"]
    assert not ok.rejected

    bad = resolve_handler_options(handler, {"edge_families": ["not_a_family"]})
    assert bad.rejected, (
        "a family outside 0143's CHECK vocabulary must be rejected at "
        "validation, not produce a silently empty graph at runtime")


# ---------------------------------------------------------------------------
# structural_balance — the IGO lattice must not move the ratio
# ---------------------------------------------------------------------------

async def _balance(pool, **options) -> dict:
    result = await structural_balance.handle(
        inputs=[], options={"augment_from_age": False, **options},
        deps=_PoolDeps(pool))
    return result.finding.data


@pytest.mark.integration
@pytest.mark.asyncio
async def test_imported_lattice_does_not_count_toward_balance(pg_pool):
    """THE defect. A reference-tier IGO triangle is three +1 legs — a textbook
    'balanced' triad — and it must contribute nothing.

    Asserted as a DELTA: the migrated test DB is session-scoped and other
    suites seed signed relations into it, so the absolute counts are not this
    test's to own — the change the new rows make is.
    """
    before = await _balance(pg_pool)
    async with pg_pool.acquire() as conn:
        await _igo_triangle(conn, uuid4().hex[:8], "reference")
    after = await _balance(pg_pool)

    assert after["edge_count"] == before["edge_count"]
    assert after["balanced_count"] == before["balanced_count"]
    assert after["unbalanced_count"] == before["unbalanced_count"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_derived_relations_do_count_toward_balance(pg_pool):
    """The other half — excluding the lattice must not exclude everything."""
    before = await _balance(pg_pool)
    async with pg_pool.acquire() as conn:
        await _igo_triangle(conn, uuid4().hex[:8], "relation")
    after = await _balance(pg_pool)

    assert after["edge_count"] == before["edge_count"] + 3
    assert (after["balanced_count"] + after["unbalanced_count"]
            > before["balanced_count"] + before["unbalanced_count"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_scope_travels_with_the_metric(pg_pool):
    """A balance_ratio is meaningless without the families it counted — a
    consumer comparing two runs across a scope change would otherwise read a
    definition change as a world change."""
    async with pg_pool.acquire() as conn:
        await _igo_triangle(conn, uuid4().hex[:8], "relation")
        row = await conn.fetchrow(
            "SELECT payload FROM graph_metrics "
            " WHERE metric_kind='structural_balance'"
            " ORDER BY computed_at DESC LIMIT 1")

    await structural_balance.handle(
        inputs=[], options={"augment_from_age": False}, deps=_PoolDeps(pg_pool))

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT payload FROM graph_metrics "
            " WHERE metric_kind='structural_balance'"
            " ORDER BY computed_at DESC LIMIT 1")
    import json
    payload = row["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else payload
    assert payload["edge_families"] == list(BALANCE_FAMILIES)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_operator_can_widen_the_scope_deliberately(pg_pool):
    before = await _balance(pg_pool, edge_families=["relation", "reference"])
    async with pg_pool.acquire() as conn:
        await _igo_triangle(conn, uuid4().hex[:8], "reference")
    after = await _balance(pg_pool, edge_families=["relation", "reference"])

    assert after["edge_count"] == before["edge_count"] + 3, (
        "widening is possible — it just has to be asked for")


# ---------------------------------------------------------------------------
# graph_mining
# ---------------------------------------------------------------------------

async def _mining_node_count(pool) -> int:
    result = await graph_mining.handle(
        inputs=[], options={"augment_from_age": False}, deps=_PoolDeps(pool))
    return int(result.finding.data["node_count"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mining_ignores_the_cooccurrence_cloud(pg_pool):
    # The migrated test DB is session-scoped and other suites seed into it, so
    # this asserts the DELTA the new rows make — the only thing under test.
    before = await _mining_node_count(pg_pool)
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a = await _entity(conn, f"Mine {tag} A")
        b = await _entity(conn, f"Mine {tag} B")
        c = await _entity(conn, f"Mine {tag} C")
        for x, y in ((a, b), (b, c)):
            await _edge(conn, x, y, rel="co occurs with", polarity=0,
                        family="cooccurrence")

    assert await _mining_node_count(pg_pool) == before, (
        "two nouns in one document is not a tie, so it adds no nodes")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mining_does_walk_the_reference_lattice(pg_pool):
    """The asymmetry with structural_balance, asserted directly."""
    before = await _mining_node_count(pg_pool)
    async with pg_pool.acquire() as conn:
        await _igo_triangle(conn, uuid4().hex[:8], "reference")

    assert await _mining_node_count(pg_pool) == before + 3


# ---------------------------------------------------------------------------
# The neutral-sign conflation in the proxy-chain sign product
# ---------------------------------------------------------------------------

def test_a_neutral_edge_no_longer_signs_a_proxy_chain_positive():
    """`sign = 1 if polarity >= 0` collapsed NEUTRAL onto POSITIVE, so a chain
    hopping via an edge that asserts no alignment carried a confident sign it
    had not earned. Zero propagates now — the same treatment
    structural_balance gives a triad with a neutral leg."""
    import networkx as nx

    g = nx.MultiDiGraph()
    g.add_edge("N_A", "N_M", label="located in", polarity=0, confidence=1.0)
    g.add_edge("N_M", "N_B", label="hostile to", polarity=-1, confidence=1.0)

    chains, _ = graph_mining._proxy_chains(g)
    assert chains, "the chain is still found — only its SIGN changes"
    assert all(c["polarity_sign"] == 0 for c in chains), (
        "a chain through a neutral tie has an undefined sign, not a positive "
        "or negative one")
