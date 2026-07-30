# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""D2 (second pass) — entity_gc source-auto-pause leg fix.

The original ``_pause_failing_sources`` queried a NON-EXISTENT ``sources`` table
(``SELECT ... FROM sources WHERE status='active' AND consecutive_failures > $1``),
so the leg logged ``entity_gc.source_pause_failed err=relation "sources" does not
exist`` on EVERY run and never paused anything. There is no ``sources`` relation
and no ``consecutive_failures`` column in the live schema: sources are descriptors
in ``source_descriptors`` and per-poll failure provenance lives in
``source_poll_outcomes`` (``outcome`` in ``'empty'`` / ``'error'``).

These pure, no-DB unit tests assert:

  * the corrected leg reads ``source_poll_outcomes`` joined to ACTIVE
    ``source_descriptors`` heads and NEVER references a ``sources`` table or a
    ``consecutive_failures`` column;
  * the pause UPDATE targets ``source_descriptors`` head rows, flips ``state`` →
    ``'paused'``, and records the reason in ``body`` jsonb;
  * the pure ``_consecutive_error_streaks`` helper counts only the contiguous
    LEADING run of ``outcome='error'`` rows (an ``'empty'`` breaks it — and,
    since migration 0114, so does a ``'success'``), keyed off the same
    newest-first per-source ordering the SQL guarantees;
  * a source that is CURRENTLY PRODUCING is never paused, whatever its poll
    history says (the ``last_ingest`` guard — see below);
  * a failing leg degrades to zero without aborting the run.

The two producing-source protections exist for different reasons and are tested
separately. ``'success'`` rows are the root-cause fix: a productive poll used to
write NO row at all, so a repaired source's error run could not be broken by its
own recovery. The ``last_ingest`` guard is defence in depth over the rows ALREADY
on disk, which carry that defect permanently — gdelt.files was auto-paused on
2026-07-27 two minutes after its fix deployed, off a 25-error streak that was
entirely historical, and still carries ~102 leading error rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from legba.data.analysts.deterministic_handlers import entity_gc


# ---------------------------------------------------------------------------
# Pure helper: _consecutive_error_streaks (no DB)
# ---------------------------------------------------------------------------


def _row(source_id: str, outcome: str, *, t: datetime | None = None) -> dict:
    return {
        "source_id": source_id,
        "outcome": outcome,
        "occurred_at": t or datetime.now(timezone.utc),
    }


def test_streak_leading_error_run_at_threshold():
    threshold = entity_gc._SOURCE_FAILURE_THRESHOLD
    rows = [_row("source.rss.bad", "error") for _ in range(threshold)]
    out = entity_gc._consecutive_error_streaks(rows, threshold=threshold)
    assert out == [("source.rss.bad", threshold)]


def test_streak_below_threshold_excluded():
    threshold = entity_gc._SOURCE_FAILURE_THRESHOLD
    rows = [_row("source.rss.flaky", "error") for _ in range(threshold - 1)]
    assert entity_gc._consecutive_error_streaks(rows, threshold=threshold) == []


def test_streak_breaks_on_first_non_error():
    # newest-first: 2 errors then an 'empty' — the leading error run is only 2,
    # so with a threshold of 3 the source does NOT qualify.
    rows = [
        _row("source.rss.x", "error"),
        _row("source.rss.x", "error"),
        _row("source.rss.x", "empty"),
        _row("source.rss.x", "error"),
        _row("source.rss.x", "error"),
    ]
    assert entity_gc._consecutive_error_streaks(rows, threshold=3) == []
    assert entity_gc._consecutive_error_streaks(rows, threshold=2) == [
        ("source.rss.x", 2)
    ]


def test_streak_per_source_independent_and_ordered():
    rows = [
        _row("source.a", "error"),
        _row("source.a", "error"),
        _row("source.a", "error"),
        _row("source.b", "error"),
        _row("source.b", "empty"),
        _row("source.c", "error"),
        _row("source.c", "error"),
    ]
    assert entity_gc._consecutive_error_streaks(rows, threshold=2) == [
        ("source.a", 3),
        ("source.c", 2),
    ]


def test_streak_threshold_non_positive_is_noop():
    rows = [_row("source.a", "error") for _ in range(50)]
    assert entity_gc._consecutive_error_streaks(rows, threshold=0) == []
    assert entity_gc._consecutive_error_streaks(rows, threshold=-1) == []


def test_streak_skips_blank_source_id():
    rows = [_row("", "error"), _row("", "error")]
    assert entity_gc._consecutive_error_streaks(rows, threshold=1) == []


def test_streak_window_exceeds_threshold():
    # The LIMIT window must not truncate a qualifying leading error-run.
    assert entity_gc._SOURCE_STREAK_WINDOW > entity_gc._SOURCE_FAILURE_THRESHOLD


# ---------------------------------------------------------------------------
# T-4(a): last_signal bound — a STALE error (at/before the source's newest
# produced signal) must NOT count. The ukrinform/nasa fossil-latch class:
# the source produced signals AFTER those errors, but productive polls write no
# outcome row, so old errors sit at the top of the outcome window forever.
# ---------------------------------------------------------------------------


def _err_at(source_id: str, t: datetime, *, last_signal=None) -> dict:
    row = {"source_id": source_id, "outcome": "error", "occurred_at": t}
    if last_signal is not None:
        row["last_signal"] = last_signal
    return row


def test_streak_stale_errors_before_last_signal_do_not_count():
    # The source produced a signal AFTER its whole error run → all errors are
    # stale evidence, so the streak is 0 and it is NOT re-latched (the fossil fix).
    base = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)
    last_signal = base + timedelta(hours=1)  # newer than every error below
    rows = [
        _err_at("source.ukrinform.all", base - timedelta(hours=i),
                last_signal=last_signal)
        for i in range(entity_gc._SOURCE_FAILURE_THRESHOLD + 5)
    ]
    assert entity_gc._consecutive_error_streaks(
        rows, threshold=entity_gc._SOURCE_FAILURE_THRESHOLD
    ) == []


def test_streak_errors_after_last_signal_still_count():
    # Errors NEWER than the last produced signal are the live run → still paused.
    base = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)
    last_signal = base - timedelta(days=30)  # production long predates the errors
    n = entity_gc._SOURCE_FAILURE_THRESHOLD
    rows = [
        _err_at("source.dead.feed", base - timedelta(hours=i), last_signal=last_signal)
        for i in range(n)
    ]
    out = entity_gc._consecutive_error_streaks(rows, threshold=n)
    assert out == [("source.dead.feed", n)]


def test_streak_bound_stops_at_first_stale_error():
    # The bound cuts the run at the first error at/before last_signal: 3 fresh
    # errors, then last_signal, then more (stale) errors → streak is exactly 3.
    base = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)
    last_signal = base - timedelta(hours=3, minutes=30)
    rows = [
        _err_at("source.x", base - timedelta(hours=i), last_signal=last_signal)
        for i in range(entity_gc._SOURCE_FAILURE_THRESHOLD + 5)
    ]
    # errors at base, -1h, -2h, -3h postdate last_signal (-3h30m) → 4 count.
    assert entity_gc._consecutive_error_streaks(rows, threshold=4) == [("source.x", 4)]
    assert entity_gc._consecutive_error_streaks(rows, threshold=5) == []


def test_streak_no_last_signal_key_unchanged():
    # Back-compat: rows without last_signal behave exactly as before (a source
    # that keeps erroring and never produced is exactly what auto-pause is for).
    n = entity_gc._SOURCE_FAILURE_THRESHOLD
    rows = [_row("source.never.produced", "error") for _ in range(n)]
    assert entity_gc._consecutive_error_streaks(rows, threshold=n) == [
        ("source.never.produced", n)
    ]


async def test_pause_query_carries_last_signal_bound():
    # The corrected SQL must surface the per-source newest signal so the streak
    # can be bounded (mirrors liveness_watchdog._fetch_source_empty_streak_rows).
    conn = _FakeConn(fetch_rows=[])
    await entity_gc._pause_failing_sources(_FakePool(conn))
    sql = conn.fetched[0]
    assert "last_signal" in sql
    assert "max(s.fetched_at)" in sql
    assert "FROM signals s" in sql


# ---------------------------------------------------------------------------
# Migration 0114: a PRODUCTIVE poll now writes outcome='success', which BREAKS
# the leading error run. This is the root-cause fix — before it, a productive
# poll wrote no row at all, so a repaired source's error run could never be
# broken by its own recovery and the latch re-fired off dead evidence.
# ---------------------------------------------------------------------------


def test_streak_broken_by_a_success_row():
    # Newest-first: 2 errors, then the poll that RECOVERED, then the old run.
    # Only the 2 post-recovery errors count.
    n = entity_gc._SOURCE_FAILURE_THRESHOLD
    rows = (
        [_row("source.gdelt.files", "error") for _ in range(2)]
        + [_row("source.gdelt.files", "success")]
        + [_row("source.gdelt.files", "error") for _ in range(n + 10)]
    )
    assert entity_gc._consecutive_error_streaks(rows, threshold=n) == []
    assert entity_gc._consecutive_error_streaks(rows, threshold=2) == [
        ("source.gdelt.files", 2)
    ]


def test_streak_leading_success_row_means_no_pause_at_all():
    # The repaired source: its very newest poll succeeded, so however long the
    # historical error run behind it is, the streak is 0.
    n = entity_gc._SOURCE_FAILURE_THRESHOLD
    rows = [_row("source.gdelt.files", "success")] + [
        _row("source.gdelt.files", "error") for _ in range(102)
    ]
    assert entity_gc._consecutive_error_streaks(rows, threshold=n) == []


# ---------------------------------------------------------------------------
# The currently-producing guard: a source whose signals are LANDING right now
# is never paused, whatever its poll history says. Defence in depth — it does
# not depend on the outcome ledger being correct, which matters because the
# pre-0114 rows on disk are structurally wrong (gdelt.files carries ~102
# leading 'error' rows from an outage that was repaired hours ago).
# ---------------------------------------------------------------------------


def _err_ingested(source_id: str, t: datetime, *, last_ingest, last_signal=None):
    row = {"source_id": source_id, "outcome": "error", "occurred_at": t,
           "last_ingest": last_ingest}
    if last_signal is not None:
        row["last_signal"] = last_signal
    return row


def test_recent_signals_guard_blocks_pause_despite_a_long_error_run():
    # The gdelt.files 2026-07-27 shape EXACTLY: a long leading run of error
    # rows (from the outage), and signals landing in the substrate right now.
    now = datetime(2026, 7, 27, 18, 17, tzinfo=timezone.utc)
    outage = now - timedelta(hours=6)
    rows = [
        _err_ingested(
            "source.gdelt.files", outage - timedelta(minutes=5 * i),
            last_ingest=now - timedelta(minutes=2),   # ingesting NOW
            # deliberately NOT bounded by last_signal — prove the guard alone
            # is sufficient, not the pre-existing stale-error bound.
            last_signal=outage - timedelta(days=4),
        )
        for i in range(102)
    ]
    out = entity_gc._consecutive_error_streaks(
        rows, threshold=entity_gc._SOURCE_FAILURE_THRESHOLD, now=now,
    )
    assert out == []


def test_recent_signals_guard_expires_so_a_truly_dead_source_still_pauses():
    # Same rows, but the last ingest is older than the guard window: the source
    # stopped producing, so the latch must still do its job.
    now = datetime(2026, 7, 27, 18, 17, tzinfo=timezone.utc)
    stale_ingest = now - timedelta(
        hours=entity_gc._SOURCE_RECENT_SIGNAL_HOURS + 1
    )
    n = entity_gc._SOURCE_FAILURE_THRESHOLD
    rows = [
        _err_ingested(
            "source.dead.feed", now - timedelta(minutes=5 * i),
            last_ingest=stale_ingest, last_signal=stale_ingest,
        )
        for i in range(n)
    ]
    out = entity_gc._consecutive_error_streaks(rows, threshold=n, now=now)
    assert out == [("source.dead.feed", n)]


def test_recent_signals_guard_absent_column_is_back_compatible():
    # Rows without last_ingest (older callers) behave exactly as before.
    n = entity_gc._SOURCE_FAILURE_THRESHOLD
    rows = [_row("source.never.produced", "error") for _ in range(n)]
    assert entity_gc._consecutive_error_streaks(
        rows, threshold=n, now=datetime.now(timezone.utc),
    ) == [("source.never.produced", n)]


def test_recent_signals_guard_window_is_the_firing_floor():
    # One platform-wide definition of "currently producing" — the same 48h
    # floor the system-status route uses to call a source 'firing'.
    assert entity_gc._SOURCE_RECENT_SIGNAL_HOURS == 48


async def test_pause_query_carries_the_substrate_landing_time():
    # The guard must key on signals.created_at (when the row LANDED), not the
    # handler-supplied fetched_at a bulk/archive loader can back-date.
    conn = _FakeConn(fetch_rows=[])
    await entity_gc._pause_failing_sources(_FakePool(conn))
    sql = conn.fetched[0]
    assert "last_ingest" in sql
    assert "max(s.created_at)" in sql


# ---------------------------------------------------------------------------
# Fake asyncpg pool/conn — capture SQL + args, drive the corrected leg.
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, fetch_rows: list[dict] | None = None):
        self._fetch_rows = fetch_rows or []
        self.fetched: list[str] = []
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.fetched.append(sql)
        return list(self._fetch_rows)

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


class _FakeAcquireCtx:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self._conn)


class _FakeDeps:
    def __init__(self, pool):
        self.pg_pool = pool


# ---------------------------------------------------------------------------
# _pause_failing_sources — corrected query shape + pause UPDATE
# ---------------------------------------------------------------------------


def _error_rows(source_id: str, n: int) -> list[dict]:
    base = datetime.now(timezone.utc)
    return [
        {
            "source_id": source_id,
            "outcome": "error",
            "occurred_at": base - timedelta(minutes=i),
        }
        for i in range(n)
    ]


async def test_pause_reads_poll_outcomes_not_sources_table():
    conn = _FakeConn(fetch_rows=[])
    await entity_gc._pause_failing_sources(_FakePool(conn))
    assert len(conn.fetched) == 1
    sql = conn.fetched[0]
    # Corrected: reads the real tables.
    assert "source_poll_outcomes" in sql
    assert "source_descriptors" in sql
    assert "d.state = 'active'" in sql
    assert "is_head" in sql
    # The D2 regression markers are GONE — never query the phantom table/column.
    assert "FROM sources" not in sql
    assert " sources " not in sql
    assert "consecutive_failures" not in sql


async def test_pause_flips_descriptor_state_and_records_reason():
    threshold = entity_gc._SOURCE_FAILURE_THRESHOLD
    rows = _error_rows("source.rss.dead", threshold + 2)
    conn = _FakeConn(fetch_rows=rows)
    paused = await entity_gc._pause_failing_sources(_FakePool(conn))
    assert paused == 1
    assert len(conn.executed) == 1
    sql, args = conn.executed[0]
    # Pauses by mutating the source_descriptors head row's lifecycle state.
    assert "UPDATE source_descriptors" in sql
    assert "state = 'paused'" in sql
    assert "is_head" in sql
    # Non-destructive metadata stamp into body jsonb.
    assert "auto_paused_at" in sql
    assert "auto_paused_reason" in sql
    assert "DELETE" not in sql.upper()
    # descriptor_id, paused_at iso, reason are bound as parameters.
    assert args[0] == "source.rss.dead"
    assert "consecutive failed polls" in args[2]


async def test_pause_below_threshold_does_nothing():
    threshold = entity_gc._SOURCE_FAILURE_THRESHOLD
    conn = _FakeConn(fetch_rows=_error_rows("source.rss.flaky", threshold - 1))
    paused = await entity_gc._pause_failing_sources(_FakePool(conn))
    assert paused == 0
    assert conn.executed == []


async def test_pause_leg_does_not_pause_a_currently_producing_source():
    # Whole-leg proof of the guard: the same over-threshold error run that
    # pauses above is inert once the rows say the source is still ingesting.
    threshold = entity_gc._SOURCE_FAILURE_THRESHOLD
    now = datetime.now(timezone.utc)
    rows = _error_rows("source.gdelt.files", threshold + 82)
    for r in rows:
        r["last_ingest"] = now - timedelta(minutes=2)
    conn = _FakeConn(fetch_rows=rows)
    assert await entity_gc._pause_failing_sources(_FakePool(conn)) == 0
    assert conn.executed == []


async def test_pause_leg_stops_at_a_success_row():
    # Whole-leg proof of the ledger fix: one recovery row at the head of the
    # window ends the run, with no recency guard involved at all.
    threshold = entity_gc._SOURCE_FAILURE_THRESHOLD
    base = datetime.now(timezone.utc)
    rows = [{
        "source_id": "source.gdelt.files",
        "outcome": "success",
        "occurred_at": base,
    }] + _error_rows("source.gdelt.files", threshold + 82)
    conn = _FakeConn(fetch_rows=rows)
    assert await entity_gc._pause_failing_sources(_FakePool(conn)) == 0
    assert conn.executed == []


async def test_pause_empty_run_not_paused():
    # All-'empty' outcomes (silent but HTTP-200) are the watchdog's job, NOT an
    # auto-pause-on-error trigger — this leg keys only on hard errors.
    threshold = entity_gc._SOURCE_FAILURE_THRESHOLD
    rows = [
        {
            "source_id": "source.rss.quiet",
            "outcome": "empty",
            "occurred_at": datetime.now(timezone.utc) - timedelta(minutes=i),
        }
        for i in range(threshold + 5)
    ]
    conn = _FakeConn(fetch_rows=rows)
    assert await entity_gc._pause_failing_sources(_FakePool(conn)) == 0
    assert conn.executed == []


# ---------------------------------------------------------------------------
# handle() — the source-pause leg is the ONLY one enabled (others off → pure)
# ---------------------------------------------------------------------------


async def test_handle_source_pause_leg_counts_and_tags():
    threshold = entity_gc._SOURCE_FAILURE_THRESHOLD
    conn = _FakeConn(fetch_rows=_error_rows("source.rss.dead", threshold + 1))
    deps = _FakeDeps(_FakePool(conn))
    result = await entity_gc.handle(
        [],
        {
            "sub_handler": "entity_gc",
            "run_dormant": False,
            "run_duplicates": False,
            "run_orphans": False,
            "run_source_pause": True,
            "run_orphan_proposed_edges": False,
        },
        deps,
    )
    data = result.finding.data
    assert data["sources_paused"] == 1
    assert "gc_actions_taken" in result.finding.tags
    # other legs untouched
    assert data["dormant_entities"] == 0
    assert data["orphan_proposed_edges"] == 0


async def test_handle_source_pause_failure_is_swallowed():
    class _BoomConn(_FakeConn):
        async def fetch(self, sql, *args):
            raise RuntimeError("boom")

    deps = _FakeDeps(_FakePool(_BoomConn()))
    result = await entity_gc.handle(
        [],
        {
            "sub_handler": "entity_gc",
            "run_dormant": False,
            "run_duplicates": False,
            "run_orphans": False,
            "run_source_pause": True,
            "run_orphan_proposed_edges": False,
        },
        deps,
    )
    # Degrades to zero, does not abort the run.
    assert result.finding.data["sources_paused"] == 0
