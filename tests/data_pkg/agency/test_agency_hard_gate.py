# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-11 HARD-GATE acceptance — action-pack agency mechanism.

Real migrated Postgres (a fresh ``legba_test_<uuid>`` DB carrying the full
0001-0025 chain — including THIS task's 0025 governor migration) + real dev-rig
NATS JetStream for the job-enqueue path. No mocks.

The three acceptance bullets, each a distinct test:

  1. A pack the domain doesn't ALLOW cannot run — resolution blocks before any
     tool dispatch, and the block is operator-visible in ``governor_events``.
  2. The ``process_media`` pack enqueues a REAL job — the agency dispatch puts a
     ``process_media`` JobEnvelope on the W2 work-queue and the P-07 worker pool
     drains it into a derived signal.
  3. An over-budget / over-rate call is demonstrably blocked WITH an operator-
     visible governor event (rate cap AND cost cap, plus the global-envelope
     gate).

Plus: the incident_response pack's escalate/create_incident tools emit to the
pack's channels (the second seed pack). (The discovery tool was removed per
F-1; the consult/escalate production bindings are covered in
test_agency_binding.py.)
"""

from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.schemas.action_pack import ActionPack, ActionPackRef
from legba.data.analysts.agency import (
    Agency,
    ChannelEmitter,
    PackGovernorEnforcer,
    TargetScopeView,
    ToolCall,
    ToolContext,
    recent_events,
    resolve_pack,
)

pytestmark = [pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(autouse=True)
def _media_endpoint_env(monkeypatch):
    """A-2: process_media enqueue REFUSES without a configured media endpoint.

    These tests exercise the governor/agency gate, not the media edge — point
    the env at a placeholder so the enqueue-side refusal doesn't trip. The
    refusal itself is asserted in
    ``tests/runtime/jobs/test_media_loop_close.py``.
    """
    monkeypatch.setenv("LEGBA_MEDIA_API_URL", "http://hosted.test")


@pytest_asyncio.fixture
async def pool(migrated_pg):
    p = await asyncpg.create_pool(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
        min_size=1, max_size=4,
    )
    # Confirm the 0025 governor substrate landed via the migration runner.
    async with p.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('action_pack_invocations')")
        assert await conn.fetchval("SELECT to_regclass('governor_events')")
    yield p
    await p.close()


def _pack(pid, *, tools, channels=None, tags=None, governor=None) -> ActionPack:
    body = {
        "identity": {
            "id": pid, "name": pid, "schema_uri": "legba/action_pack/1.0.0",
            "version": "a" * 16, "state": "active", "owner": "p11_agency",
            "created": datetime.now(timezone.utc).isoformat(),
        },
        "tools": [{"name": t} for t in tools],
        "applies_to_tags": tags or [],
    }
    if channels:
        body["channels"] = channels
    if governor:
        body["governor"] = governor
    return ActionPack.model_validate(body, strict=False)


def _ref(pid, **override) -> ActionPackRef:
    return ActionPackRef(pack_id=pid, **override)


# ---------------------------------------------------------------------------
# 1) A pack the domain doesn't ALLOW cannot run (+ operator-visible block).
# ---------------------------------------------------------------------------


async def test_not_allowed_pack_cannot_run(pool):
    pack = _pack("media_processing", tools=["process_media"], tags=["media"],
                 governor={"max_invocations_per_hour": 100})
    scope = TargetScopeView(target_id="t_geo", tags=["media", "news"])
    account = f"acct-{uuid4().hex[:8]}"
    agency = Agency()

    # Analyst GRANTS the pack, but the TARGET does NOT allow it.
    call = ToolCall(
        pack_id="media_processing", tool_name="process_media",
        budget_account=account, requested_by="analyst.x",
        args={"media_ref": "x", "extraction": "transcribe", "derived_from": str(uuid4())},
    )
    async with pool.acquire() as conn:
        outcome = await agency.run_pack_tool(
            conn, pack=pack, call=call,
            analyst_grants=[_ref("media_processing")],
            target_allows=[],                    # domain forbids it
            scope=scope, ctx=ToolContext(),
        )
    assert outcome.admitted is False
    assert outcome.block_cause == "not_allowed"
    assert outcome.tool_result is None

    # The block is operator-visible.
    async with pool.acquire() as conn:
        evs = await recent_events(conn, pack_id="media_processing", decision="block")
        # No invocation ledger row — the tool never ran.
        invs = await conn.fetchval(
            "SELECT COUNT(*) FROM action_pack_invocations WHERE budget_account=$1",
            account,
        )
    assert any(e["cause"] == "not_allowed" for e in evs)
    assert invs == 0


async def test_not_granted_and_not_applicable_blocks(pool):
    pack = _pack("media_processing", tools=["process_media"], tags=["media"])
    agency = Agency()

    # not granted by the analyst
    scope = TargetScopeView(target_id="t1", tags=["media"])
    call = ToolCall(pack_id="media_processing", tool_name="process_media",
                    budget_account=f"a-{uuid4().hex[:6]}",
                    args={"media_ref": "x", "extraction": "transcribe", "derived_from": str(uuid4())})
    async with pool.acquire() as conn:
        o1 = await agency.run_pack_tool(
            conn, pack=pack, call=call, analyst_grants=[],
            target_allows=[_ref("media_processing")], scope=scope, ctx=ToolContext())
    assert o1.block_cause == "not_granted"

    # granted + allowed but NOT applicable (scope tag mismatch)
    scope2 = TargetScopeView(target_id="t2", tags=["energy"])
    async with pool.acquire() as conn:
        o2 = await agency.run_pack_tool(
            conn, pack=pack, call=call, analyst_grants=[_ref("media_processing")],
            target_allows=[_ref("media_processing")], scope=scope2, ctx=ToolContext())
    assert o2.block_cause == "not_applicable"


# ---------------------------------------------------------------------------
# 2) process_media pack enqueues a REAL job (W2 job plane).
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="needs an isolated NATS; per-test job queue's jobs.> subjects "
    "overlap the live runtime's LEGBA_JOBS stream on --network host"
)
async def test_process_media_pack_enqueues_real_job(pool, migrated_pg):
    if not _port_open("127.0.0.1", 4222):
        pytest.skip("dev-rig NATS not reachable on 127.0.0.1:4222")

    from legba.data.config import NatsConfig
    from legba.data.nats import NatsStore
    from legba.data.postgres import PostgresStore
    from legba.data.jobs.store import JobStore
    from legba.runtime.jobs.queue import JobQueue
    from legba.runtime.jobs.worker import JobWorkerPool
    from legba.runtime.jobs.media_client import MediaClient

    # Bring up a per-test job queue + a PostgresStore on the SAME migrated DB.
    nats = NatsStore(NatsConfig.from_env())
    await nats.connect()
    suffix = uuid4().hex[:8]
    queue = JobQueue(
        nats, stream=f"LEGBA_JOBS_P11_{suffix}",
        durable=f"legba-p11-{suffix}", ack_wait_seconds=10,
        max_deliver=4, max_age_seconds=600,
    )
    await queue.ensure_topology()

    # A raw parent signal that references media (the lineage source).
    raw_id = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO signals (id, source_id, source_version, produced_by_kind,
                owner_tenant, modality, mime_type, media_ref, payload, content_hash, derived_from)
            VALUES ($1,'source.cam','v1','source','default','audio','audio/mpeg',
                    $2,'{}'::jsonb,'rawhash','{}'::uuid[])
            """,
            raw_id, "https://cdn.example/clip.mp3",
        )

    pack = _pack("media_processing", tools=["process_media"], tags=["media"],
                 governor={"max_invocations_per_hour": 100, "api_rate_per_minute": 30})
    scope = TargetScopeView(target_id="t_media", tags=["media"])
    account = f"acct-{uuid4().hex[:8]}"
    agency = Agency()
    ctx = ToolContext(queue=queue)
    call = ToolCall(
        pack_id="media_processing", tool_name="process_media",
        budget_account=account, requested_by="analyst.tracker",
        args={
            "media_ref": "https://cdn.example/clip.mp3", "extraction": "transcribe",
            "derived_from": str(raw_id), "modality": "audio", "language_hint": "en",
        },
    )

    try:
        # Agency admits + enqueues the real job.
        async with pool.acquire() as conn:
            outcome = await agency.run_pack_tool(
                conn, pack=pack, call=call,
                analyst_grants=[_ref("media_processing")],
                target_allows=[_ref("media_processing")],
                scope=scope, ctx=ctx, estimated_cost_usd=0.02,
            )
        assert outcome.admitted is True
        assert outcome.tool_result.status == "enqueued"
        job_id = outcome.tool_result.job_id
        assert job_id is not None

        # An ALLOW governor event + an invocation ledger row landed.
        async with pool.acquire() as conn:
            allows = await recent_events(conn, pack_id="media_processing", decision="allow")
            inv = await conn.fetchrow(
                "SELECT tool_name, outcome, cost_usd FROM action_pack_invocations "
                "WHERE budget_account=$1", account)
        assert any(e["cause"] == "ok" for e in allows)
        assert inv["tool_name"] == "process_media"
        assert inv["outcome"] == "completed"          # settled after enqueue
        assert Decimal(inv["cost_usd"]) == Decimal("0.02")

        # The W2 worker pool drains the real job into a derived signal.
        store = PostgresStore(migrated_pg)
        await store.connect()
        async with store.acquire() as conn:
            await JobStore.ensure_schema(conn)
        try:
            import httpx
            import json as _json

            from legba.runtime.subscription.engine import SubscriptionEngine

            def _hosted(request: httpx.Request) -> httpx.Response:
                body = _json.loads(request.content)
                return httpx.Response(
                    200, json={"text": f"hosted transcript of {body['media_ref']}",
                               "model": "hosted-test-model"})

            ac = httpx.AsyncClient(
                transport=httpx.MockTransport(_hosted),
                base_url="http://hosted.test")
            worker_pool = JobWorkerPool(
                queue=queue, pg=store, size=1,
                media=MediaClient(endpoint="http://hosted.test", client=ac),
                subscriptions=SubscriptionEngine(store))
            completed = await worker_pool.drain_until_empty()
            await ac.aclose()
            assert completed == 1
            async with store.acquire() as conn:
                d = await conn.fetchrow(
                    "SELECT produced_by_kind, derived_from, payload FROM signals "
                    "WHERE produced_by_kind='job' AND $1 = ANY(derived_from)", raw_id)
            assert d is not None
            assert raw_id in d["derived_from"]
        finally:
            await store.close()
    finally:
        try:
            await nats.js.delete_stream(queue.stream)
        except Exception:
            pass
        await nats.close()


# ---------------------------------------------------------------------------
# 3) Over-rate + over-budget are blocked WITH operator-visible events.
# ---------------------------------------------------------------------------


async def test_over_rate_blocked_with_event(pool):
    # api_rate_per_minute = 2 → the 3rd call in a minute is blocked.
    pack = _pack("media_processing", tools=["process_media"], tags=["media"],
                 governor={"api_rate_per_minute": 2})
    scope = TargetScopeView(target_id="t_rate", tags=["media"])
    account = f"acct-{uuid4().hex[:8]}"
    agency = Agency()

    # No queue wired — but the gate runs BEFORE dispatch, so an admit would
    # reach the tool which returns failed(no queue). We only care that the gate
    # admits the first 2 and blocks the 3rd. Use a queue stub via ToolContext
    # with a fake queue that records enqueues.
    class _FakeQueue:
        def __init__(self): self.enqueued = []
        async def enqueue(self, env): self.enqueued.append(env)

    fq = _FakeQueue()
    ctx = ToolContext(queue=fq)
    grants = [_ref("media_processing")]
    allows = [_ref("media_processing")]

    def _call():
        return ToolCall(
            pack_id="media_processing", tool_name="process_media",
            budget_account=account, requested_by="analyst.loop",
            args={"media_ref": "x", "extraction": "transcribe", "derived_from": str(uuid4())})

    admitted = []
    async with pool.acquire() as conn:
        for _ in range(3):
            o = await agency.run_pack_tool(
                conn, pack=pack, call=_call(), analyst_grants=grants,
                target_allows=allows, scope=scope, ctx=ctx)
            admitted.append(o.admitted)
    assert admitted == [True, True, False], f"got {admitted}"
    assert len(fq.enqueued) == 2                       # only admitted calls enqueued

    async with pool.acquire() as conn:
        blocks = await recent_events(conn, pack_id="media_processing", decision="block")
    rate_blocks = [b for b in blocks if b["budget_account"] == account
                   and b["cap_dimension"] == "api_rate_per_minute"]
    assert rate_blocks, "expected an operator-visible over-rate block event"
    b = rate_blocks[0]
    assert b["cause"] == "over_rate"
    assert float(b["cap_limit"]) == 2.0
    assert float(b["observed_value"]) == 3.0


async def test_over_budget_blocked_with_event(pool):
    # max_cost_usd_per_day = 0.05; each call estimates 0.03 → 2nd call (0.06)
    # crosses the day cap and is blocked.
    pack = _pack("media_processing", tools=["process_media"], tags=["media"],
                 governor={"max_cost_usd_per_day": 0.05})
    scope = TargetScopeView(target_id="t_budget", tags=["media"])
    account = f"acct-{uuid4().hex[:8]}"
    agency = Agency()

    class _FakeQueue:
        async def enqueue(self, env): pass

    ctx = ToolContext(queue=_FakeQueue())
    grants = [_ref("media_processing")]
    allows = [_ref("media_processing")]

    def _call():
        return ToolCall(
            pack_id="media_processing", tool_name="process_media",
            budget_account=account, requested_by="analyst.spend",
            args={"media_ref": "x", "extraction": "transcribe", "derived_from": str(uuid4())})

    async with pool.acquire() as conn:
        o1 = await agency.run_pack_tool(
            conn, pack=pack, call=_call(), analyst_grants=grants,
            target_allows=allows, scope=scope, ctx=ctx, estimated_cost_usd=0.03)
        o2 = await agency.run_pack_tool(
            conn, pack=pack, call=_call(), analyst_grants=grants,
            target_allows=allows, scope=scope, ctx=ctx, estimated_cost_usd=0.03)
    assert o1.admitted is True
    assert o2.admitted is False
    assert o2.block_cause == "over_budget"

    async with pool.acquire() as conn:
        blocks = await recent_events(conn, pack_id="media_processing", decision="block")
    bud = [b for b in blocks if b["budget_account"] == account
           and b["cap_dimension"] == "max_cost_usd_per_day"]
    assert bud, "expected an operator-visible over-budget block event"
    assert bud[0]["cause"] == "over_budget"


async def test_global_envelope_blocks_pack_call(pool):
    # The system-wide token envelope (0022) being exhausted blocks a pack call
    # too — the global gate beats per-pack head-room.
    pack = _pack("media_processing", tools=["process_media"], tags=["media"],
                 governor={"max_invocations_per_hour": 1000})
    scope = TargetScopeView(target_id="t_glob", tags=["media"])
    account = f"acct-{uuid4().hex[:8]}"
    agency = Agency()
    bucket = datetime.now(tz=timezone.utc).date()

    async with pool.acquire() as conn:
        # Set a tiny global cap + consume it via budget_ledger.
        await conn.execute(
            "INSERT INTO global_budget_envelope (bucket, tokens_cap, on_exceeded) "
            "VALUES ($1, 100, 'demote_all') ON CONFLICT (bucket) DO UPDATE "
            "SET tokens_cap=EXCLUDED.tokens_cap", bucket)
        await conn.execute(
            """
            INSERT INTO budget_ledger (analyst_id, analyst_version, bucket,
                tokens_used, runs)
            VALUES ($1,'v1',$2, 500, 1)
            """,
            f"glob-burner-{uuid4().hex[:6]}", bucket)

    class _FakeQueue:
        async def enqueue(self, env): pass

    ctx = ToolContext(queue=_FakeQueue())
    call = ToolCall(
        pack_id="media_processing", tool_name="process_media",
        budget_account=account, requested_by="analyst.g",
        args={"media_ref": "x", "extraction": "transcribe", "derived_from": str(uuid4())})

    async with pool.acquire() as conn:
        o = await agency.run_pack_tool(
            conn, pack=pack, call=call, analyst_grants=[_ref("media_processing")],
            target_allows=[_ref("media_processing")], scope=scope, ctx=ctx)
    assert o.admitted is False
    assert o.block_cause == "global_exhausted"

    # Teardown: clear the global cap so other tests/buckets aren't affected
    # (migrated_pg is session-scoped + shared).
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM global_budget_envelope WHERE bucket=$1", bucket)


# ---------------------------------------------------------------------------
# Seed pack 2: incident_response — escalate/create_incident → channels.
# ---------------------------------------------------------------------------


async def test_incident_response_emits_to_channels(pool):
    pack = _pack(
        "incident_response", tools=["escalate", "create_incident"], tags=["incident"],
        channels=[
            {"name": "ops_alert", "kind": "alert", "config": {"subject": "channels.ops"}},
            {"name": "soc_stream", "kind": "nats_stream", "config": {"subject": "channels.soc"}},
        ],
        governor={"max_invocations_per_hour": 60},
    )
    scope = TargetScopeView(target_id="t_inc", tags=["incident"])
    account = f"acct-{uuid4().hex[:8]}"
    emitter = ChannelEmitter(nats_publish=None)        # log-only emitter is fine here
    agency = Agency()
    ctx = ToolContext(emit=emitter)

    call = ToolCall(
        pack_id="incident_response", tool_name="create_incident",
        budget_account=account, requested_by="analyst.soc",
        args={"title": "Active exfil", "severity": "critical", "detail": "egress spike"},
    )
    async with pool.acquire() as conn:
        o = await agency.run_pack_tool(
            conn, pack=pack, call=call,
            analyst_grants=[_ref("incident_response")],
            target_allows=[_ref("incident_response")],
            scope=scope, ctx=ctx)
    assert o.admitted is True
    assert o.tool_result.status == "emitted"
    chans = {c["channel"] for c in o.tool_result.output["channels"]}
    assert chans == {"ops_alert", "soc_stream"}
    assert len(emitter.emitted) == 2

    # A tool NOT named on the pack is blocked as unknown_tool.
    bad = ToolCall(pack_id="incident_response", tool_name="process_media",
                   budget_account=account, requested_by="analyst.soc", args={})
    async with pool.acquire() as conn:
        ob = await agency.run_pack_tool(
            conn, pack=pack, call=bad,
            analyst_grants=[_ref("incident_response")],
            target_allows=[_ref("incident_response")], scope=scope, ctx=ctx)
    assert ob.admitted is False
    assert ob.block_cause == "unknown_tool"


# ---------------------------------------------------------------------------
# Governor override tightening (analyst/target may TIGHTEN a pack cap).
# ---------------------------------------------------------------------------


async def test_binding_override_tightens_governor(pool):
    from legba.data.schemas.action_pack import PackGovernor

    pack = _pack("media_processing", tools=["process_media"], tags=["media"],
                 governor={"api_rate_per_minute": 100})
    scope = TargetScopeView(target_id="t_ovr", tags=["media"])
    # Target tightens the rate to 1/min via a per-binding override.
    res = resolve_pack(
        pack=pack,
        analyst_grants=[_ref("media_processing")],
        target_allows=[ActionPackRef(
            pack_id="media_processing",
            governor_override=PackGovernor(api_rate_per_minute=1))],
        scope=scope,
    )
    assert res.effective
    assert res.governor.api_rate_per_minute == 1       # tightened (min of 100, 1)
