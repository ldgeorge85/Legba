# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Continuity P2 consumption — who READS the trajectory ledger (plan D5).

A ledger nothing reads is a table. The three consumers land here together
because they share one property worth protecting: each of them has an
"absent" case that must not be dressed up as an "assessed" one.

  * the COMPOSITION register — a frame with no ledger rows renders exactly as
    it did in Phase 1, and the compose still runs when the ledger is
    unreadable;
  * the ``situation_escalation`` ALERT class — three independent bars, each
    tested by the row it must refuse;
  * the ``/v3/situations/{id}/trajectory`` ROUTE — an unknown situation 404s,
    a known one with an empty ledger returns ``state: null``, and neither is
    the other.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.analysts import meta_findings_synthesizer as mfs
from legba.data.analysts.deterministic_handlers import (
    _situation_escalation_scan as ses,
)
from legba.data.analysts.deterministic_handlers import alert_trigger_scan as ats
from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.api import RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import MASTER_KEY_ENV, CredentialVault
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.situation_trajectory_api import (
    build_situation_trajectory_router,
)
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache
from legba.data.situations import trajectory as tj

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "66" * 32)

_ROUTE = "/api/v1/v3/situations/{sid}/trajectory"


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield p
    await p.close()


@pytest_asyncio.fixture
async def scope(pool):
    """A DESK NOTHING ELSE OWNS, and the class handle reset — no blank slate.

    THE BUG THIS FIXTURE IS. What stood here truncated ``situation_events`` and
    then ``DELETE FROM situations`` / ``DELETE FROM analyst_outputs`` outright.
    Three separate defects, all of them the same mistake — a test asserting
    over a substrate the whole suite shares, and reaching for the eraser:

      1. It DIED. The 08-06 nightly errored all seven integration tests here in
         BOTH orders on ``ForeignKeyViolationError: ... violates foreign key
         constraint "hypotheses_situation_id_fkey"``. Once 0184 shipped and the
         tracker began writing, a sibling's hypothesis pointed at a situation
         and the blanket DELETE could no longer run. A fixture whose
         precondition is "no other test has related rows" cannot hold in a
         suite of 10,700.
      2. ``DELETE FROM analyst_outputs`` is an unscoped wipe of the SUITE's
         findings, scorecards, critiques and alerts. It was not protecting this
         file so much as detonating every file that ran before it — an
         order-dependence generator, not a fix for one.
      3. Even when it worked it hid a REAL bound: ``read_open_situations`` is
         capped at :data:`~mfs.SITUATION_REGISTER_CAP` = 8 frames, worst-first.
         With an empty table this file's one frame was trivially in the page.
         With the live-shaped table the tracker now produces it is not, and the
         register tests' ``next(e for e in register ...)`` would raise
         StopIteration — a paging assumption written as a lookup.

    So: no deletes. Every test gets a ``target_id`` no other row can carry, and
    reads the register SCOPED to it — which is the register's own production
    contract (a per-desk compose passes ``target_id``), not a test-only trick.
    Teardown CLOSES this desk's frames instead of deleting them: ``situations``
    is referenced by ``hypotheses`` and by the append-only ledger, and
    ``status='closed'`` is exactly what every open-situation reader in the tower
    filters on, so the rows stop being visible to anyone without a DELETE that
    can collide with a foreign key.

    The one reset that stays is ``alert_trigger_watermarks`` for THIS CLASS'S
    handle. That is legitimate where the DELETEs were not: ``situation_escalation``
    is this file's own trigger class, its seeded/not-seeded flag is the thing
    two tests below are ABOUT, and the only other writer
    (``test_alert_trigger_scan.clean_slate``) truncates the whole table itself,
    so it cannot be harmed by us clearing one class out of it.
    """
    target = f"country_traj_{uuid4().hex[:10]}"
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM alert_trigger_watermarks WHERE trigger_class = $1",
            ses.TRIGGER_CLASS,
        )
    yield pool, target
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE situations SET status = 'closed' WHERE target_id = $1", target,
        )


# ---------------------------------------------------------------------------
# Fixtures for the substrate shapes
# ---------------------------------------------------------------------------


async def _situation(
    conn: Any, *, name: str = "Situation: strait", intensity: float = 5.0,
    target_id: str | None = "country_g20_ir", status: str = "active",
) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO situations
            (id, data, name, status, category, last_event_at, event_count,
             intensity_score, target_id, schema_uri, situation_signature)
        VALUES ($1, '{}'::jsonb, $2, $3, '', now() - interval '1 hour', 4, $4,
                $5, 'iglu:legba/situation/jsonschema/2-0-0', $6)
        RETURNING id
        """,
        uuid4(), name, status, intensity, target_id, f"sig:{uuid4().hex}",
    )


async def _situation_update(
    conn: Any, *, confidence: float = 0.9, faithfulness: float | None = 0.9,
) -> UUID:
    """A graded ``situation_update`` row — the claim a ledger delta points at."""
    oid = uuid4()
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, kind, title, body, confidence, data, analyst_id, produced_at,
             schema_uri)
        VALUES ($1, 'situation_update', 'Situation trajectory', 'body', $2,
                '{}'::jsonb, 'situation_tracker', now(),
                'iglu:legba/situation_update/jsonschema/1-0-0')
        """,
        oid, confidence,
    )
    if faithfulness is not None:
        await conn.execute(
            """
            INSERT INTO analyst_outputs
                (id, kind, title, body, confidence, data, produced_at, schema_uri)
            VALUES ($1, 'critique', 'Faithfulness verify', '', 1.0, $2::jsonb,
                    now(), 'iglu:legba/critique/jsonschema/1-0-0')
            """,
            uuid4(),
            json.dumps({"analyzed_output_id": str(oid),
                        "overall_score": faithfulness}),
        )
    return oid


async def _delta(
    conn: Any, situation_id: UUID, *, delta: str = tj.DELTA_ESCALATES,
    source_output_id: UUID | None = None, why: str = "the strait was closed",
    state_from: str = tj.STATE_WATCHING, state_to: str = tj.STATE_ESCALATING,
    occurred_at: datetime | None = None,
) -> None:
    event = tj.TrajectoryEvent(
        situation_id=situation_id,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        delta=delta, why=why, state_from=state_from, state_to=state_to,
        derived_from=(() if delta == tj.DELTA_UNCHANGED_CHECKPOINT else (uuid4(),)),
    )
    written = await tj.record_situation_events(
        conn, events=[event],
        source_output_id=source_output_id or await _situation_update(conn),
        # The verdict the actor hands the writer. Delta claims are gated on it;
        # a checkpoint needs none.
        verification={"faithfulness_score": 0.9},
    )
    assert written == 1


# ---------------------------------------------------------------------------
# The composition register (D5)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_register_carries_state_and_dated_deltas(scope):
    pool, target = scope
    async with pool.acquire() as conn:
        sid = await _situation(conn, target_id=target)
        now = datetime.now(timezone.utc)
        for i, d in enumerate(
            [tj.DELTA_BROADENS, tj.DELTA_ESCALATES, tj.DELTA_DE_ESCALATES], 1,
        ):
            await _delta(
                conn, sid, delta=d, occurred_at=now - timedelta(days=4 - i),
                state_from=tj.STATE_WATCHING,
                state_to=(
                    tj.STATE_DE_ESCALATING if d == tj.DELTA_DE_ESCALATES
                    else tj.STATE_ESCALATING
                ),
            )
        register = await mfs.read_open_situations(conn, target_id=target)

    assert [e["situation_id"] for e in register] == [str(sid)]
    entry = register[0]
    assert entry["trajectory_state"] == tj.STATE_DE_ESCALATING
    assert [d["delta"] for d in entry["trajectory"]] == [
        tj.DELTA_DE_ESCALATES, tj.DELTA_ESCALATES, tj.DELTA_BROADENS,
    ]
    assert all(d["occurred_at"] for d in entry["trajectory"])

    # ...and it RENDERS, dated, under the frame line.
    lines = mfs._render_situation_register_lines([entry], 7)
    body = "\n".join(lines)
    assert f"trajectory={tj.STATE_DE_ESCALATING}" in body
    assert "de_escalates: the strait was closed" in body
    # The whole block still fits inside its captured evidence cap, which is what
    # the judge grades a register-backed clause against.
    assert len(body) <= mfs.SITUATION_REGISTER_EVIDENCE_CHARS


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_frame_with_no_ledger_rows_renders_exactly_as_phase_1(scope):
    """Absent, not defaulted: "never assessed" must not read as "steady"."""
    pool, target = scope
    async with pool.acquire() as conn:
        sid = await _situation(conn, target_id=target)
        register = await mfs.read_open_situations(conn, target_id=target)
    assert [e["situation_id"] for e in register] == [str(sid)]
    entry = register[0]
    assert "trajectory_state" not in entry
    assert "trajectory" not in entry
    body = "\n".join(mfs._render_situation_register_lines([entry], 7))
    assert "trajectory=" not in body


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unreadable_ledger_degrades_the_register_never_breaks_it(
    scope, monkeypatch, caplog,
):
    """The continuity gather is an additive enrichment; a compose must never
    fail because its memory was unavailable."""
    pool, target = scope

    async def _boom(*_a, **_kw):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(tj, "read_current_states", _boom)
    async with pool.acquire() as conn:
        sid = await _situation(conn, target_id=target)
        await _delta(conn, sid)
        with caplog.at_level("WARNING"):
            register = await mfs.read_open_situations(conn, target_id=target)

    assert [e["situation_id"] for e in register] == [str(sid)]
    entry = register[0]
    assert entry["name"]                       # the Phase-1 register survives
    assert "trajectory_state" not in entry
    assert any("trajectory.unavailable" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# The situation_escalation alert class (D5)
# ---------------------------------------------------------------------------


def test_the_class_is_registered_in_every_per_class_registry():
    """The registries a new trigger class silently half-lands in."""
    assert ats.TRIGGER_SITUATION_ESCALATION == "situation_escalation"
    assert ats.TRIGGER_SITUATION_ESCALATION in ats.TRIGGER_CLASSES
    assert ats.TRIGGER_SITUATION_ESCALATION in ats._CLASS_PRIORITY
    assert ats.TRIGGER_SITUATION_ESCALATION in ats._UNVERIFIED_REASONS
    assert len(set(ats._CLASS_PRIORITY.values())) == len(ats._CLASS_PRIORITY)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_verified_escalation_pages_once(scope):
    """Seed silently, page the NEXT escalation, never page it twice.

    Every assertion is about THIS test's own situation. The scan is a global
    read over a six-hour window of ``situation_events``, so ``stats["seeded"]``,
    ``stats["paged"]`` and ``stats["already_seen"]`` are counts over whatever
    the suite (and the previous nightly, on the persistent pivot DB) left in
    that window — ``== 1`` was a statement about the whole substrate. The
    scoped form is also the sharper one: ``paged == 1`` was satisfied by a scan
    that paged somebody else's escalation and not ours.
    """
    pool, target = scope

    def _mine(cands: list[Any], situation_id: UUID) -> list[Any]:
        return [c for c in cands if c.data["situation_id"] == str(situation_id)]

    async with pool.acquire() as conn:
        sid = await _situation(conn, intensity=6.0, target_id=target)
        await _delta(conn, sid)
        # First scan SEEDS the standing window without paging.
        cands, silent, seeded, stats = await ses.scan_situation_escalations(conn)
        assert (cands, seeded) == ([], False)
        assert stats["seeded"] >= 1
        mine_silent = [
            (cls, key, state) for cls, key, state in silent
            if state["situation_id"] == str(sid)
        ]
        assert len(mine_silent) == 1, (
            "my standing escalation must be adopted, silently, exactly once"
        )
        for cls, key, state in silent:
            await ats._upsert_watermark(conn, cls, key, state, fired=False)
        await ats._mark_seeded(conn, ses.TRIGGER_CLASS)

        # A NEW escalation pages.
        await _delta(conn, sid, why="a second closure order was issued")
        cands, _s, seeded, stats = await ses.scan_situation_escalations(conn)
        assert seeded is True
        assert stats["candidate_bound_hit"] == 0, (
            "the per-scan bound must not have truncated my candidate away"
        )
        mine = _mine(cands, sid)
        assert len(mine) == 1, "the new escalation pages, once"
        cand = mine[0]
        assert cand.trigger_class == ses.TRIGGER_CLASS
        assert cand.severity == "high"          # intensity 6.0 -> high
        assert cand.target_id == target
        assert "a second closure order was issued" in cand.body
        assert "escalates" == cand.data["delta"]
        # Lineage points at the GRADED claim, not at the ledger pointer.
        assert UUID(cand.data["source_output_id"]) == cand.derived_from[0]

        # Fire-once: the same ledger row never pages twice.
        for cls, key, state in cand.watermarks:
            await ats._upsert_watermark(conn, cls, key, state, fired=True)
        cands, _s, _seeded, stats = await ses.scan_situation_escalations(conn)
        assert _mine(cands, sid) == [], (
            "a watermarked ledger row must never page a second time"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_three_bars_each_refuse_their_own_row(scope):
    """Four rows, each tripping one bar, and none of them reaches a page.

    ``stats["escalations"] == 0`` used to carry the "filters in SQL, not after
    the fact" claim, and it did so by asserting the ENTIRE ledger window was
    empty of qualifying rows — true only on the blank slate the old fixture
    tried to force. The claim itself is per-row, so it is now made per-row: the
    scan's own ``_ESCALATIONS_SQL``, run with the scan's own floors, must not
    RETURN any of these four situations. A post-hoc filter would show up as a
    returned row that failed to become a candidate, which is exactly the
    distinction the original number was reaching for, and this states it
    without a claim about anybody else's rows.
    """
    pool, target = scope
    async with pool.acquire() as conn:
        await ats._mark_seeded(conn, ses.TRIGGER_CLASS)

        # 1. Not an escalation.
        quiet = await _situation(conn, name="Situation: quiet", intensity=6.0,
                                 target_id=target)
        await _delta(conn, quiet, delta=tj.DELTA_BROADENS,
                     state_to=tj.STATE_WATCHING)

        # 2. The source claim never cleared the floor.
        demoted = await _situation(conn, name="Situation: demoted", intensity=6.0,
                                   target_id=target)
        await _delta(conn, demoted,
                     source_output_id=await _situation_update(
                         conn, faithfulness=0.2))
        ungraded = await _situation(conn, name="Situation: ungraded",
                                    intensity=6.0, target_id=target)
        await _delta(conn, ungraded,
                     source_output_id=await _situation_update(
                         conn, faithfulness=None))

        # 3. Below the intensity floor — a one-member frame is noise.
        thin = await _situation(conn, name="Situation: thin", intensity=0.5,
                                target_id=target)
        await _delta(conn, thin)

        mine = {str(quiet), str(demoted), str(ungraded), str(thin)}
        cands, _s, _seeded, _stats = await ses.scan_situation_escalations(conn)
        # The SQL itself refused them — not a filter applied to its output.
        selected = await conn.fetch(
            ses._ESCALATIONS_SQL,
            datetime.now(timezone.utc) - timedelta(hours=ses._LOOKBACK_HOURS),
            ses.DEFAULT_INTENSITY_FLOOR,
            ses.DEFAULT_FLOOR,
            ses._MAX_CANDIDATES * 4,
        )

    assert {str(r["situation_id"]) for r in selected} & mine == set(), (
        "each of the three bars must filter in SQL, not after the fact"
    )
    assert [c for c in cands if c.data["situation_id"] in mine] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_missing_ledger_takes_only_this_class_offline(pool):
    """A not-yet-migrated substrate must never kill the other seven classes."""
    # No outer transaction: asyncpg autocommits each statement, so the scan's
    # failing SELECT cannot poison the connection and the rename-back always
    # runs. (Wrapping this in one transaction leaves it aborted and the table
    # renamed for every test that follows.)
    async with pool.acquire() as conn:
        await conn.execute("ALTER TABLE situation_events RENAME TO _se_hidden")
        try:
            cands, silent, seeded, stats = (
                await ses.scan_situation_escalations(conn)
            )
        finally:
            await conn.execute(
                "ALTER TABLE _se_hidden RENAME TO situation_events"
            )
    assert (cands, silent, seeded) == ([], [], True)
    assert stats["unavailable"] == 1


# ---------------------------------------------------------------------------
# The /v3 route
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api(migrated_pg: PostgresConfig, monkeypatch):
    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()
    identity = SigningIdentity(
        signing_key=SigningKey(b"p2-situation-trajectory-route-01"[:32]),
        signer_did="did:legba:registry:p2-trajectory-test",
    )
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    vault = CredentialVault(pg_store)
    registry = DescriptorRegistry(
        pg_store, vocabulary_cache=vocab, signing_identity=identity,
        audit_logger=audit, dead_letter=dlq,
    )
    await registry.start()
    deps = RegistryAPIDeps(
        descriptor_registry=registry,
        stack_registry=StackRegistry(pg_store, vault, audit=audit, dlq=dlq),
        vault=vault, dlq=dlq, audit_logger=audit, vocabulary_cache=vocab,
        nats_store=None,
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_situation_trajectory_router(deps), prefix="/api/v1/v3")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        yield client, pg_store
    await registry.stop()
    await pg_store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_route_serves_the_ledger_newest_first(api, scope):
    client, store = api
    _pool, target = scope
    async with store.pool.acquire() as conn:
        sid = await _situation(conn, target_id=target)
        now = datetime.now(timezone.utc)
        await _delta(conn, sid, occurred_at=now - timedelta(days=2))
        await _delta(conn, sid, delta=tj.DELTA_DE_ESCALATES,
                     state_from=tj.STATE_ESCALATING,
                     state_to=tj.STATE_DE_ESCALATING,
                     occurred_at=now - timedelta(hours=2),
                     why="the closure order was rescinded")

    resp = await client.get(_ROUTE.format(sid=sid))
    assert resp.status_code == 200
    body = resp.json()
    assert body["measured"] is True
    assert body["state"] == tj.STATE_DE_ESCALATING
    assert [e["delta"] for e in body["events"]] == [
        tj.DELTA_DE_ESCALATES, tj.DELTA_ESCALATES,
    ]
    assert body["events"][0]["why"] == "the closure order was rescinded"
    assert body["events"][0]["source_output_id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_situation_404s_but_an_empty_ledger_is_a_200_null_state(
    api, scope,
):
    """The distinction the route exists to preserve."""
    client, store = api
    _pool, target = scope
    assert (await client.get(_ROUTE.format(sid=uuid4()))).status_code == 404

    async with store.pool.acquire() as conn:
        sid = await _situation(conn, name="Situation: never assessed",
                               target_id=target)
    resp = await client.get(_ROUTE.format(sid=sid))
    assert resp.status_code == 200
    body = resp.json()
    assert body["events"] == []
    assert body["state"] is None, (
        "a fabricated 'watching' would erase the difference between "
        "'never assessed' and 'assessed and steady'"
    )
    assert body["measured"] is True
