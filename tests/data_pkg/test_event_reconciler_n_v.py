# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-V — the reconciler's pair walk is indexed, equal, and off the event loop.

Live 2026-11-13: every ``event_reconciler`` tick (:19/:39/:59) spent ~115 s of
pure CPU in ``evolves_pairs`` over 5,000 rows ON the runtime's asyncio loop —
the whole runtime froze for ~2 minutes every 20 minutes and the container went
unhealthy — and since the walk ate the entire 120 s budget, ``evolves_written``
was 0 on every run after 2026-10-29. These tests pin the three halves of the
fix: the indexed walk returns exactly what the all-pairs walk returned (kept
verbatim in ``_event_reconciler_legacy``), it is fast, and a pass over a
live-sized scan never holds the loop.
"""

from __future__ import annotations

import asyncio
import gc
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _event_reconciler_fixture import make_rows  # noqa: E402
from _event_reconciler_legacy import legacy_evolves_pairs  # noqa: E402

from legba.data.analysts.deterministic_handlers import (  # noqa: E402
    event_reconciler,
)


def _same(old, new) -> None:
    assert len(new) == len(old)
    assert new == old
    for (os_, od, on), (ns, nd, nn) in zip(old, new):
        assert (on, os_["id"], od["id"]) == (nn, ns["id"], nd["id"])
        # A duplicated id resolves to the same physical row (actor set too).
        assert od["actor_ids"] == nd["actor_ids"]
        assert od.get("evidence_end") == nd.get("evidence_end")


def test_indexed_walk_equals_the_all_pairs_walk_on_a_live_shaped_scan() -> None:
    """2,500 live-shaped events (hazards on) under the live options."""
    rows = make_rows(2500, seed=11, actor_scale=0.08)
    old = legacy_evolves_pairs(rows, min_actors=2, max_gap_hours=168)
    new = event_reconciler.evolves_pairs(rows, min_actors=2, max_gap_hours=168)
    assert len(new) > 1500          # the fixture is not degenerate
    _same(old, new)


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
@pytest.mark.parametrize(
    "min_actors,max_gap",
    [(2, 168), (1, 24), (0, 168), (5, 72), (1, 1), (3, 10_000_000)],
)
def test_indexed_walk_equals_the_all_pairs_walk_across_options(
    seed: int, min_actors: int, max_gap: int
) -> None:
    rows = make_rows(300, seed=seed, actor_scale=0.05, entity_pool=400)
    old = legacy_evolves_pairs(rows, min_actors=min_actors, max_gap_hours=max_gap)
    new = event_reconciler.evolves_pairs(
        rows, min_actors=min_actors, max_gap_hours=max_gap
    )
    _same(old, new)


def test_tie_on_end_shared_and_id_keeps_the_earlier_row() -> None:
    """An exact key tie (same id twice, same end, same overlap) resolves to
    the row met first, as the all-pairs ``>`` comparison did."""
    rows = make_rows(40, seed=9, actor_scale=0.02, entity_pool=30, hazards=False)
    base = rows[5]
    twin_a = dict(base, title="first")
    twin_b = dict(base, title="second")
    rows = rows[:5] + [twin_a] + rows[6:20] + [twin_b] + rows[20:]
    old = legacy_evolves_pairs(rows, min_actors=1, max_gap_hours=10_000)
    new = event_reconciler.evolves_pairs(rows, min_actors=1, max_gap_hours=10_000)
    _same(old, new)
    assert [d["title"] for _s, d, _n in new] == [d["title"] for _s, d, _n in old]


def test_a_truncated_walk_returns_the_same_prefix() -> None:
    """``should_stop`` is consulted once per source in both walks, newest
    first, so a budget cut yields the same partial list."""
    rows = make_rows(400, seed=21, actor_scale=0.05, entity_pool=500)

    def stopper(after: int):
        calls = {"n": 0}

        def should_stop() -> bool:
            calls["n"] += 1
            return calls["n"] > after

        return should_stop

    old = legacy_evolves_pairs(rows, should_stop=stopper(150))
    new = event_reconciler.evolves_pairs(rows, should_stop=stopper(150))
    _same(old, new)
    assert 0 < len(new) < len(event_reconciler.evolves_pairs(rows))


def test_the_indexed_walk_is_fast_on_a_live_sized_scan() -> None:
    """5,000 events at the live actor density (~186 per event): the old walk
    needed ~115 s and never finished inside the 120 s budget."""
    rows = make_rows(5000, seed=7, actor_scale=1.0)
    started = time.perf_counter()
    pairs = event_reconciler.evolves_pairs(rows, min_actors=2, max_gap_hours=168)
    elapsed = time.perf_counter() - started
    assert len(pairs) > 4000
    assert elapsed < 10.0, f"evolves_pairs took {elapsed:.1f}s on 5,000 rows"


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.writes = 0

    async def fetch(self, _sql, limit):
        await asyncio.sleep(0)
        return self._rows[:limit]

    async def fetchval(self, _sql, *_args):
        self.writes += 1
        await asyncio.sleep(0)
        return True


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool._conn

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_a_live_sized_pass_never_blocks_the_event_loop(monkeypatch) -> None:
    """A heartbeat task keeps ticking while a whole pass runs over 5,000
    live-shaped events. The bound (0.5 s) is far under the 120 s the old pass
    held the loop for; the 10 s budget makes a regression fail in seconds
    rather than minutes."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    rows = make_rows(5000, seed=7, actor_scale=1.0)
    conn = _FakeConn(rows)
    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    # In the ordered suite the one long gap was a generation-2 GC
    # pass over the pytest SESSION's heap (~6,900 earlier tests: 0.52 s in one
    # collection, every gen-1 pass ~6 ms), not the reconciler — measured with
    # gc.callbacks. Freezing what exists before the pass moves the session's
    # objects out of the collector's reach; the pass's own allocations are
    # still collected inside the measured window, and the bound is unchanged.
    gc.collect()
    gc.freeze()
    try:
        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.05)
        started = time.monotonic()
        result = await event_reconciler.handle(
            [],
            {
                "sub_handler": "event_reconciler",
                "analyst_id": "analyst.event_reconciler_test",
                "analyst_version": "n-v-test",
                "run_id": uuid4(),
                "max_events": 5000,
                "evolves_min_actors": 2,
                "evolves_max_gap_hours": 168,
                "pass_budget_seconds": 10,
            },
            SimpleNamespace(pg_pool=_FakePool(conn)),
        )
        elapsed = time.monotonic() - started
        stop.set()
        await beat
    finally:
        gc.unfreeze()
    data = result.finding.data
    assert data["budget_exceeded"] is False, data
    assert data["events_examined"] == 5000
    assert data["evolves_pairs"] > 4000
    assert data["evolves_written"] == data["evolves_pairs"] == conn.writes
    assert len(gaps) > 10
    assert max(gaps) < 0.5, (
        f"the event loop was held {max(gaps):.2f}s during a {elapsed:.1f}s pass"
    )


@pytest.mark.asyncio
async def test_the_synthetic_path_counts_pairs_off_the_loop(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    rows = make_rows(200, seed=3, actor_scale=0.05, entity_pool=300)
    result = await event_reconciler.handle(rows, {}, None)
    data = result.finding.data
    assert data["synthetic"] is True
    assert data["evolves_pairs"] == len(legacy_evolves_pairs(rows))
