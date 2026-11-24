# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The signal slice's progress contract (2026-11-09) on a fake substrate.

The live ``signal_slice`` watermark sat at 2026-09-23T19:37:14Z for seven
weeks: a write cut resumed INCLUSIVE at the earliest member of any unwritten
candidate (walked in cluster order, so the page head was always owed), and a
cut before the write loop kept the prior watermark. These tests drive
``event_clustering.handle`` end to end over an in-memory watermark table and
a fake slice fetch, with no database:

* a budget cut persists the processed prefix, and repeated cut ticks always
  advance (the regression that froze the slice);
* a failing candidate is skipped and recorded, never blocking;
* the slice order is a stable total order, ties on ``fetched_at`` included;
* the catch-up floor applies once, is logged and records the skipped span;
* a zero-progress tick halves the page, and a consumed page grows it;
* rows the normalizer cannot read never block the watermark.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts.deterministic_handlers import (
    _event_slice_progress as progress_mod,
)
from legba.data.analysts.deterministic_handlers import event_clustering
from legba.data.analysts.deterministic_handlers._event_candidates import (
    _SIGNAL_SLICE_SQL,
    EventEvidence,
)

# A fixed instant, injected as the handler's clock by the ``rig`` fixture, so no
# assertion depends on wall time elapsed between import/setup and the assert.
NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    """``datetime`` whose ``now()`` is the module's fixed ``NOW``."""

    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


class _Budget:
    """``exhausted()`` answers False for ``checks`` probes, then True."""

    def __init__(self, checks: int) -> None:
        self.left = checks

    def exhausted(self) -> bool:
        self.left -= 1
        return self.left < 0


class _FakeConn:
    """The few calls the handler makes outside the monkeypatched seams: the
    watermark load/upsert, the catch-up span count and the minted-row read."""

    def __init__(self) -> None:
        self.watermarks: dict[str, dict[str, Any]] = {}
        self.span_count = 0
        self.span_queries = 0

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetch(self, sql: str, *args: Any):
        if "FROM alert_trigger_watermarks" in sql:
            return [
                {"watermark_key": k, "state": json.dumps(v)}
                for k, v in self.watermarks.items()
            ]
        raise AssertionError(f"unexpected fetch: {sql[:60]}")

    async def execute(self, sql: str, *args: Any):
        assert "alert_trigger_watermarks" in sql
        _class, key, state, _fired = args
        self.watermarks[key] = json.loads(state)

    async def fetchval(self, sql: str, *args: Any):
        assert "count(*)" in sql
        self.span_queries += 1
        return self.span_count

    async def fetchrow(self, sql: str, *args: Any):
        return {
            "id": args[0], "event_signature": f"sig-{args[0]}",
            "title": "minted", "category": "conflict", "geo": ["ir"],
            "time_start": None, "time_end": None, "updated_at": NOW,
        }


class _Pool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


_STORIES = {
    "Alpha": ("Refinery fire engulfs Basra terminal", "conflict", "iq"),
    "Bravo": ("Cabinet resigns amid Lima budget dispute", "politics", "pe"),
    "Charlie": ("Typhoon floods Manila river districts", "disaster", "ph"),
    "Delta": ("Central bank in Ankara raises lending rate", "economy", "tr"),
    "Echo": ("Strike closes Antwerp container port", "labor", "be"),
}


def _ev(title: str, source: str, at: datetime, sid: UUID | None = None) -> EventEvidence:
    word = title.split()[0]
    story, category, geo = _STORIES.get(word, (title, "conflict", "ir"))
    return EventEvidence(
        id=sid or uuid4(), title=story, category=category, fetched_at=at,
        source_id=source, source_class="reporting", geo=(geo,),
        entity_names=(word,),
    )


class _Corpus:
    """A fake slice: rows in ``(fetched_at, id)`` order, the SQL's tuple
    comparison, and optional unreadable rows (positions without evidence)."""

    def __init__(self, evidence: list[EventEvidence], unreadable=()) -> None:
        self.evidence = {e.id: e for e in evidence}
        self.positions = sorted(
            [(e.fetched_at, e.id) for e in evidence] + list(unreadable),
            key=lambda p: (p[0], str(p[1])),
        )
        self.calls: list[dict[str, Any]] = []

    async def fetch(self, conn, *, since, max_signals, cursor=None):
        self.calls.append({"cursor": cursor, "max_signals": max_signals})
        if cursor is None:
            rows = [p for p in self.positions if p[0] > since]
        else:
            rows = [
                p for p in self.positions
                if (p[0], str(p[1])) > (cursor[0], str(cursor[1]))
            ]
        rows = rows[:max_signals]
        return [self.evidence[p[1]] for p in rows if p[1] in self.evidence], rows


@pytest.fixture
def rig(monkeypatch):
    """Patch every substrate seam but the watermark plane; returns a builder."""
    monkeypatch.setattr(event_clustering, "events_enabled", lambda: True)
    monkeypatch.setattr(event_clustering, "datetime", _FrozenDatetime)

    async def _lifecycle(conn, **kw):
        return {}

    async def _cosines(conn, evidence, **kw):
        return {}, {}

    async def _open(conn, **kw):
        return []

    monkeypatch.setattr(event_clustering, "maintain_event_lifecycle", _lifecycle)
    monkeypatch.setattr(event_clustering, "_semantic_cosines", _cosines)
    monkeypatch.setattr(event_clustering, "_open_events", _open)

    written: list[list[UUID]] = []
    poison: set[UUID] = set()

    async def _write(conn, candidate, **kw):
        if set(candidate.signal_ids) & poison:
            raise ValueError("poison row")
        written.append(list(candidate.signal_ids))
        return uuid4(), len(candidate.signal_ids), 0

    monkeypatch.setattr(event_clustering, "_write_candidate", _write)

    def build(corpus: _Corpus, budgets=None):
        conn = _FakeConn()
        monkeypatch.setattr(event_clustering, "fetch_signal_page", corpus.fetch)
        it = iter(budgets or [])
        monkeypatch.setattr(
            event_clustering, "PassBudget",
            lambda seconds=None: next(it, _Budget(10 ** 6)),
        )
        return SimpleNamespace(
            conn=conn, deps=SimpleNamespace(pg_pool=_Pool(conn), extras={}),
            written=written, poison=poison,
        )

    return build


def _opts(**kw):
    # N-Z: these pin the single-segment contract; the catch-up split has its
    # own tests (test_event_clustering_backlog_n_z).
    base = {"include_tower": False, "lookback_hours": 24, "max_signals": 100,
            "catch_up_lag_minutes": 0}
    base.update(kw)
    return base


def _pairs(n: int, start: datetime) -> list[list[EventEvidence]]:
    """``n`` two-source clusters, 10 min apart, each promotable. Ids are
    chosen so uuid order REVERSES slice order (the old cluster-order walk)."""
    out = []
    for i in range(n):
        at = start + timedelta(minutes=10 * i)
        word = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"][i]
        hi = UUID(int=(n - i) << 64)
        out.append([
            _ev(f"{word} plant blast", f"rss_{i}a", at, UUID(int=hi.int + 1)),
            _ev(f"{word} plant blast", f"rss_{i}b", at + timedelta(minutes=1),
                UUID(int=hi.int + 2)),
        ])
    return out


def _wm(conn: _FakeConn) -> dict[str, Any]:
    return conn.watermarks[event_clustering.SIGNAL_CURSOR_KEY]


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_cut_persists_partial_progress_then_resumes(rig) -> None:
    clusters = _pairs(3, NOW - timedelta(hours=3))
    corpus = _Corpus([e for c in clusters for e in c])
    # Probes: post-slice, pre-cluster, post-cluster, then one per candidate:
    # four pass, so cluster A is written and B's turn check spends it.
    r = rig(corpus, budgets=[_Budget(4)])
    first = (await event_clustering.handle([], _opts(), r.deps)).finding.data
    assert first["stopped_in_phase"] == "write"
    assert r.written == [[e.id for e in clusters[0]]]
    a_last = clusters[0][-1]
    assert _wm(r.conn)["id"] == str(a_last.id)
    assert _wm(r.conn)["ts"] == a_last.fetched_at.isoformat()
    assert first["slice"]["processed"] == 2
    assert first["slice"]["advanced"] is True

    second = (await event_clustering.handle([], _opts(), r.deps)).finding.data
    assert second["stopped_in_phase"] is None
    assert second["funnel"]["examined"] == 4  # B and C only, never A again
    assert _wm(r.conn)["id"] == str(clusters[2][-1].id)
    assert len(r.written) == 3


@pytest.mark.asyncio
async def test_repeated_cut_ticks_always_advance(rig) -> None:
    """The live fixed point: every tick cut after ONE candidate. Under the
    old rule the bound fell back to the page head each time."""
    clusters = _pairs(5, NOW - timedelta(hours=3))
    corpus = _Corpus([e for c in clusters for e in c])
    r = rig(corpus, budgets=[_Budget(4) for _ in range(5)])
    seen = []
    for _ in range(5):
        await event_clustering.handle([], _opts(), r.deps)
        seen.append(_wm(r.conn)["ts"])
    assert seen == sorted(set(seen)) and len(seen) == 5
    assert seen[-1] == clusters[-1][-1].fetched_at.isoformat()


@pytest.mark.asyncio
async def test_failing_candidate_is_skipped_and_recorded(rig) -> None:
    clusters = _pairs(3, NOW - timedelta(hours=3))
    corpus = _Corpus([e for c in clusters for e in c])
    r = rig(corpus)
    r.poison.add(clusters[1][0].id)
    data = (await event_clustering.handle([], _opts(), r.deps)).finding.data
    assert data["stopped_in_phase"] is None
    assert data["funnel"]["candidates_skipped"] == 1
    assert len(r.written) == 2  # A and C landed; B did not block C
    assert _wm(r.conn)["id"] == str(clusters[2][-1].id)
    dead = _wm(r.conn)["dead_letter"]
    assert dead[0]["reason"].startswith("ValueError: poison row")
    assert str(clusters[1][0].id) in dead[0]["signal_ids"]
    assert data["slice"]["skipped_candidates"][0]["signal_count"] == 2


def test_slice_sql_has_a_stable_total_order() -> None:
    sql = " ".join(_SIGNAL_SLICE_SQL.split())
    assert "ORDER BY s.fetched_at ASC, s.id ASC" in sql
    assert "(s.fetched_at, s.id) > ($3::timestamptz, $4::uuid)" in sql


@pytest.mark.asyncio
async def test_ties_on_fetched_at_resume_exactly(rig) -> None:
    """Rows sharing one ``fetched_at`` are split by id: a cut inside the tie
    resumes after the last processed id, re-reading nothing it finished."""
    at = NOW - timedelta(hours=2)
    ids = [UUID(int=i) for i in (1, 2, 3, 4)]
    rows = [
        _ev("Alpha port strike", "rss_a", at, ids[0]),
        _ev("Alpha port strike", "rss_b", at, ids[1]),
        _ev("Bravo dam breach", "rss_c", at, ids[2]),
        _ev("Bravo dam breach", "rss_d", at, ids[3]),
    ]
    corpus = _Corpus(rows)
    r = rig(corpus, budgets=[_Budget(4)])
    await event_clustering.handle([], _opts(), r.deps)
    assert _wm(r.conn)["id"] == str(ids[1])
    await event_clustering.handle([], _opts(), r.deps)
    assert corpus.calls[1]["cursor"] == (at, ids[1])
    assert [sorted(w) for w in r.written] == [ids[:2], ids[2:]]


@pytest.mark.asyncio
async def test_catch_up_floor_applies_once_and_is_logged(rig, caplog) -> None:
    stale = NOW - timedelta(days=46)
    corpus = _Corpus(_pairs(1, NOW - timedelta(hours=2))[0])
    r = rig(corpus)
    r.conn.watermarks["signal_slice"] = {
        "ts": stale.isoformat(), "id": str(UUID(int=0)),
    }
    r.conn.span_count = 131_000
    with caplog.at_level(logging.INFO):
        data = (await event_clustering.handle([], _opts(), r.deps)).finding.data
    assert "event_clustering.catch_up_floor applied" in caplog.text
    floor = corpus.calls[0]["cursor"]
    assert abs((NOW - floor[0]) - timedelta(hours=24)) < timedelta(minutes=5)
    note = data["slice"]["catch_up"]
    assert note["applied"] is True and note["signals_skipped"] == 131_000
    assert note["from"] == stale.isoformat()
    assert _wm(r.conn)["catch_up"]["applied"] is True
    # Second tick: the record stops a re-application.
    caplog.clear()
    with caplog.at_level(logging.INFO):
        again = (await event_clustering.handle([], _opts(), r.deps)).finding.data
    assert again["slice"]["catch_up"] is None
    assert r.conn.span_queries == 1
    assert "catch_up_floor applied" not in caplog.text


@pytest.mark.asyncio
async def test_catch_up_floor_zero_drains_the_backlog_instead(rig) -> None:
    stale = NOW - timedelta(days=46)
    corpus = _Corpus(_pairs(1, stale + timedelta(hours=1))[0])
    r = rig(corpus)
    r.conn.watermarks["signal_slice"] = {"ts": stale.isoformat(), "id": str(uuid4())}
    data = (await event_clustering.handle(
        [], _opts(catch_up_floor_hours=0), r.deps)).finding.data
    assert corpus.calls[0]["cursor"][0] == stale
    assert data["funnel"]["examined"] == 2
    assert "catch_up" not in _wm(r.conn)


@pytest.mark.asyncio
async def test_a_spent_budget_still_processes_the_first_candidate(rig) -> None:
    """N-Z progress floor. Before: a budget spent before clustering cut the
    tick in ``cluster`` with nothing processed, halved the page and stalled
    (live 2026-11-14: 25 rows read, 0 processed, tick after tick). Now the
    first candidate of the pass runs anyway, so the tick always advances."""
    rows = [e for c in _pairs(3, NOW - timedelta(hours=3)) for e in c]
    corpus = _Corpus(rows)
    # Post-slice passes; pre-cluster is spent -> the floor carries on.
    r = rig(corpus, budgets=[_Budget(1)])
    data = (await event_clustering.handle(
        [], _opts(max_signals=6, min_signals_per_run=2), r.deps)).finding.data
    # The receipt names the phase the budget ran out in; the floor ran anyway.
    assert data["stopped_in_phase"] == "cluster" and data["budget_exceeded"]
    assert data["slice"]["floor_ran"] is True
    assert data["slice"]["stalled"] is False
    assert data["slice"]["advanced"] is True and data["slice"]["processed"] == 2
    assert r.written == [[e.id for e in _pairs(3, NOW - timedelta(hours=3))[0]]]
    assert _wm(r.conn)["page"] == 6 and _wm(r.conn)["stall_ticks"] == 0
    data = (await event_clustering.handle(
        [], _opts(max_signals=6, min_signals_per_run=2), r.deps)).finding.data
    assert data["slice"]["advanced"] is True
    assert data["slice"]["processed"] == 4
    assert _wm(r.conn)["stall_ticks"] == 0


@pytest.mark.asyncio
async def test_unreadable_rows_never_block_the_watermark(rig) -> None:
    at = NOW - timedelta(hours=1)
    junk = [(at + timedelta(seconds=i), uuid4()) for i in range(5)]
    corpus = _Corpus([], unreadable=junk)
    r = rig(corpus)
    data = (await event_clustering.handle([], _opts(), r.deps)).finding.data
    assert data["slice"]["unnormalizable"] == 5
    last = max(junk, key=lambda p: (p[0], str(p[1])))
    assert _wm(r.conn)["id"] == str(last[1])


def test_knobs_are_declared_in_the_catalog() -> None:
    from legba.data.option_catalog.handler_options import HANDLER_OPTIONS

    specs = {s.name: s for s in HANDLER_OPTIONS["event_clustering"]}
    assert specs["min_signals_per_run"].validate(0)[0] is False
    assert specs["catch_up_floor_hours"].validate(0.0)[0] is True
    assert specs["catch_up_floor_hours"].validate(-1.0)[0] is False
    assert specs["max_signals"].validate(300)[0] is True
    assert progress_mod.knobs({}) == (25, 24.0)
