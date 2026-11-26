# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-W — ``event_clustering`` writes the same thing, off the event loop.

Live (loop-lag monitor): every tick at :14/:34/:54 held
the runtime's asyncio loop 23-44 s — the cluster phase, the front match walk
(29.5 s over 2,000 open events, ``normalized_levenshtein``'s cell-by-cell DP
the innermost frame of every report) and the tower leg's open-set fold
(5.0 s, 0 cache hits of 2,000) all ran as plain Python on it. These tests pin
the fix's three halves over a live-shaped synthetic substrate
(``_event_clustering_fixture``):

* the pass writes EXACTLY what the pre-N-W pass wrote — the pre-N-W pass is
  reproduced by running the handler with every ``asyncio.to_thread`` call
  inlined on the loop, the prepared cache sized for one producer and the DP
  Levenshtein (kept verbatim in ``test_levenshtein_n_w``) patched back in;
* the prepared cache holds both producers' open sets, so a second tick folds
  nothing, and concurrent folds from threads are exact;
* a live-sized pass never holds the loop (heartbeat test).
"""

from __future__ import annotations

import asyncio
import gc
import json
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _event_clustering_fixture as fx  # noqa: E402
from test_levenshtein_n_w import _dp_normalized  # noqa: E402

from legba.data.analysts.deterministic_handlers import (  # noqa: E402
    _event_matcher,
    event_clustering,
)
from legba.data.analysts.deterministic_handlers._event_candidates import (  # noqa: E402
    tower_producer_id,
)


class _FrozenDatetime(event_clustering.datetime):
    @classmethod
    def now(cls, tz=None):
        return fx.NOW if tz is None else fx.NOW.astimezone(tz)


class _FakeConn:
    def __init__(self) -> None:
        self.watermarks: dict[str, Any] = {}

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetch(self, sql: str, *args: Any):
        assert "FROM alert_trigger_watermarks" in sql
        return [{"watermark_key": k, "state": json.dumps(v)}
                for k, v in self.watermarks.items()]

    async def execute(self, sql: str, *args: Any):
        _class, key, state, _fired = args
        self.watermarks[key] = json.loads(state)

    async def fetchval(self, sql: str, *args: Any):
        return 0

    async def fetchrow(self, sql: str, *args: Any):
        await asyncio.sleep(0)
        return {
            "id": args[0], "event_signature": f"minted/{args[0]}",
            "title": f"minted {args[0]}", "category": "conflict",
            "geo": [], "time_start": None, "time_end": None,
            "updated_at": fx.NOW,
        }


class _Pool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _world(*, events: int, signals: int, tower: int, seed: int):
    front = fx.open_events(events, seed=seed, producer="front")
    back = fx.open_events(events, seed=seed, producer="tower")
    page = fx.signal_page(signals, seed=seed)
    # Tower candidates are cluster candidates of a second page, relabelled.
    seeds, _counts = event_clustering._cluster_page(
        fx.signal_page(max(40, tower * 4), seed=seed + 1000), sort=True,
        threshold=0.5, embedding_threshold=0.97,
        max_members=event_clustering.MAX_CLUSTER_MEMBERS,
        block_window_hours=48.0,
    )
    return SimpleNamespace(front=front, back=back, page=page,
                           tower=fx.tower_candidates(seeds, k=tower))


def _rig(monkeypatch, world) -> SimpleNamespace:
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    monkeypatch.setattr(event_clustering, "events_enabled", lambda: True)
    monkeypatch.setattr(event_clustering, "datetime", _FrozenDatetime)
    writes: list[tuple[Any, ...]] = []

    async def _lifecycle(conn, **kw):
        await asyncio.sleep(0)
        return {}

    async def _cosines(conn, evidence, **kw):
        await asyncio.sleep(0)
        return {}, {}

    async def _open(conn, *, analyst_id, limit):
        await asyncio.sleep(0)
        rows = world.back if analyst_id == tower_producer_id() else world.front
        return [dict(r) for r in rows[:limit]]

    async def _page(conn, *, since, max_signals, cursor=None):
        await asyncio.sleep(0)
        rows = world.page[:max_signals]
        return list(rows), [(e.fetched_at, e.id) for e in rows]

    async def _tower(conn, **kw):
        await asyncio.sleep(0)
        return list(world.tower), {"tower_candidates": len(world.tower)}, {}

    async def _write(conn, candidate, *, matched, **kw):
        await asyncio.sleep(0)
        matched_id = (matched or {}).get("id")
        writes.append((
            candidate.kind,
            tuple(sorted(str(s) for s in candidate.signal_ids)),
            str(matched_id) if matched_id else None,
            candidate.decline_reason,
        ))
        event_id = matched_id or uuid5(NAMESPACE_URL, candidate.event_signature)
        return event_id, len(candidate.signal_ids), 0

    monkeypatch.setattr(event_clustering, "maintain_event_lifecycle", _lifecycle)
    monkeypatch.setattr(event_clustering, "_semantic_cosines", _cosines)
    monkeypatch.setattr(event_clustering, "_open_events", _open)
    monkeypatch.setattr(event_clustering, "fetch_signal_page", _page)
    monkeypatch.setattr(event_clustering, "fetch_tower_candidates", _tower)
    monkeypatch.setattr(event_clustering, "_write_candidate", _write)
    conn = _FakeConn()
    return SimpleNamespace(
        writes=writes, conn=conn,
        deps=SimpleNamespace(pg_pool=_Pool(conn), extras={}),
    )


def _opts(**kw):
    base = {
        "analyst_id": "event_clustering_test",
        "lookback_hours": 24 * 3,
        "max_signals": 300,
        "max_open_events": 2000,
        "pass_budget_seconds": 0,             # unbounded: no timing-dependent cut
        "tower_candidate_max_seconds": 0,
        "catch_up_lag_minutes": 0,            # N-Z split pinned separately
    }
    base.update(kw)
    return base


def _legacy(monkeypatch) -> None:
    """The pre-N-W pass: every worker-thread call inlined on the loop, the
    prepared cache sized for ONE producer, and the cell-by-cell DP."""

    async def _inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(
        event_clustering, "asyncio",
        SimpleNamespace(to_thread=_inline, wait_for=asyncio.wait_for,
                        TimeoutError=asyncio.TimeoutError),
    )
    monkeypatch.setattr(event_clustering, "_PREPARE_CACHE_SETS", 1)
    monkeypatch.setattr(_event_matcher, "_normalized_levenshtein", _dp_normalized)


def _comparable(data: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in data.items() if k != "phase_seconds"}
    tower = dict(out.get("tower") or {})
    for key in ("prepare_seconds", "per_candidate_seconds",
                "prepare_cache_hits", "prepare_cache_misses"):
        tower.pop(key, None)
    out["tower"] = tower
    return out


async def _run(monkeypatch, world, *, legacy: bool, **opts):
    with monkeypatch.context() as m:
        if legacy:
            _legacy(m)
        r = _rig(m, world)
        _event_matcher.reset_prepared_cache()
        result = await event_clustering.handle([], _opts(**opts), r.deps)
    return r.writes, result.finding.data


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [3, 17])
async def test_the_pass_writes_exactly_what_the_on_loop_pass_wrote(
    monkeypatch, seed: int
) -> None:
    world = _world(events=200, signals=70, tower=6, seed=seed)
    old_writes, old = await _run(monkeypatch, world, legacy=True)
    new_writes, new = await _run(monkeypatch, world, legacy=False)
    # Not degenerate: the walk reattached, minted and ran the tower leg.
    front = [w for w in new_writes if w[0] == "cluster"]
    assert sum(w[2] is not None for w in front) > 5
    assert sum(w[2] is None for w in front) > 0
    assert new["funnel"]["tower_promoted"] == 6
    assert new_writes == old_writes
    assert _comparable(new) == _comparable(old)


@pytest.mark.asyncio
async def test_the_front_walk_is_identical_without_the_tower_leg(monkeypatch) -> None:
    world = _world(events=300, signals=100, tower=0, seed=29)
    old_writes, old = await _run(monkeypatch, world, legacy=True,
                                 include_tower=False)
    new_writes, new = await _run(monkeypatch, world, legacy=False,
                                 include_tower=False)
    assert len(new_writes) > 20
    assert new_writes == old_writes
    assert _comparable(new) == _comparable(old)


def test_the_cache_holds_both_producers_open_sets() -> None:
    """Live receipt 2026-11-13: ``prepare_cache_hits`` 0 of 2,000 — each
    producer's fold evicted the other's at ``cache_limit=max_open_events``."""
    front = fx.open_events(300, seed=5, producer="front")
    back = fx.open_events(300, seed=5, producer="tower")
    limit = 300 * event_clustering._PREPARE_CACHE_SETS
    _event_matcher.reset_prepared_cache()
    for tick in range(3):
        f_stats: dict[str, int] = {}
        b_stats: dict[str, int] = {}
        _event_matcher.prepare_open_events(front, cache_limit=limit, stats=f_stats)
        # The tick's reattach refreshes: rows whose link time moved, folded
        # under new keys — the overflow that made a one-set cache thrash.
        moved = [dict(r, latest_linked_at=fx.NOW + timedelta(minutes=tick))
                 for r in front[:40]]
        _event_matcher.prepare_open_events(moved, cache_limit=limit)
        _event_matcher.prepare_open_events(back, cache_limit=limit, stats=b_stats)
    assert f_stats == {"hits": 300, "misses": 0}
    assert b_stats == {"hits": 300, "misses": 0}
    _event_matcher.reset_prepared_cache()


def test_concurrent_folds_from_threads_are_exact() -> None:
    rows = fx.open_events(400, seed=8, producer="front", mentions=40)
    _event_matcher.reset_prepared_cache()
    serial = [p.prepared for p in _event_matcher.prepare_open_events(
        rows, cache_limit=0)]
    _event_matcher.reset_prepared_cache()
    results: list[list[Any]] = [[] for _ in range(6)]

    def work(i: int) -> None:
        out = _event_matcher.prepare_open_events(rows, cache_limit=150)
        results[i] = [p.prepared for p in out]

    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for got in results:
        assert got == serial
    stats = _event_matcher.prepared_cache_stats()
    assert stats["hits"] + stats["misses"] == 6 * 400
    _event_matcher.reset_prepared_cache()


@pytest.mark.asyncio
async def test_a_live_sized_pass_never_blocks_the_event_loop(monkeypatch) -> None:
    """2,000 open events per producer and a 150-signal page (the live tick
    that held the loop 23-44 s read 59) with a heartbeat ticking beside it."""
    world = _world(events=2000, signals=150, tower=8, seed=41)
    r = _rig(monkeypatch, world)
    _event_matcher.reset_prepared_cache()
    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    # The session heap is frozen so a gen-2 GC pass over ~7,000
    # earlier tests' objects is not charged to the pass (test_event_reconciler_n_v).
    gc.collect()
    gc.freeze()
    try:
        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.05)
        started = time.monotonic()
        result = await event_clustering.handle(
            [], _opts(pass_budget_seconds=600, tower_candidate_max_seconds=60),
            r.deps,
        )
        elapsed = time.monotonic() - started
        stop.set()
        await beat
    finally:
        gc.unfreeze()
    data = result.finding.data
    assert data["budget_exceeded"] is False, data
    assert data["funnel"]["examined"] == 150
    assert data["funnel"]["reattached"] > 20
    assert data["tower"]["events_prepared"] == 2000
    assert len(gaps) > 10
    assert max(gaps) < 0.5, (
        f"the event loop was held {max(gaps):.2f}s during a {elapsed:.1f}s pass"
    )
    _event_matcher.reset_prepared_cache()
