# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Piece 3, Task D — hypothesis_lifecycle producer + evidence tester.

Pure-logic coverage of the deterministic classification / parsing helpers +
the synthetic (deps=None) summary path, and a live-DB integration path (against
the migrated stack) for the EMIT / idempotency / TEST-vs-later-evidence /
forward-claim-semantics / payload-validation behaviors the plan's §6.3 calls for.

The hypothesis rows are side-written via the LIVE
:func:`legba.data.provenance.writes.write_hypothesis` path (OutputKind.HYPOTHESIS
+ the hypotheses table already exist — reused, not re-plumbed); the per-run
summary FindingPayload is the cadence receipt.
"""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic import SUB_HANDLERS, OUTPUT_KIND_BY_SUB_HANDLER
from legba.data.analysts.deterministic_handlers import hypothesis_lifecycle as hl
from legba.data.config import PostgresConfig
from legba.data.provenance import HypothesisPayload
from legba.runtime.analyst_method import AnalystMethodResult


# ---------------------------------------------------------------------------
# Pure-logic unit tests (no DB)
# ---------------------------------------------------------------------------


def test_registered_in_dispatch_table():
    assert "hypothesis_lifecycle" in SUB_HANDLERS
    assert OUTPUT_KIND_BY_SUB_HANDLER["hypothesis_lifecycle"].value == "finding"


def test_thesis_and_counter_thesis_forward_claims():
    t = hl._thesis_for("Argentina energy crisis")
    ct = hl._counter_thesis_for("Argentina energy crisis")
    assert t.startswith("Argentina energy crisis will escalate")
    assert "de-escalate" in ct
    # Empty name degrades gracefully (thesis is min_length=1 on the payload).
    assert hl._thesis_for("").strip()


def test_classify_move_thresholds():
    base = 2.0
    # Rose past epsilon → support.
    assert hl._classify_move(base + hl._INTENSITY_MOVE_EPS, base) == 1
    # Fell past epsilon → refute.
    assert hl._classify_move(base - hl._INTENSITY_MOVE_EPS, base) == -1
    # Flat (within epsilon) → neutral.
    assert hl._classify_move(base + hl._INTENSITY_MOVE_EPS / 2, base) == 0


def test_intensity_at_emit_recovers_snapshot():
    ev = [{"at": "t0"}, {"intensity_at_emit": 3.5, "at": "t1"}]
    assert hl._intensity_at_emit(ev) == 3.5
    assert hl._intensity_at_emit([]) is None
    assert hl._intensity_at_emit([{"no_snapshot": True}]) is None


@pytest.mark.asyncio
async def test_handle_deps_none_returns_zero_summary():
    """deps=None unit path: no substrate work, a zero summary FINDING."""
    result = await hl.handle([], {"analyst_id": "hypothesis_lifecycle"}, None)
    assert isinstance(result, AnalystMethodResult)
    data = result.finding.data
    assert data["sub_handler"] == "hypothesis_lifecycle"
    assert data["hypotheses_created"] == 0
    # DQ P6 — the receipt reports WORKING-state moves (supported/weakened); this
    # handler never confirms/refutes from intensity drift (self-consistency).
    assert data["supported"] == 0
    assert data["weakened"] == 0
    assert "confirmed" not in data and "refuted" not in data
    assert "deterministic" in result.finding.tags


def test_hypothesis_payload_rejects_empty_thesis():
    """A HypothesisPayload with empty thesis is rejected at construction
    (models.py thesis min_length=1) — the producer never emits one."""
    with pytest.raises(Exception):
        HypothesisPayload(thesis="")


# ---------------------------------------------------------------------------
# Live-DB integration — EMIT / idempotency / TEST-vs-later-evidence
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=2)
    yield pool
    await pool.close()


class _Deps:
    """Minimal StandardDeps-shaped object the handler reaches pg_pool through."""

    def __init__(self, pool):
        self.pg_pool = pool
        self.nats_publish = None


async def _seed_situation(
    conn, *, name: str, intensity: float, status: str = "active",
    derived_from=None, last_event_offset_days: int = 0,
):
    sid = uuid4()
    await conn.execute(
        """
        INSERT INTO situations
            (id, data, name, status, category, last_event_at, event_count,
             intensity_score, analyst_id, produced_at, derived_from, schema_uri)
        VALUES ($1, '{}'::jsonb, $2, $3, '', NOW() - make_interval(days => $4),
                3, $5, 'situation_clustering', NOW(), $6,
                'iglu:legba/situation/jsonschema/2-0-0')
        """,
        sid, name, status, last_event_offset_days, intensity,
        list(derived_from or []),
    )
    return sid


@pytest.mark.integration
@pytest.mark.asyncio
async def test_emit_creates_one_hypothesis_and_is_idempotent(pg_pool):
    """A rising/notable active situation → exactly one active hypothesis for
    THAT situation with thesis set, situation_id linked, status='active',
    evidence_balance=0. A second sweep does NOT duplicate (upsert on
    (situation_id, analyst_id)).

    NOTE the handler's EMIT reads ALL active situations globally (by design),
    so assertions are scoped to the seeded situation_id — other tests' leaked
    situations would otherwise show up under this run's analyst_id.
    """
    analyst_id = f"hyp_test_{uuid4().hex[:8]}"
    root = uuid4()
    async with pg_pool.acquire() as conn:
        sid = await _seed_situation(
            conn, name="Test escalating situation", intensity=4.0,
            derived_from=[root],
        )

    deps = _Deps(pg_pool)
    opts = {"analyst_id": analyst_id, "run_id": uuid4()}

    await hl.handle([], opts, deps)

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT thesis, situation_id, status, evidence_balance "
            "FROM hypotheses WHERE analyst_id = $1 AND situation_id = $2",
            analyst_id, sid,
        )
    assert len(rows) == 1
    assert rows[0]["thesis"]  # non-empty
    assert rows[0]["situation_id"] == sid
    assert rows[0]["status"] == "active"
    assert rows[0]["evidence_balance"] == 0

    # Second sweep: same situation → still exactly one row for it (refresh, not
    # duplicate).
    await hl.handle([], {"analyst_id": analyst_id, "run_id": uuid4()}, deps)
    async with pg_pool.acquire() as conn:
        cnt = await conn.fetchval(
            "SELECT count(*) FROM hypotheses WHERE analyst_id = $1 AND situation_id = $2",
            analyst_id, sid,
        )
    assert cnt == 1  # still exactly one — idempotent


@pytest.mark.integration
@pytest.mark.asyncio
async def test_below_floor_situation_does_not_emit(pg_pool):
    """A low-intensity situation under the emit floor spawns no hypothesis for
    THAT situation (scoped to the seeded sid — the global sweep may emit for
    other tests' leaked high-intensity situations)."""
    analyst_id = f"hyp_floor_{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        sid = await _seed_situation(conn, name="Trivial", intensity=0.5)
    deps = _Deps(pg_pool)
    await hl.handle([], {"analyst_id": analyst_id, "run_id": uuid4()}, deps)
    async with pg_pool.acquire() as conn:
        cnt = await conn.fetchval(
            "SELECT count(*) FROM hypotheses WHERE analyst_id = $1 AND situation_id = $2",
            analyst_id, sid,
        )
    assert cnt == 0  # below floor → no claim for this situation


@pytest.mark.integration
@pytest.mark.asyncio
async def test_test_step_confirms_on_later_supporting_evidence(pg_pool):
    """Forward-claim semantics: a standing active hypothesis whose situation
    intensity ROSE since emit, with LATER findings linked to it, accrues
    supporting_signals and transitions to confirmed past the threshold.

    Evidence produced BEFORE the hypothesis is ignored (only later evidence
    tests it)."""
    from legba.data.provenance import AnalystContext, write_hypothesis

    analyst_id = f"hyp_conf_{uuid4().hex[:8]}"

    async with pg_pool.acquire() as conn:
        # Situation now reads HIGH intensity (it rose since the hypothesis emit).
        sid = await _seed_situation(conn, name="Rising", intensity=6.0)

        # A hypothesis emitted earlier with a LOW intensity snapshot.
        ctx = AnalystContext(
            analyst_id=analyst_id, analyst_version="v1", run_id=uuid4(),
        )
        # Seed the standing hypothesis with produced_at in the PAST so we can
        # land "later" evidence after it.
        payload = HypothesisPayload(
            thesis="Rising will escalate over the next 14 days",
            situation_id=sid,
            diagnostic_evidence=[{"intensity_at_emit": 2.0, "at": "past"}],
            status="active",
        )
        await write_hypothesis(conn, analyst_ctx=ctx, payload=payload, derived_from=[])
        # Backdate it so later findings can be produced AFTER it.
        await conn.execute(
            "UPDATE hypotheses SET produced_at = NOW() - INTERVAL '1 hour' "
            "WHERE analyst_id = $1", analyst_id,
        )

        # Two LATER findings, each linked to the situation (situations.derived_from
        # must reference them — the handler joins on s.derived_from @> ao.id).
        later_ids = []
        for i in range(2):
            fid = uuid4()
            await conn.execute(
                """
                INSERT INTO analyst_outputs
                    (id, kind, title, body, confidence, analyst_id,
                     analyst_version, produced_at, derived_from, schema_uri)
                VALUES ($1, 'finding', $2, 'b', 0.8, 'country_assessor', 'v1',
                        NOW(), '{}'::uuid[], 'iglu:legba/finding/jsonschema/1-0-0')
                """,
                fid, f"later finding {i}",
            )
            later_ids.append(fid)
        # Link the later findings to the situation via situations.derived_from.
        await conn.execute(
            "UPDATE situations SET derived_from = $2 WHERE id = $1",
            sid, later_ids,
        )

    deps = _Deps(pg_pool)
    r = await hl.handle([], {"analyst_id": analyst_id, "run_id": uuid4()}, deps)

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, evidence_balance, "
            "array_length(supporting_signals,1) AS supp "
            "FROM hypotheses WHERE analyst_id = $1 AND situation_id = $2",
            analyst_id, sid,
        )
    # DQ P6 — two later supporting findings → balance >= K → the WORKING state
    # 'supported' (NOT a terminal 'confirmed': intensity drift is a
    # self-consistency proxy, so hypothesis_lifecycle caps at working states and
    # reserves terminal confirmed/refuted for the exogenous subsequent_facts
    # resolver / operator).
    assert row["supp"] == 2
    assert row["evidence_balance"] == 2
    assert row["status"] == "supported"
    assert r.finding.data["supported"] >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_test_step_ignores_evidence_before_hypothesis(pg_pool):
    """Forward-claim semantics — evidence produced BEFORE the hypothesis must
    NOT count. A situation that rose but whose linked findings all predate the
    hypothesis leaves the hypothesis untouched (no balance move)."""
    from legba.data.provenance import AnalystContext, write_hypothesis

    analyst_id = f"hyp_before_{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        sid = await _seed_situation(conn, name="Rose but old evidence", intensity=6.0)
        # Findings produced in the PAST (before the hypothesis below).
        old_ids = []
        for i in range(2):
            fid = uuid4()
            await conn.execute(
                """
                INSERT INTO analyst_outputs
                    (id, kind, title, body, confidence, analyst_id,
                     analyst_version, produced_at, derived_from, schema_uri)
                VALUES ($1, 'finding', $2, 'b', 0.8, 'country_assessor', 'v1',
                        NOW() - INTERVAL '2 hours', '{}'::uuid[],
                        'iglu:legba/finding/jsonschema/1-0-0')
                """,
                fid, f"old finding {i}",
            )
            old_ids.append(fid)
        await conn.execute(
            "UPDATE situations SET derived_from = $2 WHERE id = $1", sid, old_ids,
        )
        ctx = AnalystContext(analyst_id=analyst_id, analyst_version="v1", run_id=uuid4())
        payload = HypothesisPayload(
            thesis="will escalate over the next 14 days",
            situation_id=sid,
            diagnostic_evidence=[{"intensity_at_emit": 2.0, "at": "past"}],
            status="active",
        )
        await write_hypothesis(conn, analyst_ctx=ctx, payload=payload, derived_from=[])
        # Hypothesis is "now" — the findings above are 2h earlier → not later.

    deps = _Deps(pg_pool)
    await hl.handle([], {"analyst_id": analyst_id, "run_id": uuid4()}, deps)

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, evidence_balance FROM hypotheses "
            "WHERE analyst_id = $1 AND situation_id = $2",
            analyst_id, sid,
        )
    # No LATER evidence → untouched.
    assert row["status"] == "active"
    assert row["evidence_balance"] == 0
