# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-Z — the event slice never stalls, catches up, and reports its lag.

Live 2026-11-14: the ``signal_slice`` cursor sat ~29 h / 5,788 rows behind and,
from 23:14 UTC, stopped moving (lifecycle 59-80 s ate the front's 60 s share;
each zero-progress tick halved the page to 25 rows and spent itself again).
These tests pin:

* the front split (``_event_clustering_front``) writes exactly what the
  pre-N-Z handler wrote — digests recorded from base ee814cfe
  (``_event_clustering_golden``);
* a page the old code could never advance (a budget spent before the slice
  runs, every tick) now advances every tick;
* a several-thousand-row backlog behind a 30-hour lag is split newest-first,
  the head is caught up on the first pass, and the gap drains completely
  within a few passes under the budget, every row read and none twice;
* lag rides the receipt, the watermark state and a log line.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _event_clustering_fixture as fx  # noqa: E402
import _event_clustering_golden as golden  # noqa: E402
from test_event_clustering_loop_n_w import _opts, _rig, _world  # noqa: E402

from legba.data.analysts.deterministic_handlers import (  # noqa: E402
    _event_matcher,
    event_clustering,
)
from legba.data.analysts.deterministic_handlers.fact_contention_pass import (  # noqa: E402
    PassBudget,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("world_key", golden.WORLDS)
async def test_the_front_split_writes_what_the_pre_split_handler_wrote(
    monkeypatch, world_key
) -> None:
    assert await golden.compute(monkeypatch, world_key) == golden.GOLDEN[world_key]


class _Spent:
    """A budget that is exhausted from the first probe on."""

    def exhausted(self) -> bool:
        return True

    def remaining(self) -> float:
        return 0.0


class _Corpus:
    """A slice over (fetched_at, id) with the SQL's cursor and ``until``."""

    def __init__(self, evidence) -> None:
        self.rows = sorted(evidence, key=lambda e: (e.fetched_at, str(e.id)))
        self.reads: list[list[Any]] = []

    async def fetch(self, conn, *, since, max_signals, cursor=None, until=None):
        out = []
        for e in self.rows:
            if cursor is None and not e.fetched_at > since:
                continue
            if cursor is not None and not (e.fetched_at, str(e.id)) > (
                cursor[0], str(cursor[1])
            ):
                continue
            if until is not None and not e.fetched_at < until:
                continue
            out.append(e)
            if len(out) >= max_signals:
                break
        self.reads.append([e.id for e in out])
        return list(out), [(e.fetched_at, e.id) for e in out]


def _retimed(n: int, *, hours: float, seed: int):
    page = fx.signal_page(n, seed=seed)
    step = timedelta(hours=hours) / n
    start = fx.NOW - timedelta(hours=hours)
    return [replace(e, fetched_at=start + step * i) for i, e in enumerate(page)]


@pytest.mark.asyncio
async def test_a_page_the_old_code_could_never_advance_now_advances(
    monkeypatch,
) -> None:
    """Every tick's budget is spent before the slice (the live shape: the
    lifecycle phase used the whole front share). The old handler cut in
    ``cluster`` with 0 processed and halved the page, forever."""
    world = _world(events=150, signals=10, tower=0, seed=7)
    r = _rig(monkeypatch, world)
    corpus = _Corpus(_retimed(60, hours=0.5, seed=7))
    monkeypatch.setattr(event_clustering, "fetch_signal_page", corpus.fetch)
    monkeypatch.setattr(event_clustering, "PassBudget", lambda seconds=None: _Spent())
    seen = []
    for _tick in range(3):
        data = (await event_clustering.handle(
            [], _opts(include_tower=False, max_signals=20,
                      min_signals_per_run=5), r.deps)).finding.data
        s = data["slice"]
        assert s["advanced"] is True and s["processed"] > 0, s
        assert s["stalled"] is False and s["stall_ticks"] == 0
        seen.append(s["watermark_after"]["ts"])
    assert seen == sorted(seen) and len(set(seen)) == 3


@pytest.mark.asyncio
async def test_a_thirty_hour_backlog_catches_up_within_a_few_passes(
    monkeypatch, caplog
) -> None:
    caplog.set_level(logging.INFO)
    world = _world(events=200, signals=10, tower=0, seed=11)
    r = _rig(monkeypatch, world)
    rows = _retimed(2400, hours=30, seed=11)
    corpus = _Corpus(rows)
    monkeypatch.setattr(event_clustering, "fetch_signal_page", corpus.fetch)
    # The stored cursor is the oldest row's predecessor, 30 h behind.
    first = rows[0]
    r.conn.watermarks[event_clustering.SIGNAL_CURSOR_KEY] = {
        "ts": (first.fetched_at - timedelta(seconds=1)).isoformat(),
        "id": "00000000-0000-0000-0000-000000000000", "page": 300,
        "catch_up": {"floor_hours": 24.0, "applied": True},
    }
    _event_matcher.reset_prepared_cache()
    opts = dict(_opts(include_tower=False, max_signals=300,
                      pass_budget_seconds=60),
                catch_up_lag_minutes=120)
    receipts = []
    for _tick in range(12):
        data = (await event_clustering.handle([], opts, r.deps)).finding.data
        receipts.append(data["slice"])
        if receipts[-1]["backfill"] is None and receipts[-1]["head_caught_up"]:
            break
    first_pass = receipts[0]
    assert first_pass["backfill_opened"]["lag_minutes"] > 29 * 60
    assert first_pass["head_caught_up"] is True
    assert first_pass["head_lag_minutes"] == 0.0
    # The gap is still open but already mostly drained in the same pass.
    assert 0 < first_pass["lag_minutes"] < first_pass["backfill_opened"]["lag_minutes"]
    assert first_pass["backfill_processed"] > 0
    done = receipts[-1]
    assert done["backfill"] is None and done["head_caught_up"] is True
    assert done["lag_minutes"] == 0.0
    assert any(x["backfill_closed"] for x in receipts)
    assert len(receipts) <= 6, [x["pages"] for x in receipts]
    # Every row read; the processed prefixes never overlap.
    processed = sum(x["processed"] + x["backfill_processed"] for x in receipts)
    assert processed == len(rows)
    assert {i for read in corpus.reads for i in read} == {e.id for e in rows}
    state = r.conn.watermarks[event_clustering.SIGNAL_CURSOR_KEY]
    assert state["lag_minutes"] == 0.0 and "backfill" not in state
    assert "event_clustering.catch_up_split" in caplog.text
    assert "event_clustering.lag lag_minutes=" in caplog.text
    assert "event_clustering.backfill_closed" in caplog.text


@pytest.mark.asyncio
async def test_a_small_lag_does_not_split_and_reports_minutes(monkeypatch) -> None:
    world = _world(events=100, signals=10, tower=0, seed=5)
    r = _rig(monkeypatch, world)
    corpus = _Corpus(_retimed(40, hours=1.0, seed=5))
    monkeypatch.setattr(event_clustering, "fetch_signal_page", corpus.fetch)
    data = (await event_clustering.handle(
        [], dict(_opts(include_tower=False, max_signals=20),
                 catch_up_lag_minutes=120), r.deps)).finding.data
    s = data["slice"]
    assert s["backfill_opened"] is None and s["backfill"] is None
    # Two full pages, then a short (empty) read: caught up, lag 0.
    assert s["pages"] == 3 and s["processed"] == 40
    assert s["head_caught_up"] is True and s["lag_minutes"] == 0.0


def test_pass_budget_type_is_what_the_handler_uses() -> None:
    assert event_clustering.PassBudget is PassBudget


def test_the_watchdog_reads_the_stored_lag_and_ages_it() -> None:
    """The lag rides the watermark state; the watchdog adds the minutes since
    it was written, so an analyst that stops writing keeps ageing."""
    import json
    from datetime import datetime, timezone

    from legba.runtime import analyst_cursor_lag as acl
    from legba.runtime.liveness_watchdog import _TRANSITION_CHANNELS

    now = datetime(2026, 11, 14, 1, 0, tzinfo=timezone.utc)
    rows = [
        {"trigger_class": "event_clustering", "watermark_key": "signal_slice",
         "state": json.dumps({"lag_minutes": 1780.0, "backfill": {}}),
         "updated_at": datetime(2026, 11, 14, 0, 40, tzinfo=timezone.utc)},
        {"trigger_class": "x", "watermark_key": "fresh",
         "state": json.dumps({"lag_minutes": 5.0}), "updated_at": now},
        {"trigger_class": "x", "watermark_key": "quiet",
         "state": json.dumps({"lag_minutes": 100.0}),
         "updated_at": datetime(2026, 11, 13, 23, 0, tzinfo=timezone.utc)},
    ]
    bad = acl.evaluate(rows, now=now, threshold=180)
    assert [b["entity_id"] for b in bad] == [
        "event_clustering:signal_slice", "x:quiet"]
    assert bad[0]["lag_minutes"] == 1800.0 and bad[0]["backfill_open"] is True
    assert bad[1]["lag_minutes"] == 220.0
    assert acl.evaluate(rows, now=now, threshold=0) == []
    kw = acl.transition_kwargs(bad[0])
    assert kw["kind"] == "analyst_cursor_lag" and kw["severity"] == "high"
    assert "30.0h behind" in kw["title"]
    assert acl.ALERT_CHANNEL_CURSOR_LAG in _TRANSITION_CHANNELS


@pytest.mark.asyncio
async def test_the_watchdog_leg_alerts_once_and_recovers() -> None:
    import json
    from contextlib import asynccontextmanager
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from legba.runtime import analyst_cursor_lag as acl

    state = {"lag_minutes": 600.0}

    class _Conn:
        async def fetch(self, sql):
            return [{"trigger_class": "event_clustering",
                     "watermark_key": "signal_slice",
                     "state": json.dumps(state),
                     "updated_at": datetime.now(timezone.utc)}]

    class _Pool:
        @asynccontextmanager
        async def acquire(self):
            yield _Conn()

    ledger: dict[str, str] = {}
    recorded: list[str] = []

    class _Wd:
        _pg = _Pool()
        _is_leader = None
        _started_at = 0.0
        _cfg = SimpleNamespace(stall_after_s=0)

        async def _get_alert_states(self, channel):
            return dict(ledger)

        async def _record_transition(self, **kw):
            ledger[kw["entity_id"]] = kw["state"]
            recorded.append(kw["kind"])

        async def _emit_recoveries(self, *, channel, kind, noun, still_bad):
            for eid, st in list(ledger.items()):
                if st == "entered" and eid not in still_bad:
                    ledger[eid] = "recovered"
                    recorded.append(kind)

    wd = _Wd()
    assert await acl.check_cursor_lag_once(wd, 10.0) == [
        "event_clustering:signal_slice"]
    assert await acl.check_cursor_lag_once(wd, 20.0) == []       # no re-alert
    state["lag_minutes"] = 3.0
    await acl.check_cursor_lag_once(wd, 30.0)
    assert recorded == ["analyst_cursor_lag", "analyst_cursor_lag_recovered"]


def test_catch_up_knobs_are_declared_in_the_catalog() -> None:
    from legba.data.analysts.deterministic_handlers import (
        _event_clustering_front as front,
    )
    from legba.data.option_catalog.handler_options import HANDLER_OPTIONS

    specs = {s.name: s for s in HANDLER_OPTIONS["event_clustering"]}
    assert specs["catch_up_lag_minutes"].validate(0.0)[0] is True
    assert specs["catch_up_lag_minutes"].validate(-1.0)[0] is False
    assert specs["catch_up_head_minutes"].validate(60.0)[0] is True
    assert specs["max_pages_per_pass"].validate(0)[0] is False
    assert front.catch_up_knobs({}) == (120.0, 60.0, 8)
