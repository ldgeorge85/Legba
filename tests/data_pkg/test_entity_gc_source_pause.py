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
    LEADING run of ``outcome='error'`` rows (an ``'empty'`` breaks it), keyed off
    the same newest-first per-source ordering the SQL guarantees;
  * a failing leg degrades to zero without aborting the run.
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
