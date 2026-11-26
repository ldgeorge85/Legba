# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The console landing's read latency (``registry/read_latency.py``).

Pure tests (no database): the findings serialiser writes the bytes FastAPI
wrote before, the TTL cache expires and computes once per key under
concurrency, the conditional-GET middleware tags and 304s only what it should,
the two cached routes reuse and stamp their answers and never cache a degraded
one, migrations 0251 / 0257 and their CONCURRENTLY script carry the same
indexes, the item-at-a-time page bytes equal the whole-page bytes, and
the module imports on the slim registry image. The SQL equivalence of the new
source-firing read is pinned against the old one on a real database in
``test_registry_read_latency_db.py``.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data.migrations import MIGRATIONS_DIR
from legba.data.registry import read_latency as rl
from legba.data.registry.read_latency import (
    ConditionalGetMiddleware,
    TTLCache,
    etag_for,
    fast_json_response,
)
from legba.data.registry.substrate_reads_api import (
    FindingRow,
    FindingsJudgmentPage,
    FindingsPage,
    FindingsSummaryPage,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
_SRC = str(REPO / "src")

#: Values that trip naive serialisers: small and huge floats, signed zero,
#: big ints, non-ASCII, line/paragraph separators, quotes, backslashes, control
#: characters, an emoji, a closing script tag, naive and aware datetimes.
TRICKY: dict[str, Any] = {
    "f_small": 1e-05, "f_tiny": 2.5e-07, "f_third": 1 / 3, "f_huge": 1e22,
    "f_negzero": -0.0, "f_whole": 100.0, "f_max": 1.7976931348623157e308,
    "i_big": 2**70, "s": 'é     "q" \\ \x01 \x7f 😀 </script>',
    "nested": [1, [2, {"a": None, "b": True, "c": False}], {"x": 0.5}],
    "when": "2026-11-14T00:00:00+00:00",
}


def _finding(i: int) -> FindingRow:
    return FindingRow(
        id=str(UUID(int=i)), kind="finding", title=f"t{i} ü", body="b\n ",
        confidence=0.1 + i, severity="high", target_id="country_x", target_version=None,
        derived_from=[str(UUID(int=i + 1000))], data={"data": TRICKY, "k": i},
        schema_uri="iglu:x/y/jsonschema/1-0-0", analyst_id="a", analyst_version="v1",
        run_id=str(UUID(int=i + 2000)),
        produced_at=datetime(2026, 11, 14, 1, 2, 3, 456789, tzinfo=timezone.utc),
        created_at=datetime(2026, 11, 14, tzinfo=timezone(timedelta(hours=-7))),
        verification={"faithfulness_score": 1e-05, "claim_verdicts": [TRICKY]},
    )


def _pages() -> list[Any]:
    return [
        FindingsPage(data=[_finding(i) for i in range(3)], next_cursor="abc"),
        FindingsPage(data=[], next_cursor=None),
        FindingsSummaryPage.model_validate({"data": [], "next_cursor": None}),
        FindingsJudgmentPage.model_validate({"data": [], "next_cursor": "z"}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("which", range(4))
async def test_fast_json_is_byte_identical_to_fastapis_own_path(which: int) -> None:
    """The bytes a ``response_model=None`` route wrote for a page model — the
    findings route before and the bytes ``fast_json_response`` writes
    are the same bytes, tricky floats included."""
    page = _pages()[which]
    app = FastAPI()

    @app.get("/old", response_model=None)
    async def old() -> Any:
        return page

    @app.get("/new", response_model=None)
    async def new() -> Any:
        return fast_json_response(page)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        a, b = await c.get("/old"), await c.get("/new")
    assert a.status_code == b.status_code == 200
    assert a.content == b.content
    assert a.headers["content-type"] == b.headers["content-type"]
    assert a.headers["content-length"] == b.headers["content-length"]


def test_fast_json_keeps_small_float_notation() -> None:
    """The reason ``model_dump_json`` is NOT used: it writes ``1e-05`` as
    ``0.00001``. Same value, different bytes."""
    body = fast_json_response(_pages()[0]).body
    assert b"1e-05" in body and b"2.5e-07" in body and b"0.00001" not in body


# ---------------------------------------------------------------------------
# TTLCache
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.mark.asyncio
async def test_ttl_cache_reuses_until_expiry_then_recomputes() -> None:
    clock = _Clock()
    cache: TTLCache[int] = TTLCache(30, clock=clock)
    calls = 0

    async def factory() -> int:
        nonlocal calls
        calls += 1
        return calls

    first = await cache.get_or_compute("k", factory)
    clock.t += 29.9
    again = await cache.get_or_compute("k", factory)
    assert (first.value, again.value, calls) == (1, 1, 1)
    assert again.computed_at == first.computed_at
    clock.t += 0.1  # exactly the TTL: expired
    third = await cache.get_or_compute("k", factory)
    assert (third.value, calls) == (2, 2)
    other = await cache.get_or_compute("other", factory)
    assert other.value == 3


@pytest.mark.asyncio
async def test_ttl_cache_single_flight_under_concurrency() -> None:
    cache: TTLCache[str] = TTLCache(30)
    calls = 0
    gate = asyncio.Event()

    async def factory() -> str:
        nonlocal calls
        calls += 1
        await gate.wait()
        return "v"

    tasks = [asyncio.create_task(cache.get_or_compute("k", factory)) for _ in range(10)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert calls == 1
    assert {r.value for r in results} == {"v"}


@pytest.mark.asyncio
async def test_ttl_cache_never_caches_a_failure_or_a_refused_value() -> None:
    cache: TTLCache[list[int]] = TTLCache(30)
    calls = 0

    async def boom() -> list[int]:
        nonlocal calls
        calls += 1
        raise RuntimeError("db down")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cache.get_or_compute("k", boom)
    assert calls == 2

    async def empty() -> list[int]:
        nonlocal calls
        calls += 1
        return []

    await cache.get_or_compute("k", empty, should_cache=bool)
    await cache.get_or_compute("k", empty, should_cache=bool)
    assert calls == 4
    assert cache.peek("k") is None


@pytest.mark.asyncio
async def test_ttl_cache_failure_reaches_every_concurrent_waiter() -> None:
    cache: TTLCache[int] = TTLCache(30)
    gate = asyncio.Event()

    async def boom() -> int:
        await gate.wait()
        raise ValueError("x")

    tasks = [asyncio.create_task(cache.get_or_compute("k", boom)) for _ in range(3)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, ValueError) for r in results)


def test_ttl_cache_bounds_its_entries_and_refuses_a_zero_ttl() -> None:
    with pytest.raises(ValueError):
        TTLCache(0)
    cache: TTLCache[int] = TTLCache(30, max_entries=2)

    async def run() -> None:
        for k in range(5):
            await cache.get_or_compute(k, lambda k=k: asyncio.sleep(0, result=k))

    asyncio.run(run())
    assert len(cache._entries) == 2  # noqa: SLF001 — the bound is the point


@pytest.mark.asyncio
async def test_ttl_cache_serves_stale_while_exactly_one_refresh_runs() -> None:
    """Past its TTL but inside ``stale_s`` an entry is answered AT ONCE
    and one background refresh replaces it; past both it is gone."""
    clock = _Clock()
    cache: TTLCache[int] = TTLCache(30, stale_s=60, clock=clock)
    calls = 0
    gate = asyncio.Event()

    async def factory() -> int:
        nonlocal calls
        calls += 1
        if calls > 1:
            await gate.wait()
        return calls

    first = await cache.get_or_compute("k", factory)
    clock.t += 31  # expired, inside the stale window
    a = await cache.get_or_compute("k", factory)
    b = await cache.get_or_compute("k", factory)
    assert (a.value, b.value) == (1, 1)  # answered without waiting on the gate
    assert a.computed_at == first.computed_at  # and still says how old it is
    await asyncio.sleep(0)
    assert calls == 2  # ONE refresh for both stale answers
    gate.set()
    for t in list(cache._refreshing):  # noqa: SLF001
        await t
    fresh = await cache.get_or_compute("k", factory)
    assert (fresh.value, calls) == (2, 2)
    clock.t += 30 + 60  # past TTL + stale window: computed inline again
    third = await cache.get_or_compute("k", factory)
    assert (third.value, calls) == (3, 3)


@pytest.mark.asyncio
async def test_ttl_cache_a_failed_or_refused_refresh_keeps_the_last_good_entry() -> None:
    clock = _Clock()
    cache: TTLCache[list[int]] = TTLCache(30, stale_s=60, clock=clock)
    state = {"mode": "ok"}

    async def factory() -> list[int]:
        if state["mode"] == "boom":
            raise RuntimeError("db down")
        return [] if state["mode"] == "empty" else [1]

    await cache.get_or_compute("k", factory, should_cache=bool)
    clock.t += 31
    for mode in ("boom", "empty"):
        state["mode"] = mode
        got = await cache.get_or_compute("k", factory, should_cache=bool)
        assert got.value == [1]
        for t in list(cache._refreshing):  # noqa: SLF001
            await t  # the failure is swallowed on the task, never re-raised
    assert (await cache.get_or_compute("k", factory, should_cache=bool)).value == [1]
    for t in list(cache._refreshing):  # noqa: SLF001
        await t


@pytest.mark.asyncio
async def test_ttl_cache_background_warm_is_joined_by_a_cold_caller() -> None:
    """``refresh_in_background`` on an empty key (the startup warm-up): a
    caller arriving mid-flight waits on THAT computation, not a second one."""
    cache: TTLCache[str] = TTLCache(30, stale_s=60)
    calls = 0
    gate = asyncio.Event()

    async def factory() -> str:
        nonlocal calls
        calls += 1
        await gate.wait()
        return "warm"

    task = cache.refresh_in_background("k", factory)
    assert task is not None
    assert cache.refresh_in_background("k", factory) is None  # single flight
    waiter = asyncio.create_task(cache.get_or_compute("k", factory))
    await asyncio.sleep(0)
    gate.set()
    assert (await waiter).value == "warm"
    await task
    assert calls == 1


def test_ttl_cache_refuses_a_negative_stale_window() -> None:
    with pytest.raises(ValueError):
        TTLCache(30, stale_s=-1)
    assert TTLCache(30).stale_s == 0.0  # off unless a route opts in


def test_stamp_as_of_names_when_and_how_old() -> None:
    from starlette.responses import Response

    then = datetime.now(timezone.utc) - timedelta(seconds=12)
    r = Response()
    rl.stamp_as_of(r, rl.Cached(value=1, computed_at=then))
    assert r.headers[rl.AS_OF_HEADER] == then.isoformat()
    assert 11 <= int(r.headers["Age"]) <= 13


# ---------------------------------------------------------------------------
# ConditionalGetMiddleware
# ---------------------------------------------------------------------------


def _mw_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/findings")
    async def findings() -> dict:
        return {"rows": [1, 2, 3], "s": "é"}

    @app.get("/api/v1/v3/system/source-firing")
    async def big() -> Any:
        from starlette.responses import Response

        return Response(b"[" + b"1," * 300_000 + b"1]", media_type="application/json")

    @app.get("/api/v1/journal_proposals")
    async def not_listed() -> dict:
        return {"x": 1}

    @app.get("/api/v1/signals")
    async def failing() -> Any:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="down")

    @app.post("/api/v1/findings")
    async def write() -> dict:
        return {"ok": True}

    app.add_middleware(ConditionalGetMiddleware)
    return app


@pytest.mark.asyncio
async def test_conditional_get_tags_and_answers_304_on_a_match() -> None:
    async with AsyncClient(transport=ASGITransport(app=_mw_app()), base_url="http://t") as c:
        r = await c.get("/api/v1/findings")
        assert r.status_code == 200
        assert r.json() == {"rows": [1, 2, 3], "s": "é"}
        tag = r.headers["etag"]
        assert tag == etag_for(r.content)
        assert r.headers["cache-control"] == "private, max-age=5"
        assert "Authorization" in r.headers["vary"]
        assert int(r.headers["content-length"]) == len(r.content)

        again = await c.get("/api/v1/findings", headers={"If-None-Match": tag})
        assert again.status_code == 304 and again.content == b""
        assert again.headers["etag"] == tag

        stale = await c.get("/api/v1/findings", headers={"If-None-Match": 'W/"nope"'})
        assert stale.status_code == 200 and stale.content == r.content

        big = await c.get("/api/v1/v3/system/source-firing")  # hashed off-loop
        assert big.status_code == 200 and big.headers["etag"] == etag_for(big.content)


@pytest.mark.asyncio
async def test_conditional_get_leaves_everything_else_alone() -> None:
    async with AsyncClient(transport=ASGITransport(app=_mw_app()), base_url="http://t") as c:
        nl = await c.get("/api/v1/journal_proposals")
        assert nl.status_code == 200 and "etag" not in nl.headers
        assert "cache-control" not in nl.headers
        err = await c.get("/api/v1/signals", headers={"If-None-Match": "*"})
        assert err.status_code == 503 and "etag" not in err.headers
        post = await c.post("/api/v1/findings")
        assert post.status_code == 200 and "etag" not in post.headers


def test_server_mounts_the_conditional_get_middleware_outside_the_display_policy() -> None:
    src = (REPO / "src/legba/data/registry/server.py").read_text(encoding="utf-8")
    display = src.index("app.add_middleware(DisplayPolicyMiddleware)")
    cond = src.index("app.add_middleware(ConditionalGetMiddleware)")
    assert cond > display  # added later = wraps it = tags the bytes served


# ---------------------------------------------------------------------------
# The two cached routes, over a fake pool
# ---------------------------------------------------------------------------


class _FakePool:
    """``acquire()`` hands out a connection whose ``fetch`` answers by SQL."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.fetches: list[str] = []
        self.open = 0
        self.max_open = 0

    @asynccontextmanager
    async def acquire(self):
        self.open += 1
        self.max_open = max(self.max_open, self.open)
        try:
            yield self
        finally:
            self.open -= 1

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.fetches.append(sql)
        await asyncio.sleep(0.01)
        return self.answer(sql)


def _firing_row(sid: str) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "source_id": sid, "state": "active", "cadence_raw": "*/30 * * * *",
        "signals_24h": 3, "signals_7d": 9, "last_seen_at": now, "age_seconds": 60,
        "signals_recent": True, "last_poll_outcome": "success", "recent_error_count": 0,
    }


def _v3_client(pool: _FakePool) -> AsyncClient:
    from legba.data.registry.v3_api import build_v3_router

    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=pool))
    app = FastAPI()
    app.include_router(build_v3_router(deps), prefix="/api/v1/v3")  # type: ignore[arg-type]
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.fixture
def _open_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    from legba.data.registry.api import API_TOKEN_ENV

    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    monkeypatch.setenv("LEGBA_DEV_MODE", "1")


@pytest.mark.asyncio
async def test_source_firing_reads_once_for_concurrent_asks_and_stamps_its_age(_open_bearer) -> None:
    pool = _FakePool(lambda sql: [_firing_row("src_a"), _firing_row("src_b")])
    async with _v3_client(pool) as c:
        rs = await asyncio.gather(*[c.get("/api/v1/v3/system/source-firing") for _ in range(3)])
        later = await c.get("/api/v1/v3/system/source-firing")
    assert len(pool.fetches) == 1
    assert "idx_signals_source_created_at" not in pool.fetches[0]  # SQL, not a hint
    assert "LEFT JOIN LATERAL" in pool.fetches[0]
    bodies = {r.content for r in [*rs, later]}
    assert len(bodies) == 1
    assert [row["source_id"] for row in later.json()] == ["src_a", "src_b"]
    assert later.json()[0]["status"] == "firing"
    for r in [*rs, later]:
        assert r.status_code == 200
        assert datetime.fromisoformat(r.headers[rl.AS_OF_HEADER]) <= datetime.now(timezone.utc)
        assert int(r.headers["Age"]) >= 0


@pytest.mark.asyncio
async def test_source_firing_does_not_cache_the_degraded_empty_answer(_open_bearer) -> None:
    state = {"fail": True}

    def answer(sql: str) -> list[Any]:
        if state["fail"]:
            raise RuntimeError("statement timeout")
        return [_firing_row("src_a")]

    pool = _FakePool(answer)
    async with _v3_client(pool) as c:
        first = await c.get("/api/v1/v3/system/source-firing")
        state["fail"] = False
        second = await c.get("/api/v1/v3/system/source-firing")
    assert first.status_code == 200 and first.json() == []
    assert [r["source_id"] for r in second.json()] == ["src_a"]
    assert len(pool.fetches) == 2


def _judge_client(pool: _FakePool) -> AsyncClient:
    from legba.data.registry.judge_stats_api import build_judge_stats_router

    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=pool))
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_judge_stats_router(deps), prefix="/api/v1/v3")  # type: ignore[arg-type]
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio
async def test_judge_stats_runs_its_reads_concurrently_and_reuses_the_payload(_open_bearer) -> None:
    pool = _FakePool(lambda sql: [])
    async with _judge_client(pool) as c:
        a, b = await asyncio.gather(
            c.get("/api/v1/v3/system/judge-stats", params={"days": 14}),
            c.get("/api/v1/v3/system/judge-stats", params={"days": 14}),
        )
        n = len(pool.fetches)
        again = await c.get("/api/v1/v3/system/judge-stats", params={"days": 14})
        assert len(pool.fetches) == n  # served from the cache
        other = await c.get("/api/v1/v3/system/judge-stats", params={"days": 7})
    assert n == 5  # cube, receipts, health calls, health streak, uncited — once
    assert pool.max_open >= 3  # on separate connections, at the same time
    assert a.content == b.content == again.content
    body = a.json()
    assert body["measured"] is True and body["health"]["measured"] is True
    assert body["uncited"]["measured"] is True
    assert body["generated_at"]  # the payload's own as-of
    assert other.json()["window_days"] == 7 and len(pool.fetches) == 10


@pytest.mark.asyncio
async def test_judge_stats_degraded_read_is_blank_and_never_cached(_open_bearer) -> None:
    from legba.data.registry import judge_stats_api as js

    state = {"fail_cube": True}

    def answer(sql: str) -> list[Any]:
        if state["fail_cube"] and sql == js._CUBE_SQL:  # noqa: SLF001
            raise RuntimeError("cube timeout")
        return []

    pool = _FakePool(answer)
    async with _judge_client(pool) as c:
        first = (await c.get("/api/v1/v3/system/judge-stats")).json()
        state["fail_cube"] = False
        second = (await c.get("/api/v1/v3/system/judge-stats")).json()
    assert first["measured"] is False
    assert second["measured"] is True


@pytest.mark.asyncio
async def test_judge_stats_a_failed_health_read_still_answers_but_is_not_cached(_open_bearer) -> None:
    from legba.data.registry import judge_stats_api as js

    state = {"fail": True}

    def answer(sql: str) -> list[Any]:
        if state["fail"] and sql == js._HEALTH_CALLS_SQL:  # noqa: SLF001
            raise RuntimeError("health timeout")
        return []

    pool = _FakePool(answer)
    async with _judge_client(pool) as c:
        first = (await c.get("/api/v1/v3/system/judge-stats")).json()
        state["fail"] = False
        second = (await c.get("/api/v1/v3/system/judge-stats")).json()
    assert first["measured"] is True and first["health"]["measured"] is False
    assert second["health"]["measured"] is True


# ---------------------------------------------------------------------------
# Migrations 0251 / 0257 and their out-of-transaction script
# ---------------------------------------------------------------------------

MIGRATION = MIGRATIONS_DIR / "0251_signals_source_created_at.sql"


def _script():
    spec = importlib.util.spec_from_file_location(
        "create_read_latency_indexes", REPO / "scripts" / "create_read_latency_indexes.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


MIGRATION_0257 = MIGRATIONS_DIR / "0257_critique_and_checkpoint_read_indexes.sql"


def test_script_index_definitions_are_the_migrations() -> None:
    script = _script()
    assert set(script.MIGRATION_OF) == set(script.INDEXES)
    for name, (_table, definition) in script.INDEXES.items():
        text = _norm((MIGRATIONS_DIR / script.MIGRATION_OF[name]).read_text())
        assert _norm(f"CREATE INDEX IF NOT EXISTS {name} {definition};") in text, name


def test_every_index_a_migration_builds_is_in_the_script() -> None:
    """An index 0251/0257 skips on a big table must be one the script can build."""
    script = _script()
    for mig in (MIGRATION, MIGRATION_0257):
        built = set(re.findall(r"CREATE INDEX IF NOT EXISTS (\w+)", mig.read_text()))
        assert built and built <= set(script.INDEXES), built - set(script.INDEXES)


def test_0257_number_is_taken_once() -> None:
    assert [p.name for p in MIGRATIONS_DIR.glob("0257_*.sql")] == [MIGRATION_0257.name]


@pytest.mark.parametrize("mig", [MIGRATION, MIGRATION_0257], ids=lambda p: p.name[:4])
def test_read_index_migrations_are_additive_and_bounded(mig: pathlib.Path) -> None:
    body = mig.read_text()
    code = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("--"))
    assert "lock_timeout" in code
    assert "50000" in code  # built inline only on a small table
    # the runner wraps the file in a transaction: no concurrent build in it
    assert not re.search(r"CREATE INDEX CONCURRENTLY", code)
    assert not re.search(r"\b(DROP|UPDATE|DELETE|COMMIT|ALTER)\b", code)
    assert not re.search(r"^\s*BEGIN\s*;", code, re.M)


def test_critique_reads_carry_the_indexed_predicates() -> None:
    """The 0257 partial indexes serve only queries whose WHERE implies
    ``kind = 'critique'`` (a literal) — pin that the readers they were
    measured for still say so, with the indexed expression spelled the same."""
    from legba.data.registry import judge_stats_api as js
    from legba.data.registry import production_gauge_integrity as pgi
    from legba.data.registry import v3_api

    for sql in (js._CUBE_SQL, js._HEALTH_STREAK_SQL, js._UNCITED_SQL,  # noqa: SLF001
                pgi._JUDGE_SQL):  # noqa: SLF001
        assert re.search(r"\w\.kind = 'critique'", sql)
        assert re.search(r"\w\.created_at > \$1", sql)
    sc = _norm(v3_api._SCORECARD_FOR_ANALYST_SQL)  # noqa: SLF001
    assert ("WHERE kind = 'critique' AND data->>'analyzed_analyst_id' = $1::text "
            "ORDER BY created_at DESC, id DESC") in sc
    assert "WHERE kind = 'critique' ORDER BY created_at DESC, id DESC" in _norm(
        v3_api._SCORECARD_SQL)  # noqa: SLF001


def test_uncited_sort_carries_scalars_not_the_block() -> None:
    """The GROUP BY reads only the second LATERAL's scalars."""
    from legba.data.registry.judge_stats_api import _UNCITED_SQL

    head = _norm(_UNCITED_SQL).split(" FROM public.analyst_outputs o ")[0]
    assert "x.v" not in head and "y.fact_sentences" in head


def test_the_route_sql_reads_what_the_index_serves() -> None:
    from legba.data.registry.v3_api import _SOURCE_FIRING_SQL

    sql = _norm(_SOURCE_FIRING_SQL)
    assert "WHERE x.source_id = h.source_id AND x.created_at > now() - interval '7 days'" in sql
    assert "SELECT max(x.created_at) AS last_seen_at FROM public.signals x WHERE x.source_id = h.source_id" in sql
    assert "GROUP BY" not in sql  # no whole-table aggregate left


# ---------------------------------------------------------------------------
# Slim image
# ---------------------------------------------------------------------------


def test_read_latency_is_a_leaf_module() -> None:
    """stdlib + starlette + pydantic only — nothing the slim image lacks."""
    tree = ast.parse((REPO / "src/legba/data/registry/read_latency.py").read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports: a leaf module"
            roots.add((node.module or "").split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"starlette", "pydantic", "__future__"}
    assert roots <= allowed, roots - allowed


def test_changed_routes_import_without_the_runtime_stack() -> None:
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore"):
            sys.modules[blocked] = None
        import legba.data.registry.read_latency
        import legba.data.registry.substrate_reads_api
        import legba.data.registry.v3_api
        import legba.data.registry.judge_stats_api
        leaked = sorted(m for m in sys.modules
                        if m.startswith("legba.data.analysts.deterministic_handlers"))
        assert not leaked, leaked
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


@pytest.mark.parametrize("which", range(4))
def test_page_json_bytes_is_fast_json_response(which: int) -> None:
    """The item-at-a-time page bytes are the whole-page bytes."""
    page = _pages()[which]
    assert rl.page_json_bytes(page) == fast_json_response(page).body


def test_page_json_bytes_other_field_order_and_values() -> None:
    from pydantic import BaseModel

    class Item(BaseModel):
        x: float
        s: str

    class Odd(BaseModel):
        before: dict[str, Any]
        data: list[Item]
        after: str | None = None

    page = Odd(before={"k": TRICKY}, data=[Item(x=1e-05, s="é"), Item(x=-0.0, s="\u2028")])
    assert rl.page_json_bytes(page) == fast_json_response(page).body
    empty = Odd(before={}, data=[])
    assert rl.page_json_bytes(empty) == fast_json_response(empty).body


@pytest.mark.asyncio
async def test_off_loop_response_is_the_same_response() -> None:
    page = _pages()[0]
    a = fast_json_response(page)
    b = await rl.page_json_response_off_loop(lambda: page)
    assert a.body == b.body and a.status_code == b.status_code
    assert a.headers["content-type"] == b.headers["content-type"]


@pytest.mark.asyncio
async def test_off_loop_builds_on_a_worker_thread() -> None:
    import threading

    seen: list[str] = []

    def build() -> Any:
        seen.append(threading.current_thread().name)
        return _pages()[0]

    await rl.page_json_response_off_loop(build)
    assert seen and seen[0] != threading.current_thread().name


def test_parse_json_text_matches_the_pool_codec() -> None:
    """The full findings read selects its jsonb columns as text; parsing them
    is the pool codec's own ``json.loads`` — same values, other keys untouched."""
    from legba.data.registry.substrate_reads_folds import parse_full_row

    doc = {"data": TRICKY, "z": [1, 2.5, None]}
    row = {"id": "x", "data": json.dumps(doc), "verification": None,
           "structural_verification": "null", "title": '{"not": "parsed"}'}
    out = parse_full_row(row)
    assert out["data"] == json.loads(json.dumps(doc))
    assert out["verification"] is None and out["structural_verification"] is None
    assert out["title"] == '{"not": "parsed"}'


def test_full_findings_sql_casts_only_returned_rows() -> None:
    """The ::text casts sit in the outer select, over the ordered page."""
    from legba.data.registry.substrate_reads_folds import full_row_sql

    sql = full_row_sql("f.kind = 'finding'", "LIMIT $1", "AFTER_FOLD_MARK")
    outer, inner = sql.split("FROM (", 1)[0], sql.split("FROM (", 1)[1]
    for col in ("data", "verification", "structural_verification"):
        assert f"p.{col}::text AS {col}" in outer
        assert "::text AS" not in inner.split("AFTER_FOLD_MARK")[0].split("SELECT f.id", 1)[1]
    assert inner.index("AFTER_FOLD_MARK") < inner.index(") p")  # facet sort + LIMIT inside p


def test_analyst_cadence_reads_latest_per_analyst_by_probe() -> None:
    from legba.data.registry.v3_api import _ANALYST_CADENCE_SQL

    sql = _norm(_ANALYST_CADENCE_SQL)
    assert "DISTINCT ON" not in sql
    assert ("WHERE t.analyst_id = a.analyst_id ORDER BY t.run_started_at DESC LIMIT 1"
            in sql)


def test_body_shapes_unchanged_json_roundtrip() -> None:
    """The fast path changes how the bytes are produced, never what they say."""
    page = _pages()[0]
    assert json.loads(fast_json_response(page).body) == json.loads(page.model_dump_json())
