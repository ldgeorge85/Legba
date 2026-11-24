# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lane N-B — the desk slice's WATERMARK window and its declared coverage gap.

The US desk's ``ORDER BY fetched_at DESC LIMIT 120`` slice covered ~6-7 h of
an ~11 h cycle; 11-10 19:13-23:44Z was in no US slice (142030d0 Jim Jordan
elected, 96e7e7e7 Sanders on Schumer). These tests pin the opt-in rule
(``method.options.slice_since_watermark``): window from the last successful
run, salience + source-diversity selection on overflow, and the gap recorded.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from legba.data.option_catalog.handler_options import ANALYST_KIND_OPTIONS
from legba.data.slice_coverage import (
    COVERAGE_KEY,
    OVERLAP_MINUTES,
    SliceRows,
    WatermarkConfig,
    coverage_options,
    coverage_stamp,
    finalize_coverage,
    plan_window,
    row_score,
    select_by_salience,
    watermark_config,
    window_clauses,
)
from legba.runtime.actor_substrate_slice import _read_substrate_slice

NOW = datetime(2026, 11, 11, 6, 24, 42, tzinfo=timezone.utc)


def _desc(options, *, kind="inline_target", window="72h"):
    return SimpleNamespace(
        identity=SimpleNamespace(id="escalation", kind=kind),
        method=SimpleNamespace(options=options),
        subscription=SimpleNamespace(substrate={}, targets=SimpleNamespace(time_window=window)),
    )


# --- the knobs are declared --------------------------------------------------


def test_the_three_knobs_are_declared_for_inline_target():
    names = {s.name for s in ANALYST_KIND_OPTIONS["inline_target"]}
    assert {"slice_since_watermark", "slice_watermark_min_hours",
            "slice_watermark_first_run_hours"} <= names


@pytest.mark.parametrize(("options", "expect"), [
    (None, None),
    ({}, None),
    ({"slice_since_watermark": False}, None),
    ({"slice_since_watermark": "yes"}, None),            # wrong type → loud degrade
    ({"slice_since_watermark": True}, WatermarkConfig()),
    ({"slice_since_watermark": True, "slice_watermark_min_hours": 3,
      "slice_watermark_first_run_hours": 12}, WatermarkConfig(3, 12)),
    ({"slice_since_watermark": True, "slice_watermark_min_hours": 0}, WatermarkConfig()),
])
def test_watermark_config_resolves_through_the_catalog(options, expect):
    assert watermark_config(_desc(options)) == expect


# --- the window ---------------------------------------------------------------


def test_window_starts_at_the_last_success_less_the_overlap():
    last = NOW - timedelta(hours=11, minutes=9)
    w = plan_window(now=NOW, window_hours=72, cfg=WatermarkConfig(),
                    last_run_id="r1", last_run_at=last, last_input_ids=["a", "b"])
    assert w.start == last - timedelta(minutes=OVERLAP_MINUTES)
    assert w.basis == "last_success_run" and w.seen_ids == ["a", "b"]
    assert w.hours() == 12


def test_first_run_uses_the_first_run_floor():
    w = plan_window(now=NOW, window_hours=72, cfg=WatermarkConfig(first_run_hours=24),
                    last_run_id=None, last_run_at=None, last_input_ids=None)
    assert w.start == NOW - timedelta(hours=24) and w.basis == "first_run"


def test_a_rerun_minutes_later_still_reads_the_min_window():
    w = plan_window(now=NOW, window_hours=72, cfg=WatermarkConfig(min_hours=6),
                    last_run_id="r", last_run_at=NOW - timedelta(minutes=5),
                    last_input_ids=["a"])
    assert w.start == NOW - timedelta(hours=6)
    assert w.basis == "last_success_run+min_hours" and w.seen_ids == []


def test_a_desk_down_for_days_is_clamped_to_its_descriptor_window():
    w = plan_window(now=NOW, window_hours=72, cfg=WatermarkConfig(),
                    last_run_id="r", last_run_at=NOW - timedelta(days=9),
                    last_input_ids=[])
    assert w.start == NOW - timedelta(hours=72) and w.clamped_to_descriptor_window


def test_window_clauses_number_from_the_params_and_exclude_seen_ids():
    w = plan_window(now=NOW, window_hours=72, cfg=WatermarkConfig(),
                    last_run_id="r", last_run_at=NOW - timedelta(hours=11),
                    last_input_ids=["00000000-0000-0000-0000-000000000001"])
    params: list = []
    assert window_clauses(w, params) == [
        "fetched_at > $1::timestamptz", "NOT (id = ANY($2::uuid[]))",
    ]
    assert params[0] == w.start and params[1] == ["00000000-0000-0000-0000-000000000001"]


# --- selection ------------------------------------------------------------------


def _row(src, mag, minutes_ago, *, fallback=False, rid=None):
    payload = {"title": "t"}
    if fallback:
        payload["geo_fallback"] = {"basis": "publisher_origin", "iso2": ["US"]}
    return {
        "id": rid or str(uuid4()), "source_id": src,
        "salience": None if mag is None else json.dumps({"magnitude": mag}),
        "payload": payload, "fetched_at": NOW - timedelta(minutes=minutes_ago),
    }


def test_under_the_cap_every_row_is_kept():
    rows = [_row("a", 0.1, i) for i in range(5)]
    assert select_by_salience(rows, limit=10) == rows


def test_salience_beats_recency_and_the_old_rows_get_in():
    old_salient = _row("ht", 0.35, 600, rid="142030d0")
    fresh_noise = [_row(f"s{i}", 0.05, i) for i in range(10)]
    out = select_by_salience(fresh_noise + [old_salient], limit=5)
    assert "142030d0" in {r["id"] for r in out}


def test_source_diversity_decays_a_firehose():
    firehose = [_row("nws", 0.30, i) for i in range(20)]
    others = [_row(f"n{i}", 0.20, i) for i in range(5)]
    out = select_by_salience(firehose + others, limit=10)
    assert sum(r["source_id"] == "nws" for r in out) < 10
    assert all(any(r["source_id"] == f"n{i}" for r in out) for i in range(5))


def test_declared_per_source_cap_is_strict():
    rows = [_row("a", 0.9, i) for i in range(10)] + [_row("b", 0.1, 1)]
    out = select_by_salience(rows, limit=5, per_source_cap=2)
    assert sum(r["source_id"] == "a" for r in out) == 2 and len(out) == 3


def test_fallback_geo_rows_weigh_half_and_unscored_rows_last():
    assert row_score(_row("a", 0.4, 1, fallback=True)) == pytest.approx(0.2)
    assert row_score(_row("a", None, 1)) == 0.0
    out = select_by_salience([_row("a", None, 1), _row("b", 0.01, 500)], limit=1)
    assert out[0]["source_id"] == "b"


def test_selection_returns_newest_first():
    rows = [_row(f"s{i}", 0.1 * i, 100 - i) for i in range(6)]
    out = select_by_salience(rows, limit=3)
    ts = [r["fetched_at"] for r in out]
    assert ts == sorted(ts, reverse=True)


# --- the coverage stamp -------------------------------------------------------


def test_coverage_stamp_declares_the_gap_and_finalize_counts_the_read():
    w = plan_window(now=NOW, window_hours=72, cfg=WatermarkConfig(),
                    last_run_id="r", last_run_at=NOW - timedelta(hours=11),
                    last_input_ids=[])
    rows = [_row(f"s{i}", 0.01 * i, i) for i in range(30)]
    sel = select_by_salience(rows, limit=10)
    cov = coverage_stamp(w, in_window=30, selected=sel, candidates=rows, fetch_capped=False)
    assert cov["in_window"] == 30 and cov["selected"] == 10 and cov["not_selected"] == 20
    assert cov["selection"] == "salience_diversity" and len(cov["unselected_top"]) == 5
    fin = finalize_coverage(cov, read=8)
    assert fin["read"] == 8 and fin["not_read"] == 22
    assert finalize_coverage(None, read=3) is None


def test_coverage_options_are_empty_for_a_plain_list():
    assert coverage_options([1, 2]) == {}
    rows = SliceRows([1])
    rows.slice_coverage = {"window_hours": 12, "in_window": 3}
    assert coverage_options(rows) == {
        COVERAGE_KEY: {"window_hours": 12, "in_window": 3}, "slice_window_hours": 12,
    }


# --- the reader -----------------------------------------------------------------


class _Conn:
    """Target body, a watermark trace, and a window of signals."""

    def __init__(self, rows, *, trace=None):
        self.rows = rows
        self.trace = trace
        self.sqls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *a):
        if "analyst_traces" in sql:
            return self.trace
        return {"body": {"scope": {"geo": ["US"]}}}

    async def fetch(self, sql, *params):
        if "FROM signals" in sql:
            self.sqls.append((sql, params))
            return list(self.rows)
        return []

    async def fetchval(self, sql, *params):                 # pragma: no cover
        return len(self.rows)


async def test_reader_flag_off_is_the_recency_statement(monkeypatch):
    monkeypatch.setenv("LEGBA_SLICE_GRAPH_STRUCTURE_CAP", "0")
    conn = _Conn([])
    await _read_substrate_slice(conn, descriptor=_desc({}), target_filter="country_g20_us")
    sql, params = conn.sqls[0]
    assert "fetched_at > NOW() - INTERVAL '72 hours'" in sql
    assert "analyst_traces" not in sql and params[0] == ["US"]


async def test_reader_watermark_window_selects_and_stamps(monkeypatch):
    monkeypatch.setenv("LEGBA_SLICE_GRAPH_STRUCTURE_CAP", "0")
    monkeypatch.setenv("LEGBA_SLICE_ROW_CAP", "5")
    monkeypatch.delenv("LEGBA_SLICE_GEO_V2", raising=False)
    rows = [_row(f"s{i}", 0.05, i) for i in range(12)] + [_row("ht", 0.35, 600, rid="142030d0")]
    trace = {"run_id": uuid4(), "run_started_at": datetime.now(timezone.utc) - timedelta(hours=11),
             "input_row_refs": []}
    conn = _Conn(rows, trace=trace)
    out = await _read_substrate_slice(
        conn, descriptor=_desc({"slice_since_watermark": True}), target_filter="country_g20_us",
    )
    sql, params = conn.sqls[0]
    assert "fetched_at > $1::timestamptz" in sql and "INTERVAL" not in sql
    assert len(out) == 5 and "142030d0" in {r["id"] for r in out}
    cov = out.slice_coverage
    assert cov["in_window"] == 13 and cov["selected"] == 5 and cov["not_selected"] == 8
    assert cov["basis"] == "last_success_run"
    assert coverage_options(out)["slice_window_hours"] == 12
