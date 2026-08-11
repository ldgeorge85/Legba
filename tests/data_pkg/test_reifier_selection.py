# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-G2 — the reifier's candidate window: pending-only + merge-aware dedup.

Built from the LIVE shape the bake-off measured (``docs/TYPING_BAKEOFF_
2026-08-03.md`` §1), not from an invented one:

  * ``proposed_edges`` holds four statuses. Only ``pending`` can become a new
    edge; ``promoted``/``rejected``/``orphaned`` are dead rows that reach
    confidence 1.000 while ``pending`` tops out at 0.750 — so under the old
    ``ORDER BY confidence DESC`` they sorted above every live candidate.
  * A promoted pair's nexus carries the KEEPER-rewritten endpoints, not the raw
    ``proposed_edges`` surfaces. Live: ``Iran → US`` (promoted) matched **0**
    open nexuses on the raw surfaces; its keeper form ``Iran → United States``
    has two. So the old raw-surface guard let promoted pairs stay eligible
    forever.

The two headline assertions are exactly those: a promoted row must NEVER
re-enter the window; a pending row must.
"""

from __future__ import annotations

import json
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.edge_qualification import (
    MIN_INDEPENDENT_SOURCES,
    RECOMMENDED_BAR,
)
from legba.data.analysts.reifier_selection import (
    CANDIDATE_FETCH_SQL,
    MIN_EDGE_CONFIDENCE,
    PENDING_STATUS,
    QUALIFICATION_SCAN_SQL,
    SelectionCounters,
    already_reified,
    resolve_pair,
    select_candidates,
)
from legba.data.config import PostgresConfig

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


async def _seed_signals(conn, *, n: int, tag: str, title: str = "") -> list:
    """``n`` backing signals, each from a DISTINCT publisher.

    ``source_id`` is ``source.<publisher>.<feed>`` and the qualification score
    folds families on the publisher segment, so distinct publishers means
    distinct families. ``content_hash`` is unique per row so the syndication
    collapse (which dedups on it) does not fold them into one unit of support.
    """
    ids = []
    for i in range(n):
        sid = uuid4()
        await conn.execute(
            """
            INSERT INTO signals (id, source_id, payload, content_hash, fetched_at)
            VALUES ($1, $2, $3::jsonb, $4, now())
            """,
            sid,
            f"source.pub{tag}{i}.feed",
            json.dumps({"title": title or f"story {tag} {i}", "summary": ""}),
            f"hash-{sid}",
        )
        ids.append(sid)
    return ids


async def _seed_edge(
    conn, *, src: str, tgt: str, status: str, conf: float, sources: int = 3,
    title: str = "",
):
    """A candidate with REAL evidence behind it.

    Three independent publishers is the cheapest way over the recommended bar:
    ``multi_source`` scores (3-1)/3 = 0.667 at weight 0.45 and
    ``source_diversity`` scores 1.0 at weight 0.20, for 0.50 — clear of 0.42
    without needing corpus salience or a desk hit. A candidate with NO signal
    lineage scores 0.0 and is correctly invisible to selection, which is why
    every fixture here carries evidence.
    """
    tag = uuid4().hex[:6]
    sig_ids = await _seed_signals(conn, n=sources, tag=tag, title=title)
    await conn.execute(
        """
        INSERT INTO proposed_edges
            (source_entity, target_entity, relationship_type, confidence,
             evidence_text, status, derived_from)
        VALUES ($1, $2, 'co_occurs', $3, $4, $5, $6::uuid[])
        """,
        src, tgt, conf, f"{src} and {tgt} appeared together", status, sig_ids,
    )


async def _seed_nexus(conn, *, subject: str, object_: str, rel: str = "HostileTo"):
    await conn.execute(
        """
        INSERT INTO nexuses (subject, object, rel_type, label, polarity,
                             intent, channel, confidence, valid_from)
        VALUES ($1, $2, $3, $4, -1, 'hostile', 'direct', 0.7, now())
        """,
        subject, object_, rel, f"{subject} {rel} {object_}",
    )


async def _seed_keeper(conn, *, canonical: str, aliases: list[str], cls="country"):
    """An ``entity_profiles`` keeper that claims ``aliases`` as merged surfaces —
    the mechanism by which ``US`` resolves to ``United States`` live."""
    await conn.execute(
        """
        INSERT INTO entity_profiles (canonical_name, entity_class, data)
        VALUES ($1, $2, $3::jsonb)
        """,
        canonical, cls, json.dumps({"merged_aliases": aliases}),
    )


# ---------------------------------------------------------------------------
# (a) the status filter — the dead-row flood
# ---------------------------------------------------------------------------


async def test_promoted_row_never_re_enters_the_window(pg_pool):
    """THE regression. A promoted pair at confidence 1.000 outranks every
    pending row under the old ordering; it must not appear at all."""
    tag = uuid4().hex[:8]
    dead = f"DeadSubj{tag}"
    live = f"LiveSubj{tag}"
    obj = f"Obj{tag}"
    async with pg_pool.acquire() as conn:
        # the exact live shape: promoted sits at 1.000, pending below 0.750
        await _seed_edge(conn, src=dead, tgt=obj, status="promoted", conf=1.0)
        await _seed_edge(conn, src=live, tgt=obj, status="pending", conf=0.70)
        rows, counters = await select_candidates(conn, limit=500)

    names = {(r["source_entity"], r["target_entity"]) for r in rows}
    assert (dead, obj) not in names, "a promoted row re-entered the typing window"
    assert (live, obj) in names, "the pending row must be typed"
    assert isinstance(counters, SelectionCounters)


@pytest.mark.parametrize("status", ["promoted", "rejected", "orphaned"])
async def test_every_non_pending_status_is_excluded(pg_pool, status):
    """All three dead buckets, each at the confidence ceiling only they reach."""
    tag = uuid4().hex[:8]
    src, tgt = f"S{status}{tag}", f"T{status}{tag}"
    async with pg_pool.acquire() as conn:
        await _seed_edge(conn, src=src, tgt=tgt, status=status, conf=1.0)
        rows, _ = await select_candidates(conn, limit=500)
    assert (src, tgt) not in {
        (r["source_entity"], r["target_entity"]) for r in rows
    }, f"a {status!r} row entered the window"


async def test_the_scan_sql_binds_status_and_is_read_only():
    """The defect was a MISSING predicate — pin its presence in the SQL text so
    a future edit cannot quietly drop it again."""
    assert "pe.status = $1" in QUALIFICATION_SCAN_SQL
    assert "pe.status = $2" in CANDIDATE_FETCH_SQL
    assert PENDING_STATUS == "pending"
    for sql in (QUALIFICATION_SCAN_SQL, CANDIDATE_FETCH_SQL):
        lowered = sql.lower()
        for verb in ("insert ", "update ", "delete ", "drop ", "truncate "):
            assert verb not in lowered


async def test_the_window_is_ordered_by_qualification_not_confidence():
    """§2.1 — ``proposed_edges.confidence`` is accumulated co-mention weight and
    cannot tell nine newsrooms from one wire story on nine outlets. The scan must
    rank on the qualification score."""
    assert "ORDER BY qual_score DESC" in QUALIFICATION_SCAN_SQL
    assert "ORDER BY pe.confidence" not in QUALIFICATION_SCAN_SQL


async def test_confidence_floor_still_applies(pg_pool):
    tag = uuid4().hex[:8]
    src, tgt = f"Thin{tag}", f"Pair{tag}"
    async with pg_pool.acquire() as conn:
        await _seed_edge(
            conn, src=src, tgt=tgt, status="pending",
            conf=MIN_EDGE_CONFIDENCE - 0.10,
        )
        rows, _ = await select_candidates(conn, limit=500)
    assert (src, tgt) not in {
        (r["source_entity"], r["target_entity"]) for r in rows
    }


# ---------------------------------------------------------------------------
# (b) the merge-aware dedup guard
# ---------------------------------------------------------------------------


async def test_dedup_compares_through_the_keeper_not_the_raw_surface(pg_pool):
    """The live ``Iran → US`` case, reproduced.

    The pending edge names ``US``; the open nexus names ``United States``. The
    raw-surface guard sees no match and keeps typing the pair forever. The
    keeper-aware guard resolves ``US`` onto its keeper and excludes it.
    """
    tag = uuid4().hex[:8]
    subj = f"Iran{tag}"
    keeper = f"United States{tag}"
    alias = f"US{tag}"
    async with pg_pool.acquire() as conn:
        await _seed_keeper(conn, canonical=keeper, aliases=[alias])
        await _seed_keeper(conn, canonical=subj, aliases=[])
        # the edge names the ALIAS; the nexus names the KEEPER
        await _seed_edge(conn, src=subj, tgt=alias, status="pending", conf=0.70)
        await _seed_nexus(conn, subject=subj, object_=keeper)

        # the raw surfaces genuinely do NOT match — this is the defect's premise
        raw_match = await conn.fetchval(
            "SELECT count(*) FROM nexuses n WHERE n.valid_until IS NULL "
            "AND n.superseded_by IS NULL AND lower(n.subject)=lower($1) "
            "AND lower(n.object)=lower($2)",
            subj, alias,
        )
        assert raw_match == 0, "premise: the raw surfaces must not match"

        # ... but the keeper-resolved pair does
        pair = await resolve_pair(conn, subj, alias)
        assert pair is not None
        assert pair[1] == keeper, "the alias must resolve onto its keeper"

        rows, counters = await select_candidates(conn, limit=500)

    assert (subj, alias) not in {
        (r["source_entity"], r["target_entity"]) for r in rows
    }, "an already-reified pair survived the keeper-aware guard"
    assert counters.already_reified >= 1


async def test_dedup_is_bidirectional(pg_pool):
    """A ``co_occurs`` pair is UNORDERED — both A→B and B→A can exist as rows.
    An open nexus in either direction retires the candidate, so one co-mention
    can never mint two nexuses."""
    tag = uuid4().hex[:8]
    a, b = f"Alpha{tag}", f"Beta{tag}"
    async with pg_pool.acquire() as conn:
        await _seed_nexus(conn, subject=a, object_=b)
        await _seed_edge(conn, src=b, tgt=a, status="pending", conf=0.70)
        rows, counters = await select_candidates(conn, limit=500)
    assert (b, a) not in {
        (r["source_entity"], r["target_entity"]) for r in rows
    }, "the reverse orientation was typed a second time"
    assert counters.already_reified >= 1


async def test_a_pending_pair_with_no_nexus_survives(pg_pool):
    """The guard must not be a blanket suppressor — the whole point is that live
    work reaches the typer."""
    tag = uuid4().hex[:8]
    a, b = f"Gamma{tag}", f"Delta{tag}"
    async with pg_pool.acquire() as conn:
        await _seed_edge(conn, src=a, tgt=b, status="pending", conf=0.70)
        rows, counters = await select_candidates(conn, limit=500)
    assert (a, b) in {(r["source_entity"], r["target_entity"]) for r in rows}
    assert counters.selected >= 1


async def test_superseded_nexus_does_not_retire_a_candidate(pg_pool):
    """Only an OPEN nexus counts. A closed one means the pair needs re-typing."""
    tag = uuid4().hex[:8]
    a, b = f"Eps{tag}", f"Zeta{tag}"
    async with pg_pool.acquire() as conn:
        await _seed_nexus(conn, subject=a, object_=b)
        await conn.execute(
            "UPDATE nexuses SET valid_until = now() "
            "WHERE lower(subject)=lower($1) AND lower(object)=lower($2)",
            a, b,
        )
        await _seed_edge(conn, src=a, tgt=b, status="pending", conf=0.70)
        rows, _ = await select_candidates(conn, limit=500)
    assert (a, b) in {(r["source_entity"], r["target_entity"]) for r in rows}


async def test_already_reified_probe_returns_asked_orientation(pg_pool):
    tag = uuid4().hex[:8]
    a, b = f"eta{tag}".lower(), f"theta{tag}".lower()
    async with pg_pool.acquire() as conn:
        await _seed_nexus(conn, subject=a, object_=b)
        hit = await already_reified(conn, [(b, a), (a, "nothing" + tag)])
    assert (b, a) in hit, "the probe must answer in the orientation asked"
    assert (a, "nothing" + tag) not in hit


async def test_already_reified_is_empty_for_no_pairs(pg_pool):
    async with pg_pool.acquire() as conn:
        assert await already_reified(conn, []) == set()


# ---------------------------------------------------------------------------
# endpoint hygiene at selection (cap slots are spent on usable pairs)
# ---------------------------------------------------------------------------


async def test_junk_and_self_loop_endpoints_are_dropped_before_the_cap(pg_pool):
    tag = uuid4().hex[:8]
    async with pg_pool.acquire() as conn:
        await _seed_edge(conn, src="Iran", tgt="Iranian", status="pending", conf=0.70)
        rows, counters = await select_candidates(conn, limit=500)
    assert ("Iran", "Iranian") not in {
        (r["source_entity"], r["target_entity"]) for r in rows
    }, "a demonym self-loop must never spend a typing slot"
    assert counters.skipped_endpoints >= 1


async def test_window_carries_the_resolved_endpoints(pg_pool):
    """Selection already paid for the keeper election; the window hands the
    result on rather than making the caller resolve twice."""
    tag = uuid4().hex[:8]
    a, b = f"Iota{tag}", f"Kappa{tag}"
    async with pg_pool.acquire() as conn:
        await _seed_edge(conn, src=a, tgt=b, status="pending", conf=0.70)
        rows, _ = await select_candidates(conn, limit=500)
    row = next(r for r in rows if r["source_entity"] == a)
    assert row["keeper_source"] and row["keeper_target"]


async def test_cap_is_applied_after_the_guards(pg_pool):
    """``eligible`` counts every survivor; ``selected`` is what the cap allowed.
    A run that is starved of live work must be distinguishable from one that hit
    its cap — that distinction is what the K-G2 defect hid."""
    tag = uuid4().hex[:8]
    async with pg_pool.acquire() as conn:
        for i in range(5):
            await _seed_edge(
                conn, src=f"Cap{tag}S{i}", tgt=f"Cap{tag}T{i}",
                status="pending", conf=0.70,
            )
        rows, counters = await select_candidates(conn, limit=2)
    assert len(rows) == 2
    assert counters.selected == 2
    assert counters.eligible >= 5
    assert counters.examined >= counters.eligible


async def test_counters_serialise_flat_for_the_run_receipt(pg_pool):
    async with pg_pool.acquire() as conn:
        _rows, counters = await select_candidates(conn, limit=1)
    d = counters.as_dict()
    assert set(d) == {
        "examined", "qualified", "skipped_endpoints", "already_reified",
        "keeper_self_loop", "eligible", "selected",
    }
    assert all(isinstance(v, int) for v in d.values())


# ---------------------------------------------------------------------------
# the qualification bar
# ---------------------------------------------------------------------------


async def test_single_sourced_candidate_never_enters_the_window(pg_pool):
    """92.1% of the live pending pool rests on ONE independent source. The hard
    floor removes exactly that, and it is the sludge the graph exists to
    exclude."""
    tag = uuid4().hex[:8]
    a, b = f"Thin{tag}", f"Sourced{tag}"
    async with pg_pool.acquire() as conn:
        await _seed_edge(conn, src=a, tgt=b, status="pending", conf=0.70, sources=1)
        rows, _ = await select_candidates(conn, limit=500)
    assert (a, b) not in {(r["source_entity"], r["target_entity"]) for r in rows}


async def test_candidate_with_no_signal_lineage_never_enters_the_window(pg_pool):
    tag = uuid4().hex[:8]
    a, b = f"Bare{tag}", f"Edge{tag}"
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO proposed_edges (source_entity, target_entity, "
            "relationship_type, confidence, evidence_text, status) "
            "VALUES ($1,$2,'co_occurs',0.75,'x','pending')",
            a, b,
        )
        rows, _ = await select_candidates(conn, limit=500)
    assert (a, b) not in {(r["source_entity"], r["target_entity"]) for r in rows}


async def test_syndication_cannot_buy_a_way_past_the_floor(pg_pool):
    """One story on nine outlets is ONE unit of support. The evidence CTE
    collapses on content_hash before counting sources, so nine syndicated rows
    must not clear a floor of two."""
    tag = uuid4().hex[:8]
    a, b = f"Wire{tag}", f"Story{tag}"
    async with pg_pool.acquire() as conn:
        shared = f"syndicated-{tag}"
        ids = []
        for i in range(9):
            sid = uuid4()
            await conn.execute(
                "INSERT INTO signals (id, source_id, payload, content_hash, "
                "fetched_at) VALUES ($1,$2,'{}'::jsonb,$3, now())",
                sid, f"source.outlet{tag}{i}.feed", shared,
            )
            ids.append(sid)
        await conn.execute(
            "INSERT INTO proposed_edges (source_entity, target_entity, "
            "relationship_type, confidence, evidence_text, status, derived_from) "
            "VALUES ($1,$2,'co_occurs',0.75,'x','pending',$3::uuid[])",
            a, b, ids,
        )
        rows, _ = await select_candidates(conn, limit=500)
    assert (a, b) not in {
        (r["source_entity"], r["target_entity"]) for r in rows
    }, "syndication inflated support past the independent-source floor"


async def test_qualifying_candidate_carries_its_score(pg_pool):
    tag = uuid4().hex[:8]
    a, b = f"Good{tag}", f"Pair{tag}"
    async with pg_pool.acquire() as conn:
        await _seed_edge(conn, src=a, tgt=b, status="pending", conf=0.70, sources=3)
        rows, counters = await select_candidates(conn, limit=500)
    row = next(r for r in rows if r["source_entity"] == a)
    assert row["qual_score"] >= RECOMMENDED_BAR
    assert counters.qualified >= 1


async def test_the_window_is_ranked_best_first(pg_pool):
    """A 4-source pair must outrank a 2-source one, whatever their raw
    ``confidence`` says — that inversion IS the point of the change."""
    tag = uuid4().hex[:8]
    weak_a, weak_b = f"WeakA{tag}", f"WeakB{tag}"
    strong_a, strong_b = f"StrongA{tag}", f"StrongB{tag}"
    async with pg_pool.acquire() as conn:
        # the WEAK pair carries the HIGHER raw confidence
        await _seed_edge(
            conn, src=weak_a, tgt=weak_b, status="pending", conf=0.75, sources=2
        )
        await _seed_edge(
            conn, src=strong_a, tgt=strong_b, status="pending", conf=0.50, sources=4
        )
        rows, _ = await select_candidates(conn, limit=500)
    order = [r["source_entity"] for r in rows]
    assert strong_a in order
    if weak_a in order:
        assert order.index(strong_a) < order.index(weak_a), (
            "the better-evidenced pair must rank above the higher-confidence one"
        )


async def test_lowering_the_bar_cannot_re_admit_single_sourced_sludge(pg_pool):
    """At bar >= 0.40 the weighted score alone already excludes single-sourced
    candidates, so the explicit floor does no work. It exists precisely so that
    widening the queue later cannot silently undo that."""
    tag = uuid4().hex[:8]
    a, b = f"Sludge{tag}", f"Pair{tag}"
    async with pg_pool.acquire() as conn:
        await _seed_edge(conn, src=a, tgt=b, status="pending", conf=0.75, sources=1)
        rows, _ = await select_candidates(conn, limit=500, bar=0.0)
    assert (a, b) not in {
        (r["source_entity"], r["target_entity"]) for r in rows
    }, "a bar of 0.0 re-admitted a single-sourced candidate"
    assert MIN_INDEPENDENT_SOURCES == 2


async def test_below_bar_multi_source_candidate_is_held_back(pg_pool):
    """Two independent sources, no salience, no desk: 0.35 — short of 0.42. It
    stays pending (it may earn more support), it just does not get a GPU call."""
    tag = uuid4().hex[:8]
    a, b = f"Two{tag}", f"Source{tag}"
    async with pg_pool.acquire() as conn:
        await _seed_edge(conn, src=a, tgt=b, status="pending", conf=0.75, sources=2)
        rows, _ = await select_candidates(conn, limit=500)
        # ... and a lower bar lets exactly that candidate through
        wide, _ = await select_candidates(conn, limit=500, bar=0.30)
    names = {(r["source_entity"], r["target_entity"]) for r in rows}
    wide_names = {(r["source_entity"], r["target_entity"]) for r in wide}
    assert (a, b) not in names
    assert (a, b) in wide_names
