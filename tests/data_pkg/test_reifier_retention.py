# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-G2 retention — the below-bar remainder ages out, it does not fester.

The pending queue is ~176,000 rows growing ~9,941/day, and 92.1% of it rests on
a single independent source. Only ~12,000 rows can ever clear the qualification
bar; the rest are sediment. Two halves keep the queue honest:

  * migration ``0160`` clears the standing backlog ONCE;
  * ``proposed_edge_governance._retire_below_bar_stale`` keeps it clear.

Both come from ONE generator (:func:`edge_qualification.retirement_update_sql`),
because a one-shot and a recurring rule that disagree about who retires is the
worst possible outcome — it would look like it worked. The byte-identity of the
migration's inlined statement against the generator is pinned below.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers.proposed_edge_governance import (
    DEFAULT_MAX_RETIREMENTS_PER_RUN,
    _retire_below_bar_stale,
)
from legba.data.analysts.edge_qualification import (
    MIN_INDEPENDENT_SOURCES,
    RECOMMENDED_BAR,
    RETENTION_STALE_DAYS,
    RETIRED_STATUS,
    retirement_update_sql,
)
from legba.data.config import PostgresConfig

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src" / "legba" / "data" / "migrations"
    / "0160_retire_below_bar_proposed_edges.sql"
)


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


def _migration_statement() -> str:
    """The migration's SQL with its comment header and trailing ';' stripped."""
    body = "\n".join(
        line for line in MIGRATION.read_text().splitlines()
        if not line.startswith("--")
    )
    return body.strip().rstrip(";").strip()


# ---------------------------------------------------------------------------
# one generator, two callers
# ---------------------------------------------------------------------------


def test_migration_sql_is_the_module_sql():
    """THE anti-drift pin. A migration cannot import Python, so the statement is
    inlined — and inlined code rots. If someone edits either side, this is red."""
    assert _migration_statement() == retirement_update_sql().strip()


def test_the_migration_uses_the_recommended_thresholds():
    stmt = _migration_statement()
    assert f">= {MIN_INDEPENDENT_SOURCES} AND" in stmt
    assert f">= {RECOMMENDED_BAR}" in stmt
    assert f"age_days >= {RETENTION_STALE_DAYS}" in stmt


def test_retirement_is_a_status_flip_never_a_delete():
    """The co-mention evidence must stay addressable: a pair that re-earns
    support returns through the normal producer path."""
    stmt = _migration_statement().lower()
    assert stmt.startswith("update proposed_edges")
    for verb in ("delete", "drop ", "truncate"):
        assert verb not in stmt, f"the migration must never {verb.strip()}"
    assert f"status = '{RETIRED_STATUS}'" in _migration_statement()
    assert "reviewed_at = now()" in _migration_statement()


def test_retired_is_a_new_status_not_a_reuse_of_rejected():
    """'rejected' means a human or the governance pass REFUSED the pair.
    Retirement is weaker and different — it never earned enough support. Folding
    them together would erase the distinction that gives the row audit value."""
    assert RETIRED_STATUS == "retired"
    assert RETIRED_STATUS not in {"pending", "promoted", "rejected", "orphaned"}
    assert "'rejected'" not in _migration_statement()


def test_the_migration_only_ever_touches_pending_rows():
    """Idempotence, and the guarantee that it cannot disturb promoted history."""
    stmt = _migration_statement()
    assert "WHERE status = 'pending'" in stmt
    assert stmt.count("pe.status = 'pending'") == 1


def test_the_sweep_form_is_bounded_and_the_migration_form_is_not():
    unbounded = retirement_update_sql()
    bounded = retirement_update_sql(limit=500)
    assert "LIMIT" not in unbounded
    assert "LIMIT 500" in bounded
    # oldest first, so a bounded sweep converges rather than thrashing
    assert "ORDER BY r.age_days DESC" in bounded


def test_thresholds_are_rendered_as_literals_and_coerced():
    """A migration cannot bind parameters, so the values are interpolated —
    which is only safe because they go through float()/int() first."""
    stmt = retirement_update_sql(bar=0.5, min_sources=3, stale_days=7, limit=10)
    assert ">= 0.5" in stmt and ">= 3 AND" in stmt
    assert "age_days >= 7" in stmt and "LIMIT 10" in stmt
    assert "$1" not in stmt and "$2" not in stmt and "$3" not in stmt
    # a hostile value cannot survive the coercion
    with pytest.raises((ValueError, TypeError)):
        retirement_update_sql(bar="0.5; DROP TABLE proposed_edges")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# behaviour against a real substrate
# ---------------------------------------------------------------------------

pytestmark_integration = pytest.mark.integration


async def _seed(conn, *, src, tgt, sources, age_days, status="pending"):
    tag = uuid4().hex[:6]
    ids = []
    for i in range(sources):
        sid = uuid4()
        await conn.execute(
            "INSERT INTO signals (id, source_id, payload, content_hash, "
            "fetched_at) VALUES ($1,$2,'{}'::jsonb,$3, "
            "now() - make_interval(days => $4))",
            sid, f"source.pub{tag}{i}.feed", f"hash-{sid}", int(age_days),
        )
        ids.append(sid)
    await conn.execute(
        "INSERT INTO proposed_edges (source_entity, target_entity, "
        "relationship_type, confidence, evidence_text, status, derived_from, "
        "produced_at) VALUES ($1,$2,'co_occurs',0.6,'x',$3,$4::uuid[], "
        "now() - make_interval(days => $5))",
        src, tgt, status, ids, int(age_days),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_below_bar_and_stale_is_retired(pg_pool):
    tag = uuid4().hex[:8]
    a, b = f"Sediment{tag}", f"Pair{tag}"
    async with pg_pool.acquire() as conn:
        await _seed(conn, src=a, tgt=b, sources=1, age_days=90)
        n = await _retire_below_bar_stale(
            conn, bar=RECOMMENDED_BAR, min_sources=MIN_INDEPENDENT_SOURCES,
            stale_days=RETENTION_STALE_DAYS, limit=DEFAULT_MAX_RETIREMENTS_PER_RUN,
        )
        status = await conn.fetchval(
            "SELECT status FROM proposed_edges WHERE source_entity=$1", a
        )
    assert n >= 1
    assert status == RETIRED_STATUS


@pytest.mark.integration
@pytest.mark.asyncio
async def test_below_bar_but_fresh_is_kept(pg_pool):
    """A slow-burning story gets a month to earn a second source."""
    tag = uuid4().hex[:8]
    a, b = f"Fresh{tag}", f"Pair{tag}"
    async with pg_pool.acquire() as conn:
        await _seed(conn, src=a, tgt=b, sources=1, age_days=3)
        await _retire_below_bar_stale(
            conn, bar=RECOMMENDED_BAR, min_sources=MIN_INDEPENDENT_SOURCES,
            stale_days=RETENTION_STALE_DAYS, limit=DEFAULT_MAX_RETIREMENTS_PER_RUN,
        )
        status = await conn.fetchval(
            "SELECT status FROM proposed_edges WHERE source_entity=$1", a
        )
    assert status == "pending"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_qualifying_candidate_is_never_retired_however_old(pg_pool):
    """The queue IS the work. Age alone must never take a qualifying pair."""
    tag = uuid4().hex[:8]
    a, b = f"Earned{tag}", f"Pair{tag}"
    async with pg_pool.acquire() as conn:
        await _seed(conn, src=a, tgt=b, sources=4, age_days=400)
        await _retire_below_bar_stale(
            conn, bar=RECOMMENDED_BAR, min_sources=MIN_INDEPENDENT_SOURCES,
            stale_days=RETENTION_STALE_DAYS, limit=DEFAULT_MAX_RETIREMENTS_PER_RUN,
        )
        status = await conn.fetchval(
            "SELECT status FROM proposed_edges WHERE source_entity=$1", a
        )
    assert status == "pending", "a well-evidenced candidate was aged out"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_staleness_is_measured_from_the_newest_backing_signal(pg_pool):
    """An OLD row that recently gained a signal has restarted its clock — the
    property `_reject_stale_thin` (which reads produced_at) cannot express."""
    tag = uuid4().hex[:8]
    a, b = f"Revived{tag}", f"Pair{tag}"
    async with pg_pool.acquire() as conn:
        await _seed(conn, src=a, tgt=b, sources=1, age_days=400)
        fresh = uuid4()
        await conn.execute(
            "INSERT INTO signals (id, source_id, payload, content_hash, "
            "fetched_at) VALUES ($1,'source.newpub.feed','{}'::jsonb,$2, now())",
            fresh, f"hash-{fresh}",
        )
        await conn.execute(
            "UPDATE proposed_edges SET derived_from = derived_from || $2::uuid[] "
            "WHERE source_entity = $1",
            a, [fresh],
        )
        await _retire_below_bar_stale(
            conn, bar=RECOMMENDED_BAR, min_sources=MIN_INDEPENDENT_SOURCES,
            stale_days=RETENTION_STALE_DAYS, limit=DEFAULT_MAX_RETIREMENTS_PER_RUN,
        )
        status = await conn.fetchval(
            "SELECT status FROM proposed_edges WHERE source_entity=$1", a
        )
    assert status == "pending", "a candidate that gained support was still retired"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retirement_is_idempotent(pg_pool):
    tag = uuid4().hex[:8]
    a, b = f"Twice{tag}", f"Pair{tag}"
    async with pg_pool.acquire() as conn:
        await _seed(conn, src=a, tgt=b, sources=1, age_days=90)
        await _retire_below_bar_stale(
            conn, bar=RECOMMENDED_BAR, min_sources=MIN_INDEPENDENT_SOURCES,
            stale_days=RETENTION_STALE_DAYS, limit=DEFAULT_MAX_RETIREMENTS_PER_RUN,
        )
        again = await _retire_below_bar_stale(
            conn, bar=RECOMMENDED_BAR, min_sources=MIN_INDEPENDENT_SOURCES,
            stale_days=RETENTION_STALE_DAYS, limit=DEFAULT_MAX_RETIREMENTS_PER_RUN,
        )
        rows = await conn.fetch(
            "SELECT status FROM proposed_edges WHERE source_entity=$1", a
        )
    assert len(rows) == 1, "the row must be retained, not deleted"
    assert rows[0]["status"] == RETIRED_STATUS
    assert again == 0, "a second pass must match nothing"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_days_zero_disables_retirement(pg_pool):
    """Matches the sibling age-out leg's convention — a non-positive window is
    the off switch, not 'retire everything immediately'."""
    tag = uuid4().hex[:8]
    a, b = f"Off{tag}", f"Pair{tag}"
    async with pg_pool.acquire() as conn:
        await _seed(conn, src=a, tgt=b, sources=1, age_days=999)
        n = await _retire_below_bar_stale(
            conn, bar=RECOMMENDED_BAR, min_sources=MIN_INDEPENDENT_SOURCES,
            stale_days=0, limit=DEFAULT_MAX_RETIREMENTS_PER_RUN,
        )
        status = await conn.fetchval(
            "SELECT status FROM proposed_edges WHERE source_entity=$1", a
        )
    assert n == 0
    assert status == "pending"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_retired_row_never_returns_to_the_typing_window(pg_pool):
    """The point of the whole exercise: retirement must actually remove the row
    from the reifier's queue, not just relabel it."""
    from legba.data.analysts.reifier_selection import select_candidates

    tag = uuid4().hex[:8]
    a, b = f"Gone{tag}", f"Pair{tag}"
    async with pg_pool.acquire() as conn:
        await _seed(conn, src=a, tgt=b, sources=4, age_days=400)
        # force it out of the queue by retiring against a bar it cannot clear
        await _retire_below_bar_stale(
            conn, bar=1.0, min_sources=MIN_INDEPENDENT_SOURCES,
            stale_days=RETENTION_STALE_DAYS, limit=DEFAULT_MAX_RETIREMENTS_PER_RUN,
        )
        rows, _ = await select_candidates(conn, limit=500)
    assert (a, b) not in {(r["source_entity"], r["target_entity"]) for r in rows}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_migration_statement_runs_against_the_real_schema(pg_pool):
    """EXPLAIN plans the statement without executing it — proof the inlined SQL
    is valid against the live shape, not merely well-formed text."""
    async with pg_pool.acquire() as conn:
        plan = await conn.fetch("EXPLAIN " + _migration_statement())
    assert plan
    assert re.search(r"proposed_edges", "\n".join(r[0] for r in plan))
