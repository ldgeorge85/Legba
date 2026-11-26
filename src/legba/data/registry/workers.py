# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The registry at ``LEGBA_REGISTRY_WORKERS = N`` — what is shared, what runs
once, and what stays per process.

The registry ran ONE uvicorn worker. Every analyst run's descriptor GET, every
stack-component GET, the UI's panels and consult all queue behind it. This
module is the one place the worker count is read and the one place the
multi-worker posture is decided; ``server.main`` launches through it.

``LEGBA_REGISTRY_WORKERS`` unset, blank, malformed or < 1 is ONE worker, and
one worker is exactly the launch it was before this module existed: the same
``uvicorn.run(app, ...)`` on an app object, no lock taken, the same pool size,
in-memory rate-limit buckets.

At N > 1, per process state, by verdict:

* SHARED already — the session cookie (Postgres ``user_sessions``), the bearer
  and service-token gates (env read per request, ``hmac.compare_digest``, a
  ``sha256`` principal label: stateless), the sign-in lockout and the consult
  run owners (Redis, ``auth_state``), the descriptor / stack / vault rows
  (Postgres), ``/metrics`` (read from Postgres per scrape), the signing key
  (env — refused at N > 1 when it would be ephemeral, see
  :func:`assert_multi_worker_safe`).
* PER PROCESS AND CORRECT — the vocabulary cache: each worker subscribes on
  core NATS to ``vocabulary.updated.>`` with no queue group, so an eviction
  reaches every worker. The pure memos (route-role regexes, cron budgets).
  The NATS and Redis clients. The step-stream and events-socket relays (a
  NATS subscription per connection, wherever it lands).
* MADE SHARED HERE — the consult / deep-consult rate-limit buckets move to
  Redis (:func:`rate_limit_storage_uri`), so ``10/minute`` is per principal,
  not per principal per worker.
* RUN ONCE HERE — the JetStream stream provisioning and the stack health
  loop run on the worker holding a Postgres session advisory lock
  (:class:`OnceOnlyLease`, the ``runtime/leader.py`` idiom); a standby
  promotes when the holder dies (Postgres drops a dead session's lock).
* SPLIT HERE — the Postgres pool: each worker gets ``pool_max // N`` (floor
  2), so N workers hold the connections one did (:func:`worker_pg_config`).
* PER PROCESS AND NOT SHARED (SEAMS #74) — the consult RUN plane:
  ``ConsultRunManager`` (status, stop, synthesize), the armed-write map and
  the staged confirm codes. A run lives on the worker its POST landed on; a
  ``/consult/runs/{id}`` read or stop, or a ``/consult/writes`` submit, that
  lands on another worker 404s / 403s, with :func:`run_miss_hint` saying why.
  Also per process: the manual stack health check's cache (a check on a
  worker that never saw the component emits ``health_changed`` from
  ``None``).

REGISTRY-SLIM: the stdlib and ``legba.data.config`` only.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from typing import Any, Awaitable, Callable
from urllib.parse import quote

logger = logging.getLogger(__name__)

WORKERS_ENV = "LEGBA_REGISTRY_WORKERS"

#: Postgres advisory-lock key the registry's once-only startup work contends
#: for. Distinct from ``runtime/leader.py:RECONCILE_LEADER_LOCK_KEY``
#: (0x1E6BA5106) — the key space is database-global.
ONCE_ONLY_LOCK_KEY = 0x1E6BA_4E61

#: Floor on a worker's Postgres pool after the split.
MIN_POOL_PER_WORKER = 2

#: The rate limiter's Redis key namespace.
RATE_LIMIT_KEY_PREFIX = "legba:ratelimit"


def worker_count() -> int:
    """``LEGBA_REGISTRY_WORKERS`` — default 1; malformed or < 1 is 1, loudly
    (a bad knob fails SAFE to today's single worker)."""
    raw = os.getenv(WORKERS_ENV, "").strip()
    if not raw:
        return 1
    try:
        n = int(raw)
    except ValueError:
        logger.warning("registry.workers.malformed %s=%r — running 1 worker", WORKERS_ENV, raw)
        return 1
    if n < 1:
        logger.warning("registry.workers.nonpositive %s=%d — running 1 worker", WORKERS_ENV, n)
        return 1
    return n


def multi_worker() -> bool:
    return worker_count() > 1


class MultiWorkerUnsafe(RuntimeError):
    """Raised at launch when N > 1 would split state that must be one."""


def assert_multi_worker_safe(workers: int | None = None) -> None:
    """Refuse N > 1 without a configured signing key: each worker would sign
    the audit trail with its own ephemeral key under one ``signer_did``."""
    n = worker_count() if workers is None else workers
    if n <= 1:
        return
    if not (os.getenv("LEGBA_REGISTRY_SIGNING_KEY", "").strip()
            or os.getenv("LEGBA_REGISTRY_SIGNING_KEY_FILE", "").strip()):
        raise MultiWorkerUnsafe(
            f"{WORKERS_ENV}={n} needs LEGBA_REGISTRY_SIGNING_KEY or "
            "LEGBA_REGISTRY_SIGNING_KEY_FILE: without one every worker signs "
            "audit rows with its own ephemeral key. Set the key, or run 1 worker."
        )


def worker_pg_config(cfg: Any, workers: int | None = None) -> Any:
    """``cfg`` with the pool split across N workers (``pool_max // N``, floor
    :data:`MIN_POOL_PER_WORKER`; ``pool_min`` never above it). N = 1 returns
    ``cfg`` itself."""
    n = worker_count() if workers is None else workers
    if n <= 1:
        return cfg
    pool_max = max(MIN_POOL_PER_WORKER, int(cfg.pool_max) // n)
    return dataclasses.replace(cfg, pool_max=pool_max, pool_min=min(int(cfg.pool_min), pool_max))


def rate_limit_storage_uri(workers: int | None = None) -> str | None:
    """The limiter's shared Redis storage at N > 1, else ``None`` (slowapi's
    in-memory default — today's behaviour)."""
    n = worker_count() if workers is None else workers
    if n <= 1:
        return None
    from ..config import RedisConfig

    cfg = RedisConfig.from_env()
    auth = f":{quote(cfg.password, safe='')}@" if cfg.password else ""
    return f"redis://{auth}{cfg.host}:{cfg.port}/{cfg.db}"


def run_miss_hint(workers: int | None = None) -> str:
    """Appended to a consult-run miss at N > 1 (SEAMS #74); empty at N = 1."""
    n = worker_count() if workers is None else workers
    if n <= 1:
        return ""
    return (
        f" The registry runs {n} workers ({WORKERS_ENV}) and consult runs are "
        "per worker (SEAMS #74): this request may have reached a worker that "
        "does not hold the run."
    )


def log_posture(workers: int) -> None:
    """One line at launch saying what N means here."""
    if workers <= 1:
        return
    logger.warning(
        "registry.workers n=%d — shared: sessions, bearer gates, lockout, run "
        "owners, rate limits (redis); once-only on the advisory-lock holder: "
        "stream provisioning, health loop; PER WORKER (SEAMS #74): consult run "
        "status/stop/synthesize, armed consult writes, confirm codes", workers,
    )


class OnceOnlyLease:
    """Exactly one registry worker runs the once-only startup work.

    A Postgres SESSION advisory lock on :data:`ONCE_ONLY_LOCK_KEY`, held on a
    connection taken out of the pool for the holder's life — the
    ``runtime/leader.py:LeaderLease`` idiom. Postgres releases it when that
    connection dies, and a standby's retry loop promotes it.

    At one worker the lease is a no-op holder: ``is_holder`` is True, no lock
    is taken, no loop runs.
    """

    def __init__(
        self,
        pg_store: Any,
        *,
        workers: int | None = None,
        lock_key: int = ONCE_ONLY_LOCK_KEY,
        retry_interval_s: float = 15.0,
    ) -> None:
        self._pg = pg_store
        self._lock_key = lock_key
        self._interval = retry_interval_s
        self.enabled = (worker_count() if workers is None else workers) > 1
        self._holder = not self.enabled
        self._conn: Any | None = None
        self._task: asyncio.Task | None = None
        self._on_promote: Callable[[], Awaitable[None]] | None = None
        self._on_demote: Callable[[], Awaitable[None]] | None = None

    @property
    def is_holder(self) -> bool:
        return self._holder

    async def acquire(self) -> bool:
        """The first attempt, synchronous with startup. Returns ``is_holder``."""
        if self.enabled:
            await self._try_acquire(fire=False)
            logger.info(
                "registry.once_only pid=%d lock_key=%#x holder=%s",
                os.getpid(), self._lock_key, self._holder,
            )
        return self._holder

    def watch(
        self,
        *,
        on_promote: Callable[[], Awaitable[None]] | None = None,
        on_demote: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Keep contending in the background (N > 1 only): a standby that wins
        the lock runs ``on_promote``; a holder whose lock connection died runs
        ``on_demote``."""
        self._on_promote, self._on_demote = on_promote, on_demote
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="legba-registry-once-only")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self.enabled:
            self._holder = False
            await self._release()

    async def _try_acquire(self, *, fire: bool) -> None:
        if self._holder and self._conn is not None:
            try:
                await self._conn.fetchval("SELECT 1")
                return
            except Exception as exc:  # noqa: BLE001 — the lock went with it
                logger.warning("registry.once_only.lost pid=%d err=%s", os.getpid(), exc)
                self._holder = False
                await self._release()
                if fire and self._on_demote is not None:
                    await self._run_hook(self._on_demote, "demote")
        try:
            conn = await self._pg.pool.acquire()
        except Exception as exc:  # noqa: BLE001 — a pool outage: stay standby
            logger.warning("registry.once_only.acquire_error pid=%d err=%s", os.getpid(), exc)
            return
        try:
            got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", self._lock_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("registry.once_only.acquire_error pid=%d err=%s", os.getpid(), exc)
            got = False
        if not got:
            await self._pg.pool.release(conn)
            return
        self._conn, self._holder = conn, True
        logger.info("registry.once_only.acquired pid=%d", os.getpid())
        if fire and self._on_promote is not None:
            await self._run_hook(self._on_promote, "promote")

    @staticmethod
    async def _run_hook(hook: Callable[[], Awaitable[None]], what: str) -> None:
        try:
            await hook()
        except Exception as exc:  # noqa: BLE001 — a failed hook never kills the lease
            logger.warning("registry.once_only.%s_failed err=%s", what, exc)

    async def _release(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            await conn.fetchval("SELECT pg_advisory_unlock($1)", self._lock_key)
        except Exception:  # noqa: BLE001 — the connection may be dead already
            pass
        try:
            await self._pg.pool.release(conn)
        except Exception:  # noqa: BLE001
            pass

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                await self._try_acquire(fire=True)
        except asyncio.CancelledError:
            return


__all__ = [
    "MIN_POOL_PER_WORKER",
    "MultiWorkerUnsafe",
    "ONCE_ONLY_LOCK_KEY",
    "OnceOnlyLease",
    "RATE_LIMIT_KEY_PREFIX",
    "WORKERS_ENV",
    "assert_multi_worker_safe",
    "log_posture",
    "multi_worker",
    "rate_limit_storage_uri",
    "run_miss_hint",
    "worker_count",
    "worker_pg_config",
]
