# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The registry at ``LEGBA_REGISTRY_WORKERS = N``.

All fakes — no Postgres, NATS or Redis is reached. Two app instances (or two
leases) in one process stand in for two uvicorn workers; what they share is
the fake substrate, exactly what two workers share in production.
"""
from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from legba.data.config import PostgresConfig
from legba.data.registry import workers as W

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _AdvisoryLocks:
    """A database's advisory-lock table, shared by every fake pool on it."""

    def __init__(self) -> None:
        self.held: dict[int, int] = {}  # key -> conn id


class _Conn:
    _next = 0

    def __init__(self, locks: _AdvisoryLocks) -> None:
        _Conn._next += 1
        self.id = _Conn._next
        self.locks = locks
        self.dead = False

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if self.dead:
            raise ConnectionError("connection is closed")
        if sql == "SELECT 1":
            return 1
        if sql.startswith("SELECT pg_try_advisory_lock"):
            key = args[0]
            if key in self.locks.held and self.locks.held[key] != self.id:
                return False
            self.locks.held[key] = self.id
            return True
        if sql.startswith("SELECT pg_advisory_unlock"):
            if self.locks.held.get(args[0]) == self.id:
                del self.locks.held[args[0]]
                return True
            return False
        raise AssertionError(f"unexpected sql {sql!r}")

    def die(self) -> None:
        """The backend went away: Postgres frees its session locks."""
        self.dead = True
        for key, cid in list(self.locks.held.items()):
            if cid == self.id:
                del self.locks.held[key]


class _Pool:
    def __init__(self, locks: _AdvisoryLocks) -> None:
        self.locks = locks
        self.acquired: list[_Conn] = []

    async def acquire(self) -> _Conn:
        conn = _Conn(self.locks)
        self.acquired.append(conn)
        return conn

    async def release(self, conn: _Conn) -> None:
        return None


class _PgStore:
    def __init__(self, locks: _AdvisoryLocks) -> None:
        self.pool = _Pool(locks)


class _NoPool:
    async def acquire(self) -> Any:
        raise AssertionError("one worker must take no lock connection")


# ---------------------------------------------------------------------------
# the worker count
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 1), ("", 1), ("  ", 1), ("1", 1), ("3", 3), (" 2 ", 2),
     ("two", 1), ("0", 1), ("-4", 1), ("1.5", 1)],
)
def test_worker_count_reads_env_and_defaults_to_one(monkeypatch, raw, expected) -> None:
    if raw is None:
        monkeypatch.delenv(W.WORKERS_ENV, raising=False)
    else:
        monkeypatch.setenv(W.WORKERS_ENV, raw)
    assert W.worker_count() == expected
    assert W.multi_worker() is (expected > 1)


def test_compose_passes_the_env_with_default_one() -> None:
    from pathlib import Path

    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    assert 'LEGBA_REGISTRY_WORKERS: "${LEGBA_REGISTRY_WORKERS:-1}"' in compose


def _patch_main(monkeypatch) -> list[tuple[tuple, dict]]:
    from legba.data.registry import server

    calls: list[tuple[tuple, dict]] = []
    sentinel = object()
    monkeypatch.setattr(server, "create_app", lambda **kw: sentinel)
    monkeypatch.setattr(server.uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.delenv("LEGBA_REGISTRY_API_PORT", raising=False)
    monkeypatch.delenv("LEGBA_REGISTRY_API_HOST", raising=False)
    return calls


def test_one_worker_launches_exactly_as_before(monkeypatch) -> None:
    from legba.data.registry import server

    monkeypatch.delenv(W.WORKERS_ENV, raising=False)
    calls = _patch_main(monkeypatch)
    server.main()
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert len(args) == 1 and not isinstance(args[0], str)  # the app OBJECT
    assert kwargs == {"host": "0.0.0.0", "port": 8090, "log_config": None}


def test_n_workers_launch_through_the_factory(monkeypatch) -> None:
    from legba.data.registry import server

    monkeypatch.setenv(W.WORKERS_ENV, "3")
    monkeypatch.setenv("LEGBA_REGISTRY_SIGNING_KEY", "00" * 32)
    calls = _patch_main(monkeypatch)
    server.main()
    args, kwargs = calls[0]
    assert args == (server.APP_FACTORY,)
    assert kwargs == {
        "factory": True, "host": "0.0.0.0", "port": 8090, "workers": 3,
        "log_config": None,
    }
    module, _, attr = server.APP_FACTORY.partition(":")
    assert getattr(sys.modules[module], attr) is server.app_factory


def test_n_workers_refuse_an_ephemeral_signing_key(monkeypatch) -> None:
    from legba.data.registry import server

    monkeypatch.setenv(W.WORKERS_ENV, "2")
    monkeypatch.delenv("LEGBA_REGISTRY_SIGNING_KEY", raising=False)
    monkeypatch.delenv("LEGBA_REGISTRY_SIGNING_KEY_FILE", raising=False)
    calls = _patch_main(monkeypatch)
    with pytest.raises(SystemExit, match="LEGBA_REGISTRY_SIGNING_KEY"):
        server.main()
    assert calls == []


def test_the_pool_is_split_across_workers() -> None:
    cfg = PostgresConfig(pool_min=2, pool_max=50)
    assert W.worker_pg_config(cfg, 1) is cfg
    assert W.worker_pg_config(cfg, 2).pool_max == 25
    assert W.worker_pg_config(cfg, 4).pool_max == 12
    tiny = W.worker_pg_config(PostgresConfig(pool_min=5, pool_max=6), 8)
    assert (tiny.pool_max, tiny.pool_min) == (W.MIN_POOL_PER_WORKER, W.MIN_POOL_PER_WORKER)


def test_run_miss_hint_is_empty_at_one_worker() -> None:
    assert W.run_miss_hint(1) == ""
    hint = W.run_miss_hint(2)
    assert "SEAMS #74" in hint and W.WORKERS_ENV in hint


# ---------------------------------------------------------------------------
# once-only startup work
# ---------------------------------------------------------------------------


def test_one_worker_lease_is_a_no_op_holder() -> None:
    async def go() -> None:
        store = types.SimpleNamespace(pool=_NoPool())
        lease = W.OnceOnlyLease(store, workers=1)
        assert await lease.acquire() is True
        lease.watch(on_promote=None)
        assert lease._task is None
        await lease.stop()

    asyncio.run(go())


def test_once_only_task_runs_once_across_two_workers_and_a_standby_promotes() -> None:
    async def go() -> None:
        locks = _AdvisoryLocks()
        ran: list[str] = []
        leases = [
            W.OnceOnlyLease(_PgStore(locks), workers=2, retry_interval_s=0.01)
            for _ in range(2)
        ]
        for i, lease in enumerate(leases):
            if await lease.acquire():
                ran.append(f"w{i}:start")

            async def promoted(i: int = i) -> None:
                ran.append(f"w{i}:promoted")

            async def demoted(i: int = i) -> None:
                ran.append(f"w{i}:demoted")

            lease.watch(on_promote=promoted, on_demote=demoted)
        assert ran == ["w0:start"]
        assert [lease.is_holder for lease in leases] == [True, False]

        await asyncio.sleep(0.08)  # standby retries while the holder lives
        assert ran == ["w0:start"]

        # the holder WORKER dies: its loop stops and Postgres frees its lock
        leases[0]._task.cancel()
        leases[0]._conn.die()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if "w1:promoted" in ran:
                break
        assert ran == ["w0:start", "w1:promoted"]
        assert leases[1].is_holder
        await asyncio.sleep(0.05)
        assert ran.count("w1:promoted") == 1  # a holder does not re-promote
        for lease in leases:
            await lease.stop()
        assert locks.held == {}

    asyncio.run(go())


def test_a_live_holder_that_loses_its_lock_connection_demotes_then_recontends() -> None:
    async def go() -> None:
        locks = _AdvisoryLocks()
        events: list[str] = []
        lease = W.OnceOnlyLease(_PgStore(locks), workers=2, retry_interval_s=0.01)
        assert await lease.acquire()

        async def promoted() -> None:
            events.append("promoted")

        async def demoted() -> None:
            events.append("demoted")

        lease.watch(on_promote=promoted, on_demote=demoted)
        lease._conn.die()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if "promoted" in events:
                break
        assert events == ["demoted", "promoted"]
        assert lease.is_holder
        await lease.stop()

    asyncio.run(go())


def _two_worker_apps(monkeypatch) -> tuple[list[Any], dict[str, int]]:
    """Two create_app() instances over one fake substrate."""
    from legba.data.registry import server
    from legba.data.registry.stack import StackRegistry
    from legba.data.registry.vocabulary_cache import VocabularyCache

    locks = _AdvisoryLocks()
    counts = {"streams": 0, "health_start": 0}

    class _FakePg(_PgStore):
        def __init__(self, cfg: Any) -> None:
            super().__init__(locks)
            self.cfg = cfg

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            return None

    class _FakeNats:
        def __init__(self, cfg: Any) -> None:
            self.nc = None

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            return None

    async def _streams(_nats: Any) -> list:
        counts["streams"] += 1
        return []

    def _start_health(self: Any) -> None:
        counts["health_start"] += 1

    async def _stop_health(self: Any) -> None:
        return None

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    async def _refresh(self: Any) -> int:
        return 0

    monkeypatch.setenv(W.WORKERS_ENV, "2")
    monkeypatch.setattr(server, "PostgresStore", _FakePg)
    monkeypatch.setattr(server, "NatsStore", _FakeNats)
    monkeypatch.setattr(server, "ensure_runtime_event_streams", _streams)
    monkeypatch.setattr(server, "open_auth_state", _noop)
    monkeypatch.setattr(server, "close_auth_state", _noop)
    monkeypatch.setattr(StackRegistry, "start_health_loop", _start_health)
    monkeypatch.setattr(StackRegistry, "stop_health_loop", _stop_health)
    monkeypatch.setattr(VocabularyCache, "refresh", _refresh)
    apps = [
        server.create_app(
            pg_config=PostgresConfig(), enable_healthcheck_loop=True,
            enable_vocabulary_subscription=False,
        )
        for _ in range(2)
    ]
    return apps, counts


def test_two_workers_provision_streams_and_start_the_health_loop_once(monkeypatch) -> None:
    apps, counts = _two_worker_apps(monkeypatch)

    async def go() -> None:
        async with apps[0].router.lifespan_context(apps[0]):
            async with apps[1].router.lifespan_context(apps[1]):
                assert counts == {"streams": 1, "health_start": 1}

    asyncio.run(go())


def test_the_env_pool_is_split_and_an_explicit_config_is_not(monkeypatch) -> None:
    from legba.data.registry import server

    apps, _ = _two_worker_apps(monkeypatch)
    pg = apps[0].state.registry_deps.descriptor_registry.pg
    assert pg.cfg.pool_max == PostgresConfig().pool_max
    monkeypatch.setenv("LEGBA_DATA_PG_POOL_MAX", "50")
    env_app = server.create_app(enable_vocabulary_subscription=False)
    assert env_app.state.registry_deps.descriptor_registry.pg.cfg.pool_max == 25


# ---------------------------------------------------------------------------
# the rate limiter's buckets
# ---------------------------------------------------------------------------


def _fake_slowapi(monkeypatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    class Limiter:
        def __init__(self, **kwargs: Any) -> None:
            seen.append(kwargs)

    mod = types.ModuleType("slowapi")
    mod.Limiter = Limiter
    monkeypatch.setitem(sys.modules, "slowapi", mod)
    return seen


def test_one_worker_keeps_in_memory_buckets(monkeypatch) -> None:
    from legba.data.registry import rate_limit

    monkeypatch.delenv(W.WORKERS_ENV, raising=False)
    seen = _fake_slowapi(monkeypatch)
    limiter = rate_limit.build_limiter()
    assert limiter.enabled is True
    assert set(seen[0]) == {"key_func", "headers_enabled"}


def test_n_workers_share_buckets_in_redis(monkeypatch) -> None:
    from legba.data.registry import rate_limit

    monkeypatch.setenv(W.WORKERS_ENV, "2")
    monkeypatch.setenv("LEGBA_DATA_REDIS_HOST", "redis")
    monkeypatch.setenv("LEGBA_DATA_REDIS_PASSWORD", "p@ss:w/rd$x")
    seen = _fake_slowapi(monkeypatch)
    rate_limit.build_limiter()
    kw = seen[0]
    assert kw["storage_uri"] == "redis://:p%40ss%3Aw%2Frd%24x@redis:6379/0"
    assert kw["in_memory_fallback_enabled"] is True
    assert kw["key_prefix"] == W.RATE_LIMIT_KEY_PREFIX


# ---------------------------------------------------------------------------
# the vocabulary eviction reaches every worker
# ---------------------------------------------------------------------------


class _FakeNc:
    def __init__(self) -> None:
        self.subs: list[tuple[str, Any, dict]] = []

    async def subscribe(self, subject: str, cb: Any = None, **kwargs: Any) -> Any:
        self.subs.append((subject, cb, kwargs))
        return types.SimpleNamespace(unsubscribe=lambda: None)

    async def publish(self, subject: str) -> None:
        for pattern, cb, _ in self.subs:
            if subject.startswith(pattern.rstrip(">")):
                await cb(types.SimpleNamespace(subject=subject, data=b""))


def test_a_vocabulary_eviction_reaches_both_workers() -> None:
    from legba.data.registry.vocabulary_cache import VOCABULARY_UPDATED_TOPIC, VocabularyCache

    async def go() -> None:
        nc = _FakeNc()  # the one NATS server both workers connect to
        refreshed: list[int] = []
        caches = []
        for i in range(2):
            cache = VocabularyCache(pg_store=None)

            async def refresh(i: int = i) -> int:
                refreshed.append(i)
                return 0

            cache.refresh = refresh  # type: ignore[method-assign]
            await cache.start_subscription(types.SimpleNamespace(nc=nc))
            caches.append(cache)
        # a core subscription, no queue group: every worker gets every event
        assert all("queue" not in kw for _, _, kw in nc.subs)
        await nc.publish(f"{VOCABULARY_UPDATED_TOPIC}.analyst_kind")
        assert sorted(refreshed) == [0, 1]

    asyncio.run(go())


# ---------------------------------------------------------------------------
# the sign-in lockout is shared across workers
# ---------------------------------------------------------------------------


class _FakeRedis:
    """The sorted-set subset ``RedisFailureStore`` uses."""

    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}

    def pipeline(self, transaction: bool = True) -> "_Pipe":
        return _Pipe(self)

    async def delete(self, key: str) -> int:
        return int(self.zsets.pop(key, None) is not None)


class _Pipe:
    def __init__(self, r: _FakeRedis) -> None:
        self.r, self.ops = r, []

    async def __aenter__(self) -> "_Pipe":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def zremrangebyscore(self, key: str, lo: Any, hi: float) -> None:
        self.ops.append(("zrem", key, hi))

    def zcard(self, key: str) -> None:
        self.ops.append(("zcard", key))

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.ops.append(("zadd", key, mapping))

    def expire(self, key: str, seconds: int) -> None:
        self.ops.append(("expire", key))

    async def execute(self) -> list[Any]:
        out: list[Any] = []
        for op in self.ops:
            zset = self.r.zsets.setdefault(op[1], {})
            if op[0] == "zrem":
                for m in [m for m, s in zset.items() if s <= op[2]]:
                    del zset[m]
                out.append(None)
            elif op[0] == "zcard":
                out.append(len(zset))
            elif op[0] == "zadd":
                zset.update(op[2])
                out.append(len(op[2]))
            else:
                out.append(True)
        return out


def test_the_lockout_holds_on_a_second_worker() -> None:
    from legba.data import users as U

    class _NoDb:
        async def fetchrow(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("a locked email must not reach the database")

    async def go() -> None:
        shared = _FakeRedis()
        email = "worker-split@example.invalid"
        first = U.RedisFailureStore(shared)
        for _ in range(U.SIGN_IN_FAILURE_LIMIT):
            await first.record(email)
        second = U.LocalPasswordProvider()  # a fresh worker: empty local counter
        assert not second.failures.blocked(email)
        second.use_failure_store(U.RedisFailureStore(shared))
        with pytest.raises(U.AuthRefused) as refused:
            await second.authenticate(_NoDb(), {"email": email, "password": "guess"})
        assert refused.value.code == "too_many_attempts"

    asyncio.run(go())


def test_workers_module_imports_registry_slim() -> None:
    import os
    import subprocess
    from pathlib import Path

    src = str(Path(__file__).resolve().parents[2] / "src")
    code = (
        "import sys\n"
        "for m in ('feedparser','telethon','warcio','aiobotocore','pycountry',"
        "'networkx','qdrant_client','dapr'):\n"
        "    sys.modules[m] = None\n"
        "import legba.data.registry.workers\n"
        "assert 'legba.runtime' not in sys.modules\n"
        "print('OK')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
        # keep the interpreter's own module path (the container keeps its
        # deps under /install via PYTHONPATH); only PREPEND src.
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (src, os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert out.returncode == 0 and "OK" in out.stdout, out.stderr
