# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W3-A step 2 — the two legacy merge-propagation defects in `proposed_edges`.

DEFECT 1 — the repoint gap. `_compact_merged_edges` has re-pointed `nexuses`
and `facts` onto the merge keeper since it was written and has NEVER touched
`proposed_edges`; there is no other repoint path for that table. 13,222 rows
name a tombstone on the live substrate, 5,844 of them still `pending`. Because
`uq_proposed_edges_triple` keys the queue on NAMES, one real pair sits in the
queue twice and both copies get accrued, qualified and typed — 3,268 rows'
worth of re-spend.

DEFECT 2 — the tombstone hole in the quarantine. `_quarantine_orphan_proposed_
edges` probed `ep.canonical_name = pe.source_entity`, which a merged TOMBSTONE
satisfies, so an endpoint naming a merged-away entity was neither quarantined
nor re-pointed and sat `pending` forever while the sweep re-counted it hourly.

Both halves are asserted here: the CODE path (`_repoint_proposed_edges`, which
stops the class recurring) and the shipped MIGRATION body read from disk (which
clears the historical backlog). They must reach the same three outcomes, or a
row repaired today and a row repaired by tomorrow's sweep disagree.

ON THE MIGRATION'S NAME. It shipped as 0181, was renumbered to 0183, and then
took a `.deferred` suffix to step out of the runner's `*.sql` glob until the
fixpoint rework lands. NEITHER rename updated the constant below, so this file
spent both renames reading a path that no longer existed and failed three tests
with FileNotFoundError in the ORDERED nightly pass — not a shuffle artefact, a
dead reference. Deferring the migration deliberately stops it EXECUTING; it
does not stop this file asserting that its body still agrees with the code
path, which is the one thing that keeps the two from drifting apart while the
rework is outstanding. Hence: read it from disk under whatever name it
currently has, and fail loudly and by name if that name moves again.
(The rework landed as 0185: the fixpoint demotion loop for stayer-occupied
destinations — fix 7, exercised by the tests at the bottom of this file —
plus the transitive name-mapping closure, fix 6b. The stem-based loader
survived its third rename because of exactly this paragraph.)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers.entity_gc import (
    _compact_merged_edges,
    _quarantine_orphan_proposed_edges,
    _repoint_proposed_edges,
)
from legba.data.config import PostgresConfig
from legba.data.migrations import MIGRATIONS_DIR

# Matched by STEM, not by full filename: the number is sequencing metadata and
# the `.deferred` suffix is runner-glob metadata, and this file cares about
# neither — it wants the body. A hardcoded name here has already gone stale
# twice (0181 -> 0183 -> 0183...deferred), each time turning a passing test into
# a FileNotFoundError that reads like a substrate fault.
REPOINT_MIGRATION_STEM = "repoint_proposed_edges_onto_merge_keepers"


def _repoint_migration_path() -> Path:
    """The shipped repoint migration, whatever it is numbered/suffixed today."""
    matches = sorted(
        p for p in Path(MIGRATIONS_DIR).iterdir()
        if REPOINT_MIGRATION_STEM in p.name and p.name.endswith((".sql", ".deferred"))
    )
    assert matches, (
        f"no migration matching {REPOINT_MIGRATION_STEM!r} in {MIGRATIONS_DIR}. "
        "If the repoint migration was deleted rather than renumbered, delete "
        "this file's migration half too — do not leave it reading a dead path."
    )
    assert len(matches) == 1, (
        f"ambiguous repoint migrations: {[p.name for p in matches]}. Exactly one "
        "body may be authoritative or the code path has nothing to agree WITH."
    )
    return matches[0]


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


async def _run_migration(conn: Any) -> None:
    async with conn.transaction():
        await conn.execute(_repoint_migration_path().read_text())


async def _seed(conn: Any, name: str, *, cls: str = "organization") -> str:
    eid = str(uuid4())
    await conn.execute(
        """INSERT INTO entity_profiles
             (id, canonical_name, entity_class, entity_type, data)
           VALUES ($1::uuid, $2, $3, $3, '{}'::jsonb)""",
        eid, name, cls)
    return eid


async def _merge(conn: Any, loser: str, keeper: str) -> None:
    await conn.execute(
        "UPDATE entity_profiles SET merged_into=$2::uuid WHERE id=$1::uuid",
        loser, keeper)


async def _pe(conn: Any, src: str, dst: str, *, status: str = "pending",
              rel: str = "co_occurs", confidence: float = 0.5,
              evidence: str = "", derived: list[Any] | None = None) -> str:
    pid = str(uuid4())
    await conn.execute(
        """INSERT INTO proposed_edges
             (id, source_entity, target_entity, relationship_type, confidence,
              evidence_text, status, derived_from)
           VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8::uuid[])""",
        pid, src, dst, rel, confidence, evidence, status, derived or [])
    return pid


async def _row(conn: Any, pid: str) -> asyncpg.Record:
    return await conn.fetchrow(
        "SELECT * FROM proposed_edges WHERE id=$1::uuid", pid)


# ---------------------------------------------------------------------------
# DEFECT 1 — the repoint gap, code path
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_clean_repoint_rewrites_the_endpoint(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        k_n, l_n, p_n = f"Zzr K {tag}", f"Zzr L {tag}", f"Zzr P {tag}"
        keeper, loser = await _seed(conn, k_n), await _seed(conn, l_n)
        await _seed(conn, p_n)
        await _merge(conn, loser, keeper)

        pid = await _pe(conn, l_n, p_n)
        touched = await _repoint_proposed_edges(conn, l_n, k_n)
        row = await _row(conn, pid)

    assert touched == 1
    assert row["source_entity"] == k_n, "the endpoint must name the keeper"
    assert row["status"] == "pending", "a clean repoint stays in the work-set"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collision_folds_evidence_before_retiring_the_loser(pg_pool):
    """THE RE-SPEND. Both rows are the same real pair; folding must not cost a
    citation, and the survivor must stay workable."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        k_n, l_n, p_n = f"Zzc K {tag}", f"Zzc L {tag}", f"Zzc P {tag}"
        keeper, loser = await _seed(conn, k_n), await _seed(conn, l_n)
        await _seed(conn, p_n)
        await _merge(conn, loser, keeper)

        lineage_a, lineage_b = uuid4(), uuid4()
        survivor = await _pe(conn, k_n, p_n, confidence=0.4,
                             derived=[lineage_a])
        doomed = await _pe(conn, l_n, p_n, confidence=0.9, evidence="carry me",
                           derived=[lineage_b])

        await _repoint_proposed_edges(conn, l_n, k_n)
        s, d = await _row(conn, survivor), await _row(conn, doomed)

    assert d["status"] == "merged", "the duplicate leaves the work-set"
    assert s["status"] == "pending", "the survivor stays workable"
    assert s["confidence"] == pytest.approx(0.9), "confidence is maxed, not lost"
    assert s["evidence_text"] == "carry me", "empty evidence is filled"
    assert set(s["derived_from"]) == {lineage_a, lineage_b}, (
        "the merge must not drop a citation")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_repointed_self_loop_is_rejected(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        k_n, l_n = f"Zzs K {tag}", f"Zzs L {tag}"
        keeper, loser = await _seed(conn, k_n), await _seed(conn, l_n)
        await _merge(conn, loser, keeper)

        pid = await _pe(conn, l_n, k_n)
        await _repoint_proposed_edges(conn, l_n, k_n)
        row = await _row(conn, pid)

    assert row["status"] == "rejected", (
        "an entity is not related to itself — the same verdict the promoter "
        "gives a self-referential candidate")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_compaction_sweep_now_reaches_proposed_edges(pg_pool):
    """The wiring, not just the function: `_compact_merged_edges` is the caller
    that had never invoked this path."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        k_n, l_n, p_n = f"Zzw K {tag}", f"Zzw L {tag}", f"Zzw P {tag}"
        keeper, loser = await _seed(conn, k_n), await _seed(conn, l_n)
        await _seed(conn, p_n)
        await _merge(conn, loser, keeper)
        pid = await _pe(conn, l_n, p_n)

    await _compact_merged_edges(pg_pool)

    async with pg_pool.acquire() as conn:
        row = await _row(conn, pid)
    assert row["source_entity"] == k_n


# ---------------------------------------------------------------------------
# DEFECT 2 — the tombstone hole in the quarantine
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_endpoint_with_no_entity_at_all_is_orphaned(pg_pool):
    """The behaviour that must NOT regress."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        known = f"Zzq Known {tag}"
        await _seed(conn, known)
        pid = await _pe(conn, f"Zzq Ghost {tag}", known)

    await _quarantine_orphan_proposed_edges(pg_pool)

    async with pg_pool.acquire() as conn:
        row = await _row(conn, pid)
    assert row["status"] == "orphaned"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tombstone_with_a_live_keeper_is_NOT_quarantined(pg_pool):
    """The load-bearing half. A candidate naming a merged loser is repairable —
    the repoint path preserves it. Quarantining it would destroy a promotable
    edge to fix a bookkeeping error."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        k_n, l_n, p_n = f"Zzt K {tag}", f"Zzt L {tag}", f"Zzt P {tag}"
        keeper, loser = await _seed(conn, k_n), await _seed(conn, l_n)
        await _seed(conn, p_n)
        await _merge(conn, loser, keeper)
        pid = await _pe(conn, l_n, p_n)

    await _quarantine_orphan_proposed_edges(pg_pool)

    async with pg_pool.acquire() as conn:
        row = await _row(conn, pid)
    assert row["status"] == "pending", (
        "still repairable — it must survive to be re-pointed")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tombstone_whose_chain_dead_ends_IS_quarantined(pg_pool):
    """The hole itself. A degenerate chain reaching no live entity used to
    satisfy the bare name probe and sit `pending` forever."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        a_n, b_n, p_n = f"Zzd A {tag}", f"Zzd B {tag}", f"Zzd P {tag}"
        a, b = await _seed(conn, a_n), await _seed(conn, b_n)
        await _seed(conn, p_n)
        # a -> b -> a: cycle-safe resolve_entity terminates, but no live
        # survivor is reachable from either name.
        await _merge(conn, a, b)
        await _merge(conn, b, a)
        pid = await _pe(conn, a_n, p_n)

    await _quarantine_orphan_proposed_edges(pg_pool)

    async with pg_pool.acquire() as conn:
        row = await _row(conn, pid)
    assert row["status"] == "orphaned"


# ---------------------------------------------------------------------------
# The shipped migration body — same three outcomes as the code path
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_0181_reaches_the_same_three_outcomes(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        k_n, l_n, p_n = f"Zzm K {tag}", f"Zzm L {tag}", f"Zzm P {tag}"
        keeper, loser = await _seed(conn, k_n), await _seed(conn, l_n)
        await _seed(conn, p_n)
        await _merge(conn, loser, keeper)

        lineage = uuid4()
        clean = await _pe(conn, l_n, p_n, rel="allied_with")
        survivor = await _pe(conn, k_n, p_n, rel="co_occurs", confidence=0.2)
        doomed = await _pe(conn, l_n, p_n, rel="co_occurs", confidence=0.8,
                           evidence="fold me", derived=[lineage])
        selfloop = await _pe(conn, l_n, k_n, rel="co_occurs")

        await _run_migration(conn)

        rows = {k: await _row(conn, v) for k, v in
                (("clean", clean), ("survivor", survivor),
                 ("doomed", doomed), ("selfloop", selfloop))}

    assert rows["clean"]["source_entity"] == k_n
    assert rows["clean"]["status"] == "pending"
    assert rows["selfloop"]["status"] == "rejected"
    assert rows["doomed"]["status"] == "merged"
    assert rows["survivor"]["confidence"] == pytest.approx(0.8)
    assert rows["survivor"]["evidence_text"] == "fold me"
    assert lineage in set(rows["survivor"]["derived_from"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_0181_is_idempotent(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        k_n, l_n, p_n = f"Zzi K {tag}", f"Zzi L {tag}", f"Zzi P {tag}"
        keeper, loser = await _seed(conn, k_n), await _seed(conn, l_n)
        await _seed(conn, p_n)
        await _merge(conn, loser, keeper)
        pid = await _pe(conn, l_n, p_n)

        await _run_migration(conn)
        first = await _row(conn, pid)
        await _run_migration(conn)
        second = await _row(conn, pid)

    assert first["source_entity"] == second["source_entity"] == k_n
    assert first["status"] == second["status"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_0181_deletes_nothing(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        k_n, l_n, p_n = f"Zzn K {tag}", f"Zzn L {tag}", f"Zzn P {tag}"
        keeper, loser = await _seed(conn, k_n), await _seed(conn, l_n)
        await _seed(conn, p_n)
        await _merge(conn, loser, keeper)
        await _pe(conn, l_n, p_n, rel="co_occurs")
        await _pe(conn, k_n, p_n, rel="co_occurs")
        await _pe(conn, l_n, k_n, rel="co_occurs")

        before = await conn.fetchval("SELECT count(*) FROM proposed_edges")
        await _run_migration(conn)
        after = await conn.fetchval("SELECT count(*) FROM proposed_edges")

    assert before == after, "every row survives with a status that says why"


# ---------------------------------------------------------------------------
# Fix 7 — the seventh collision shape: occupancy by an unrewritten STAYER.
#
# A rejected self-loop (and a folded loser) keeps its old triple forever, but
# the election only ever sees a plan row at its DESTINATION — so a mover bound
# for a stayer's current triple used to be elected keeper of an "empty" group
# and then die on uq_proposed_edges_triple. This is the shape that deferred
# 0183. The fixpoint demotes such movers onto the occupant instead, iterating
# because each demotion creates a new stayer.
#
# The shape exists only through NAMESAKES: a mover's destination is made of
# living keeper surfaces, so the occupied triple's surface must be carried by
# a living keeper too — under a DIFFERENT case than the mapped tombstone name,
# or the closure (fix 6b) would have resolved it. The seeds below build
# exactly that: a tombstone named "Zz7n ..." whose cross-class living namesake
# is cased "ZZ7N ...".
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_mover_landing_on_a_stayer_occupied_triple_is_demoted(pg_pool):
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        k_n = f"Zz7k {tag}"          # living keeper, both movers point here
        l1_n = f"Zz7n {tag}"         # tombstone; chain ends at k_n
        k2_n = f"ZZ7N {tag}"         # living CROSS-CLASS NAMESAKE of l1_n
        l3_n = f"Zz7m {tag}"         # tombstone; chain ends at k2_n
        keeper = await _seed(conn, k_n)
        l1 = await _seed(conn, l1_n)
        k2 = await _seed(conn, k2_n, cls="location")
        l3 = await _seed(conn, l3_n)
        await _merge(conn, l1, keeper)
        await _merge(conn, l3, k2)

        lineage = uuid4()
        # The STAYER: resolves to (k_n, k_n), a self-loop -> rejected in
        # place, holding (zz7n, zz7k) forever.
        stayer = await _pe(conn, l1_n, k_n)
        # The MOVER: source resolves to k2's surface "ZZ7N ...", so its
        # destination lower-triple IS the stayer's current triple.
        mover = await _pe(conn, l3_n, k_n, confidence=0.9,
                          evidence="the living reading", derived=[lineage])

        await _run_migration(conn)
        s, m = await _row(conn, stayer), await _row(conn, mover)

    assert m["status"] == "merged", (
        "the mover must be DEMOTED, not landed — its destination is occupied "
        "by a stayer that will never vacate")
    assert m["source_entity"] == l3_n, "a demoted mover keeps its old triple"
    assert s["confidence"] == pytest.approx(0.9), "demotion folds, fold maxes"
    assert s["evidence_text"] == "the living reading", "empty evidence filled"
    assert lineage in set(s["derived_from"]), "demotion must not cost a citation"
    assert s["status"] == "pending", (
        "pending wins on the demotion fold too: the occupied surface is one a "
        "living keeper carries, and the demoted pending mover is the candidate "
        "for the LIVING reading of that name — the triple goes back in play")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_demotion_cascades_iterate_to_closure(pg_pool):
    """A demoted mover becomes a stayer at ITS old triple — which can be a
    further mover's destination. One pass cannot see that in advance (whether
    a row stays depends on the election; who collides depends on who stays),
    which is WHY the loop exists. Evidence propagates one hop per fold,
    exactly like consecutive runs of the code-half sweep."""
    async with pg_pool.acquire() as conn:
        tag = uuid4().hex[:8]
        k_n = f"Zz8k {tag}"
        l1_n, k2_n = f"Zz8n {tag}", f"ZZ8N {tag}"   # namesake pair, as above
        l3_n, k3_n = f"Zz8m {tag}", f"ZZ8M {tag}"   # second namesake pair
        l4_n = f"Zz8p {tag}"
        keeper = await _seed(conn, k_n)
        l1 = await _seed(conn, l1_n)
        k2 = await _seed(conn, k2_n, cls="location")
        l3 = await _seed(conn, l3_n)
        k3 = await _seed(conn, k3_n, cls="location")
        l4 = await _seed(conn, l4_n)
        await _merge(conn, l1, keeper)
        await _merge(conn, l3, k2)
        await _merge(conn, l4, k3)

        lin1, lin2 = uuid4(), uuid4()
        stayer = await _pe(conn, l1_n, k_n)                       # self-loop
        m1 = await _pe(conn, l3_n, k_n, derived=[lin1])           # -> stayer's triple
        m2 = await _pe(conn, l4_n, k_n, derived=[lin2])           # -> m1's triple

        await _run_migration(conn)
        s = await _row(conn, stayer)
        r1, r2 = await _row(conn, m1), await _row(conn, m2)

    assert r1["status"] == "merged", "pass 1 demotes m1 onto the stayer"
    assert r2["status"] == "merged", (
        "pass 2 must demote m2 onto the now-staying m1 — a single pass cannot "
        "know m1 stays before demoting it")
    assert r1["source_entity"] == l3_n and r2["source_entity"] == l4_n, (
        "demoted movers keep their old triples")
    assert lin1 in set(s["derived_from"]), "m1's evidence reaches the stayer"
    assert lin2 in set(r1["derived_from"]), (
        "m2's evidence reaches m1 — one hop per fold, like consecutive sweep "
        "runs; it rests on the retained audit row, not on the stayer")
