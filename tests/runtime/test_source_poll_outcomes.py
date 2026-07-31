# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DQ-H5b (#88) + migration 0114 — per-poll provenance at the source poll path.

Exercises ``SourceCore._record_poll_outcome`` via ``pull_once``, driven against
in-memory substrate doubles (no live Postgres / Dapr — the outcome-classification
+ best-effort-write logic is the unit, not the DB). A capturing pool records the
``source_poll_outcomes`` INSERT so we can assert the row that would be written.

Covered:
  * an empty (HTTP-200-but-0-items) poll → ONE row, outcome='empty', no health;
  * a handler-swallowed failure (4xx/parse-fail → health 'unhealthy', or a
    transient → 'degraded') → outcome='error' with the health state + last_error
    surfaced (the case ``errored`` can't see because no exception escapes);
  * an escaped exception → outcome='error' with the exception string;
  * a PRODUCTIVE poll (>=1 signal) → outcome='success' carrying its
    ``signals_written`` count. This is migration 0114: the original design
    wrote NOTHING here, on the premise that signals rows are self-evidencing —
    which is true of one poll and false of any reader that walks a RUN, because
    an absence cannot break a run. A repaired source therefore kept presenting
    its historical 'error' rows as the leading run and ``entity_gc`` op 4
    re-paused it mid-ingest;
  * 'empty' STILL means exactly "polled fine, nothing new" — the two are not
    collapsed;
  * a productive poll that ALSO failed part-way → 'success' (it produced), with
    the error text preserved on the row rather than discarded;
  * a poll that wrote 0 rows but COLLAPSED an intra-source duplicate → 'success'
    with signals_written=0 (it saw current content and bumped recency);
  * a capped 0-written poll → outcome='empty' + capped, and the (possibly stale)
    handler health is NOT consulted;
  * a provenance-write failure NEVER masks the pull result.
"""

from __future__ import annotations

from datetime import datetime, timezone
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
from legba.runtime.source_actor import SourceCore, SourceDeps, _RawConfig

_SOURCE_VERSION = "a" * 16
_TENANT = "acme"


# ---------------------------------------------------------------------------
# Capturing substrate doubles (no live Postgres / Dapr)
# ---------------------------------------------------------------------------


class _CapturingConn:
    """asyncpg-conn double. Signal writes (fetchrow) succeed; poll-outcome
    writes (execute) are captured — or made to fail, to prove they never mask
    the pull result."""

    def __init__(self, sink: list, *, fail_execute: bool = False) -> None:
        self._sink = sink
        self._fail = fail_execute

    async def fetchrow(self, query, *args):
        return {"id": args[0]}        # write_canonical_signal passes id first

    async def execute(self, query, *args):
        if self._fail:
            raise RuntimeError("boom-provenance")
        self._sink.append((query, args))
        return None

    async def fetchval(self, *a, **k):  # pragma: no cover - unused
        return None


class _CapturingPool:
    def __init__(self, *, fail_execute: bool = False) -> None:
        self.executes: list = []
        self._fail = fail_execute

    def acquire(self):
        sink = self.executes
        fail = self._fail

        class _Acq:
            async def __aenter__(self_inner):
                return _CapturingConn(sink, fail_execute=fail)

            async def __aexit__(self_inner, *exc):
                return False

        return _Acq()

    # -- helpers ---------------------------------------------------------
    @property
    def outcome_writes(self) -> list[dict]:
        """Decode every captured poll-outcome INSERT into a kwargs dict, by the
        column order in ``_INSERT_POLL_OUTCOME``."""
        cols = (
            "source_id", "source_version", "owner_tenant", "outcome",
            "health_state", "capped", "signals_written", "error",
            "newest_entry_ts",
        )
        rows = []
        for query, args in self.executes:
            if "source_poll_outcomes" in query:
                rows.append(dict(zip(cols, args)))
        return rows


class _StubHandler:
    """Yields a controllable list of Signals; optionally records a health
    record under ``health_state_key`` (mirroring the real handlers) and/or
    raises after yielding."""

    health_state_key = "stub_health"

    def __init__(self, signals, *, health=None, raise_exc=None) -> None:
        self._signals = signals
        self._health = health
        self._raise = raise_exc

    async def pull(self, ctx: SourceContext, since=None):
        if self._health is not None:
            await ctx.state_store.set(self.health_state_key, self._health)
        for sig in self._signals:
            yield sig
        if self._raise is not None:
            raise self._raise


def _descriptor(source_id: str) -> SourceDescriptor:
    return SourceDescriptor(
        identity=SourceIdentity(
            id=source_id,
            name="poll-outcome-test",
            kind="rss",
            schema_uri="legba/source/3.0.0",
            version=_SOURCE_VERSION,
            owner="test:poll_outcome",
            created=datetime.now(tz=timezone.utc),
            state=LifecycleState.ACTIVE,
        ),
        scope=SourceScope(owner_tenant=_TENANT, languages=["en"]),
        acquisition="poll",
        config={"url": "http://unused"},
        cadence=CadenceBlock(schedule=Cron(raw="*/5 * * * *")),
        pipeline=SourcePipeline(media="reference"),
    )


def _entry(source_id: str) -> Signal:
    return Signal(
        source_id=source_id,
        modality="text",
        payload={"title": "item", "_published_at_dt": datetime(2025, 6, 1, tzinfo=timezone.utc)},
        content_hash=uuid4().hex,
    )


def _build(handler, *, fail_execute: bool = False):
    source_id = f"source.test.po_{uuid4().hex[:8]}"
    sd = _descriptor(source_id)
    pool = _CapturingPool(fail_execute=fail_execute)
    deps = StandardDeps(pg_pool=pool, nats_publish=None)
    core = SourceCore(f"source::{source_id}::po", SourceDeps(descriptor=sd, deps=deps))
    store = InMemoryStateStore()
    core.sd.handler = handler

    def _make_context():
        return SourceContext(
            target_id=source_id,
            target_version=_SOURCE_VERSION,
            source_id=source_id,
            config=_RawConfig(**(sd.config or {})),
            state_store=store,
            scope_geo=[],
            scope_languages=["en"],
        )

    core._make_context = _make_context  # type: ignore[method-assign]
    return core, pool, store, source_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_poll_writes_empty_outcome() -> None:
    core, pool, _store, source_id = _build(_StubHandler([]))
    result = await core.pull_once()
    assert result["signals_written"] == 0

    writes = pool.outcome_writes
    assert len(writes) == 1
    row = writes[0]
    assert row["source_id"] == source_id
    assert row["source_version"] == _SOURCE_VERSION
    assert row["owner_tenant"] == _TENANT
    assert row["outcome"] == "empty"
    assert row["health_state"] is None
    assert row["capped"] is False
    assert row["signals_written"] == 0
    assert row["error"] is None
    assert row["newest_entry_ts"] is None  # no health record → no observation


@pytest.mark.asyncio
async def test_unhealthy_handler_writes_error_outcome() -> None:
    # 4xx / parse-fail: the handler swallows it (no exception escapes) but
    # records unhealthy health — that's the case `errored` can't see.
    health = {"state": "unhealthy", "last_error": "HTTP 403", "detail": {}}
    core, pool, _store, _sid = _build(_StubHandler([], health=health))
    await core.pull_once()

    row = pool.outcome_writes[0]
    assert row["outcome"] == "error"
    assert row["health_state"] == "unhealthy"
    assert row["error"] == "HTTP 403"
    assert row["signals_written"] == 0


@pytest.mark.asyncio
async def test_degraded_handler_writes_error_outcome() -> None:
    health = {"state": "degraded", "last_error": "timeout", "detail": {}}
    core, pool, _store, _sid = _build(_StubHandler([], health=health))
    await core.pull_once()

    row = pool.outcome_writes[0]
    assert row["outcome"] == "error"
    assert row["health_state"] == "degraded"
    assert row["error"] == "timeout"


@pytest.mark.asyncio
async def test_healthy_empty_handler_stays_empty() -> None:
    # A healthy feed that simply had no new items → genuine 'empty', not 'error'.
    health = {"state": "healthy", "last_error": None, "detail": {"items_seen": 0}}
    core, pool, _store, _sid = _build(_StubHandler([], health=health))
    await core.pull_once()

    row = pool.outcome_writes[0]
    assert row["outcome"] == "empty"
    assert row["health_state"] == "healthy"
    assert row["error"] is None


@pytest.mark.asyncio
async def test_escaped_exception_writes_error_outcome() -> None:
    core, pool, _store, _sid = _build(
        _StubHandler([], raise_exc=RuntimeError("connection reset"))
    )
    result = await core.pull_once()
    assert result["outcome"] == "hard_fail"

    row = pool.outcome_writes[0]
    assert row["outcome"] == "error"
    assert "connection reset" in (row["error"] or "")


@pytest.mark.asyncio
async def test_productive_poll_writes_success_outcome() -> None:
    # >=1 signal written → ONE row, outcome='success', carrying the count.
    # (Migration 0114 — this used to write nothing at all, which is what let a
    # recovered source's historical error run stay the LEADING run forever.)
    core, pool, _store, source_id = _build(_StubHandler([_entry("source.test")]))
    result = await core.pull_once()
    assert result["signals_written"] == 1

    writes = pool.outcome_writes
    assert len(writes) == 1
    row = writes[0]
    assert row["source_id"] == source_id
    assert row["outcome"] == "success"
    assert row["signals_written"] == 1
    assert row["error"] is None


@pytest.mark.asyncio
async def test_success_row_carries_the_real_signal_count() -> None:
    core, pool, _store, _sid = _build(
        _StubHandler([_entry("source.test") for _ in range(7)])
    )
    result = await core.pull_once()
    assert result["signals_written"] == 7

    row = pool.outcome_writes[0]
    assert row["outcome"] == "success"
    assert row["signals_written"] == 7


@pytest.mark.asyncio
async def test_empty_and_success_stay_distinct_outcomes() -> None:
    # "polled and found nothing" and "polled and ingested items" are DIFFERENT
    # facts — the empty-streak watchdog and the freshness reads depend on the
    # distinction, so 0114 must not have collapsed them.
    core_quiet, pool_quiet, _s1, _i1 = _build(_StubHandler([]))
    await core_quiet.pull_once()
    core_busy, pool_busy, _s2, _i2 = _build(_StubHandler([_entry("source.test")]))
    await core_busy.pull_once()

    assert pool_quiet.outcome_writes[0]["outcome"] == "empty"
    assert pool_quiet.outcome_writes[0]["signals_written"] == 0
    assert pool_busy.outcome_writes[0]["outcome"] == "success"
    assert pool_busy.outcome_writes[0]["signals_written"] == 1


@pytest.mark.asyncio
async def test_partial_failure_after_writing_is_success_but_keeps_the_error() -> None:
    # Wrote a signal, THEN the handler raised. The source produced, so this is
    # not a failing poll (counting it as one is exactly how a producing source
    # gets latched), but the error detail must survive on the row.
    core, pool, _store, _sid = _build(
        _StubHandler(
            [_entry("source.test")], raise_exc=RuntimeError("connection reset"),
        )
    )
    result = await core.pull_once()
    assert result["signals_written"] == 1

    row = pool.outcome_writes[0]
    assert row["outcome"] == "success"
    assert row["signals_written"] == 1
    assert "connection reset" in (row["error"] or "")


@pytest.mark.asyncio
async def test_dedup_collapsed_poll_is_success_not_empty() -> None:
    # S-4: 0 rows written but >=1 intra-source duplicate collapsed — the poll
    # saw current content and bumped recency. It is productive, so it records
    # 'success' with a 0 count; recording 'empty' would let a hazard feed that
    # healthily re-serves active events trip the empty-streak degradation.
    core, pool, _store, _sid = _build(_StubHandler([_entry("source.test")]))

    async def _collapse(conn, ctx, raw, *, dedup_stats=None):
        if dedup_stats is not None:
            dedup_stats["deduped"] = dedup_stats.get("deduped", 0) + 1
        return None

    core._process_one = _collapse  # type: ignore[method-assign]

    result = await core.pull_once()
    assert result["signals_written"] == 0

    row = pool.outcome_writes[0]
    assert row["outcome"] == "success"
    assert row["signals_written"] == 0


@pytest.mark.asyncio
async def test_capped_zero_written_does_not_consult_health(monkeypatch) -> None:
    import legba.runtime.source_actor as sa

    monkeypatch.setattr(sa, "_MAX_ENTRIES_PER_POLL", 1)

    # Handler reports 'unhealthy' but the pull is CAPPED mid-stream, so that
    # health record may be stale — the stale-guard must NOT consult it (nor
    # its newest_entry_ts observation).
    health = {
        "state": "unhealthy", "last_error": "stale!",
        "detail": {"newest_entry_ts": "2026-06-20T10:00:00+00:00"},
    }
    core, pool, _store, source_id = _build(
        _StubHandler([_entry("source.test") for _ in range(3)], health=health)
    )

    async def _drop(conn, ctx, raw, *, dedup_stats=None):  # all dropped → 0 written
        return None

    core._process_one = _drop  # type: ignore[method-assign]

    result = await core.pull_once()
    assert result["signals_written"] == 0

    row = pool.outcome_writes[0]
    assert row["capped"] is True
    assert row["outcome"] == "empty"        # NOT 'error' — health was not read
    assert row["health_state"] is None
    assert row["error"] is None
    assert row["newest_entry_ts"] is None   # stale health NOT consulted (B0-12 too)


@pytest.mark.asyncio
async def test_capped_is_still_recorded_at_a_handler_advertised_cap() -> None:
    """R5 — the cap moved (per-source resolution), the PROVENANCE did not.

    A poll cut at the source's OWN resolved entry cap still records
    ``capped=True``, so the 'is this source being truncated?' question stays
    answerable from ``source_poll_outcomes`` — that column is how the
    starvation was diagnosed in the first place (100 of 105 polls capped).
    """
    class _AdvertisingStubHandler(_StubHandler):
        max_entries_per_poll = 2      # this source's own bound, not the default

    core, pool, _store, _sid = _build(
        _AdvertisingStubHandler([_entry("source.test") for _ in range(5)])
    )

    result = await core.pull_once()
    assert result["signals_written"] == 2          # cut at the advertised cap

    row = pool.outcome_writes[0]
    assert row["capped"] is True
    assert row["outcome"] == "success"             # it produced
    assert row["signals_written"] == 2


@pytest.mark.asyncio
async def test_outcome_write_failure_does_not_mask_pull() -> None:
    # The provenance INSERT raises; pull_once must still return its summary and
    # not propagate (best-effort write).
    core, _pool, store, _sid = _build(_StubHandler([]), fail_execute=True)
    result = await core.pull_once()
    assert result["outcome"] == "noop"
    assert result["signals_written"] == 0
    # The cursor still persisted normally (the failure was isolated).
    assert (await store.get("cursor")) is not None


# ---------------------------------------------------------------------------
# B0-12: the handler's newest-observed-upstream-entry evidence flows onto the
# poll-outcome row (the watchdog's quiet-vs-cursor-fault discriminator input).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_newest_entry_ts_flows_to_outcome_row() -> None:
    health = {
        "state": "healthy", "last_error": None,
        "detail": {"newest_entry_ts": "2026-06-20T10:00:00+00:00"},
    }
    core, pool, _store, _sid = _build(_StubHandler([], health=health))
    await core.pull_once()

    row = pool.outcome_writes[0]
    assert row["outcome"] == "empty"
    assert row["newest_entry_ts"] == datetime(
        2026, 6, 20, 10, 0, 0, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_health_naive_newest_entry_ts_coerced_to_utc() -> None:
    health = {
        "state": "healthy", "last_error": None,
        "detail": {"newest_entry_ts": "2026-06-20T10:00:00"},  # tz-less
    }
    core, pool, _store, _sid = _build(_StubHandler([], health=health))
    await core.pull_once()

    row = pool.outcome_writes[0]
    assert row["newest_entry_ts"] == datetime(
        2026, 6, 20, 10, 0, 0, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_health_bad_newest_entry_ts_degrades_to_null() -> None:
    # A junk observation string must not break the provenance write — it
    # degrades to NULL (the watchdog's no-evidence class).
    health = {
        "state": "healthy", "last_error": None,
        "detail": {"newest_entry_ts": "not-a-timestamp"},
    }
    core, pool, _store, _sid = _build(_StubHandler([], health=health))
    await core.pull_once()

    row = pool.outcome_writes[0]
    assert row["outcome"] == "empty"
    assert row["newest_entry_ts"] is None


# ---------------------------------------------------------------------------
# Schema drift guard: every outcome the writer can emit must be admitted by the
# table's CHECK constraint. A value the poll path emits but the constraint
# rejects does not fail loudly — the provenance INSERT is best-effort and only
# logs a warning, so the poll would go unrecorded again, which is exactly the
# invisible-poll class migration 0114 exists to end.
# ---------------------------------------------------------------------------


def test_writer_outcome_vocabulary_matches_the_check_constraint() -> None:
    import re

    from legba.data.migrations import MIGRATIONS_DIR

    sql = (
        MIGRATIONS_DIR / "0114_source_poll_outcome_success.sql"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"CHECK\s*\(\s*outcome\s+IN\s*\(([^)]*)\)", sql, re.IGNORECASE,
    )
    assert match, "0114 must (re)state the outcome CHECK constraint"
    admitted = set(re.findall(r"'([a-z_]+)'", match.group(1)))

    # The exact vocabulary — 'success' added, 'empty'/'error' preserved with
    # their existing meanings (every row already on disk stays valid).
    assert admitted == {"empty", "error", "success"}
