# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the scaling-multinode singleton guard + leader election.

Covers:
  * (a) the fail-loud single-replica guard — fires iff >1 replica declared with
    leader election OFF, and passes in every safe configuration;
  * (b) the :class:`LeaderLease` — no-op-leader (election off) path, real
    advisory-lock acquisition with a fake pool, mutual exclusion across two
    replicas contending for one lock, demotion on a dropped connection, and a
    standby promoting after the leader releases.

No live Postgres: a fake asyncpg-shaped pool models ``pg_try_advisory_lock`` /
``pg_advisory_unlock`` semantics (one holder at a time per key, released on
connection close), which is exactly the contract :class:`LeaderLease` relies on.
"""
from __future__ import annotations

import asyncio

import pytest

from legba.runtime.leader import (
    LEADER_ELECTION_ENV,
    REPLICA_COUNT_ENV,
    LeaderLease,
    SingletonSafetyError,
    assert_singleton_safe,
    leader_election_enabled,
    replica_count,
)


# ---------------------------------------------------------------------------
# (a) fail-loud single-replica guard
# ---------------------------------------------------------------------------


def test_guard_ok_single_replica_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REPLICA_COUNT_ENV, raising=False)
    monkeypatch.delenv(LEADER_ELECTION_ENV, raising=False)
    assert replica_count() == 1
    assert leader_election_enabled() is False
    assert_singleton_safe()  # must not raise


def test_guard_ok_single_replica_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REPLICA_COUNT_ENV, "1")
    monkeypatch.delenv(LEADER_ELECTION_ENV, raising=False)
    assert_singleton_safe()


def test_guard_fails_loud_multi_replica_no_election(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REPLICA_COUNT_ENV, "2")
    monkeypatch.delenv(LEADER_ELECTION_ENV, raising=False)
    with pytest.raises(SingletonSafetyError) as ei:
        assert_singleton_safe()
    # The refusal must name both knobs so the operator can act on it.
    msg = str(ei.value)
    assert REPLICA_COUNT_ENV in msg
    assert LEADER_ELECTION_ENV in msg


def test_guard_ok_multi_replica_with_election(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REPLICA_COUNT_ENV, "3")
    monkeypatch.setenv(LEADER_ELECTION_ENV, "pg-advisory")
    assert leader_election_enabled() is True
    assert_singleton_safe()  # election ON → safe


def test_replica_count_malformed_fails_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    # A malformed count must clamp to 1 (fail-safe = single-replica, which the
    # guard always permits) rather than letting an unparseable value slip past.
    monkeypatch.setenv(REPLICA_COUNT_ENV, "not-a-number")
    monkeypatch.delenv(LEADER_ELECTION_ENV, raising=False)
    assert replica_count() == 1
    assert_singleton_safe()


@pytest.mark.parametrize("value", ["pg-advisory", "1", "true", "ON", "yes"])
def test_election_truthy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(LEADER_ELECTION_ENV, value)
    assert leader_election_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "random"])
def test_election_falsy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(LEADER_ELECTION_ENV, value)
    assert leader_election_enabled() is False


# ---------------------------------------------------------------------------
# Fake asyncpg-shaped pool modelling advisory-lock semantics
# ---------------------------------------------------------------------------


class _FakeLockRegistry:
    """Shared lock table: maps lock_key → the connection currently holding it.

    Models Postgres advisory locks: exactly one session holds a given key; a
    second ``pg_try_advisory_lock`` for a held key returns False; closing/
    releasing the holder's connection frees the key.
    """

    def __init__(self) -> None:
        self.held: dict[int, "_FakeConn"] = {}


class _FakeConn:
    def __init__(self, registry: _FakeLockRegistry) -> None:
        self._registry = registry
        self.alive = True

    async def fetchval(self, query: str, *args):  # noqa: ANN001, ANN201
        if not self.alive:
            raise RuntimeError("connection is closed")
        q = query.strip().lower()
        if q.startswith("select 1"):
            return 1
        if "pg_try_advisory_lock" in q:
            key = args[0]
            holder = self._registry.held.get(key)
            if holder is None:
                self._registry.held[key] = self
                return True
            return holder is self  # re-entrant True for the same holder
        if "pg_advisory_unlock" in q:
            key = args[0]
            if self._registry.held.get(key) is self:
                del self._registry.held[key]
                return True
            return False
        raise AssertionError(f"unexpected query: {query!r}")

    def _die(self) -> None:
        """Simulate a dropped connection — Postgres frees its session locks."""
        self.alive = False
        for key, holder in list(self._registry.held.items()):
            if holder is self:
                del self._registry.held[key]


class _FakePool:
    def __init__(self, registry: _FakeLockRegistry) -> None:
        self._registry = registry
        self.checked_out: list[_FakeConn] = []

    async def acquire(self) -> _FakeConn:
        conn = _FakeConn(self._registry)
        self.checked_out.append(conn)
        return conn

    async def release(self, conn: _FakeConn) -> None:
        # Returning a connection to the pool ends its session → frees its locks.
        conn._die()
        if conn in self.checked_out:
            self.checked_out.remove(conn)


class _FakePg:
    def __init__(self, registry: _FakeLockRegistry) -> None:
        self.pool = _FakePool(registry)


# ---------------------------------------------------------------------------
# (b) LeaderLease
# ---------------------------------------------------------------------------


async def test_lease_noop_leader_when_election_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(LEADER_ELECTION_ENV, raising=False)
    pg = _FakePg(_FakeLockRegistry())
    lease = LeaderLease(pg)
    acquired = {"n": 0}

    async def _on_acquire() -> None:
        acquired["n"] += 1

    assert lease.is_leader is True  # no-op leader immediately
    await lease.start(on_acquire=_on_acquire)
    # on_acquire fires so the single-node singleton loops still start.
    assert acquired["n"] == 1
    # No lock taken, no background task.
    assert pg.pool.checked_out == []
    await lease.stop()


async def test_lease_acquires_lock_when_election_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LEADER_ELECTION_ENV, "pg-advisory")
    registry = _FakeLockRegistry()
    pg = _FakePg(registry)
    lease = LeaderLease(pg, acquire_interval_seconds=0.05)
    acquired = {"n": 0}

    async def _on_acquire() -> None:
        acquired["n"] += 1

    assert lease.is_leader is False  # not leader until the lock is taken
    await lease.start(on_acquire=_on_acquire)
    assert lease.is_leader is True
    assert acquired["n"] == 1
    assert lease._lock_key in registry.held
    await lease.stop()
    # Stop releases the lock so a sibling can take over.
    assert lease._lock_key not in registry.held


async def test_two_replicas_mutual_exclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LEADER_ELECTION_ENV, "pg-advisory")
    registry = _FakeLockRegistry()  # ONE shared lock table = one "database"
    pg_a = _FakePg(registry)
    pg_b = _FakePg(registry)
    lease_a = LeaderLease(pg_a, acquire_interval_seconds=0.05, identity="a")
    lease_b = LeaderLease(pg_b, acquire_interval_seconds=0.05, identity="b")

    await lease_a.start()
    await lease_b.start()
    # Exactly one leader.
    assert lease_a.is_leader is True
    assert lease_b.is_leader is False

    await lease_a.stop()
    await lease_b.stop()


async def test_standby_promotes_after_leader_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LEADER_ELECTION_ENV, "pg-advisory")
    registry = _FakeLockRegistry()
    pg_a = _FakePg(registry)
    pg_b = _FakePg(registry)
    promoted_b = {"n": 0}

    async def _b_acquire() -> None:
        promoted_b["n"] += 1

    lease_a = LeaderLease(pg_a, acquire_interval_seconds=0.05, identity="a")
    lease_b = LeaderLease(pg_b, acquire_interval_seconds=0.05, identity="b")
    await lease_a.start()
    await lease_b.start(on_acquire=_b_acquire)
    assert lease_a.is_leader and not lease_b.is_leader

    # Leader a stops → releases the lock → b's re-attempt loop must promote it.
    await lease_a.stop()
    for _ in range(40):  # up to ~2s of 0.05s ticks
        if lease_b.is_leader:
            break
        await asyncio.sleep(0.05)
    assert lease_b.is_leader is True
    assert promoted_b["n"] == 1
    await lease_b.stop()


async def test_leader_demotes_on_dropped_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LEADER_ELECTION_ENV, "pg-advisory")
    registry = _FakeLockRegistry()
    pg = _FakePg(registry)
    lost = {"n": 0}

    async def _on_lose() -> None:
        lost["n"] += 1

    lease = LeaderLease(pg, acquire_interval_seconds=0.05)
    await lease.start(on_lose=_on_lose)
    assert lease.is_leader is True

    # Simulate the dedicated connection dying (network partition / PG restart).
    lease._conn._die()  # type: ignore[union-attr]
    # The next re-attempt tick detects the dead connection, demotes, then
    # re-acquires on a fresh connection (the lock is free again).
    for _ in range(40):
        await asyncio.sleep(0.05)
        if lost["n"] >= 1 and lease.is_leader:
            break
    assert lost["n"] >= 1            # on_lose fired (demotion observed)
    assert lease.is_leader is True   # and it re-acquired (still the only contender)
    await lease.stop()
