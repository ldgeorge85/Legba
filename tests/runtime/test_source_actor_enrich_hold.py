# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W11-F103 — a transiently skipped entry never advances the cursor past itself.

INCIDENT 2026-10-23: the hosted NLP service died, per-entry enrichment hung
until the 12 s timeout, the timed-out entries were skipped ("the next poll
re-pulls it") — and the cursor advanced past them anyway, so the next poll
never re-pulled them. These tests drive :class:`SourceCore.pull_once` against
the in-memory doubles of ``test_source_actor_cursor`` (no DB, no network) with
an enrichment stage that HANGS on chosen entries:

  * entry 2 of 5 times out -> cursor = entry 2's ts - 1µs; entries 3-5 written
    unenriched (the NLP chain is bypassed for the rest of the poll);
  * permanent drops only -> cursor at the last entry (unchanged behaviour);
  * the same entry skipped on N consecutive polls -> written unenriched;
  * hard-error and first-ever-pull branches unchanged; the bulk offset holds.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

import legba.runtime.source_actor as sa
from legba.data.sources._contract import InMemoryStateStore, SourceContext
from legba.data.stack.nlp_service.client import NlpServiceUnavailable
from legba.runtime.deps import StandardDeps
from legba.runtime.source_actor import SourceCore, SourceDeps
from legba.runtime.source_enrich_hold import (
    CURSOR_EPSILON,
    ENRICH_HOLD_MAX_SKIPS,
    PENDING_DEGRADED,
    PENDING_HOLD_EXHAUSTED,
    EnrichHold,
)
from tests.runtime.test_source_actor_cursor import (
    _BulkStubHandler,
    _bulk_descriptor,
    _entry,
    _FakeConn,
    _FakePool,
    _poll_descriptor,
    _StubHandler,
    _wire_core,
)

BASE = datetime(2025, 6, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fast_timeout(monkeypatch):
    monkeypatch.setattr(sa, "_ENRICH_TIMEOUT_S", 0.05)


class _Stage:
    """Enrichment-stage double: records each call; hangs (or raises) for the
    titles in ``hang`` (``None`` = every entry)."""

    def __init__(self, hang=None, *, raise_exc: Exception | None = None, drop=False):
        self.hang = hang
        self.raise_exc = raise_exc
        self.drop = drop
        self.calls: list[str] = []

    async def __call__(self, signal, ctx):
        title = signal.payload.get("title")
        self.calls.append(title)
        if self.hang is None or title in self.hang:
            if self.raise_exc is not None:
                raise self.raise_exc
            await asyncio.sleep(30)
        if self.drop:
            return None
        signal.payload["enriched"] = True
        return signal


def _core(stage, sd=None):
    sd = sd or _poll_descriptor(f"source.test.hold_{uuid4().hex[:8]}")
    deps = StandardDeps(pg_pool=_FakePool(), nats_publish=None)
    return SourceCore(
        f"source::{sd.identity.id}::h",
        SourceDeps(descriptor=sd, deps=deps, enrichment_stage=stage),
    )


def _entries(core, n, *, descending=False):
    sid = core.descriptor.identity.id
    out = [_entry(sid, BASE + timedelta(minutes=i)) for i in range(n)]
    return list(reversed(out)) if descending else out


class _RecordingHandler(_StubHandler):
    def __init__(self, signals, *, fail_after: int | None = None):
        super().__init__(signals)
        self.fail_after = fail_after

    async def pull(self, ctx: SourceContext, since=None):
        self.seen_since = since
        for i, sig in enumerate(self._signals):
            if self.fail_after is not None and i == self.fail_after:
                raise RuntimeError("upstream exploded")
            yield sig


async def _cursor(store):
    cur = await store.get("cursor")
    return cur, datetime.fromisoformat(cur["last_pulled_at"])


# ---------------------------------------------------------------------------
# 1. entry 2 of 5 times out
# ---------------------------------------------------------------------------


async def test_timeout_on_entry_2_holds_cursor_before_it_and_degrades_the_rest():
    stage = _Stage()
    core = _core(stage)
    entries = _entries(core, 5)
    stage.hang = {entries[1].payload["title"]}
    store = InMemoryStateStore()
    handler = _RecordingHandler(entries)
    _wire_core(core, store, handler)

    result = await core.pull_once()

    # Entry 2 skipped; 1 enriched; 3-5 written WITHOUT the NLP chain.
    assert result["signals_written"] == 4
    assert result["enrichment_skipped"] == 1
    assert result["enrichment_bypassed"] == 3
    assert stage.calls == [entries[0].payload["title"], entries[1].payload["title"]]
    assert entries[0].payload.get("enriched") is True
    assert "enrichment_pending" not in entries[0].payload
    for e in entries[2:]:
        assert e.payload["enrichment_pending"] == PENDING_DEGRADED
        assert "enriched" not in e.payload

    cur, advanced = await _cursor(store)
    e2_ts = BASE + timedelta(minutes=1)
    assert advanced == e2_ts - CURSOR_EPSILON
    assert advanced < e2_ts                      # next strict-after pull re-yields it
    assert cur["enrich_hold"]["skips"] == 1
    assert cur["enrichment_skipped_last_poll"] == 1

    # The next poll is handed the held cursor — entry 2 comes back.
    await core.pull_once()
    assert handler.seen_since == e2_ts - CURSOR_EPSILON


async def test_newest_first_feed_holds_at_the_earliest_skipped_ts():
    """RSS iterates newest-first: the hold is the skipped entry's ts, not the
    first-iterated one's, and the older entries after it are not re-held."""
    stage = _Stage()
    core = _core(stage)
    entries = _entries(core, 5, descending=True)  # ts +4, +3, +2, +1, +0 min
    stage.hang = {entries[1].payload["title"]}    # the +3 min entry
    store = InMemoryStateStore()
    _wire_core(core, store, _RecordingHandler(entries))

    await core.pull_once()

    _, advanced = await _cursor(store)
    assert advanced == BASE + timedelta(minutes=3) - CURSOR_EPSILON


async def test_nlp_unavailable_is_a_transient_skip_on_the_poll_path():
    stage = _Stage(raise_exc=NlpServiceUnavailable("models host down"))
    core = _core(stage)
    entries = _entries(core, 3)
    stage.hang = {entries[0].payload["title"]}
    store = InMemoryStateStore()
    _wire_core(core, store, _RecordingHandler(entries))

    result = await core.pull_once()

    assert result["error"] is None
    assert result["signals_written"] == 2
    _, advanced = await _cursor(store)
    assert advanced == BASE - CURSOR_EPSILON


async def test_push_path_still_raises_nlp_unavailable():
    """The push path has no cursor to hold: its behaviour is unchanged."""
    stage = _Stage(raise_exc=NlpServiceUnavailable("down"))
    core = _core(stage)
    raw = _entries(core, 1)[0]
    store = InMemoryStateStore()
    _wire_core(core, store, _RecordingHandler([]))
    with pytest.raises(NlpServiceUnavailable):
        await core._process_one(_FakeConn(), core._make_context(), raw)


# ---------------------------------------------------------------------------
# 2. permanent drops only -> unchanged
# ---------------------------------------------------------------------------


async def test_permanent_drops_still_advance_to_the_last_entry():
    stage = _Stage(hang=set(), drop=True)       # baseline filter drops every entry
    core = _core(stage)
    entries = _entries(core, 4)
    store = InMemoryStateStore()
    _wire_core(core, store, _RecordingHandler(entries))

    result = await core.pull_once()

    assert result["signals_written"] == 0
    assert result["enrichment_skipped"] == 0
    cur, advanced = await _cursor(store)
    assert advanced == BASE + timedelta(minutes=3)
    assert cur["enrich_hold"] is None


# ---------------------------------------------------------------------------
# 3. N consecutive skips -> written unenriched
# ---------------------------------------------------------------------------


async def test_entry_skipped_on_n_consecutive_polls_is_written_unenriched():
    stage = _Stage(hang=None)                    # the plane is dead
    core = _core(stage)
    entry = _entries(core, 1)[0]
    store = InMemoryStateStore()
    _wire_core(core, store, _RecordingHandler([entry]))

    for poll in range(1, ENRICH_HOLD_MAX_SKIPS + 1):
        result = await core.pull_once()
        assert result["signals_written"] == 0
        cur, advanced = await _cursor(store)
        assert advanced == BASE - CURSOR_EPSILON     # held every poll
        assert cur["enrich_hold"]["skips"] == poll
    assert len(stage.calls) == ENRICH_HOLD_MAX_SKIPS

    result = await core.pull_once()                  # poll N+1

    assert result["signals_written"] == 1
    assert result["enrichment_bypassed"] == 1
    assert len(stage.calls) == ENRICH_HOLD_MAX_SKIPS  # not attempted again
    assert entry.payload["enrichment_pending"] == PENDING_HOLD_EXHAUSTED
    cur, advanced = await _cursor(store)
    assert advanced == BASE                          # progress resumes
    assert cur["enrich_hold"] is None


async def test_a_different_skipped_entry_restarts_the_count():
    hold = EnrichHold({"enrich_hold": {"key": "other", "skips": 2}})
    hold.at(_entry("s", BASE), BASE, 0)
    assert hold.bypass_reason() is None
    hold.skip("timeout")
    assert hold.cursor_fields(errored=False)["enrich_hold"]["skips"] == 1


# ---------------------------------------------------------------------------
# 4. degraded for the rest of the poll
# ---------------------------------------------------------------------------


async def test_dead_plane_pays_the_timeout_once_per_poll_not_per_entry():
    stage = _Stage(hang=None)
    core = _core(stage)
    entries = _entries(core, 20)
    store = InMemoryStateStore()
    _wire_core(core, store, _RecordingHandler(entries))

    result = await core.pull_once()

    assert len(stage.calls) == 1                 # one hung call, then bypass
    assert result["enrichment_skipped"] == 1
    assert result["signals_written"] == 19
    _, advanced = await _cursor(store)
    assert advanced == BASE - CURSOR_EPSILON     # held before the skipped entry


# ---------------------------------------------------------------------------
# 5. unchanged branches: hard error, first-ever pull, clamp, bulk
# ---------------------------------------------------------------------------


async def test_hard_error_keeps_prior_cursor_and_prior_hold():
    stage = _Stage()
    core = _core(stage)
    entries = _entries(core, 4)
    stage.hang = {entries[0].payload["title"]}
    prior = (BASE - timedelta(hours=1)).isoformat()
    prior_hold = {"key": "k", "ts": prior, "skips": 2}
    store = InMemoryStateStore(
        {"cursor": {"last_pulled_at": prior, "enrich_hold": prior_hold}},
    )
    _wire_core(core, store, _RecordingHandler(entries, fail_after=2))

    result = await core.pull_once()

    assert result["error"] == "upstream exploded"
    cur = await store.get("cursor")
    assert cur["last_pulled_at"] == prior
    assert cur["enrich_hold"] == prior_hold


async def test_first_ever_empty_pull_still_seeds_now():
    core = _core(_Stage())
    store = InMemoryStateStore()
    _wire_core(core, store, _RecordingHandler([]))
    before = datetime.now(tz=timezone.utc)
    await core.pull_once()
    _, advanced = await _cursor(store)
    assert before <= advanced <= datetime.now(tz=timezone.utc)


async def test_hold_never_rewinds_below_the_prior_cursor():
    """A handler that ignores ``since`` may yield an entry older than the
    cursor; skipping it must not rewind (it re-yields regardless)."""
    hold = EnrichHold({})
    hold.at(_entry("s", BASE), BASE, 0)
    hold.skip("timeout")
    since = BASE + timedelta(hours=1)
    assert hold.cursor_ts(BASE + timedelta(hours=2), since) == since
    assert hold.cursor_ts(BASE, None) == BASE - CURSOR_EPSILON


async def test_bulk_offset_holds_at_the_first_skipped_row():
    stage = _Stage()
    sd = _bulk_descriptor(f"source.test.bulkhold_{uuid4().hex[:8]}")
    core = _core(stage, sd)
    store = InMemoryStateStore({"cursor": {"bulk_offset": 10}})
    handler = _BulkStubHandler(sd.identity.id, total_rows=15)   # 5 rows left

    calls = {"n": 0}

    async def _hang_second(signal, ctx):
        calls["n"] += 1
        if calls["n"] == 2:
            await asyncio.sleep(30)
        return signal

    core.sd.enrichment_stage = _hang_second
    _wire_core(core, store, handler)

    await core.pull_once()

    cur = await store.get("cursor")
    # Row index 1 of this walk was skipped: advance by 1 (not 5), and the
    # held poll never reports end-of-stream (no reset to 0).
    assert cur["bulk_offset"] == 11
