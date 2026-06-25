# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-3 / review §3.2-jobs (items 2.6 + 2.7) — jobs-plane crash-safety.

Three coupled fixes the review called out, each with targeted coverage here
(complementing the lease/reaper + backoff suite in
``test_worker_reaper_backoff.py``):

  2.6a **nak WITH delay** — a bare ``msg.nak()`` redelivers instantly, so a
        sibling-claim-held nak (or a transient-error retry) burns the
        ``max_deliver`` budget in seconds. The worker now naks with a delay
        >= one lease (``ack_wait``). Asserted at the message level with a fake
        NATS msg recording the ``delay=`` kwarg.

  2.6b **reaper releases (not DELETEs) + re-enqueues** — the reaper used to
        DELETE an under-budget zombie's row claiming "a redelivery will claim
        it", but a spent broker budget means none comes → silent
        disappearance. Now the row is flipped to ``released`` (kept, with its
        cached envelope) and the worker re-enqueues it; ``JobStore.claim``
        reclaims the surviving ``released`` row in place. DB-backed.

  2.7  **ledger lands in public** — the data pool pins
        ``search_path=ag_catalog,"$user",public`` for AGE, so an unqualified
        ``CREATE TABLE legba_jobs`` would land in ``ag_catalog``. Every ledger
        DDL/DML statement is now ``public.legba_jobs``-qualified; asserted on
        the SQL text and (DB-backed) on the table's resolved schema.

The non-DB tests use a test-local fake queue / fake msg (test code only — no
NATS, no production-path stubs). DB-backed tests reuse the ``job_pg`` fixture.
"""

from __future__ import annotations

import asyncio
import inspect
from uuid import uuid4

import pytest

from legba.data.jobs.envelope import JobEnvelope
from legba.data.jobs.store import LEDGER_DDL, JobStore
from legba.runtime.jobs.worker import JobWorker


def _envelope(*, attempts: int = 0, kind: str = "process_media") -> JobEnvelope:
    return JobEnvelope(
        job_kind=kind,
        input_refs={"signal_id": str(uuid4()), "extraction": "transcribe"},
        idempotency_key=f"harden-test-{uuid4().hex}",
        attempts=attempts,
    )


async def _backdate_claim(pg, key: str, *, seconds: float) -> None:
    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE public.legba_jobs SET claimed_at = NOW() - "
            "make_interval(secs => $2) WHERE idempotency_key = $1",
            key,
            float(seconds),
        )


# ---------------------------------------------------------------------------
# 2.7 — schema qualification (pure SQL-text; no DB)
# ---------------------------------------------------------------------------


def test_ledger_ddl_qualifies_table_to_public():
    """The CREATE TABLE / ALTER / index DDL all name ``public.legba_jobs`` so a
    fresh create lands in public, not ag_catalog (the pinned search_path's
    first writable schema)."""
    assert "CREATE TABLE IF NOT EXISTS public.legba_jobs" in LEDGER_DDL
    # The bare (unqualified) create would silently land in ag_catalog.
    assert "CREATE TABLE IF NOT EXISTS legba_jobs (" not in LEDGER_DDL
    # Indexes target the qualified table too.
    assert "ON public.legba_jobs(" in LEDGER_DDL
    # Additive envelope column is qualified.
    assert "ALTER TABLE public.legba_jobs ADD COLUMN IF NOT EXISTS envelope" in LEDGER_DDL


def test_every_ledger_statement_is_public_qualified():
    """Every legba_jobs touch in store.py's source is ``public.``-qualified —
    no statement can resolve to ag_catalog under the pinned search_path."""
    src = inspect.getsource(JobStore) + LEDGER_DDL
    # Find every 'legba_jobs' occurrence that is an actual table reference
    # (preceded by INSERT INTO / UPDATE / FROM / DELETE FROM / TABLE / ON / a
    # qualified 'public.'). Any table-ref token not prefixed by 'public.' is a
    # bug.
    keywords = ("INTO ", "UPDATE ", "FROM ", "TABLE ", " ON ", "DELETE FROM ")
    for line in src.splitlines():
        if "legba_jobs" not in line:
            continue
        # Skip prose / docstrings / index identifiers / column refs.
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("--"):
            continue
        if "``legba_jobs``" in line or "legba_jobs_" in line or "legba_jobs." in line:
            continue
        if any(kw in line for kw in keywords):
            assert "public.legba_jobs" in line, (
                f"unqualified legba_jobs table-ref would land in ag_catalog: {line!r}"
            )


# ---------------------------------------------------------------------------
# 2.6a — nak WITH delay (fake NATS msg, no broker)
# ---------------------------------------------------------------------------


class _FakeMsg:
    """Records ack/nak/term and the nak ``delay`` kwarg."""

    def __init__(self, data: bytes, *, num_delivered: int = 1) -> None:
        self.data = data
        self.acked = False
        self.termed = False
        self.nak_calls: list[float | None] = []
        self._num_delivered = num_delivered

    async def ack(self) -> None:
        self.acked = True

    async def nak(self, delay=None) -> None:  # noqa: ANN001 — mirror nats sig
        self.nak_calls.append(delay)

    async def term(self) -> None:
        self.termed = True

    @property
    def metadata(self):
        class _M:
            num_delivered = self._num_delivered
        return _M()


class _NoFetchQueue:
    """Queue facade exposing only what _process_msg / nak-delay reads."""

    def __init__(self, *, ack_wait: int = 90, max_deliver: int = 5) -> None:
        self.ack_wait_seconds = ack_wait
        self.max_deliver = max_deliver
        self.enqueued: list[JobEnvelope] = []

    async def bind_pull(self):  # pragma: no cover — not used in these tests
        raise AssertionError("bind not expected")

    async def enqueue(self, env: JobEnvelope) -> None:
        self.enqueued.append(env)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nak_uses_delay_when_sibling_holds_claim(job_pg):
    """A redelivery whose idempotency_key is already 'claimed' by a sibling is
    nak'd WITH a delay >= one lease (ack_wait) — never a bare instant nak."""
    env = _envelope(attempts=1)
    # Pre-claim the key as if a sibling worker holds it.
    async with job_pg.acquire() as conn:
        claim = await JobStore.claim(conn, env)
        assert claim.acquired

    queue = _NoFetchQueue(ack_wait=90, max_deliver=5)
    worker = JobWorker(worker_id="nak-delay-test", queue=queue, pg=job_pg)
    msg = _FakeMsg(env.to_bytes(), num_delivered=env.attempts)

    result = await worker._process_msg(msg)
    assert result is None  # deferred, not processed
    assert msg.nak_calls == [90.0], "nak must carry a delay == ack_wait, not bare"
    assert not msg.acked and not msg.termed


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nak_uses_delay_on_transient_handler_error(job_pg):
    """A transient handler error (attempts < max_deliver) releases the claim
    and naks WITH a delay so the retry backs off instead of tight-redelivering.
    """
    env = _envelope(attempts=1)
    queue = _NoFetchQueue(ack_wait=90, max_deliver=5)

    async def _boom(_env, _ctx):
        raise RuntimeError("transient downstream blip")

    class _Dispatch:
        def handler_for(self, _kind):
            return _boom

    worker = JobWorker(
        worker_id="nak-transient-test",
        queue=queue,
        pg=job_pg,
        dispatch=_Dispatch(),
    )
    msg = _FakeMsg(env.to_bytes(), num_delivered=env.attempts)

    result = await worker._process_msg(msg)
    assert result is None
    assert msg.nak_calls == [90.0], "transient-error retry must nak with a delay"
    # The claim was released (status 'released'), not left 'claimed' or deleted.
    async with job_pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, envelope FROM public.legba_jobs WHERE idempotency_key = $1",
            env.idempotency_key,
        )
    assert row is not None, "release must KEEP the row (not DELETE it)"
    assert row["status"] == "released"
    assert row["envelope"] is not None, "envelope cached for re-enqueue"


def test_nak_delay_is_at_least_one_ack_wait():
    """The computed nak delay never drops below one ack_wait (and floors at 1s).
    """
    w_hi = JobWorker(
        worker_id="d1", queue=_NoFetchQueue(ack_wait=120), pg=None,
    )
    assert w_hi._nak_delay_seconds() == pytest.approx(120.0)
    w_lo = JobWorker(
        worker_id="d2", queue=_NoFetchQueue(ack_wait=0), pg=None,
    )
    assert w_lo._nak_delay_seconds() == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 2.6b — reaper marks 'released' (not DELETE) + re-enqueues; claim reclaims
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reaper_releases_without_deleting_and_returns_envelope(job_pg):
    """A stale claim with budget remaining is marked 'released' (row KEPT) and
    the reaper hands back the cached envelope for re-enqueue — not DELETEd."""
    env = _envelope(attempts=1)
    async with job_pg.acquire() as conn:
        claim = await JobStore.claim(conn, env)
        assert claim.acquired
    await _backdate_claim(job_pg, env.idempotency_key, seconds=600.0)

    async with job_pg.acquire() as conn:
        reaped = await JobStore.reap_stale_claims(
            conn, lease_seconds=40.0, max_deliver=4,
        )
    by_key = {r["idempotency_key"]: r for r in reaped}
    entry = by_key[env.idempotency_key]
    assert entry["disposition"] == "released"
    assert entry["envelope"], "reaper returns the cached envelope to re-enqueue"

    # Row survives in 'released' (NOT deleted → no silent disappearance).
    async with job_pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM public.legba_jobs WHERE idempotency_key = $1",
            env.idempotency_key,
        )
    assert row is not None
    assert row["status"] == "released"

    # The cached envelope round-trips back to a usable JobEnvelope.
    raw = entry["envelope"]
    restored = (
        JobEnvelope.from_bytes(raw.encode("utf-8"))
        if isinstance(raw, str)
        else JobEnvelope.model_validate(raw)
    )
    assert restored.idempotency_key == env.idempotency_key
    assert restored.input_refs == env.input_refs


@pytest.mark.integration
@pytest.mark.asyncio
async def test_claim_reclaims_a_released_row_in_place(job_pg):
    """A re-enqueued delivery whose key sits in 'released' RECLAIMS the row
    (status flips back to 'claimed') — the redelivery is not lost and the row
    is not duplicated."""
    env = _envelope(attempts=2)
    async with job_pg.acquire() as conn:
        assert (await JobStore.claim(conn, env)).acquired
        await JobStore.release(conn, env.idempotency_key)
        row = await conn.fetchrow(
            "SELECT status FROM public.legba_jobs WHERE idempotency_key = $1",
            env.idempotency_key,
        )
        assert row["status"] == "released"

    # A redelivery (incremented attempts) reclaims it.
    redelivered = env.model_copy(update={"attempts": 3})
    async with job_pg.acquire() as conn:
        reclaim = await JobStore.claim(conn, redelivered)
        assert reclaim.acquired, "a 'released' row must be reclaimable in place"
        row = await conn.fetchrow(
            "SELECT status, attempts FROM public.legba_jobs WHERE idempotency_key = $1",
            env.idempotency_key,
        )
    assert row["status"] == "claimed"
    assert row["attempts"] == 3, "reclaim refreshes attempts from the redelivery"

    # Exactly one ledger row for the key — reclaim is in place, not a 2nd row.
    async with job_pg.acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM public.legba_jobs WHERE idempotency_key = $1",
            env.idempotency_key,
        )
    assert n == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_terminal_rows_are_not_reclaimable(job_pg):
    """A completed/failed row is terminal — claim never resurrects it (the
    'released'-only reclaim guard must not match terminal states)."""
    from legba.data.jobs.envelope import JobResult

    env = _envelope(attempts=1)
    async with job_pg.acquire() as conn:
        assert (await JobStore.claim(conn, env)).acquired
        await JobStore.complete(
            conn, env.idempotency_key,
            JobResult(job_id=env.job_id, job_kind=env.job_kind,
                      status="completed", worker_id="w0"),
        )
    async with job_pg.acquire() as conn:
        reclaim = await JobStore.claim(conn, env)
    assert not reclaim.acquired
    assert reclaim.status == "completed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_reaper_hook_reenqueues_released_zombie(job_pg):
    """The worker loop's reaper hook re-enqueues a released zombie's cached
    envelope onto the queue (end-to-end of the 2.6b fix, no NATS)."""
    env = _envelope(attempts=1)
    async with job_pg.acquire() as conn:
        assert (await JobStore.claim(conn, env)).acquired
    # Lease = 10 × 4 = 40s; backdate well past it, budget remaining (1 < 4).
    await _backdate_claim(job_pg, env.idempotency_key, seconds=600.0)

    queue = _NoFetchQueue(ack_wait=10, max_deliver=4)
    worker = JobWorker(worker_id="reenqueue-hook", queue=queue, pg=job_pg)
    worker._next_reap_at = 0.0  # sweep due immediately

    await worker._maybe_reap_stale_claims()

    assert worker.reaped == 1
    assert len(queue.enqueued) == 1, "released zombie must be re-enqueued"
    assert queue.enqueued[0].idempotency_key == env.idempotency_key
    # The row is 'released' and awaits the re-enqueued delivery's reclaim.
    async with job_pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM public.legba_jobs WHERE idempotency_key = $1",
            env.idempotency_key,
        )
    assert row["status"] == "released"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_reap_is_not_reenqueued(job_pg):
    """A budget-spent zombie is terminal-failed and is NOT re-enqueued (only
    'released' dispositions re-enqueue)."""
    env = _envelope(attempts=4)
    async with job_pg.acquire() as conn:
        assert (await JobStore.claim(conn, env)).acquired
    await _backdate_claim(job_pg, env.idempotency_key, seconds=600.0)

    queue = _NoFetchQueue(ack_wait=10, max_deliver=4)
    worker = JobWorker(worker_id="failed-no-reenqueue", queue=queue, pg=job_pg)
    worker._next_reap_at = 0.0

    await worker._maybe_reap_stale_claims()

    assert worker.reaped == 1
    assert queue.enqueued == [], "a terminal-failed reap must not be re-enqueued"
    async with job_pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM public.legba_jobs WHERE idempotency_key = $1",
            env.idempotency_key,
        )
    assert row["status"] == "failed"


# ---------------------------------------------------------------------------
# 2.7 — ledger physically lands in public (DB-backed)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ledger_table_lands_in_public_schema(job_pg):
    """Under the pinned search_path (ag_catalog,"$user",public) the ledger
    table resolves to the PUBLIC schema, never ag_catalog."""
    async with job_pg.acquire() as conn:
        schemas = await conn.fetch(
            "SELECT table_schema FROM information_schema.tables "
            "WHERE table_name = 'legba_jobs'"
        )
    schema_set = {r["table_schema"] for r in schemas}
    assert "public" in schema_set, f"legba_jobs not in public: {schema_set}"
    assert "ag_catalog" not in schema_set, (
        "legba_jobs leaked into ag_catalog — the 2.7 bug"
    )


# ---------------------------------------------------------------------------
# 2.6 — run() loop is supervised against a ledger/PG error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_loop_survives_a_drain_exception():
    """A PG/ledger error escaping drain_once must NOT end the worker task: the
    run loop catches it, backs off, and continues (the worker survives)."""

    class _Q:
        ack_wait_seconds = 10
        max_deliver = 4

    worker = JobWorker(worker_id="supervised", queue=_Q(), pg=None)

    calls = {"n": 0}

    async def _bind():
        return None

    async def _boom_then_stop():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated PG error from the ledger path")
        worker.stop()  # second iteration: clean exit
        return []

    worker.bind = _bind            # type: ignore[assignment]
    worker.drain_once = _boom_then_stop  # type: ignore[assignment]
    # Keep the backoff sleep ~instant so the test doesn't actually wait.
    worker._error_backoff_seconds = lambda: 0.0  # type: ignore[assignment]

    # Must return (loop survived the exception + reached the stop), not raise.
    await asyncio.wait_for(worker.run(), timeout=5.0)
    assert calls["n"] == 2, "the loop continued past the exception"
