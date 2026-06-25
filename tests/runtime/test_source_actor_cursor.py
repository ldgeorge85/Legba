# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source-cursor regression tests for :mod:`legba.runtime.source_actor`.

Covers the two 2026-06 source-cursor fixes, driven against an in-memory
substrate (no live Postgres / Dapr — the cursor + offset logic is the unit
under test, not the substrate write):

  A. Capped/timed-out poll must advance the ``since`` cursor to the LAST
     PROCESSED entry's logical timestamp — NOT NOW. The earlier NOW-advance
     was an anti-trap that, on a capped pull, skipped the unprocessed backlog
     past the cap (observed dropping live entries). Forward-progress is kept
     (the cursor still moves) WITHOUT skipping; a genuinely empty / fully
     drained pull still advances to NOW.

  B. ``bulk_highwater_advance`` — the pure high-water-mark cursor-advance for
     bulk dataset-streaming kinds (e.g. OpenSanctions ``bulk_csv``). The
     ~50k-row snapshot shares a coarse daily ``last_seen`` so the ``since``
     cursor can't paginate within it; the actor instead persists a row OFFSET
     so each capped pull RESUMES where the prior stopped, walking the dataset
     across pulls, and resets to 0 at end-of-stream.

These follow the source_actor / acquisition test patterns but stay DB-free so
they run in the unit lane; the live bulk traversal is vault-key-gated and
exercised separately (handler-side, ``tests/data_pkg/test_source_opensanctions``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.properties import Cron
from legba.data.schemas.source import (
    CadenceBlock,
    SourceDescriptor,
    SourceIdentity,
    SourcePipeline,
    SourceScope,
)
from legba.data.sources._contract import InMemoryStateStore, Signal, SourceContext
from legba.runtime.deps import StandardDeps
from legba.runtime.source_actor import (
    BULK_RESUME_OFFSET_KEY,
    BULK_TRAVERSED_KEY,
    SourceCore,
    SourceDeps,
    _RawConfig,
    bulk_highwater_advance,
)


# ---------------------------------------------------------------------------
# B — pure cursor-advance logic (the unit the brief asks for explicitly)
# ---------------------------------------------------------------------------


def test_bulk_highwater_advance_resumes_mid_snapshot():
    # A capped pull (reached_end=False) advances the high-water mark by the
    # rows it walked, so the next pull resumes PAST them.
    assert bulk_highwater_advance(0, 100, reached_end=False) == 100
    assert bulk_highwater_advance(100, 100, reached_end=False) == 200
    assert bulk_highwater_advance(49900, 100, reached_end=False) == 50000


def test_bulk_highwater_advance_resets_at_end_of_stream():
    # A complete walk (reached_end=True) resets to 0 so the next pull re-walks
    # the refreshed snapshot from the top — regardless of where it stopped.
    assert bulk_highwater_advance(50000, 17, reached_end=True) == 0
    assert bulk_highwater_advance(0, 0, reached_end=True) == 0
    assert bulk_highwater_advance(123, 0, reached_end=True) == 0


def test_bulk_highwater_advance_clamps_garbage():
    # A negative / garbage traversal report can NOT rewind the mark below where
    # it already stood (defensive — a bad handler report never loses progress).
    assert bulk_highwater_advance(100, -5, reached_end=False) == 100
    assert bulk_highwater_advance(-3, 10, reached_end=False) == 10


# ---------------------------------------------------------------------------
# In-memory substrate doubles (no live Postgres / Dapr)
# ---------------------------------------------------------------------------


class _FakeConn:
    """Minimal asyncpg-conn double: every INSERT ... RETURNING id succeeds."""

    async def fetchrow(self, query, *args):
        # write_canonical_signal passes the signal_id first.
        return {"id": args[0]}

    async def fetchval(self, *a, **k):  # pragma: no cover - unused here
        return None

    async def execute(self, *a, **k):  # pragma: no cover - unused here
        return None


class _FakeAcquire:
    async def __aenter__(self):
        return _FakeConn()

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def acquire(self):
        return _FakeAcquire()


class _StubHandler:
    """Yields a controllable list of raw Signals; records the ``since`` it saw.

    Optionally yields MORE than the actor's per-poll cap so the actor caps the
    pull (Fix A path). Each yielded signal carries a logical timestamp on the
    payload (``_published_at_dt``) so the actor advances along source ordering.
    """

    def __init__(self, signals: list[Signal]) -> None:
        self._signals = signals
        self.seen_since: datetime | None = None

    async def pull(self, ctx: SourceContext, since=None):
        self.seen_since = since
        for sig in self._signals:
            yield sig


def _poll_descriptor(source_id: str) -> SourceDescriptor:
    return SourceDescriptor(
        identity=SourceIdentity(
            id=source_id,
            name="cursor-test",
            kind="rss",
            schema_uri="legba/source/3.0.0",
            version="a" * 16,
            owner="test:cursor",
            created=datetime.now(tz=timezone.utc),
            state=LifecycleState.ACTIVE,
        ),
        scope=SourceScope(owner_tenant="acme", languages=["en"]),
        acquisition="poll",
        config={"url": "http://unused"},
        cadence=CadenceBlock(schedule=Cron(raw="*/5 * * * *")),
        pipeline=SourcePipeline(media="reference"),
    )


def _entry(source_id: str, ts: datetime) -> Signal:
    return Signal(
        source_id=source_id,
        modality="text",
        payload={"title": f"item-{ts.isoformat()}", "_published_at_dt": ts},
        content_hash=uuid4().hex,
    )


def _wire_core(core: SourceCore, store: InMemoryStateStore, handler) -> None:
    """Point the core's context + handler at the in-memory doubles."""
    core.sd.handler = handler

    def _make_context():
        return SourceContext(
            target_id=core.descriptor.identity.id,
            target_version=core.descriptor.identity.version,
            source_id=core.descriptor.identity.id,
            config=_RawConfig(**(core.descriptor.config or {})),
            state_store=store,
            scope_geo=list(core.descriptor.scope.geo),
            scope_languages=list(core.descriptor.scope.languages),
        )

    core._make_context = _make_context  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# A — capped pull advances to the last processed entry, not NOW
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capped_pull_advances_to_last_processed_not_now(monkeypatch):
    import legba.runtime.source_actor as sa

    # Shrink the cap so a small fixture trips it deterministically.
    monkeypatch.setattr(sa, "_MAX_ENTRIES_PER_POLL", 3)

    source_id = f"source.test.cap_{uuid4().hex[:8]}"
    sd = _poll_descriptor(source_id)
    deps = StandardDeps(pg_pool=_FakePool(), nats_publish=None)
    core = SourceCore(f"source::{source_id}::cap", SourceDeps(descriptor=sd, deps=deps))

    base = datetime(2025, 6, 1, 9, 0, tzinfo=timezone.utc)
    # 10 backlog entries at +1min steps; the actor will cap after 3.
    entries = [_entry(source_id, base + timedelta(minutes=i)) for i in range(10)]
    store = InMemoryStateStore()
    _wire_core(core, store, _StubHandler(entries))

    before = datetime.now(tz=timezone.utc)
    result = await core.pull_once()
    after = datetime.now(tz=timezone.utc)

    assert result["signals_written"] == 3        # capped at 3

    cursor = await store.get("cursor")
    advanced = datetime.fromisoformat(cursor["last_pulled_at"])
    # The cursor advanced to the 3rd entry's logical ts (base + 2min) — the
    # LAST PROCESSED entry — NOT to wall-clock NOW (which would skip entries
    # 4..10 of the backlog).
    assert advanced == base + timedelta(minutes=2)
    assert advanced < before        # provably not NOW
    assert not (before <= advanced <= after)


@pytest.mark.asyncio
async def test_capped_pull_resumes_backlog_on_next_pull(monkeypatch):
    """The next pull feeds the advanced cursor back as ``since`` — so the
    backlog is RESUMED from where the cap stopped, never skipped."""
    import legba.runtime.source_actor as sa

    monkeypatch.setattr(sa, "_MAX_ENTRIES_PER_POLL", 3)

    source_id = f"source.test.resume_{uuid4().hex[:8]}"
    sd = _poll_descriptor(source_id)
    deps = StandardDeps(pg_pool=_FakePool(), nats_publish=None)
    core = SourceCore(f"source::{source_id}::resume", SourceDeps(descriptor=sd, deps=deps))

    base = datetime(2025, 6, 1, 9, 0, tzinfo=timezone.utc)
    entries = [_entry(source_id, base + timedelta(minutes=i)) for i in range(10)]
    store = InMemoryStateStore()
    handler = _StubHandler(entries)
    _wire_core(core, store, handler)

    await core.pull_once()
    cursor = await store.get("cursor")
    advanced = datetime.fromisoformat(cursor["last_pulled_at"])

    # Second pull: the handler is handed the advanced cursor as ``since`` (the
    # resume point), proving the next window starts mid-backlog, not at NOW.
    await core.pull_once()
    assert handler.seen_since == advanced
    assert handler.seen_since == base + timedelta(minutes=2)


@pytest.mark.asyncio
async def test_empty_pull_advances_to_now(monkeypatch):
    """A genuinely empty pull (zero entries) keeps the original anti-trap:
    advance to NOW so a caught-up source doesn't re-grind an old window."""
    source_id = f"source.test.empty_{uuid4().hex[:8]}"
    sd = _poll_descriptor(source_id)
    deps = StandardDeps(pg_pool=_FakePool(), nats_publish=None)
    core = SourceCore(f"source::{source_id}::empty", SourceDeps(descriptor=sd, deps=deps))

    store = InMemoryStateStore()
    _wire_core(core, store, _StubHandler([]))

    before = datetime.now(tz=timezone.utc)
    await core.pull_once()
    after = datetime.now(tz=timezone.utc)

    cursor = await store.get("cursor")
    advanced = datetime.fromisoformat(cursor["last_pulled_at"])
    assert before <= advanced <= after        # advanced to NOW


@pytest.mark.asyncio
async def test_complete_uncapped_pull_advances_to_now(monkeypatch):
    """A pull that drains the whole window (not capped) advances to NOW — the
    last-processed-timestamp rule only kicks in on a CAPPED pull."""
    import legba.runtime.source_actor as sa

    monkeypatch.setattr(sa, "_MAX_ENTRIES_PER_POLL", 100)

    source_id = f"source.test.full_{uuid4().hex[:8]}"
    sd = _poll_descriptor(source_id)
    deps = StandardDeps(pg_pool=_FakePool(), nats_publish=None)
    core = SourceCore(f"source::{source_id}::full", SourceDeps(descriptor=sd, deps=deps))

    base = datetime(2025, 6, 1, 9, 0, tzinfo=timezone.utc)
    entries = [_entry(source_id, base + timedelta(minutes=i)) for i in range(3)]
    store = InMemoryStateStore()
    _wire_core(core, store, _StubHandler(entries))

    before = datetime.now(tz=timezone.utc)
    result = await core.pull_once()
    after = datetime.now(tz=timezone.utc)

    assert result["signals_written"] == 3
    cursor = await store.get("cursor")
    advanced = datetime.fromisoformat(cursor["last_pulled_at"])
    assert before <= advanced <= after        # NOW, not the last entry ts


# ---------------------------------------------------------------------------
# B — actor persists + advances the bulk high-water-mark offset
# ---------------------------------------------------------------------------


def _bulk_descriptor(source_id: str) -> SourceDescriptor:
    return SourceDescriptor(
        identity=SourceIdentity(
            id=source_id,
            name="bulk-test",
            kind="opensanctions",
            schema_uri="legba/source/3.0.0",
            version="b" * 16,
            owner="test:cursor",
            created=datetime.now(tz=timezone.utc),
            state=LifecycleState.ACTIVE,
        ),
        scope=SourceScope(owner_tenant="acme"),
        acquisition="poll",
        config={"mode": "bulk_csv", "dataset": "default"},
        cadence=CadenceBlock(schedule=Cron(raw="0 3 * * *")),
        pipeline=SourcePipeline(media="reference"),
    )


class _BulkStubHandler:
    """Stub bulk handler mirroring the real OpenSanctions bulk contract.

    Reads ``bulk_resume_offset`` (an EMITTED-row high-water mark) from the
    shared store, skips that many emit-eligible rows, yields the rest, and —
    only when it drains the whole snapshot (no actor cap suspends it) — writes
    a ``bulk_traversed`` report with ``reached_end=True``. When the actor caps
    it mid-stream the generator suspends before the EOF report, so the actor
    falls back to its own ``capped`` knowledge (reached_end=False). The actor
    advances the offset by ITS OWN processed-count, so over-producing one row
    at the cap boundary is harmless (it is re-emitted, never skipped).
    """

    def __init__(self, source_id: str, total_rows: int) -> None:
        self._source_id = source_id
        self._total = total_rows

    async def pull(self, ctx: SourceContext, since=None):
        offset = int(await ctx.state_store.get(BULK_RESUME_OFFSET_KEY) or 0)
        base = datetime(2025, 6, 1, tzinfo=timezone.utc)
        emit_index = 0
        emitted = 0
        for _ in range(self._total):
            emit_index += 1
            if emit_index <= offset:
                continue
            emitted += 1
            yield _entry(self._source_id, base)
        # Stream fully consumed -> reached end (a complete walk).
        await ctx.state_store.set(
            BULK_TRAVERSED_KEY, {"rows": emitted, "reached_end": True},
        )


@pytest.mark.asyncio
async def test_bulk_offset_advances_across_capped_pulls(monkeypatch):
    import legba.runtime.source_actor as sa

    monkeypatch.setattr(sa, "_MAX_ENTRIES_PER_POLL", 100)

    source_id = f"source.test.bulk_{uuid4().hex[:8]}"
    sd = _bulk_descriptor(source_id)
    deps = StandardDeps(pg_pool=_FakePool(), nats_publish=None)
    core = SourceCore(f"source::{source_id}::bulk", SourceDeps(descriptor=sd, deps=deps))

    store = InMemoryStateStore()
    # 250-row snapshot, 100/pull cap -> three pulls (100, 100, 50) then reset.
    _wire_core(core, store, _BulkStubHandler(source_id, total_rows=250))

    # Pull 1: rows 0..99 -> offset 100.
    await core.pull_once()
    cur = await store.get("cursor")
    assert cur["bulk_offset"] == 100

    # Pull 2: resumes at 100, walks 100..199 -> offset 200.
    await core.pull_once()
    cur = await store.get("cursor")
    assert cur["bulk_offset"] == 200

    # Pull 3: resumes at 200, walks 200..249 (50 rows), hits end-of-stream ->
    # offset RESETS to 0 so the next snapshot re-walks from the top.
    await core.pull_once()
    cur = await store.get("cursor")
    assert cur["bulk_offset"] == 0


@pytest.mark.asyncio
async def test_non_bulk_source_has_no_bulk_offset(monkeypatch):
    """A non-bulk (rss) source never grows a bulk_offset key — the high-water
    machinery is mode-scoped, not applied to every poll source."""
    source_id = f"source.test.nonbulk_{uuid4().hex[:8]}"
    sd = _poll_descriptor(source_id)        # kind=rss
    deps = StandardDeps(pg_pool=_FakePool(), nats_publish=None)
    core = SourceCore(f"source::{source_id}::nb", SourceDeps(descriptor=sd, deps=deps))

    store = InMemoryStateStore()
    base = datetime(2025, 6, 1, 9, 0, tzinfo=timezone.utc)
    _wire_core(core, store, _StubHandler([_entry(source_id, base)]))

    await core.pull_once()
    cur = await store.get("cursor")
    assert "bulk_offset" not in cur
    assert await store.get(BULK_RESUME_OFFSET_KEY) is None
