# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single-replica fail-loud guard + Postgres-advisory-lock leader election.

The runtime hosts a few **singleton control-plane loops** — the
:class:`~legba.runtime.reconcile.ReconcileLoop` periodic resync and the
:class:`~legba.runtime.nats_informer.NatsReconcileInformer` — that MUST run on
exactly one replica. The hot ingest/analysis path is already replica-safe (Dapr
placement consistent-hashes actors across replicas; the coalescer is CAS-guarded
against double-fire; the trigger engine shares ONE durable PULL consumer that
load-balances across replicas). But the singleton loops are NOT: each replica's
resync would walk the full registry and (re-)issue CREATE/RETIRE/TRANSITION
actor mutations, and the informer takes a *per-instance* durable consumer
(``DeliverPolicy.NEW``), so under the descriptor-events stream's interest
retention each replica receives every event → duplicate enqueue → duplicate
control-plane mutation. Reconcile is idempotent, so the worst case is wasted
work and noisy logs, not corruption — but it is wrong, and a naive
``deploy.replicas: 2`` would silently double-run it.

This module provides two primitives, both keyed off two env vars:

  * ``LEGBA_REPLICA_COUNT`` — the operator's *declared* replica count for this
    deployment (default ``1``). The runtime cannot introspect how many sibling
    replicas the orchestrator actually launched, so this is the operator's
    assertion of intent. It can drift from reality; that is the known limit of
    the interim guard, and why item (b) — real leader election — exists.
  * ``LEGBA_LEADER_ELECTION`` — the election mechanism. Empty/unset = OFF
    (single-replica posture). ``"pg-advisory"`` (or the alias ``"1"`` /
    ``"true"``) = a Postgres **session-level advisory lock** elects exactly one
    leader across all replicas, regardless of the declared count.

**(a) fail-loud guard** — :func:`assert_singleton_safe` refuses to boot when
``LEGBA_REPLICA_COUNT > 1`` and leader election is OFF. This converts the silent
double-mount into a loud :class:`SingletonSafetyError` at startup. (Declared as
a SEAM in ``docs/SEAMS.md`` — the guard relies on an operator-declared count, so
it is fail-loud-on-the-honest-config, not a perfect runtime headcount.)

**(b) leader election** — :class:`LeaderLease` wraps
``pg_try_advisory_lock(key)`` held on a **dedicated, long-lived connection**.
The lock is *session-scoped*: it survives for the life of that connection and is
released automatically by Postgres if the connection drops (crash, network
partition) — so a dead leader's lock frees within the TCP/keepalive window and a
standby acquires it on its next attempt. The lease loop re-tries acquisition on
a cadence so a standby promotes itself after the leader dies, and it heals its
own connection if the dedicated connection is lost while still leader.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Env knobs (documented in the module docstring + docker-compose.replicas.yml).
REPLICA_COUNT_ENV = "LEGBA_REPLICA_COUNT"
LEADER_ELECTION_ENV = "LEGBA_LEADER_ELECTION"

# Truthy values of LEGBA_LEADER_ELECTION that select the pg-advisory mechanism.
_PG_ADVISORY_VALUES = frozenset({"pg-advisory", "pg_advisory", "1", "true", "on", "yes"})

# Stable 64-bit advisory-lock key for the runtime singleton loops. Advisory
# locks share a global namespace per-database; a fixed, documented constant
# keeps every replica contending for the SAME lock. Derived as a fixed literal
# (not a hash) so it's greppable + stable across releases. Mnemonic: "legba
# reconcile singleton leader".
RECONCILE_LEADER_LOCK_KEY = 0x1E6BA_5106  # 0x1E6BA5106 — "LEGBA" + "SING"-ish nibble tag


class SingletonSafetyError(RuntimeError):
    """Raised at boot when >1 replica is declared without leader election.

    Fail-loud: the runtime refuses to start rather than silently double-running
    the singleton control-plane loops across replicas.
    """


def replica_count() -> int:
    """Operator-declared replica count for this deployment (default 1).

    Reads ``LEGBA_REPLICA_COUNT``. A non-integer or <1 value is clamped to 1
    with a warning — a malformed knob must not let the guard pass by accident,
    so it fails *safe* (treated as single-replica, which the guard always
    permits) while logging loudly.
    """
    raw = os.getenv(REPLICA_COUNT_ENV, "").strip()
    if not raw:
        return 1
    try:
        n = int(raw)
    except ValueError:
        logger.warning(
            "leader.replica_count.malformed %s=%r — treating as 1", REPLICA_COUNT_ENV, raw,
        )
        return 1
    if n < 1:
        logger.warning(
            "leader.replica_count.nonpositive %s=%d — treating as 1", REPLICA_COUNT_ENV, n,
        )
        return 1
    return n


def leader_election_enabled() -> bool:
    """True iff ``LEGBA_LEADER_ELECTION`` selects a real election mechanism."""
    return os.getenv(LEADER_ELECTION_ENV, "").strip().lower() in _PG_ADVISORY_VALUES


def assert_singleton_safe() -> None:
    """Fail loud if the runtime would silently double-run its singleton loops.

    Called once at the top of production bring-up. The contract:

      * ``LEGBA_REPLICA_COUNT <= 1``                 → always OK (single node).
      * ``LEGBA_REPLICA_COUNT > 1`` + election ON    → OK (leader election will
        gate the singleton loops to one replica).
      * ``LEGBA_REPLICA_COUNT > 1`` + election OFF   → :class:`SingletonSafetyError`.

    This is the interim guard from ``planning/SCALING.md`` §scaling-multinode:
    it cannot count sibling replicas at runtime (no orchestrator introspection),
    so it trusts the operator-declared count — converting a *silently wrong*
    multi-replica deploy into a *loud refusal at boot*.
    """
    n = replica_count()
    elected = leader_election_enabled()
    if n > 1 and not elected:
        raise SingletonSafetyError(
            f"{REPLICA_COUNT_ENV}={n} (>1) but {LEADER_ELECTION_ENV} is unset/off. "
            "Running >1 runtime replica without leader election would double-run "
            "the singleton control-plane loops (reconcile resync + descriptor "
            "informer) on every replica. Set "
            f"{LEADER_ELECTION_ENV}=pg-advisory to enable Postgres-advisory-lock "
            f"leader election, or set {REPLICA_COUNT_ENV}=1 for a single-node "
            "deployment. See docs/SEAMS.md + planning/SCALING.md."
        )
    logger.info(
        "leader.singleton_guard.ok replica_count=%d leader_election=%s",
        n, "on" if elected else "off",
    )


class LeaderLease:
    """Postgres-advisory-lock leader election over a dedicated connection.

    Exactly one replica across the cluster holds the session-level advisory lock
    on :data:`RECONCILE_LEADER_LOCK_KEY` at a time. The holder is the *leader*
    and runs the singleton loops; the others stand by, re-attempting on a
    cadence so one of them promotes after the leader dies (Postgres releases a
    session lock when its owning connection drops).

    Usage::

        lease = LeaderLease(pg_store)
        await lease.start(on_acquire=..., on_lose=...)
        ...
        if lease.is_leader:
            ...
        await lease.stop()

    ``on_acquire`` fires (once) when this replica becomes leader; ``on_lose``
    fires if it ever loses the lock (only via connection death — we never
    voluntarily release while running). Both are optional coroutines.

    When ``LEGBA_LEADER_ELECTION`` is OFF the lease is a **no-op leader**:
    ``is_leader`` is True immediately and no lock is taken (single-node posture —
    the one replica is trivially the leader). This lets bring-up gate the
    singleton loops on ``is_leader`` unconditionally.
    """

    def __init__(
        self,
        pg_store: Any,
        *,
        lock_key: int = RECONCILE_LEADER_LOCK_KEY,
        acquire_interval_seconds: float = 10.0,
        identity: str | None = None,
    ) -> None:
        self._pg = pg_store
        self._lock_key = lock_key
        self._interval = acquire_interval_seconds
        # A human-readable identity for the logs (which replica won) — the
        # container hostname is the natural per-replica discriminator.
        self._identity = identity or os.getenv("HOSTNAME", "") or "replica"
        self._enabled = leader_election_enabled()
        self._is_leader = not self._enabled  # no-op leader when election is off
        self._conn: Any | None = None  # dedicated connection holding the lock
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._on_acquire: Callable[[], Awaitable[None]] | None = None
        self._on_lose: Callable[[], Awaitable[None]] | None = None

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(
        self,
        *,
        on_acquire: Callable[[], Awaitable[None]] | None = None,
        on_lose: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Begin contending for leadership.

        When election is OFF this is a no-op (``is_leader`` already True); the
        caller's ``on_acquire`` is invoked synchronously so the single-node
        path still starts its singleton loops. When ON, this tries to acquire
        the lock immediately, then launches the background re-attempt loop.
        """
        self._on_acquire = on_acquire
        self._on_lose = on_lose
        if not self._enabled:
            # Single-node: the one replica is the leader by definition.
            if on_acquire is not None:
                await on_acquire()
            logger.info("leader.election.disabled identity=%s — single-node leader", self._identity)
            return
        # First acquisition attempt is synchronous so a cold-boot leader starts
        # its singleton loops immediately rather than after one interval.
        await self._try_acquire()
        self._task = asyncio.create_task(self._loop(), name="legba-leader-lease")
        logger.info(
            "leader.election.started identity=%s lock_key=%#x is_leader=%s",
            self._identity, self._lock_key, self._is_leader,
        )

    async def stop(self) -> None:
        """Stop contending and release the lock (if held).

        Releasing explicitly lets a graceful restart hand leadership over fast
        rather than waiting for the connection-death timeout.
        """
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._release()

    async def _try_acquire(self) -> None:
        """Attempt to acquire (or confirm we still hold) the advisory lock."""
        if self._is_leader and self._conn is not None:
            # Already leader — verify the dedicated connection is still alive so
            # a silently-dropped connection (which Postgres treats as a lock
            # release) demotes us instead of leaving a stale is_leader=True.
            try:
                await self._conn.fetchval("SELECT 1")
                return
            except Exception as exc:
                logger.warning(
                    "leader.connection.lost identity=%s err=%s — demoting + re-contending",
                    self._identity, exc,
                )
                await self._demote()
                # fall through to re-acquire on a fresh connection
        try:
            # A dedicated connection from the pool holds the session lock for as
            # long as we stay leader. asyncpg's pool.acquire() context manager
            # would return the connection on exit, dropping the session lock —
            # so we take a raw connection out of the pool and keep it.
            conn = await self._pg.pool.acquire()
            got = await conn.fetchval(
                "SELECT pg_try_advisory_lock($1)", self._lock_key,
            )
            if got:
                self._conn = conn
                if not self._is_leader:
                    self._is_leader = True
                    logger.info(
                        "leader.acquired identity=%s lock_key=%#x", self._identity, self._lock_key,
                    )
                    if self._on_acquire is not None:
                        await self._on_acquire()
            else:
                # Someone else holds it — return the probe connection to the pool.
                await self._pg.pool.release(conn)
        except Exception as exc:  # pragma: no cover — pool/db outage
            logger.warning("leader.acquire.error identity=%s err=%s", self._identity, exc)

    async def _demote(self) -> None:
        was_leader = self._is_leader
        self._is_leader = False
        await self._release()
        if was_leader and self._on_lose is not None:
            try:
                await self._on_lose()
            except Exception as exc:  # pragma: no cover
                logger.warning("leader.on_lose.error identity=%s err=%s", self._identity, exc)

    async def _release(self) -> None:
        """Release the advisory lock + return its connection to the pool."""
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            await conn.fetchval("SELECT pg_advisory_unlock($1)", self._lock_key)
        except Exception:  # pragma: no cover — connection may already be dead
            pass
        try:
            await self._pg.pool.release(conn)
        except Exception:  # pragma: no cover
            pass

    async def _loop(self) -> None:
        try:
            while not self._stopped:
                await asyncio.sleep(self._interval)
                if self._stopped:
                    return
                await self._try_acquire()
        except asyncio.CancelledError:
            return


__all__ = [
    "LEADER_ELECTION_ENV",
    "LeaderLease",
    "RECONCILE_LEADER_LOCK_KEY",
    "REPLICA_COUNT_ENV",
    "SingletonSafetyError",
    "assert_singleton_safe",
    "leader_election_enabled",
    "replica_count",
]
