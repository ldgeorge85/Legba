# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the V3/P3 temporal reader leaf.

:mod:`legba.runtime.substrate_temporal` is the ONE place the as-of contract
is written down as code (spec §3.2): the canonical validity predicate, the
decision-time predicate for ``analyst_outputs``, the ISO-8601
parse-or-refuse, and the ``unbounded_start`` honest counter. These tests are
pure — no database — because every behavior they pin is pure.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from legba.runtime import substrate_temporal as t


# ---------------------------------------------------------------------------
# The deploy marker
# ---------------------------------------------------------------------------


def test_version_marker() -> None:
    assert t.TEMPORAL_READER_VERSION == "2026-09/p3"


# ---------------------------------------------------------------------------
# parse_instant — ISO-8601 in, aware UTC datetime out, loud refusal otherwise
# ---------------------------------------------------------------------------


def test_parse_date_only_is_utc_midnight() -> None:
    dt = t.parse_instant("2026-08-15", name="as_of")
    assert dt == datetime(2026, 8, 15, tzinfo=timezone.utc)


def test_parse_zulu_and_offset() -> None:
    z = t.parse_instant("2026-08-15T10:30:00Z", name="as_of")
    off = t.parse_instant("2026-08-15T12:30:00+02:00", name="as_of")
    assert z == off  # the same instant — both normalize to UTC


def test_parse_naive_datetime_reads_as_utc() -> None:
    dt = t.parse_instant(datetime(2026, 8, 15, 10, 0), name="as_of")
    assert dt.tzinfo is not None
    assert dt == datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "bad",
    ["last tuesday", "2026-13-40", "", "   ", "2026", 0, None, object()],
)
def test_parse_malformed_refuses_loud(bad) -> None:
    """A malformed temporal parameter is a refusal, NEVER a silent now()."""
    with pytest.raises(t.TemporalParameterError):
        t.parse_instant(bad, name="as_of")


def test_parse_error_names_the_parameter() -> None:
    with pytest.raises(t.TemporalParameterError) as ei:
        t.parse_instant("not a date", name="believed_as_of")
    assert "believed_as_of" in str(ei.value)


# ---------------------------------------------------------------------------
# temporal_predicate — open-row vs canonical as-of
# ---------------------------------------------------------------------------


def test_open_predicate_is_byte_identical_to_the_inline_form() -> None:
    """None keeps today's behavior: the exact pair the readers carried."""
    assert (
        t.temporal_predicate("e", None)
        == "e.valid_until IS NULL AND e.superseded_by IS NULL"
    )
    assert (
        t.temporal_predicate("", None)
        == "valid_until IS NULL AND superseded_by IS NULL"
    )


def test_as_of_predicate_is_the_canonical_shape() -> None:
    """COALESCE('-infinity') over-includes a NULL start; superseded_by drops
    out entirely — valid_until alone carries the close (spec §3.2)."""
    pred = t.temporal_predicate("e", 5)
    assert "COALESCE(e.valid_from, '-infinity'::timestamptz) <= $5" in pred
    assert "(e.valid_until IS NULL OR e.valid_until > $5)" in pred
    assert "superseded_by" not in pred


def test_as_of_predicate_binds_the_given_param_index() -> None:
    assert "$9" in t.temporal_predicate("f", 9)


# ---------------------------------------------------------------------------
# decision_predicate — the OTHER clock (analyst_outputs produced/superseded)
# ---------------------------------------------------------------------------


def test_decision_predicate_uses_produced_and_superseded_at() -> None:
    pred = t.decision_predicate("o", 2)
    assert "o.produced_at <= $2" in pred
    assert "(o.superseded_at IS NULL OR o.superseded_at > $2)" in pred
    # Not interchangeable with the validity predicate — no valid_* columns.
    assert "valid_from" not in pred and "valid_until" not in pred


# ---------------------------------------------------------------------------
# anchor_window_clause — the get_timeline [since, until) window
# ---------------------------------------------------------------------------


def test_window_clause_half_open_bounds() -> None:
    clause = t.anchor_window_clause(
        "COALESCE(fetched_at, created_at)", since_param=3, until_param=4)
    assert "COALESCE(fetched_at, created_at) >= $3" in clause
    assert "COALESCE(fetched_at, created_at) < $4" in clause


def test_window_clause_single_bound_and_none() -> None:
    only_since = t.anchor_window_clause(
        "anchor", since_param=2, until_param=None)
    assert ">= $2" in only_since and "<" not in only_since
    assert t.anchor_window_clause(
        "anchor", since_param=None, until_param=None) == ""


# ---------------------------------------------------------------------------
# unbounded_start — the honest NULL-start counter
# ---------------------------------------------------------------------------


def test_unbounded_start_counts_null_valid_from() -> None:
    rows = [
        {"valid_from": None},
        {"valid_from": "2026-01-01T00:00:00+00:00"},
        {"valid_from": None},
        {"valid_from": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        {},  # missing key counts — there is no recorded start
    ]
    assert t.unbounded_start(rows) == 3


def test_unbounded_start_empty() -> None:
    assert t.unbounded_start([]) == 0
