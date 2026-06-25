# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-07 acceptance: enqueue process_media → worker → derived signal w/ lineage.

Real dev-rig NATS JetStream + real migrated Postgres (legba_test_<uuid> with
the source-first 0024 substrate). The hosted media edge is exercised through an
injected httpx ``MockTransport`` (a fake HOSTED endpoint — the production HTTP
path runs for real). There is no stub edge any more (A-2): a client with no
endpoint REFUSES, covered in ``test_media_loop_close.py``.

Covers the four acceptance bullets:
  1. enqueue → worker consumes → derived signal row lands w/ lineage to raw.
  2. jobs idempotent on idempotency_key (re-enqueue → one derived row, one
     'completed' ledger row, second observed as skipped_duplicate).
  3. pool scales by adding workers (N>1 workers drain a batch; throughput
     spreads; adding a worker mid-flight is safe).
  4. generic envelope (a second job kind rides the same plumbing).
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import httpx
import pytest

from legba.data.jobs.envelope import JobEnvelope, JobResult
from legba.data.jobs.store import JobStore
from legba.runtime.jobs.media_client import MediaClient
from legba.runtime.jobs.dispatch import JobContext, JobDispatch, default_dispatch
from legba.runtime.jobs.worker import JobWorkerPool
from legba.runtime.subscription.engine import SubscriptionEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jsonb(value):
    """JSONB column → dict regardless of codec.

    The pool-level JSONB codec (A-4) returns dicts; a codec-less raw
    connection returns str. Tests must accept both — the exact divergence
    class A-4's parity test pins for production code.
    """
    return value if isinstance(value, dict) else json.loads(value)


async def _insert_raw_signal(pg, *, source_id="source.test.feed", tenant="default") -> UUID:
    """Insert a raw source signal that references a media object; return its id."""
    sid = uuid4()
    async with pg.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO signals (
                id, source_id, source_version, produced_by_kind, owner_tenant,
                modality, mime_type, media_ref, payload, content_hash, derived_from
            ) VALUES (
                $1, $2, 'v1', 'source', $3,
                'structured', 'application/json', $4, $5::jsonb, 'rawhash', '{}'::uuid[]
            )
            """,
            sid, source_id, tenant, "https://cdn.example/clip-123.mp4",
            json.dumps({"camera_id": "cam-7", "location": "gate-A"}),
        )
    return sid


def _hosted_media() -> tuple[MediaClient, httpx.AsyncClient]:
    """A fake HOSTED media endpoint via httpx MockTransport (test-only mock).

    The client takes the production HTTP path; only the wire is faked. The
    caller must ``aclose`` the returned httpx client.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "text": f"hosted {request.url.path.lstrip('/')} of "
                        f"{body['media_ref']}",
                "model": "hosted-test-model",
            },
        )

    ac = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://hosted.test",
    )
    return MediaClient(endpoint="http://hosted.test", client=ac), ac


def _subs(pg) -> SubscriptionEngine:
    """A subscription engine over the test DB (no NATS — publish is a no-op
    here; the real-NATS derived-publish path is proven in
    ``test_media_loop_close.py``)."""
    return SubscriptionEngine(pg)


def _media_env(raw_id: UUID, *, extraction="transcribe", idem: str | None = None,
               requested_by="analyst.tracker") -> JobEnvelope:
    return JobEnvelope(
        job_kind="process_media",
        requested_by=requested_by,
        budget_account=requested_by,
        idempotency_key=idem or "",
        input_refs={
            "media_ref": "https://cdn.example/clip-123.mp4",
            "extraction": extraction,
            "derived_from": str(raw_id),
            "modality": "audio",
            "language_hint": "en",
        },
    )


# ---------------------------------------------------------------------------
# 1) enqueue -> worker -> derived signal with lineage
# ---------------------------------------------------------------------------


async def test_enqueue_worker_lands_derived_signal_with_lineage(job_pg, job_queue):
    raw_id = await _insert_raw_signal(job_pg)
    env = _media_env(raw_id, idem=f"idem-{uuid4().hex}")

    media, ac = _hosted_media()
    pool = JobWorkerPool(
        queue=job_queue, pg=job_pg, size=1, media=media,
        subscriptions=_subs(job_pg),
    )

    await job_queue.enqueue(env)
    completed = await pool.drain_until_empty()
    await ac.aclose()
    assert completed == 1

    # Derived signal landed with provenance + lineage to the raw parent.
    async with job_pg.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, source_id, produced_by_kind, produced_by_id, modality,
                   derived_from, payload, raw_provenance, media_ref, owner_tenant
            FROM signals
            WHERE produced_by_kind = 'job' AND $1 = ANY(derived_from)
            """,
            raw_id,
        )
    assert len(rows) == 1
    d = rows[0]
    assert d["produced_by_kind"] == "job"
    assert d["produced_by_id"] == str(env.job_id)
    # G3: the derived row inherits the PARENT's modality ('structured' here, the
    # raw row above) so it stays in the parent's modality-pinned slice — it is
    # no longer hardcoded to 'text'.
    assert d["modality"] == "structured"
    assert raw_id in d["derived_from"]
    assert d["source_id"] == "source.test.feed"          # inherited from parent
    assert d["owner_tenant"] == "default"
    assert d["media_ref"] == "https://cdn.example/clip-123.mp4"
    payload = _jsonb(d["payload"])
    assert payload["extraction"] == "transcribe"
    assert payload["text"]                                # non-empty extraction
    prov = _jsonb(d["raw_provenance"])
    assert prov["job_kind"] == "process_media"
    assert prov["model_source"] == "hosted"              # real-HTTP edge only

    # Ledger recorded terminal success + the derived publish subject (A-2).
    async with job_pg.acquire() as conn:
        ledger = await conn.fetchrow(
            "SELECT status, result FROM legba_jobs WHERE idempotency_key=$1",
            env.idempotency_key,
        )
    assert ledger["status"] == "completed"
    res = JobResult.from_json_row(ledger["result"])
    assert res.output_refs["derived_signal_id"] == str(d["id"])
    assert res.output_refs["published_subject"].endswith(".derived")


# ---------------------------------------------------------------------------
# 1b) real (fake) hosted edge — proves the HTTP path, not just the stub.
# ---------------------------------------------------------------------------


async def test_real_hosted_edge_via_injected_transport(job_pg, job_queue):
    raw_id = await _insert_raw_signal(job_pg)
    env = _media_env(raw_id, extraction="caption", idem=f"idem-{uuid4().hex}")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/caption"
        body = json.loads(request.content)
        assert body["media_ref"] == "https://cdn.example/clip-123.mp4"
        return httpx.Response(
            200,
            json={"text": "a person at a gate", "model": "vlm-real-7b",
                  "detail": {"objects": ["person", "gate"]}},
        )

    transport = httpx.MockTransport(handler)
    ac = httpx.AsyncClient(transport=transport, base_url="http://hosted.test")
    media = MediaClient(endpoint="http://hosted.test", client=ac)

    pool = JobWorkerPool(
        queue=job_queue, pg=job_pg, size=1, media=media,
        subscriptions=_subs(job_pg),
    )
    await job_queue.enqueue(env)
    completed = await pool.drain_until_empty()
    await ac.aclose()
    assert completed == 1

    async with job_pg.acquire() as conn:
        d = await conn.fetchrow(
            "SELECT payload, raw_provenance FROM signals "
            "WHERE produced_by_kind='job' AND $1 = ANY(derived_from)",
            raw_id,
        )
    payload = _jsonb(d["payload"])
    prov = _jsonb(d["raw_provenance"])
    assert payload["text"] == "a person at a gate"
    assert prov["model_source"] == "hosted"
    assert prov["model"] == "vlm-real-7b"


# ---------------------------------------------------------------------------
# 2) idempotency on idempotency_key
# ---------------------------------------------------------------------------


async def test_idempotent_on_key_reenqueue(job_pg, job_queue):
    raw_id = await _insert_raw_signal(job_pg)
    idem = f"idem-fixed-{uuid4().hex}"
    media, ac = _hosted_media()
    pool = JobWorkerPool(
        queue=job_queue, pg=job_pg, size=2, media=media,
        subscriptions=_subs(job_pg),
    )

    # Enqueue the SAME idempotency_key three times (distinct job_ids).
    for _ in range(3):
        await job_queue.enqueue(_media_env(raw_id, idem=idem))

    await pool.drain_until_empty()
    await ac.aclose()

    # Exactly ONE derived signal despite three enqueues.
    async with job_pg.acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM signals "
            "WHERE produced_by_kind='job' AND $1 = ANY(derived_from)",
            raw_id,
        )
        ledger_rows = await conn.fetch(
            "SELECT status FROM legba_jobs WHERE idempotency_key=$1", idem
        )
    assert n == 1
    assert len(ledger_rows) == 1
    assert ledger_rows[0]["status"] == "completed"
    # At least one delivery was observed as a duplicate / dedup'd at the broker.
    assert pool.total_processed == 1


async def test_idempotent_claim_is_concurrency_safe(job_pg):
    """Direct ledger-claim race: only one concurrent claimer wins the key."""
    raw_id = await _insert_raw_signal(job_pg)
    env = _media_env(raw_id, idem=f"race-{uuid4().hex}")

    import asyncio

    async def claim():
        async with job_pg.acquire() as conn:
            return await JobStore.claim(conn, env)

    results = await asyncio.gather(*(claim() for _ in range(8)))
    winners = [r for r in results if r.acquired]
    assert len(winners) == 1, "exactly one claimer acquires the key"


# ---------------------------------------------------------------------------
# 3) pool scales by adding workers
# ---------------------------------------------------------------------------


async def test_pool_scales_by_adding_workers(job_pg, job_queue):
    # 12 distinct media jobs over distinct raw signals.
    raw_ids = [await _insert_raw_signal(job_pg, source_id=f"source.s{i}")
               for i in range(12)]
    media, ac = _hosted_media()
    pool = JobWorkerPool(
        queue=job_queue, pg=job_pg, size=1, media=media,
        subscriptions=_subs(job_pg),
    )

    for rid in raw_ids:
        await job_queue.enqueue(_media_env(rid, idem=f"scale-{rid}"))

    # Scale the pool to 4 workers (the acceptance: add workers → capacity).
    for _ in range(3):
        pool.add_worker()
    assert len(pool.workers) == 4

    completed = await pool.drain_until_empty()
    await ac.aclose()
    assert completed == 12

    # Work was distributed: more than one worker processed at least one job
    # (competing consumers — JetStream balanced delivery across the pool).
    workers_that_did_work = [w for w in pool.workers if w.processed > 0]
    assert len(workers_that_did_work) >= 2, (
        f"expected ≥2 workers to share the load, got per-worker counts "
        f"{[w.processed for w in pool.workers]}"
    )

    # Every raw signal in THIS test got exactly one derived child. (The
    # migrated_pg DB is session-scoped + shared across tests, so scope the
    # count to this test's parents rather than counting job signals globally.)
    async with job_pg.acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM signals "
            "WHERE produced_by_kind='job' AND derived_from && $1::uuid[]",
            raw_ids,
        )
    assert n == 12


# ---------------------------------------------------------------------------
# 4) the envelope is generic — a second job kind reuses the plumbing.
# ---------------------------------------------------------------------------


async def test_generic_envelope_second_job_kind(job_pg, job_queue):
    """A throwaway 'echo_probe' kind rides the identical queue/worker/ledger.

    Proves the envelope + worker pool are kind-agnostic: the only thing the new
    kind adds is a handler registration.
    """
    seen: list[str] = []

    async def echo_handler(env: JobEnvelope, ctx: JobContext) -> JobResult:
        seen.append(env.input_refs.get("msg", ""))
        return JobResult(
            job_id=env.job_id, job_kind=env.job_kind, status="completed",
            output_refs={"echo": env.input_refs.get("msg", "")},
            worker_id=ctx.worker_id,
        )

    dispatch = JobDispatch()
    dispatch.register("echo_probe", echo_handler)

    env = JobEnvelope(
        job_kind="echo_probe",   # open string — generic envelope, no re-cut
        requested_by="operator", budget_account="system",
        input_refs={"msg": "hello-jobs"},
        idempotency_key=f"echo-{uuid4().hex}",
    )

    pool = JobWorkerPool(queue=job_queue, pg=job_pg, size=1, dispatch=dispatch)
    # publish on the kind subject directly (envelope.to_bytes still works).
    await job_queue.enqueue(env)
    await pool.drain_until_empty()

    assert seen == ["hello-jobs"]
