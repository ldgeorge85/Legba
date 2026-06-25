# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A-2 (review G3): the media/derived-signal loop CLOSES.

Real dev-rig NATS JetStream + real migrated Postgres. Proves the three legs of
the close:

  1. **Re-entry into fan-out** — a completed ``process_media`` job publishes
     its landed derived signal onto the SAME ``legba_signals`` stream
     (``event_class="derived"``); the P-10 :class:`TriggerEngine` consumes it,
     re-checks the binding's structured filter, and MARKS THE (analyst, target)
     PAIR DIRTY in the durable trigger state.
  2. **Scope inheritance** — ``build_derived_signal`` inherits the parent's
     geo / tags / entity_classes / language, so a geo-scoped target's batch
     read-slice (SQL ``WHERE``) includes the derived row.
  3. **No-stub refusals** — with no real ``LEGBA_MEDIA_API_URL``: enqueue
     (agency tool) refuses, execution (worker/handler) refuses terminally with
     no row written, and a worker without the subscription engine refuses (a
     derived signal must never land without re-entering fan-out).

The only mock is the httpx ``MockTransport`` standing in for the HOSTED media
endpoint (test boundary — the production HTTP path runs for real).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
import pytest

from legba.data.jobs.envelope import JobEnvelope, JobResult
from legba.data.schemas.source import Subscription
from legba.runtime.jobs.media_client import MediaClient
from legba.runtime.jobs.worker import JobWorkerPool
from legba.runtime.subscription.engine import SubscriptionEngine
from legba.runtime.subscription.subjects import (
    ResolvedBinding,
    subject_filters_for,
)
from legba.runtime.triggers.coalescer import Coalescer
from legba.runtime.triggers.dispatch import (
    DeterministicTriggerRunner,
    TriggerFire,
)
from legba.runtime.triggers.engine import TriggerEngine, TriggerRegistration
from legba.runtime.triggers.policy import TriggerPolicy
from legba.runtime.triggers.state import TriggerStateStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_parent(
    pg,
    *,
    source_id: str,
    tenant: str = "default",
    geo: list[str] | None = None,
    tags: list[str] | None = None,
    entity_classes: list[str] | None = None,
    language: str | None = "pt",
) -> UUID:
    """A raw parent signal carrying structured-filter columns to inherit."""
    sid = uuid4()
    async with pg.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO signals (
                id, source_id, source_version, produced_by_kind, owner_tenant,
                modality, mime_type, media_ref, payload, content_hash,
                derived_from, geo, tags, entity_classes, language
            ) VALUES (
                $1, $2, 'v1', 'source', $3,
                'audio', 'audio/mpeg', $4, $5::jsonb, $6,
                '{}'::uuid[], $7::text[], $8::text[], $9::text[], $10
            )
            """,
            sid, source_id, tenant, "https://cdn.example/clip-a2.mp3",
            json.dumps({"title": "loop-close parent"}), f"hash-{sid}",
            list(geo or []), list(tags or []), list(entity_classes or []),
            language,
        )
    return sid


def _media_env(raw_id: UUID, *, idem: str | None = None) -> JobEnvelope:
    return JobEnvelope(
        job_kind="process_media",
        requested_by="analyst.loop",
        budget_account="analyst.loop",
        idempotency_key=idem or f"a2-{uuid4().hex}",
        input_refs={
            "media_ref": "https://cdn.example/clip-a2.mp3",
            "extraction": "transcribe",
            "derived_from": str(raw_id),
            "modality": "audio",
            "language_hint": "pt",
        },
    )


def _hosted_media() -> tuple[MediaClient, httpx.AsyncClient]:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"text": f"hosted transcript of {body['media_ref']}",
                  "model": "hosted-test-model"},
        )

    ac = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://hosted.test",
    )
    return MediaClient(endpoint="http://hosted.test", client=ac), ac


# ---------------------------------------------------------------------------
# 1) Derived signal re-enters fan-out and marks the trigger pair dirty.
# ---------------------------------------------------------------------------


async def test_derived_signal_reenters_fanout_and_marks_trigger_pair_dirty(
    job_pg, job_queue, job_nats,
):
    source_id = f"source.loop.{uuid4().hex[:8]}"
    analyst_id = "analyst.loop.assessor"
    target_id = f"target.loop.{uuid4().hex[:8]}"
    tenant = "default"

    raw_id = await _insert_parent(
        job_pg, source_id=source_id, geo=["BR"], tags=["g20"],
    )

    # One (analyst, target) registration over a tag-scoped binding. The
    # derived signal only matches because it INHERITS the parent's tags.
    binding = ResolvedBinding(
        source_id=source_id, owner_tenant=tenant,
        subscription=Subscription(tags=["g20"]), via_selector=False,
    )
    reg = TriggerRegistration(
        analyst_id=analyst_id, target_id=target_id, tenant=tenant,
        bindings=[binding], subject_filters=subject_filters_for([binding]),
    )

    state = TriggerStateStore(job_pg.pool)
    await state.ensure_schema()

    fires: list[TriggerFire] = []

    async def _work(fire: TriggerFire) -> dict:
        fires.append(fire)
        return {}

    # accumulation_threshold=2 → ONE derived signal marks the pair DIRTY
    # without firing, so the dirty state itself is observable.
    policy = TriggerPolicy(accumulation_threshold=2)
    coalescer = Coalescer(
        state=state, runner=DeterministicTriggerRunner(_work),
        policy_for=lambda a, t: policy,
    )
    trigger_engine = TriggerEngine(
        nats=job_nats, coalescer=coalescer,
        durable=f"legba-trigger-a2-{uuid4().hex[:8]}", fetch_timeout=1.0,
    )
    trigger_engine.register(reg)

    subs = SubscriptionEngine(job_pg, nats=job_nats)
    await subs.ensure_signal_stream()
    # deliver_policy=new → bind BEFORE the job runs/publishes.
    await trigger_engine.ensure_consumer()
    await trigger_engine.bind()

    media, ac = _hosted_media()
    try:
        pool = JobWorkerPool(
            queue=job_queue, pg=job_pg, size=1, media=media,
            subscriptions=subs,
        )
        env = _media_env(raw_id)
        await job_queue.enqueue(env)
        completed = await pool.drain_until_empty()
        assert completed == 1

        # The job's ledger result records the derived-class publish subject.
        async with job_pg.acquire() as conn:
            ledger = await conn.fetchrow(
                "SELECT result FROM legba_jobs WHERE idempotency_key=$1",
                env.idempotency_key,
            )
        res = JobResult.from_json_row(ledger["result"])
        assert res.output_refs["published_subject"].endswith(".derived")

        # The trigger engine consumes the published DERIVED signal, the
        # structured re-check matches (inherited tags) and the pair goes dirty.
        for _ in range(10):
            await trigger_engine.drain_once()
            if trigger_engine.matched >= 1:
                break
        assert trigger_engine.delivered >= 1, "derived signal not delivered"
        assert trigger_engine.matched == 1, (
            "derived signal did not match the tag-scoped binding "
            "(tags not inherited?)"
        )
        assert trigger_engine.fired == 0    # below threshold — dirty, not fired
        dirty = await state.list_dirty()
        assert (analyst_id, target_id, tenant) in dirty, (
            f"(analyst, target) pair not marked dirty; dirty set = {dirty}"
        )
        assert not fires
    finally:
        await ac.aclose()
        try:
            await job_nats.js.delete_consumer(
                trigger_engine._stream, trigger_engine._durable,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 2) Geo-scoped slice includes the derived row (geo inherited).
# ---------------------------------------------------------------------------


async def test_geo_scoped_slice_includes_derived_row(job_pg, job_queue):
    source_id = f"source.geo.{uuid4().hex[:8]}"
    raw_id = await _insert_parent(
        job_pg, source_id=source_id, geo=["BR"], tags=["g20"],
        entity_classes=["org"], language="pt",
    )

    media, ac = _hosted_media()
    pool = JobWorkerPool(
        queue=job_queue, pg=job_pg, size=1, media=media,
        subscriptions=SubscriptionEngine(job_pg),   # no NATS — slice test only
    )
    await job_queue.enqueue(_media_env(raw_id))
    completed = await pool.drain_until_empty()
    await ac.aclose()
    assert completed == 1

    # The derived row inherited the parent's structured-filter columns.
    async with job_pg.acquire() as conn:
        d = await conn.fetchrow(
            "SELECT id, geo, tags, entity_classes, language FROM signals "
            "WHERE produced_by_kind='job' AND $1 = ANY(derived_from)",
            raw_id,
        )
    assert d is not None
    assert list(d["geo"]) == ["BR"]
    assert list(d["tags"]) == ["g20"]
    assert list(d["entity_classes"]) == ["org"]
    assert d["language"] == "pt"

    # A geo-scoped binding's batch read-slice (SQL WHERE) includes BOTH the
    # raw parent AND the derived row — the G3 gap was the derived row falling
    # out of exactly this slice.
    engine = SubscriptionEngine(job_pg)
    binding = ResolvedBinding(
        source_id=source_id, owner_tenant="default",
        subscription=Subscription(geo=["BR"]), via_selector=False,
    )
    rows = await engine.read_slice(binding)
    ids = {str(r["id"]) for r in rows}
    assert str(raw_id) in ids
    assert str(d["id"]) in ids, "geo-scoped slice excluded the derived row"


# ---------------------------------------------------------------------------
# 3) No real LEGBA_MEDIA_API_URL → refuse loudly everywhere; no stub lands.
# ---------------------------------------------------------------------------


async def test_enqueue_refused_without_media_endpoint(monkeypatch):
    """The agency tool refuses to ENQUEUE when no media endpoint is configured."""
    from legba.data.schemas.action_pack import ActionPack
    from legba.data.analysts.agency import ToolCall, ToolContext
    from legba.data.analysts.agency.tools import process_media_tool

    monkeypatch.delenv("LEGBA_MEDIA_API_URL", raising=False)

    pack = ActionPack.model_validate({
        "identity": {
            "id": "media_processing", "name": "media_processing",
            "schema_uri": "legba/action_pack/1.0.0", "version": "a" * 16,
            "state": "active", "owner": "a2_test",
            "created": datetime.now(timezone.utc).isoformat(),
        },
        "tools": [{"name": "process_media"}],
        "applies_to_tags": ["media"],
    }, strict=False)

    class _RecordingQueue:
        def __init__(self):
            self.enqueued = []

        async def enqueue(self, env):
            self.enqueued.append(env)

    queue = _RecordingQueue()
    call = ToolCall(
        pack_id="media_processing", tool_name="process_media",
        budget_account="acct-a2", requested_by="analyst.a2",
        args={"media_ref": "https://cdn.example/x.mp3",
              "extraction": "transcribe", "derived_from": str(uuid4())},
    )
    result = await process_media_tool(call, pack, ToolContext(queue=queue))

    assert result.status == "failed"
    assert "LEGBA_MEDIA_API_URL" in (result.error or "")
    assert queue.enqueued == [], "a doomed job must not be enqueued"


async def test_execution_refused_without_media_endpoint(job_pg, job_queue):
    """A worker with no media endpoint REFUSES terminally — no row written."""
    source_id = f"source.refuse.{uuid4().hex[:8]}"
    raw_id = await _insert_parent(job_pg, source_id=source_id)

    env = _media_env(raw_id)
    pool = JobWorkerPool(
        queue=job_queue, pg=job_pg, size=1,
        media=MediaClient(endpoint=None),           # nothing configured
        subscriptions=SubscriptionEngine(job_pg),
    )
    await job_queue.enqueue(env)
    completed = await pool.drain_until_empty()
    assert completed == 0

    async with job_pg.acquire() as conn:
        ledger = await conn.fetchrow(
            "SELECT status, result FROM legba_jobs WHERE idempotency_key=$1",
            env.idempotency_key,
        )
        n_rows = await conn.fetchval(
            "SELECT COUNT(*) FROM signals "
            "WHERE produced_by_kind='job' AND $1 = ANY(derived_from)",
            raw_id,
        )
    assert ledger["status"] == "failed"
    res = JobResult.from_json_row(ledger["result"])
    assert "LEGBA_MEDIA_API_URL" in (res.error or "")
    assert n_rows == 0, "no stub output may land in the pool"


async def test_execution_refused_without_subscription_engine(job_pg, job_queue):
    """No subscription engine wired → refuse BEFORE landing anything.

    A derived signal that lands but never re-enters fan-out is exactly the
    half-state review finding G3 flagged — the handler refuses it outright.
    """
    source_id = f"source.nosubs.{uuid4().hex[:8]}"
    raw_id = await _insert_parent(job_pg, source_id=source_id)

    media, ac = _hosted_media()
    env = _media_env(raw_id)
    pool = JobWorkerPool(
        queue=job_queue, pg=job_pg, size=1, media=media,
        subscriptions=None,
    )
    await job_queue.enqueue(env)
    completed = await pool.drain_until_empty()
    await ac.aclose()
    assert completed == 0

    async with job_pg.acquire() as conn:
        ledger = await conn.fetchrow(
            "SELECT status, result FROM legba_jobs WHERE idempotency_key=$1",
            env.idempotency_key,
        )
        n_rows = await conn.fetchval(
            "SELECT COUNT(*) FROM signals "
            "WHERE produced_by_kind='job' AND $1 = ANY(derived_from)",
            raw_id,
        )
    assert ledger["status"] == "failed"
    res = JobResult.from_json_row(ledger["result"])
    assert "subscription engine" in (res.error or "")
    assert n_rows == 0
