# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Situation clustering handler — materializes the `situations` table from the
`situation_signature`-stamped findings that finding_supersession produces.

Pure-logic coverage of the grouping + situation-field derivation + the
synthetic (deps=None) summary path. The live DB upsert is verified against the
running stack."""
from __future__ import annotations

from datetime import datetime, timezone

from legba.data.analysts.deterministic import SUB_HANDLERS, OUTPUT_KIND_BY_SUB_HANDLER
from legba.data.analysts.deterministic_handlers import situation_clustering as sc
from legba.runtime.analyst_method import AnalystMethodResult


def _row(rid: str, sig: str | None, title: str, day: int) -> dict:
    r = {"id": rid, "title": title,
         "produced_at": datetime(2026, 6, day, tzinfo=timezone.utc)}
    if sig is not None:
        r["situation_signature"] = sig
    return r


def test_registered_in_dispatch_table():
    assert "situation_clustering" in SUB_HANDLERS
    assert OUTPUT_KIND_BY_SUB_HANDLER["situation_clustering"].value == "finding"


def test_topic_from_signature():
    assert sc._topic_from_signature("sig:country_g20_ar|argentina,earthquake") == "country_g20_ar"
    assert sc._topic_from_signature("sit:explicit-42") == ""
    assert sc._topic_from_signature("garbage") == ""


def test_group_by_signature_skips_unstamped():
    rows = [
        _row("a", "sig:x|e1", "A", 1),
        _row("b", "sig:x|e1", "B", 2),
        _row("c", None, "C", 3),  # no signature → excluded
    ]
    groups = sc._group_by_signature(rows)
    assert set(groups) == {"sig:x|e1"}
    assert len(groups["sig:x|e1"]) == 2


def test_situation_fields_name_is_latest_and_counts():
    rows = [
        _row("a", "sig:x|e1", "older framing", 1),
        _row("b", "sig:x|e1", "newest framing", 5),
    ]
    # Evaluate "as of" the newest member so the decay is deterministic.
    f = sc._situation_fields(
        "sig:x|e1", rows, now=datetime(2026, 6, 5, tzinfo=timezone.utc),
    )
    assert f["name"] == "newest framing"  # latest produced_at wins
    assert f["category"] == "x"
    assert f["event_count"] == 2
    # Recency-weighted intensity (exp half-life): newest member ≈ 1.0, the
    # 4-day-older one decays below 1.0 → total is < the raw count of 2.
    assert 1.0 < f["intensity_score"] < 2.0
    assert f["status"] == "active"  # freshest member is "now" → active
    assert set(f["member_finding_ids"]) == {"a", "b"}
    assert f["last_event_at"] == datetime(2026, 6, 5, tzinfo=timezone.utc)
    # Temporal frame (Phase 5a): valid_from = earliest member; an active
    # situation is an OPEN frame (valid_until NULL).
    assert f["valid_from"] == datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert f["valid_until"] is None


def test_situation_lifecycle_decays_active_dormant_closed():
    """A situation fades + transitions status as its newest member ages — the
    'events come and go' mechanic."""
    rows = [_row("a", "sig:x", "framing", 5)]  # newest member at 2026-06-05
    # ~1 day later → still active, intensity ≈ 1.0
    fa = sc._situation_fields("sig:x", rows, now=datetime(2026, 6, 6, tzinfo=timezone.utc))
    assert fa["status"] == "active"
    # ~4 days later → dormant, intensity decayed below the 1-day value
    fd = sc._situation_fields("sig:x", rows, now=datetime(2026, 6, 9, tzinfo=timezone.utc))
    assert fd["status"] == "dormant"
    assert fd["intensity_score"] < fa["intensity_score"]
    # ~10 days later → closed
    fc = sc._situation_fields("sig:x", rows, now=datetime(2026, 6, 15, tzinfo=timezone.utc))
    assert fc["status"] == "closed"
    assert fc["intensity_score"] < fd["intensity_score"]
    # Temporal frame: the situation stays OPEN (valid_until NULL) while active
    # AND dormant, and only stamps valid_until = last_event_at when it CLOSES.
    assert fa["valid_from"] == datetime(2026, 6, 5, tzinfo=timezone.utc)
    assert fa["valid_until"] is None
    assert fd["valid_until"] is None  # dormant is still an open frame
    assert fc["valid_until"] == datetime(2026, 6, 5, tzinfo=timezone.utc)


async def test_handle_synthetic_summarizes_clusters():
    inputs = [
        _row("a", "sig:x|e1", "A", 1),
        _row("b", "sig:x|e1", "B", 2),
        _row("c", "sig:y|e2", "C", 3),
        _row("d", "sig:y|e2", "D", 4),
    ]
    result = await sc.handle(inputs, {"analyst_id": "situation_clustering"}, None)
    assert isinstance(result, AnalystMethodResult)
    data = result.finding.data
    assert data["sub_handler"] == "situation_clustering"
    assert data["situations_created"] == 0  # synthetic path does not write
    assert len(data["clusters"]) == 2
    assert result.finding.tags == ["deterministic", "situation_clustering"]
    assert result.finding.kind_marker == "finding"
    # 0 new situations → idempotent refresh, suppressed from the feed
    # (force_trace_only) so it doesn't repeat the identical summary every tick.
    assert result.force_trace_only is True


async def test_no_new_situations_run_is_trace_only():
    """A no-new-situation run is suppressed from the feed; the summary finding
    is still BUILT (it flows into the trace, so nothing is lost)."""
    result = await sc.handle([], {"analyst_id": "situation_clustering"}, None)
    assert result.force_trace_only is True
    assert "0 new" in result.finding.title


# ---------------------------------------------------------------------------
# DQ P6 — snapshot/JSON name reject + steady_state tag + composition exclusion
# ---------------------------------------------------------------------------


def test_situation_name_rejects_dated_snapshot_and_json_titles():
    """A dated-snapshot title ('… — YYYY-MM-DD') or a leaked JSON-envelope
    fragment ('"title": …') must NOT name a frame — it falls back to the
    signature's topic label so a report receipt never mints a JSON/date name."""
    dated = [_row("a", "sig:world", "World situational assessment — 2026-06-30", 1)]
    assert sc._situation_name(dated, "sig:world") == "Situation: world"
    jsonleak = [_row("b", "sig:world",
                     '"title": "World situational assessment — 2026-06-30",', 2)]
    assert sc._situation_name(jsonleak, "sig:world") == "Situation: world"
    # a normal event title is kept verbatim
    ok = [_row("c", "sig:x", "Russia – Energy-weapon coercion", 3)]
    assert sc._situation_name(ok, "sig:x") == "Russia – Energy-weapon coercion"


def test_situation_fields_marks_steady_state():
    """A non-event / status-quo frame is authoritatively tagged steady_state at
    materialization (the same shared predicate the grounding read uses); a real
    event frame is not."""
    steady = [_row("a", "sig:country_g20_us",
                   "United States – No observable WMD proliferation activity", 5)]
    fs = sc._situation_fields("sig:country_g20_us", steady,
                              now=datetime(2026, 6, 5, tzinfo=timezone.utc))
    assert fs["steady_state"] is True
    real = [_row("b", "sig:country_watch_ua",
                 "Ukraine – Energy-weapon coercion escalates", 5)]
    fr = sc._situation_fields("sig:country_watch_ua", real,
                              now=datetime(2026, 6, 5, tzinfo=timezone.utc))
    assert fr["steady_state"] is False


def test_group_by_signature_excludes_composition_analysts():
    """A composition/meta producer's already-stamped row is excluded from
    re-materialization (defense-in-depth for the synthetic path)."""
    unit = {**_row("u", "sig:country_g20_us", "US read", 2),
            "analyst_id": "internal_stability"}
    comp = {**_row("c", "sig:country_g20_us", "US – Composite Assessment", 3),
            "analyst_id": "country_composition"}
    groups = sc._group_by_signature([unit, comp])
    assert set(groups) == {"sig:country_g20_us"}
    members = {str(r["id"]) for r in groups["sig:country_g20_us"]}
    assert members == {"u"}  # the country_composition row is dropped
