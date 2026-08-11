# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Continuity P2 — the situation trajectory LEDGER (migration 0184).

Everything here runs against a REALLY migrated Postgres. That is not ceremony:
three of this table's four guarantees are schema objects (two triggers and two
CHECKs), and a test that mocked the connection would assert only that the Python
called the SQL it was written to call — the exact shape of test that has passed
while production silently did nothing.

The four guarantees, one section each:
  1. the objects exist (table, indexes, both triggers, the vocabulary row);
  2. the ledger is append-only, ENFORCED — UPDATE and DELETE both fail loud;
  3. a delta cannot exist without evidence, ENFORCED — in Python at
     construction and in the database at insert;
  4. the reads say what happened and nothing more (no fabricated state).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.registry import production_gauge as pg
from legba.data.situations import trajectory as tj

DRAIN_ID = "situation_trajectory_ledger"


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield p
    await p.close()


async def _situation(
    conn: Any,
    *,
    name: str = "Situation: test frame",
    status: str = "active",
    intensity: float = 3.0,
    last_event_hours_ago: float | None = 2.0,
    event_count: int = 4,
) -> UUID:
    last = (
        datetime.now(timezone.utc) - timedelta(hours=last_event_hours_ago)
        if last_event_hours_ago is not None
        else None
    )
    return await conn.fetchval(
        """
        INSERT INTO situations
            (id, data, name, status, category, last_event_at, event_count,
             intensity_score, schema_uri, situation_signature)
        VALUES ($1, '{}'::jsonb, $2, $3, '', $4, $5, $6,
                'iglu:legba/situation/jsonschema/2-0-0', $7)
        RETURNING id
        """,
        uuid4(), name, status, last, event_count, intensity, f"sig:{uuid4().hex}",
    )


async def _append(
    conn: Any,
    situation_id: UUID,
    *,
    delta: str = tj.DELTA_ESCALATES,
    derived_from: tuple[UUID, ...] = (),
    state_from: str = tj.STATE_WATCHING,
    state_to: str = tj.STATE_ESCALATING,
    occurred_at: datetime | None = None,
    source_output_id: UUID | None = None,
    why: str = "the cited item reports a new deployment",
) -> int:
    if delta != tj.DELTA_UNCHANGED_CHECKPOINT and not derived_from:
        derived_from = (uuid4(),)
    event = tj.TrajectoryEvent(
        situation_id=situation_id,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        delta=delta,
        why=why,
        state_from=state_from,
        state_to=state_to,
        derived_from=derived_from,
    )
    return await tj.record_situation_events(
        conn, events=[event], source_output_id=source_output_id or uuid4(),
        verification={"faithfulness_score": 0.9},
    )


# ---------------------------------------------------------------------------
# 1 — the objects exist
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_objects_exist(pool):
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT to_regclass('public.situation_events')::text"
        ) == "situation_events"
        for index in (
            "idx_situation_events_situation",
            "idx_situation_events_escalations",
        ):
            assert await conn.fetchval(
                "SELECT indexname FROM pg_indexes WHERE indexname = $1", index,
            ) == index, index
        # The append-only posture is a TRIGGER, not a convention — on BOTH
        # mutation paths, which is where this table goes further than 0107's
        # review_flags (that one legitimately UPDATEs to close).
        for trigger in (
            "trg_situation_events_forbid_delete",
            "trg_situation_events_forbid_update",
        ):
            assert await conn.fetchval(
                "SELECT tgname FROM pg_trigger WHERE tgname = $1", trigger,
            ) == trigger, trigger
        # The registry-side kind registration ships WITH the schema, so the
        # descriptor PUT cannot fail on a step an operator forgot to run.
        assert await conn.fetchval(
            "SELECT value FROM vocabulary_entries "
            "WHERE family = 'analyst_kind' AND value = 'situation_tracker'"
        ) == "situation_tracker"


# ---------------------------------------------------------------------------
# 2 — append-only, enforced
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_ledger_refuses_update_and_delete(pool):
    async with pool.acquire() as conn:
        sid = await _situation(conn)
        assert await _append(conn, sid) == 1
        row_id = await conn.fetchval(
            "SELECT id FROM situation_events WHERE situation_id = $1", sid,
        )

        with pytest.raises(asyncpg.PostgresError, match="never updated or deleted"):
            await conn.execute(
                "UPDATE situation_events SET why = 'rewritten' WHERE id = $1",
                row_id,
            )
        with pytest.raises(asyncpg.PostgresError, match="never updated or deleted"):
            await conn.execute(
                "DELETE FROM situation_events WHERE id = $1", row_id,
            )
        # Both refusals leave the row exactly as written.
        row = await conn.fetchrow(
            "SELECT why, delta FROM situation_events WHERE id = $1", row_id,
        )
        assert row["why"] == "the cited item reports a new deployment"
        assert row["delta"] == tj.DELTA_ESCALATES


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_writer_is_idempotent_per_source_finding(pool):
    """A re-run over the same situation_update lands nothing new.

    The runtime materializes ledger rows on the output-write flow; a retried
    actor turn that re-writes its output row must not double the trajectory.
    """
    async with pool.acquire() as conn:
        sid = await _situation(conn)
        source = uuid4()
        assert await _append(conn, sid, source_output_id=source) == 1
        assert await _append(conn, sid, source_output_id=source) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM situation_events WHERE situation_id = $1", sid,
        ) == 1
        # A DIFFERENT source finding is a genuinely new episode and lands.
        assert await _append(conn, sid, source_output_id=uuid4()) == 1


# ---------------------------------------------------------------------------
# 3 — a delta requires evidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "delta",
    [tj.DELTA_ESCALATES, tj.DELTA_DE_ESCALATES, tj.DELTA_BROADENS],
)
def test_an_evidence_free_delta_is_unconstructable(delta):
    """The rule bites BEFORE SQL: the tracker can count the refusal."""
    with pytest.raises(tj.TrajectoryTransitionError, match="REQUIRES"):
        tj.TrajectoryEvent(
            situation_id=uuid4(),
            occurred_at=datetime.now(timezone.utc),
            delta=delta,
            why="something moved",
            state_from=tj.STATE_WATCHING,
            state_to=tj.STATE_ESCALATING,
            derived_from=(),
        )


def test_an_unchanged_checkpoint_needs_no_evidence():
    """It asserts nothing about the world, so it needs nothing to rest on."""
    event = tj.TrajectoryEvent(
        situation_id=uuid4(),
        occurred_at=datetime.now(timezone.utc),
        delta=tj.DELTA_UNCHANGED_CHECKPOINT,
        why="no evidence has attached since 2026-07-01",
        state_from=tj.STATE_WATCHING,
        state_to=tj.STATE_DORMANT,
    )
    assert event.derived_from == ()


def test_an_empty_why_and_an_unknown_vocabulary_are_refused():
    base = dict(
        situation_id=uuid4(),
        occurred_at=datetime.now(timezone.utc),
        delta=tj.DELTA_UNCHANGED_CHECKPOINT,
        state_from=tj.STATE_WATCHING,
        state_to=tj.STATE_WATCHING,
    )
    with pytest.raises(tj.TrajectoryTransitionError, match="why"):
        tj.TrajectoryEvent(**{**base, "why": "   "})
    with pytest.raises(tj.TrajectoryTransitionError, match="delta"):
        tj.TrajectoryEvent(**{**base, "why": "ok", "delta": "worsens"})
    with pytest.raises(tj.TrajectoryTransitionError, match="state"):
        tj.TrajectoryEvent(**{**base, "why": "ok", "state_to": "spicy"})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_database_refuses_it_too(pool):
    """Belt to the Python suspenders: a future writer cannot bypass the rule."""
    async with pool.acquire() as conn:
        sid = await _situation(conn)
        with pytest.raises(
            asyncpg.PostgresError, match="delta_requires_evidence",
        ):
            await conn.execute(
                """
                INSERT INTO situation_events
                    (situation_id, occurred_at, delta, state_from, state_to,
                     why, derived_from, source_output_id)
                VALUES ($1, now(), 'escalates', 'watching', 'escalating',
                        'no evidence at all', '{}'::uuid[], $2)
                """,
                sid, uuid4(),
            )
        with pytest.raises(asyncpg.PostgresError, match="why_nonempty"):
            await conn.execute(
                """
                INSERT INTO situation_events
                    (situation_id, occurred_at, delta, state_from, state_to,
                     why, derived_from, source_output_id)
                VALUES ($1, now(), 'unchanged_checkpoint', 'watching',
                        'watching', '  ', '{}'::uuid[], $2)
                """,
                sid, uuid4(),
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_ungraded_delta_claim_is_refused_but_a_checkpoint_is_not(
    pool, caplog,
):
    """The ledger has no UPDATE and no DELETE, and the trajectory READS surface
    the newest row as THE state with no confidence filter of their own. So a
    delta claim is written only on a verdict at or above the floor, and a MISSING
    verdict refuses rather than passes — "we could not grade it" must never
    become a permanent row. A checkpoint asserts nothing, so nothing gates it.
    """
    async with pool.acquire() as conn:
        sid = await _situation(conn)

        def _event(delta: str) -> tj.TrajectoryEvent:
            return tj.TrajectoryEvent(
                situation_id=sid,
                occurred_at=datetime.now(timezone.utc),
                delta=delta,
                why="why",
                state_from=tj.STATE_WATCHING,
                state_to=(
                    tj.STATE_ESCALATING if delta == tj.DELTA_ESCALATES
                    else tj.STATE_WATCHING
                ),
                derived_from=((uuid4(),) if delta == tj.DELTA_ESCALATES else ()),
            )

        with caplog.at_level("WARNING"):
            # Below the floor.
            assert await tj.record_situation_events(
                conn, events=[_event(tj.DELTA_ESCALATES)],
                source_output_id=uuid4(),
                verification={"faithfulness_score": 0.3},
            ) == 0
            # No verdict at all (the verify pass raised).
            assert await tj.record_situation_events(
                conn, events=[_event(tj.DELTA_ESCALATES)],
                source_output_id=uuid4(), verification=None,
            ) == 0
        assert any("delta_refused" in r.message for r in caplog.records)

        # A checkpoint lands with no verdict.
        assert await tj.record_situation_events(
            conn, events=[_event(tj.DELTA_UNCHANGED_CHECKPOINT)],
            source_output_id=uuid4(), verification=None,
        ) == 1
        # ...and only the checkpoint is on the ledger.
        deltas = [r["delta"] for r in await tj.read_trajectory(conn, sid)]
        assert deltas == [tj.DELTA_UNCHANGED_CHECKPOINT]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_failed_append_never_raises_into_the_run(pool, caplog):
    """The writer runs inside the actor's output flow and must degrade, LOUDLY.

    A situation_id with no matching row trips the FK. The finding has already
    landed at that point, so unwinding the run would throw away a graded claim
    to protect a pointer.
    """
    async with pool.acquire() as conn:
        event = tj.TrajectoryEvent(
            situation_id=uuid4(),                      # no such situation
            occurred_at=datetime.now(timezone.utc),
            delta=tj.DELTA_UNCHANGED_CHECKPOINT,
            why="checkpoint",
            state_from=tj.STATE_WATCHING,
            state_to=tj.STATE_WATCHING,
        )
        with caplog.at_level("WARNING"):
            written = await tj.record_situation_events(
                conn, events=[event], source_output_id=uuid4(),
            )
    assert written == 0
    assert any("append_failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 4 — the reads
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_current_state_is_the_newest_rows_state_to(pool):
    async with pool.acquire() as conn:
        sid = await _situation(conn)
        now = datetime.now(timezone.utc)
        await _append(
            conn, sid, occurred_at=now - timedelta(days=3),
            state_from=tj.STATE_WATCHING, state_to=tj.STATE_ESCALATING,
        )
        await _append(
            conn, sid, delta=tj.DELTA_DE_ESCALATES,
            occurred_at=now - timedelta(days=1),
            state_from=tj.STATE_ESCALATING, state_to=tj.STATE_DE_ESCALATING,
        )
        states = await tj.read_current_states(conn, [sid])
        assert states[str(sid)] == tj.STATE_DE_ESCALATING

        rows = await tj.read_trajectory(conn, sid)
        assert [r["delta"] for r in rows] == [
            tj.DELTA_DE_ESCALATES, tj.DELTA_ESCALATES,
        ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_situation_with_no_ledger_rows_is_absent_not_defaulted(pool):
    """"Never assessed" must stay distinguishable from "assessed and steady"."""
    async with pool.acquire() as conn:
        sid = await _situation(conn)
        assert await tj.read_current_states(conn, [sid]) == {}
        assert await tj.read_trajectories(conn, [sid]) == {}
        assert await tj.read_trajectory(conn, sid) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_batch_read_is_bounded_per_situation(pool):
    """The composition register asks for the last N of up to eight frames."""
    async with pool.acquire() as conn:
        a = await _situation(conn, name="Situation: A")
        b = await _situation(conn, name="Situation: B")
        now = datetime.now(timezone.utc)
        for i in range(5):
            await _append(conn, a, occurred_at=now - timedelta(days=5 - i))
        await _append(conn, b, occurred_at=now)

        got = await tj.read_trajectories(conn, [a, b], per_situation=3)
        assert len(got[str(a)]) == 3
        assert len(got[str(b)]) == 1
        # Newest first, within each situation.
        occurred = [r["occurred_at"] for r in got[str(a)]]
        assert occurred == sorted(occurred, reverse=True)


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------


def test_direction_deltas_set_direction():
    assert tj.next_state(tj.STATE_WATCHING, tj.DELTA_ESCALATES) == tj.STATE_ESCALATING
    assert tj.next_state(
        tj.STATE_ESCALATING, tj.DELTA_DE_ESCALATES
    ) == tj.STATE_DE_ESCALATING


def test_broadening_is_scope_not_direction():
    """A widening crisis that is not intensifying must not read as escalation."""
    assert tj.next_state(
        tj.STATE_DE_ESCALATING, tj.DELTA_BROADENS
    ) == tj.STATE_DE_ESCALATING
    # ...but it still re-opens a quiet thread, because evidence attached.
    assert tj.next_state(tj.STATE_DORMANT, tj.DELTA_BROADENS) == tj.STATE_WATCHING


def test_silence_never_closes_a_situation():
    """D4: NEVER auto-closed by silence alone. It goes dormant and waits."""
    assert tj.next_state(
        tj.STATE_ESCALATING, tj.DELTA_UNCHANGED_CHECKPOINT, dormant=True,
    ) == tj.STATE_DORMANT
    # Not past the horizon => no state change at all.
    assert tj.next_state(
        tj.STATE_ESCALATING, tj.DELTA_UNCHANGED_CHECKPOINT, dormant=False,
    ) == tj.STATE_ESCALATING


def test_closing_requires_resolution_grounded_de_escalation():
    assert tj.next_state(
        tj.STATE_ESCALATING, tj.DELTA_DE_ESCALATES, resolution_grounded=True,
    ) == tj.STATE_CLOSED
    # Resolution on any OTHER delta is not a close — the flag is only wired to
    # the one delta that cannot exist without evidence.
    assert tj.next_state(
        tj.STATE_ESCALATING, tj.DELTA_ESCALATES, resolution_grounded=True,
    ) == tj.STATE_ESCALATING


def test_new_evidence_reopens_a_closed_situation():
    """Closing is a judgment, not a tombstone."""
    assert tj.next_state(tj.STATE_CLOSED, tj.DELTA_ESCALATES) == tj.STATE_ESCALATING
    assert tj.next_state(tj.STATE_CLOSED, tj.DELTA_BROADENS) == tj.STATE_WATCHING
    # ...but silence about a closed situation changes nothing.
    assert tj.next_state(
        tj.STATE_CLOSED, tj.DELTA_UNCHANGED_CHECKPOINT, dormant=True,
    ) == tj.STATE_CLOSED


def test_unknown_vocabulary_fails_loud_rather_than_coercing():
    with pytest.raises(tj.TrajectoryTransitionError):
        tj.next_state("simmering", tj.DELTA_ESCALATES)
    with pytest.raises(tj.TrajectoryTransitionError):
        tj.next_state(tj.STATE_WATCHING, "worsens")


# ---------------------------------------------------------------------------
# The S-1 gauge loop
# ---------------------------------------------------------------------------


def test_the_ledger_drain_is_declared():
    drain = next(
        (d for d in pg.BACKLOG_DRAINS if d.backlog_id == DRAIN_ID), None,
    )
    assert drain is not None, f"{DRAIN_ID} must be a declared backlog"
    assert drain.owner_analyst_id == "situation_tracker"
    assert drain.unit == "situation"
    # The gauge must measure the queue the writer actually drains.
    assert "situation_events" in drain.overdue_sql
    assert "v.newest_verified > l.newest_delta" in drain.overdue_sql
    assert "newest_delta IS NOT NULL" in drain.overdue_sql, (
        "a situation with NO ledger row is pre-seed backlog, not a deficit — "
        "counting it would manufacture a permanent unclearable deficit"
    )
    assert "Faithfulness verify" in drain.overdue_sql, (
        "the drain must compare against the newest VERIFIED member, not "
        "last_event_at: the tracker may only cite members clearing the floor, so "
        "a situation whose newest member is ungraded would otherwise be overdue "
        "forever no matter how correctly the tracker had adjudicated"
    )


async def _overdue(conn: Any) -> int:
    drain = next(d for d in pg.BACKLOG_DRAINS if d.backlog_id == DRAIN_ID)
    row = await conn.fetchrow(drain.overdue_sql)
    return int(row["overdue"])


async def _member(
    conn: Any, situation_id: UUID, *, hours_ago: float, faithfulness: float | None,
) -> UUID:
    """One attached member finding, optionally carrying a faithfulness verdict."""
    fid = uuid4()
    produced = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, kind, title, body, confidence, data, produced_at, schema_uri)
        VALUES ($1, 'finding', 'member', '', 0.9, '{}'::jsonb, $2,
                'iglu:legba/finding/jsonschema/1-0-0')
        """,
        fid, produced,
    )
    if faithfulness is not None:
        await conn.execute(
            """
            INSERT INTO analyst_outputs
                (id, kind, title, body, confidence, data, produced_at, schema_uri)
            VALUES ($1, 'critique', 'Faithfulness verify', '', 1.0, $2::jsonb, $3,
                    'iglu:legba/critique/jsonschema/1-0-0')
            """,
            uuid4(),
            json.dumps({"analyzed_output_id": str(fid),
                        "overall_score": faithfulness}),
            produced,
        )
    await conn.execute(
        "UPDATE situations SET derived_from = derived_from || $2::uuid[], "
        "last_event_at = GREATEST(last_event_at, $3) WHERE id = $1",
        situation_id, [fid], produced,
    )
    return fid


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_situation_with_verified_news_past_its_newest_delta_is_overdue(pool):
    async with pool.acquire() as conn:
        base = await _overdue(conn)
        sid = await _situation(conn, last_event_hours_ago=6)
        await _member(conn, sid, hours_ago=6, faithfulness=0.9)
        # A delta from BEFORE the news: the tracker has not spoken to it yet.
        await _append(
            conn, sid,
            occurred_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        assert await _overdue(conn) == base + 1

        # A delta covering the news clears it.
        await _append(
            conn, sid, source_output_id=uuid4(),
            occurred_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        assert await _overdue(conn) == base


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unverifiable_news_is_not_a_deficit(pool):
    """THE unclearable-deficit trap. `situations.last_event_at` counts EVERY
    member; the tracker may only cite members clearing the floor. A situation
    whose newest member is ungraded or demoted is one the tracker has already
    adjudicated as far as it is permitted to — so it must not read as overdue,
    forever, for evidence nobody is allowed to act on."""
    async with pool.acquire() as conn:
        base = await _overdue(conn)
        sid = await _situation(conn, last_event_hours_ago=6)
        await _member(conn, sid, hours_ago=48, faithfulness=0.9)
        # A delta that COVERS that member (in production the ledger row's
        # occurred_at IS the newest cited member's produced_at; the SQL compares
        # with `>`, so covered means not-overdue).
        await _append(
            conn, sid,
            occurred_at=datetime.now(timezone.utc) - timedelta(hours=47),
        )
        assert await _overdue(conn) == base, "the verified member is covered"

        # Newer members that the tracker is NOT allowed to cite.
        await _member(conn, sid, hours_ago=5, faithfulness=None)   # ungraded
        await _member(conn, sid, hours_ago=4, faithfulness=0.2)    # demoted
        assert await _overdue(conn) == base, (
            "unverifiable news must not open a deficit the tracker cannot drain"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_drain_never_manufactures_an_unclearable_deficit(pool):
    async with pool.acquire() as conn:
        base = await _overdue(conn)
        # Never-tracked situation: pre-seed backlog, NOT a deficit.
        untracked = await _situation(conn, last_event_hours_ago=6)
        await _member(conn, untracked, hours_ago=6, faithfulness=0.9)
        # Closed situation: out of scope entirely.
        closed = await _situation(conn, status="closed", last_event_hours_ago=6)
        await _member(conn, closed, hours_ago=6, faithfulness=0.9)
        await _append(
            conn, closed,
            occurred_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
        # Inside the grace window: a scan racing an in-flight cycle.
        fresh = await _situation(conn, last_event_hours_ago=0.1)
        await _member(conn, fresh, hours_ago=0.1, faithfulness=0.9)
        await _append(
            conn, fresh,
            occurred_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
        assert await _overdue(conn) == base
