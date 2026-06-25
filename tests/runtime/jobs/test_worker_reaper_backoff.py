# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-3 jobs-plane hardening tests — claimed-row lease/reaper + fetch backoff.

Two failure modes the review (§3.2-jobs) called out:

  1. **Zombie claims** — a worker that crashes mid-job leaves its
     ``legba_jobs`` row ``claimed`` forever. Once the redelivery budget
     (``ack_wait × max_deliver``) is spent, no redelivery can flip it.
     :meth:`JobStore.reap_stale_claims` (driven by the worker loop's
     ``_maybe_reap_stale_claims`` hook) expires such rows: released back
     to retryable while delivery budget remains, terminally failed (with
     an explicit lease-expiry result) once it is spent.

  2. **Tight-loop on broker errors** — ``drain_once``'s error path used
     to return immediately on ANY fetch exception, so ``run()`` would
     hammer a broken broker. It now sleeps an exponential backoff
     (0.5 s … 30 s) on real errors; an empty-queue timeout is normal
     flow control and resets the streak.

DB-backed tests use the per-test migrated DB (``job_pg`` fixture);
the backoff tests use a test-local fake queue (test code only — no
NATS required, and no production-path stubs involved).
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import pytest

from legba.data.jobs.envelope import JobEnvelope
from legba.data.jobs.store import JobStore
from legba.runtime.jobs.worker import (
    ERROR_BACKOFF_BASE_SECONDS,
    ERROR_BACKOFF_CAP_SECONDS,
    JobWorker,
)


def _envelope(*, attempts: int = 0, kind: str = "process_media") -> JobEnvelope:
    return JobEnvelope(
        job_kind=kind,
        input_refs={"signal_id": str(uuid4()), "extraction": "transcribe"},
        idempotency_key=f"reaper-test-{uuid4().hex}",
        attempts=attempts,
    )


async def _backdate_claim(pg, key: str, *, seconds: float) -> None:
    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE legba_jobs SET claimed_at = NOW() - make_interval(secs => $2) "
            "WHERE idempotency_key = $1",
            key,
            float(seconds),
        )


# ---------------------------------------------------------------------------
# JobStore.reap_stale_claims (DB-backed)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reaper_fails_stale_claim_with_spent_budget(job_pg):
    """attempts >= max_deliver + lease expired → terminal failed with an
    explicit lease-expiry result cached on the row."""
    env = _envelope(attempts=4)
    async with job_pg.acquire() as conn:
        claim = await JobStore.claim(conn, env)
        assert claim.acquired
    await _backdate_claim(job_pg, env.idempotency_key, seconds=120.0)

    async with job_pg.acquire() as conn:
        reaped = await JobStore.reap_stale_claims(
            conn, lease_seconds=40.0, max_deliver=4, reaper_id="test-reaper",
        )
    by_key = {r["idempotency_key"]: r for r in reaped}
    assert env.idempotency_key in by_key
    assert by_key[env.idempotency_key]["disposition"] == "failed"

    # Row is terminal-failed; a redelivery claim now short-circuits.
    async with job_pg.acquire() as conn:
        claim2 = await JobStore.claim(conn, env)
    assert not claim2.acquired
    assert claim2.status == "failed"
    assert claim2.prior_result is not None
    assert "lease expired" in (claim2.prior_result.error or "")
    assert claim2.prior_result.worker_id == "test-reaper"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reaper_releases_stale_claim_with_budget_remaining(job_pg):
    """attempts < max_deliver + lease expired → row marked 'released'
    (KEPT, not deleted — C-3 / 2.6): a re-enqueue / pending redelivery
    reclaims the surviving row's key in place."""
    env = _envelope(attempts=1)
    async with job_pg.acquire() as conn:
        claim = await JobStore.claim(conn, env)
        assert claim.acquired
    await _backdate_claim(job_pg, env.idempotency_key, seconds=120.0)

    async with job_pg.acquire() as conn:
        reaped = await JobStore.reap_stale_claims(
            conn, lease_seconds=40.0, max_deliver=4,
        )
    by_key = {r["idempotency_key"]: r for r in reaped}
    assert by_key[env.idempotency_key]["disposition"] == "released"

    # The key is claimable again — exactly the retryable semantic.
    async with job_pg.acquire() as conn:
        claim2 = await JobStore.claim(conn, env)
    assert claim2.acquired


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reaper_leaves_fresh_claims_alone(job_pg):
    """A claim younger than the lease is live work — never reaped."""
    env = _envelope(attempts=4)
    async with job_pg.acquire() as conn:
        claim = await JobStore.claim(conn, env)
        assert claim.acquired
        reaped = await JobStore.reap_stale_claims(
            conn, lease_seconds=3600.0, max_deliver=4,
        )
    assert env.idempotency_key not in {r["idempotency_key"] for r in reaped}
    async with job_pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM legba_jobs WHERE idempotency_key = $1",
            env.idempotency_key,
        )
    assert row["status"] == "claimed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reaper_never_touches_terminal_rows(job_pg):
    """completed/failed rows are terminal — the reaper's WHERE re-checks
    status='claimed' so it can never resurrect or double-fail them."""
    from legba.data.jobs.envelope import JobResult

    env = _envelope(attempts=2)
    async with job_pg.acquire() as conn:
        claim = await JobStore.claim(conn, env)
        assert claim.acquired
        await JobStore.complete(
            conn,
            env.idempotency_key,
            JobResult(
                job_id=env.job_id, job_kind=env.job_kind,
                status="completed", worker_id="w0",
            ),
        )
    # Backdate (claimed_at predates the lease) — still must not be reaped.
    await _backdate_claim(job_pg, env.idempotency_key, seconds=999_999.0)
    async with job_pg.acquire() as conn:
        reaped = await JobStore.reap_stale_claims(
            conn, lease_seconds=1.0, max_deliver=4,
        )
        row = await conn.fetchrow(
            "SELECT status FROM legba_jobs WHERE idempotency_key = $1",
            env.idempotency_key,
        )
    assert env.idempotency_key not in {r["idempotency_key"] for r in reaped}
    assert row["status"] == "completed"


# ---------------------------------------------------------------------------
# Worker-loop reaper hook (DB-backed; fake queue, no NATS needed)
# ---------------------------------------------------------------------------


class _FakePullSub:
    """Test-local pull subscription: empty queue or scripted errors."""

    def __init__(self, errors: list[Exception] | None = None) -> None:
        self._errors = list(errors or [])
        self.fetch_calls = 0

    async def fetch(self, batch: int, timeout: float = 2.0):
        self.fetch_calls += 1
        if self._errors:
            raise self._errors.pop(0)
        raise asyncio.TimeoutError()  # empty queue — normal flow control


class _FakeQueue:
    """Test-local queue facade exposing the surface JobWorker reads."""

    def __init__(self, psub: _FakePullSub, *, ack_wait: int = 10, max_deliver: int = 4) -> None:
        self._psub = psub
        self.ack_wait_seconds = ack_wait
        self.max_deliver = max_deliver

    async def bind_pull(self):
        return self._psub


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_loop_reaps_due_stale_claim(job_pg):
    """drain_once runs the reaper hook when the sweep interval elapses and
    expires a zombie claim through the worker's own lease arithmetic
    (ack_wait × max_deliver)."""
    env = _envelope(attempts=4)
    async with job_pg.acquire() as conn:
        claim = await JobStore.claim(conn, env)
        assert claim.acquired
    # Lease = 10 s × 4 = 40 s; backdate the claim past it.
    await _backdate_claim(job_pg, env.idempotency_key, seconds=300.0)

    worker = JobWorker(
        worker_id="reaper-hook-test",
        queue=_FakeQueue(_FakePullSub(), ack_wait=10, max_deliver=4),
        pg=job_pg,
    )
    worker._next_reap_at = 0.0  # sweep due immediately
    results = await worker.drain_once()
    assert results == []  # queue empty
    assert worker.reaped == 1

    async with job_pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, result FROM legba_jobs WHERE idempotency_key = $1",
            env.idempotency_key,
        )
    assert row["status"] == "failed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_loop_reaper_respects_interval(job_pg):
    """Between sweeps the hook is a no-op — drain_once doesn't hit the
    ledger on every fetch."""
    worker = JobWorker(
        worker_id="reaper-interval-test",
        queue=_FakeQueue(_FakePullSub(), ack_wait=10, max_deliver=4),
        pg=job_pg,
    )
    # Lease 40 s → sweep interval 20 s → next sweep is in the future.
    assert worker._reap_interval == pytest.approx(20.0)
    before = worker._next_reap_at
    await worker.drain_once()
    assert worker._next_reap_at == before  # not rescheduled = not run


# ---------------------------------------------------------------------------
# drain_once error backoff (no DB writes; fake queue)
# ---------------------------------------------------------------------------


def test_error_backoff_is_exponential_and_capped():
    worker = JobWorker(
        worker_id="backoff-math-test",
        queue=_FakeQueue(_FakePullSub()),
        pg=None,  # backoff math never touches the ledger
    )
    seen = []
    for n in (1, 2, 3, 4, 100):
        worker._consecutive_fetch_errors = n
        seen.append(worker._error_backoff_seconds())
    assert seen[0] == pytest.approx(ERROR_BACKOFF_BASE_SECONDS)
    assert seen[1] == pytest.approx(ERROR_BACKOFF_BASE_SECONDS * 2)
    assert seen[2] == pytest.approx(ERROR_BACKOFF_BASE_SECONDS * 4)
    assert seen[3] == pytest.approx(ERROR_BACKOFF_BASE_SECONDS * 8)
    assert seen[4] == ERROR_BACKOFF_CAP_SECONDS


@pytest.mark.integration
@pytest.mark.asyncio
async def test_drain_once_backs_off_on_repeated_errors(job_pg):
    """Two consecutive broker errors → the second drain sleeps longer than
    the first (exponential), and the streak counter climbs."""
    psub = _FakePullSub(errors=[
        RuntimeError("broker exploded"),
        RuntimeError("broker exploded again"),
    ])
    worker = JobWorker(
        worker_id="backoff-test",
        queue=_FakeQueue(psub),
        pg=job_pg,
    )
    worker._next_reap_at = float("inf")  # keep the reaper out of timings

    t0 = time.monotonic()
    assert await worker.drain_once() == []
    first = time.monotonic() - t0
    assert worker._consecutive_fetch_errors == 1
    assert first >= ERROR_BACKOFF_BASE_SECONDS * 0.9

    t1 = time.monotonic()
    assert await worker.drain_once() == []
    second = time.monotonic() - t1
    assert worker._consecutive_fetch_errors == 2
    assert second >= ERROR_BACKOFF_BASE_SECONDS * 2 * 0.9
    assert second > first


@pytest.mark.integration
@pytest.mark.asyncio
async def test_drain_once_timeout_is_not_an_error(job_pg):
    """An empty-queue fetch timeout resets the error streak and does NOT
    sleep the backoff (it is normal flow control)."""
    psub = _FakePullSub(errors=[RuntimeError("broker exploded")])
    worker = JobWorker(
        worker_id="backoff-reset-test",
        queue=_FakeQueue(psub),
        pg=job_pg,
    )
    worker._next_reap_at = float("inf")

    await worker.drain_once()  # error → streak 1
    assert worker._consecutive_fetch_errors == 1

    t0 = time.monotonic()
    await worker.drain_once()  # TimeoutError → reset, no backoff sleep
    elapsed = time.monotonic() - t0
    assert worker._consecutive_fetch_errors == 0
    assert elapsed < ERROR_BACKOFF_BASE_SECONDS
