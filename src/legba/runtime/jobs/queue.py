# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""NATS JetStream work-queue for the async job plane (P-07 / PIVOT §5.1).

The job plane is the **competing-consumer work-queue** execution shape:

  * One JetStream stream with ``retention=workqueue`` so a message is removed
    once *any* consumer acks it (exactly the competing-consumer semantic — a
    job is processed by one worker, not fanned out).
  * Coarse subjects ``jobs.<job_kind>`` — the work-queue is sharded by kind so
    a worker pool can subscribe to one kind (``jobs.process_media``) or all
    (``jobs.>``). This mirrors the PIVOT coarse-subject rule (the envelope's
    fine-grained routing is in its fields, not the subject).
  * A single shared **durable pull consumer** per stream. Every worker in the
    pool binds to the SAME durable name and ``fetch``es — JetStream load-
    balances delivery across them (NATS-native competing consumers). Adding a
    worker = adding throughput, no rebalancing config.

``ack_wait`` + ``max_deliver`` give at-least-once with redelivery; the
idempotency ledger (:class:`legba.data.jobs.JobStore`) makes it effectively-
once. Slow / failed acks redeliver (incrementing the envelope's ``attempts``
is the worker's job); ``max_deliver`` caps the redelivery storm before a
message lands in the work-queue's dead-letter (a worker that exhausts
``max_deliver`` terms the job as failed in the ledger).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...data.jobs.envelope import JobEnvelope

if TYPE_CHECKING:  # pragma: no cover — typing only
    from ...data.nats import NatsStore

logger = logging.getLogger(__name__)


# The single work-queue stream + the subject wildcard it captures. All job
# kinds publish under ``jobs.<kind>``; one stream owns them all.
JOBS_STREAM = "LEGBA_JOBS"
JOBS_SUBJECT_PREFIX = "jobs"
JOBS_SUBJECT_WILDCARD = f"{JOBS_SUBJECT_PREFIX}.>"

# Default durable pull-consumer name. Every worker in a pool binds this same
# name → JetStream balances delivery across them (competing consumers).
DEFAULT_DURABLE = "legba-job-workers"


def subject_for(job_kind: str, *, prefix: str = JOBS_SUBJECT_PREFIX) -> str:
    """Coarse work-queue subject for a job kind."""
    return f"{prefix}.{job_kind}"


class JobQueue:
    """Thin wrapper over :class:`legba.data.nats.NatsStore` for the job plane.

    Owns the work-queue stream declaration + the durable pull consumer + the
    publish path. Construct one per process; share it across the worker pool.
    """

    def __init__(
        self,
        nats: "NatsStore",
        *,
        stream: str = JOBS_STREAM,
        durable: str = DEFAULT_DURABLE,
        subject_prefix: str = JOBS_SUBJECT_PREFIX,
        # ack_wait MUST exceed the slowest job handler's own timeout, else
        # JetStream redelivers a still-running job (the media handler's HTTP
        # timeout is 120s — MediaClient.timeout_seconds; C-3 / 2.6). At the old
        # 60s a long transcription would be redelivered to a SECOND worker
        # mid-flight: a sibling claim-held nak storm + duplicate work. 180s
        # gives the 120s media handler full headroom plus margin.
        ack_wait_seconds: int = 180,
        max_deliver: int = 5,
        max_age_seconds: int = 86_400,
    ) -> None:
        self._nats = nats
        self._stream = stream
        self._durable = durable
        # The subject space this queue's stream owns (default ``jobs``). A
        # non-default prefix gives an isolated work-queue (e.g. a per-test
        # stream that must not overlap the production LEGBA_JOBS ``jobs.>``).
        self._subject_prefix = subject_prefix
        self._ack_wait = ack_wait_seconds
        self._max_deliver = max_deliver
        self._max_age = max_age_seconds
        self._psub = None  # bound pull subscription (lazy)

    @property
    def durable(self) -> str:
        return self._durable

    @property
    def stream(self) -> str:
        return self._stream

    @property
    def max_deliver(self) -> int:
        return self._max_deliver

    @property
    def ack_wait_seconds(self) -> int:
        """Consumer ack_wait. ``ack_wait × max_deliver`` is the redelivery
        budget — the claim lease the worker-side reaper expires against."""
        return self._ack_wait

    # ------------------------------------------------------------------
    # Topology
    # ------------------------------------------------------------------

    async def ensure_topology(self) -> None:
        """Idempotently declare the work-queue stream + durable consumer.

        Stream: ``retention=workqueue`` (a msg leaves on first ack — the
        competing-consumer guarantee). Consumer: a durable, explicit-ack pull
        consumer shared by the whole pool.
        """
        from nats.js.api import ConsumerConfig, AckPolicy

        await self._nats.ensure_stream(
            self._stream,
            [f"{self._subject_prefix}.>"],
            retention="workqueue",
            max_age_seconds=self._max_age,
            max_msgs=-1,
        )

        js = self._nats.js
        # Idempotently create the shared durable pull consumer.
        try:
            await js.consumer_info(self._stream, self._durable)
        except Exception:
            await js.add_consumer(
                self._stream,
                ConsumerConfig(
                    durable_name=self._durable,
                    ack_policy=AckPolicy.EXPLICIT,
                    ack_wait=self._ack_wait,
                    max_deliver=self._max_deliver,
                    # No filter_subject → consume every jobs.* kind. A
                    # kind-specific pool can pass a filter when it binds.
                ),
            )

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def enqueue(self, env: JobEnvelope) -> None:
        """Publish a job envelope onto the work-queue.

        Publishes with a Nats-Msg-Id header set to the idempotency_key so
        JetStream's own duplicate-window also collapses a double-publish of the
        same key (the broker-side half of idempotency; the ledger is the
        substrate-side half).
        """
        subject = subject_for(env.job_kind, prefix=self._subject_prefix)
        await self._nats.js.publish(
            subject,
            env.to_bytes(),
            headers={"Nats-Msg-Id": env.idempotency_key},
        )
        logger.debug(
            "job.enqueued kind=%s job_id=%s idem=%s subject=%s",
            env.job_kind, env.job_id, env.idempotency_key, subject,
        )

    # ------------------------------------------------------------------
    # Consume (pull) — bound per worker
    # ------------------------------------------------------------------

    async def bind_pull(self):
        """Bind a pull subscription to the shared durable consumer.

        Each worker calls this once and then ``fetch``es. Multiple workers
        binding the same durable share the delivery load.
        """
        js = self._nats.js
        return await js.pull_subscribe(
            f"{self._subject_prefix}.>",
            durable=self._durable,
            stream=self._stream,
        )

    async def consumer_pending(self) -> int:
        """Return the number of unacked / undelivered messages on the queue.

        The pool's depth gauge — drains to 0 when all jobs are processed.
        """
        info = await self._nats.js.consumer_info(self._stream, self._durable)
        return int(info.num_pending) + int(info.num_ack_pending)


__all__ = [
    "DEFAULT_DURABLE",
    "JOBS_STREAM",
    "JOBS_SUBJECT_PREFIX",
    "JOBS_SUBJECT_WILDCARD",
    "JobQueue",
    "subject_for",
]
