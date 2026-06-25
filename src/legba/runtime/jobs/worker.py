# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Competing-consumer job worker + pool (P-07 / PIVOT §5.1).

A :class:`JobWorker` is one stateless consumer: it binds to the shared durable
pull consumer, ``fetch``es a batch, and for each message:

  1. Parse the :class:`JobEnvelope` (malformed → term, never redeliver).
  2. Drop expired jobs (past ``deadline``) → term + ledger ``failed``.
  3. **Claim** the ``idempotency_key`` in the ledger. If already terminal,
     ack the redelivery and skip the work (effectively-once). If another
     worker holds the claim, ``nak`` so it redelivers later.
  4. Dispatch to the kind's handler.
  5. On success → ledger ``complete`` + ack. On a redelivery-worthy error →
     ledger ``release`` + nak (retry). On a terminal error / exhausted
     ``max_deliver`` → ledger ``fail`` + term.

A :class:`JobWorkerPool` runs N workers against the SAME durable consumer —
JetStream load-balances delivery across them (NATS-native competing
consumers). Scaling the pool = adding workers (the P-07 "pool scales by adding
workers" acceptance criterion); no rebalancing config, no per-worker subjects.

Hardening (C-3, review §3.2-jobs):

  * **Claimed-row lease/reaper** — a ledger row stuck ``claimed`` longer
    than the redelivery budget (``ack_wait × max_deliver``) means its
    claim holder died mid-job and the broker has stopped redelivering.
    Each worker's fetch loop periodically runs
    :meth:`legba.data.jobs.store.JobStore.reap_stale_claims`, which
    releases under-delivered rows back to retryable and terminally fails
    rows whose delivery budget is spent (with an explicit lease-expiry
    result — never a silent zombie).
  * **Error backoff** — repeated *broker* errors in the fetch path back
    off exponentially (0.5 s … 30 s) instead of tight-looping; an empty
    fetch (timeout) is normal flow-control, not an error, and resets the
    backoff.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from ...data.jobs.envelope import JobEnvelope, JobResult
from ...data.jobs.store import JobStore
from .dispatch import JobContext, JobDispatch, default_dispatch

if TYPE_CHECKING:  # pragma: no cover — typing only
    from ...data.postgres import PostgresStore
    from .media_client import MediaClient
    from .queue import JobQueue

logger = logging.getLogger(__name__)


# Backoff envelope for repeated fetch errors (broker down, auth failure,
# consumer deleted, …): base * 2^(n-1), capped. Tuned so a transient blip
# costs sub-second and a hard outage settles at one probe per 30 s.
ERROR_BACKOFF_BASE_SECONDS = 0.5
ERROR_BACKOFF_CAP_SECONDS = 30.0


class JobWorker:
    """One competing consumer. Bind once, then ``run`` or ``drain_once``."""

    def __init__(
        self,
        *,
        worker_id: str,
        queue: "JobQueue",
        pg: "PostgresStore",
        dispatch: JobDispatch | None = None,
        media: "MediaClient | None" = None,
        subscriptions: object | None = None,
        fetch_batch: int = 8,
        fetch_timeout: float = 2.0,
    ) -> None:
        self.worker_id = worker_id
        self._queue = queue
        self._pg = pg
        self._dispatch = dispatch or default_dispatch()
        self._media = media
        self._subscriptions = subscriptions
        self._fetch_batch = fetch_batch
        self._fetch_timeout = fetch_timeout
        self._psub = None
        self._stopped = asyncio.Event()
        self.processed = 0
        self.skipped_duplicate = 0
        self.failed = 0
        self.reaped = 0
        # Lease/reaper bookkeeping. The lease is the redelivery budget —
        # past (ack_wait × max_deliver) seconds, a 'claimed' row's holder
        # is presumed dead and no further redelivery is coming. The
        # reaper sweep runs at half the lease so a zombie is expired at
        # most ~1.5 leases after its claim.
        self._reap_interval = max(1.0, self._lease_seconds() / 2.0)
        self._next_reap_at = time.monotonic() + self._reap_interval
        # Exponential backoff state for repeated fetch errors.
        self._consecutive_fetch_errors = 0

    @property
    def ctx(self) -> JobContext:
        return JobContext(
            pg=self._pg,
            media=self._media,
            queue=self._queue,
            worker_id=self.worker_id,
            subscriptions=self._subscriptions,
        )

    async def bind(self) -> None:
        if self._psub is None:
            self._psub = await self._queue.bind_pull()

    # ------------------------------------------------------------------
    # Per-message processing
    # ------------------------------------------------------------------

    async def _process_msg(self, msg) -> JobResult | None:
        """Process one NATS message. Acks/terms/naks the msg itself."""
        # 1) Parse — a malformed envelope can never succeed → term.
        try:
            env = JobEnvelope.from_bytes(msg.data)
        except Exception as exc:
            logger.error("job.parse_failed: %s — terming msg", exc)
            await _term(msg)
            return None

        # Reflect the JetStream delivery count onto the envelope so a handler
        # / the ledger sees the true attempt number.
        try:
            env = env.model_copy(update={"attempts": _delivered(msg)})
        except Exception:
            pass

        # 2) Deadline.
        if env.is_expired():
            result = JobResult(
                job_id=env.job_id, job_kind=env.job_kind, status="expired",
                error="past deadline", worker_id=self.worker_id,
            )
            async with self._pg.acquire() as conn:
                claim = await JobStore.claim(conn, env)
                if claim.acquired:
                    await JobStore.fail(conn, env.idempotency_key, result)
            await _ack(msg)
            self.failed += 1
            return result

        # 3) Idempotency claim.
        async with self._pg.acquire() as conn:
            claim = await JobStore.claim(conn, env)
        if not claim.acquired:
            if claim.status in ("completed", "failed"):
                # Terminal — this is a redelivery of finished work. Ack + skip.
                self.skipped_duplicate += 1
                await _ack(msg)
                logger.debug(
                    "job.skip_duplicate kind=%s idem=%s status=%s",
                    env.job_kind, env.idempotency_key, claim.status,
                )
                return JobResult(
                    job_id=env.job_id, job_kind=env.job_kind,
                    status="skipped_duplicate", worker_id=self.worker_id,
                    output_refs=(
                        claim.prior_result.output_refs if claim.prior_result else {}
                    ),
                )
            # Another worker holds the claim → defer redelivery by a full
            # lease so the holder can finish (or be reaped + re-enqueued)
            # instead of an instant re-nak storm that burns max_deliver.
            await _nak(msg, delay=self._nak_delay_seconds())
            return None

        # 4) Dispatch.
        handler = self._dispatch.handler_for(env.job_kind)
        if handler is None:
            result = JobResult(
                job_id=env.job_id, job_kind=env.job_kind, status="failed",
                error=f"no handler for kind {env.job_kind!r}",
                worker_id=self.worker_id,
            )
            async with self._pg.acquire() as conn:
                await JobStore.fail(conn, env.idempotency_key, result)
            await _term(msg)
            self.failed += 1
            return result

        try:
            result = await handler(env, self.ctx)
        except Exception as exc:
            # Handler crashed. If we've exhausted delivery, term + ledger-fail;
            # otherwise release the claim + nak so a fresh attempt can retry.
            logger.exception("job.handler_error kind=%s: %s", env.job_kind, exc)
            if env.attempts >= self._max_deliver():
                result = JobResult(
                    job_id=env.job_id, job_kind=env.job_kind, status="failed",
                    error=f"handler error (final attempt): {exc}",
                    worker_id=self.worker_id,
                )
                async with self._pg.acquire() as conn:
                    await JobStore.fail(conn, env.idempotency_key, result)
                await _term(msg)
                self.failed += 1
                return result
            async with self._pg.acquire() as conn:
                await JobStore.release(conn, env.idempotency_key)
            # Defer the retry by a lease so a transient failure backs off
            # rather than tight-redelivering and torching the budget.
            await _nak(msg, delay=self._nak_delay_seconds())
            return None

        # 5) Terminal outcome.
        async with self._pg.acquire() as conn:
            if result.status == "completed":
                await JobStore.complete(conn, env.idempotency_key, result)
            else:
                await JobStore.fail(conn, env.idempotency_key, result)
        await _ack(msg)
        if result.status == "completed":
            self.processed += 1
        else:
            self.failed += 1
        return result

    def _max_deliver(self) -> int:
        return self._queue.max_deliver

    def _nak_delay_seconds(self) -> float:
        """Redelivery defer for a nak (C-3 / 2.6).

        At least one ``ack_wait`` so a deferred redelivery lands no sooner
        than a normal at-least-once redelivery would — enough time for the
        current claim holder to finish (or for its crashed claim to be
        reaped + re-enqueued) instead of an instant re-nak storm that burns
        ``max_deliver`` in seconds. Never below 1 s.
        """
        return max(1.0, float(getattr(self._queue, "ack_wait_seconds", 60.0)))

    def _lease_seconds(self) -> float:
        """Claim lease = the redelivery budget, ack_wait × max_deliver.

        Past this window the broker has stopped redelivering the message,
        so a still-'claimed' ledger row can only be a dead worker's zombie.
        """
        ack_wait = float(getattr(self._queue, "ack_wait_seconds", 60.0))
        return ack_wait * max(1, int(self._max_deliver()))

    # ------------------------------------------------------------------
    # Reaper — expire claims stuck past the lease
    # ------------------------------------------------------------------

    async def _maybe_reap_stale_claims(self) -> None:
        """Run the stale-claim reaper if the sweep interval has elapsed.

        Best-effort: a reaper error (e.g. DB blip) is logged and retried
        on the next interval; it never takes the fetch loop down.
        """
        now = time.monotonic()
        if now < self._next_reap_at:
            return
        self._next_reap_at = now + self._reap_interval
        try:
            async with self._pg.acquire() as conn:
                reaped = await JobStore.reap_stale_claims(
                    conn,
                    lease_seconds=self._lease_seconds(),
                    max_deliver=self._max_deliver(),
                    reaper_id=self.worker_id,
                )
        except Exception as exc:
            logger.warning("job.reaper_error worker=%s: %s", self.worker_id, exc)
            return
        if reaped:
            self.reaped += len(reaped)
            for entry in reaped:
                logger.warning(
                    "job.claim_reaped kind=%s idem=%s attempts=%s "
                    "disposition=%s worker=%s",
                    entry["job_kind"], entry["idempotency_key"],
                    entry["attempts"], entry["disposition"], self.worker_id,
                )
            # Re-enqueue every released zombie (C-3 / 2.6). The reaper marked
            # the row 'released' instead of DELETEing it, but with a spent
            # broker budget no redelivery is coming — so we re-publish the
            # cached envelope onto the work-queue. A fresh delivery then lands,
            # and JobStore.claim reclaims the surviving 'released' row in place
            # (status flips back to 'claimed'). Best-effort: a re-enqueue
            # failure is logged and retried on the next sweep (the row stays
            # 'released', so the next reaper pass picks it up again).
            await self._reenqueue_released(reaped)

    async def _reenqueue_released(self, reaped: list[dict]) -> None:
        """Re-publish the envelopes of reaper-released zombies (C-3 / 2.6)."""
        for entry in reaped:
            if entry.get("disposition") != "released":
                continue
            raw = entry.get("envelope")
            if not raw:
                # No cached envelope (a pre-envelope-column row) — can't
                # reconstruct the work; log loudly so it isn't a silent drop.
                logger.error(
                    "job.reaper_reenqueue_skipped reason=no_envelope "
                    "kind=%s idem=%s worker=%s",
                    entry["job_kind"], entry["idempotency_key"], self.worker_id,
                )
                continue
            try:
                env = (
                    JobEnvelope.from_bytes(raw.encode("utf-8"))
                    if isinstance(raw, str)
                    else JobEnvelope.model_validate(raw)
                )
                await self._queue.enqueue(env)
                logger.warning(
                    "job.reaper_reenqueued kind=%s idem=%s worker=%s",
                    entry["job_kind"], entry["idempotency_key"], self.worker_id,
                )
            except Exception as exc:
                logger.error(
                    "job.reaper_reenqueue_failed kind=%s idem=%s worker=%s: %s",
                    entry["job_kind"], entry["idempotency_key"],
                    self.worker_id, exc,
                )

    # ------------------------------------------------------------------
    # Fetch loops
    # ------------------------------------------------------------------

    def _error_backoff_seconds(self) -> float:
        """Exponential backoff for the current error streak (capped)."""
        n = max(1, self._consecutive_fetch_errors)
        return min(
            ERROR_BACKOFF_CAP_SECONDS,
            ERROR_BACKOFF_BASE_SECONDS * (2 ** (n - 1)),
        )

    async def drain_once(self) -> list[JobResult]:
        """Fetch + process one batch; return the per-msg results.

        Returns an empty list when the queue is momentarily empty (fetch
        timeout). The test harness loops this until the queue depth hits 0.

        Error path: a *real* fetch error (broker down, consumer deleted —
        anything that isn't the normal empty-queue timeout) sleeps an
        exponentially-increasing backoff before returning, so ``run``'s
        continuous loop probes a broken broker at a bounded rate instead
        of tight-looping. Any successful fetch or normal timeout resets
        the streak.
        """
        await self.bind()
        await self._maybe_reap_stale_claims()
        try:
            msgs = await self._psub.fetch(
                self._fetch_batch, timeout=self._fetch_timeout
            )
        except (asyncio.TimeoutError, Exception) as exc:  # nats TimeoutError
            is_timeout = (
                isinstance(exc, asyncio.TimeoutError)
                or "timeout" in str(exc).lower()
            )
            if is_timeout:
                # Empty queue — normal flow control, not an error.
                self._consecutive_fetch_errors = 0
                return []
            self._consecutive_fetch_errors += 1
            backoff = self._error_backoff_seconds()
            logger.warning(
                "job.fetch_error worker=%s streak=%d backoff=%.1fs: %s",
                self.worker_id, self._consecutive_fetch_errors, backoff, exc,
            )
            await asyncio.sleep(backoff)
            return []
        self._consecutive_fetch_errors = 0
        results: list[JobResult] = []
        for msg in msgs:
            r = await self._process_msg(msg)
            if r is not None:
                results.append(r)
        return results

    async def run(self) -> None:
        """Run until :meth:`stop`. Continuous competing-consumer loop.

        Supervised (C-3 / 2.6): ``drain_once`` already absorbs *broker* fetch
        errors with its own backoff, but a PG error escaping from the ledger
        path (claim / complete / fail / release / reaper) would otherwise
        bubble out of the loop, end the worker's task, and silently shrink the
        pool with no log. Catch every non-cancellation exception here, log it,
        back off (reusing the same exponential envelope so a hard DB outage
        settles at one probe per 30 s), and continue. Only ``CancelledError``
        (a ``stop()``/task-cancel) breaks the loop.
        """
        await self.bind()
        while not self._stopped.is_set():
            try:
                await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._consecutive_fetch_errors += 1
                backoff = self._error_backoff_seconds()
                logger.exception(
                    "job.run_loop_error worker=%s streak=%d backoff=%.1fs: %s",
                    self.worker_id, self._consecutive_fetch_errors, backoff, exc,
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise

    def stop(self) -> None:
        self._stopped.set()


class JobWorkerPool:
    """N competing :class:`JobWorker`s bound to one shared durable consumer."""

    def __init__(
        self,
        *,
        queue: "JobQueue",
        pg: "PostgresStore",
        size: int = 2,
        dispatch: JobDispatch | None = None,
        media: "MediaClient | None" = None,
        subscriptions: object | None = None,
    ) -> None:
        self._queue = queue
        self._pg = pg
        self._dispatch = dispatch
        self._media = media
        self._subscriptions = subscriptions
        self._workers = [
            JobWorker(
                worker_id=f"worker-{i}",
                queue=queue,
                pg=pg,
                dispatch=dispatch,
                media=media,
                subscriptions=subscriptions,
            )
            for i in range(size)
        ]
        self._tasks: list[asyncio.Task] = []

    @property
    def workers(self) -> list[JobWorker]:
        return list(self._workers)

    def add_worker(self) -> JobWorker:
        """Scale the pool by one worker (the P-07 scaling acceptance).

        A new worker binds the SAME durable consumer; JetStream immediately
        starts load-balancing delivery to it. If the pool is running, the new
        worker is launched too.
        """
        w = JobWorker(
            worker_id=f"worker-{len(self._workers)}",
            queue=self._queue,
            pg=self._pg,
            dispatch=self._dispatch,
            media=self._media,
            subscriptions=self._subscriptions,
        )
        self._workers.append(w)
        if self._tasks:
            self._tasks.append(asyncio.create_task(w.run()))
        return w

    async def start(self) -> None:
        for w in self._workers:
            await w.bind()
        self._tasks = [asyncio.create_task(w.run()) for w in self._workers]

    async def stop(self) -> None:
        for w in self._workers:
            w.stop()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        # Best-effort unbind of each worker's pull subscription so a later
        # NatsStore.close()/drain() doesn't block on a live consumer binding
        # (the source of the ~30s DrainTimeoutError on host shutdown).
        for w in self._workers:
            psub = getattr(w, "_psub", None)
            if psub is None:
                continue
            try:
                await psub.unsubscribe()
            except Exception:  # pragma: no cover — best-effort cleanup
                logger.debug("worker.unsubscribe failed (already gone)")
            w._psub = None

    async def drain_until_empty(self, *, max_rounds: int = 200) -> int:
        """Drive every worker's ``drain_once`` until the queue depth is 0.

        Test/operator helper — runs the pool to completion without a long-
        lived loop. Returns the total number of ``completed`` results.
        """
        for w in self._workers:
            await w.bind()
        completed = 0
        for _ in range(max_rounds):
            results = await asyncio.gather(
                *(w.drain_once() for w in self._workers)
            )
            completed += sum(
                1 for batch in results for r in batch if r.status == "completed"
            )
            pending = await self._queue.consumer_pending()
            if pending == 0:
                # One more drain to mop up in-flight redeliveries, then stop.
                final = await asyncio.gather(
                    *(w.drain_once() for w in self._workers)
                )
                completed += sum(
                    1 for batch in final for r in batch if r.status == "completed"
                )
                if await self._queue.consumer_pending() == 0:
                    break
        return completed

    @property
    def total_processed(self) -> int:
        return sum(w.processed for w in self._workers)

    @property
    def total_skipped_duplicate(self) -> int:
        return sum(w.skipped_duplicate for w in self._workers)

    @property
    def total_reaped(self) -> int:
        return sum(w.reaped for w in self._workers)


# ---------------------------------------------------------------------------
# NATS msg ack helpers — tolerate broker hiccups.
# ---------------------------------------------------------------------------


async def _ack(msg) -> None:
    try:
        await msg.ack()
    except Exception:  # pragma: no cover
        logger.debug("ack failed (already acked / timed out)")


async def _nak(msg, *, delay: float | None = None) -> None:
    """Negative-ack a message, deferring redelivery by ``delay`` seconds.

    C-3 / 2.6: a BARE ``nak()`` redelivers immediately. When a sibling worker
    holds the idempotency claim, every redelivery instantly re-naks → the
    broker burns ``max_deliver`` in seconds and the message is gone before the
    claim holder finishes (or before the reaper can re-enqueue a crashed
    holder's work). A nak WITH a delay >= the claim's remaining lease spaces
    redeliveries out so the holder has time to complete and the redelivery
    budget is not torched.
    """
    try:
        if delay is not None:
            await msg.nak(delay=delay)
        else:
            await msg.nak()
    except Exception:  # pragma: no cover
        logger.debug("nak failed")


async def _term(msg) -> None:
    try:
        await msg.term()
    except Exception:  # pragma: no cover
        logger.debug("term failed")


def _delivered(msg) -> int:
    """Best-effort JetStream delivery count for this message."""
    try:
        md = msg.metadata
        return int(md.num_delivered)
    except Exception:
        return 0


__all__ = ["JobWorker", "JobWorkerPool"]
