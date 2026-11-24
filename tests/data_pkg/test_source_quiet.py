# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""U-9 — ``source_quiet.quiet_class``: why a quiet source is quiet, on the
source-firing route (the console's Watch surface tells "looked, nothing
there" from "could not look" by it).

The truth table is the liveness watchdog's (``_source_stall_is_honest_quiet``
/ ``_classify_streak``); the agreement test below pins the two together.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from legba.data.registry import source_quiet
from legba.data.registry.source_quiet import QUIET_CLASSES, quiet_class
from legba.data.registry.v3_api import _SOURCE_FIRING_SQL, SourceFiringRow

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _q(status: str, *, seen=NOW - timedelta(days=3), outcome="empty", newest=None):
    return quiet_class(status=status, last_seen_at=seen, last_poll_outcome=outcome, newest_entry_at=newest)


def test_a_firing_or_paused_source_is_not_quiet() -> None:
    assert _q("firing") is None
    assert _q("paused", outcome="error") is None


def test_errors_mean_polling_fails() -> None:
    assert _q("error", outcome="error") == "polling_fails"
    # the route says silent but the latest poll erred: still could-not-look
    assert _q("silent", outcome="error") == "polling_fails"
    # an erroring source that never produced is a polling fault first
    assert _q("error", seen=None, outcome="error") == "polling_fails"


def test_a_gated_poll_is_filtered_to_nothing() -> None:
    assert _q("silent", outcome="gated") == "filtered_to_nothing"


def test_never_ingested_is_never_produced() -> None:
    assert _q("silent", seen=None, outcome="empty", newest=NOW) == "never_produced"
    assert _q("silent", seen=None, outcome=None) == "never_produced"


def test_empty_poll_splits_on_the_upstream_stamp() -> None:
    last = NOW - timedelta(days=3)
    assert _q("silent", seen=last, newest=last - timedelta(hours=1)) == "published_nothing"
    assert _q("silent", seen=last, newest=last) == "published_nothing"
    assert _q("silent", seen=last, newest=last + timedelta(hours=1)) == "published_not_kept"
    # no stamp recorded: undecided, never guessed
    assert _q("silent", seen=last, newest=None) == "unrecorded"


def test_naive_times_are_read_as_utc() -> None:
    last = datetime(2026, 11, 10, 12, 0)
    assert _q("silent", seen=last, newest=datetime(2026, 11, 10, 11, 0, tzinfo=timezone.utc)) == "published_nothing"


def test_a_success_with_nothing_landed_is_unrecorded() -> None:
    assert _q("silent", outcome="success") == "unrecorded"


def test_every_returned_class_is_in_the_closed_list() -> None:
    seen = NOW - timedelta(days=2)
    cases = [
        ("error", seen, "error", None), ("silent", seen, "gated", None), ("silent", None, "empty", NOW),
        ("silent", seen, "empty", seen), ("silent", seen, "empty", NOW), ("silent", seen, "empty", None),
    ]
    got = {quiet_class(status=s, last_seen_at=a, last_poll_outcome=o, newest_entry_at=n) for s, a, o, n in cases}
    assert got == set(QUIET_CLASSES)


@pytest.mark.parametrize(
    "outcome,newest_delta_h,honest",
    [("empty", -1, True), ("empty", 0, True), ("empty", 1, False), ("gated", None, True), ("error", None, False)],
)
def test_agrees_with_the_watchdogs_honest_quiet_gate(outcome, newest_delta_h, honest) -> None:
    """Where the watchdog calls a stale source honest-quiet, the route's class
    is a looked-and-saw-nothing class; where it keeps the alert, it is not."""
    from legba.runtime.liveness_watchdog import _source_stall_is_honest_quiet

    last = NOW - timedelta(days=3)
    newest = None if newest_delta_h is None else last + timedelta(hours=newest_delta_h)
    row = {"last_poll_outcome": outcome, "last_poll_newest_entry_ts": newest, "last_signal": last}
    assert _source_stall_is_honest_quiet(row) is honest
    cls = _q("silent", seen=last, outcome=outcome, newest=newest)
    assert (cls in ("published_nothing", "filtered_to_nothing")) is honest


def test_the_row_carries_the_class_additively() -> None:
    legacy = SourceFiringRow(source_id="source.old", status="firing")
    assert legacy.quiet_class is None and legacy.last_poll_newest_entry_at is None
    row = SourceFiringRow(source_id="source.q", status="silent", quiet_class="published_nothing")
    assert row.model_dump()["quiet_class"] == "published_nothing"


def test_the_firing_read_takes_the_stamp_off_the_latest_poll_row_last() -> None:
    sql = " ".join(_SOURCE_FIRING_SQL.split())
    assert "o.newest_entry_ts AS last_poll_newest_entry_at" in sql
    # appended LAST, so the pinned ten-column comparison with the old form holds
    assert sql.split(" FROM heads h")[0].rstrip().endswith("lp.last_poll_newest_entry_at")


def test_the_leaf_is_stdlib_only() -> None:
    """Registry-slim: the module imports nothing outside the stdlib."""
    code = (
        "import sys; sys.modules['fastapi'] = None; sys.modules['pydantic'] = None; "
        "sys.modules['asyncpg'] = None; "
        "import importlib.util, pathlib; "
        f"spec = importlib.util.spec_from_file_location('sq', {source_quiet.__file__!r}); "
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print(m.QUIET_CLASSES[0])"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "polling_fails"


@pytest.mark.parametrize(
    "written,newest_delta_h,honest",
    [(0, -1, True), (0, 0, True), (0, 1, False), (1, -1, False), (None, -1, False)],
)
def test_a_zero_write_success_is_honest_quiet_to_the_watchdog(written, newest_delta_h, honest) -> None:
    """Roll 86 (source.usgs.earthquakes_m45 paged every 6.5 h): the geojson
    valve's forced refetch re-yields the unchanged feature, the sink collapses
    it, and the poll records 'success' with 0 written. Same upstream-stamp
    rule as an empty poll; a success that wrote, or an unrecorded count,
    keeps the alert."""
    from legba.runtime.liveness_watchdog import _source_stall_is_honest_quiet

    last = NOW - timedelta(days=1)
    row = {"last_poll_outcome": "success", "last_poll_signals_written": written,
           "last_poll_newest_entry_ts": last + timedelta(hours=newest_delta_h), "last_signal": last}
    assert _source_stall_is_honest_quiet(row) is honest
