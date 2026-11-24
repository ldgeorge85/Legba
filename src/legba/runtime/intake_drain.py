# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The intake drain — a bounded worker that claims ingestion requests and
runs them (:mod:`.intake_run`).

WHERE IT RUNS: a small loop in the runtime host, started beside the inbound
webhook drain (``source_first_runtime``), behind ``LEGBA_INTAKE_DRAIN``
(off by default — the roll changes nothing until it is switched on). Not an
actor, because the queue's durability is the TABLE, not the process: a claim
is a row with a token and a lease, so a dead worker's request is reclaimed
by whichever drain ticks next, and two drains (two runtime replicas) share
the queue through ``SKIP LOCKED`` claims. An actor would add a reminder on
the shared sidecar (the actor-plane stall watchdog exists for a reason) and
serialise every request through one turn queue, where the drain needs per-
host concurrency across requests. No workflow engine: each request is one
bounded run of the existing poll code.

Each tick: expire queued requests past their expiry, reclaim claims whose
lease expired (their worker died), claim up to ``batch`` runnable requests
(per-host concurrency and minimum interval apply at claim), run them
concurrently, complete or fail each with its claim token. The drift
re-check batch (:mod:`.intake_drift`, its own switch) runs from the same loop
on its declared cadence.

THE EVENT LOOP: everything here is awaits on the database and the network;
the CPU-bound parts of a run (page text extraction, hashing, zstd) already
run in worker threads in the code a poll uses, and the ``page`` kind's
extraction and markup reading do too (``data/sources/page.py``; the example
scraper's as well). Heartbeat tests pin that a drain tick over large pages
never holds the loop (tests/data_pkg/test_ingestion_intake.py and
tests/data_pkg/test_page_kind.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..data.intake_contract import IntakeLimits
from ..data.intake_store import (
    ClaimedRequest,
    claim_requests,
    complete_request,
    expire_requests,
    fail_request,
    reclaim_expired_leases,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DRAIN_ENV",
    "DrainStats",
    "IntakeDrain",
    "intake_drain_enabled",
    "start_intake_drain_if_enabled",
]

#: The switch. Off by default.
DRAIN_ENV = "LEGBA_INTAKE_DRAIN"
_ON = frozenset({"1", "on", "true", "yes"})

Runner = Callable[[ClaimedRequest], Awaitable[Any]]


def intake_drain_enabled() -> bool:
    return os.environ.get(DRAIN_ENV, "").strip().lower() in _ON


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class DrainStats:
    ticks: int = 0
    claimed: int = 0
    done: int = 0
    failed: int = 0
    retried: int = 0
    lost_claims: int = 0
    reclaimed: int = 0
    expired: int = 0
    drift_batches: int = 0


class IntakeDrain:
    """Claim → run → complete/fail, bounded. ``runner`` is
    :func:`legba.runtime.intake_run.run_request` bound to the pool (tests
    pass their own resolver through it)."""

    def __init__(
        self, pool: Any, *, runner: Runner | None = None, batch: int | None = None,
        idle_s: float | None = None, limits: IntakeLimits | None = None,
        worker: str | None = None, drift_batch: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self._pool = pool
        self._runner = runner or self._default_runner
        self._batch = int(batch if batch is not None else _env_num("LEGBA_INTAKE_DRAIN_BATCH", 4))
        self._idle_s = float(idle_s if idle_s is not None else _env_num("LEGBA_INTAKE_DRAIN_IDLE_S", 5.0))
        self._limits = limits or IntakeLimits.from_env()
        self._worker = worker or f"{socket.gethostname()}:{os.getpid()}"
        self._drift_batch = drift_batch
        self._task: asyncio.Task | None = None
        self._stopped = False
        self.stats = DrainStats()

    async def _default_runner(self, req: ClaimedRequest) -> Any:
        from .intake_run import run_request  # noqa: PLC0415 — pulls the actor code

        return await run_request(req, pool=self._pool)

    async def tick(self) -> int:
        """One pass. Returns how many requests were claimed."""
        self.stats.ticks += 1
        self.stats.expired += await expire_requests(self._pool)
        self.stats.reclaimed += len(await reclaim_expired_leases(self._pool))
        claimed = await claim_requests(
            self._pool, worker=self._worker, limit=self._batch, limits=self._limits,
        )
        self.stats.claimed += len(claimed)
        if claimed:
            await asyncio.gather(*(self._run_one(req) for req in claimed))
        return len(claimed)

    async def _run_one(self, req: ClaimedRequest) -> None:
        try:
            outcome = await self._runner(req)
        except Exception as exc:  # a runner bug: fail the request, keep draining
            logger.warning("intake.drain.runner_error id=%s err=%s", req.id, exc, exc_info=True)
            outcome = None
            error, permanent = f"runner_error: {exc}"[:2000], False
        else:
            error, permanent = outcome.error, outcome.permanent
        if error is None:
            ok = await complete_request(
                self._pool, req.id, req.claim_token, result=outcome.result,
                result_version_id=outcome.version_id, records=outcome.records,
            )
            if ok:
                self.stats.done += 1
                logger.info("intake.done id=%s target=%s result=%s", req.id, req.target_kind,
                            {k: v for k, v in outcome.result.items() if k != "poll_outcome"})
            else:
                self.stats.lost_claims += 1
                logger.warning("intake.claim_lost id=%s (lease reclaimed before completion)", req.id)
            return
        state = await fail_request(
            self._pool, req.id, req.claim_token, error=error, permanent=permanent, limits=self._limits,
        )
        if state is None:
            self.stats.lost_claims += 1
        elif state == "failed":
            self.stats.failed += 1
        else:
            self.stats.retried += 1
        logger.warning("intake.run_failed id=%s state=%s err=%s", req.id, state, error)

    async def run_forever(self) -> None:
        while not self._stopped:
            try:
                n = await self.tick()
                if self._drift_batch is not None:
                    if await self._drift_batch():
                        self.stats.drift_batches += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a DB blip: log, back off, keep going
                logger.warning("intake.drain.tick_error err=%s", exc, exc_info=True)
                n = 0
            await asyncio.sleep(0 if n >= self._batch else self._idle_s)

    async def start(self) -> "IntakeDrain":
        if self._task is None or self._task.done():
            self._stopped = False
            self._task = asyncio.create_task(self.run_forever(), name="legba-intake-drain")
        return self

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None


async def start_intake_drain_if_enabled(pool: Any) -> IntakeDrain | None:
    """Bring-up hook: the drain when ``LEGBA_INTAKE_DRAIN`` is on, else None
    (logged either way)."""
    if not intake_drain_enabled():
        logger.info("intake.drain.disabled %s is off", DRAIN_ENV)
        return None
    from .intake_drift import drift_batch_if_due, drift_recheck_enabled  # noqa: PLC0415

    drift = (lambda: drift_batch_if_due(pool)) if drift_recheck_enabled() else None
    drain = await IntakeDrain(pool, drift_batch=drift).start()
    logger.info("intake.drain.started batch=%d drift_recheck=%s", drain._batch, drift is not None)
    return drain
