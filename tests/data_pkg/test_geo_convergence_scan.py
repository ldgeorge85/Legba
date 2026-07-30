# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A7 — the ``geo_convergence_scan`` module (geographic convergence binning).

2026-07-29 alert-plane consolidation: this module's alert EMISSION (the
former standalone analyst's ``handle()``) folded into ``alert_trigger_scan``
as its trigger class 6 (``trigger_class='geo_convergence'``). The DB-level
seed → fire-once → never-refire lifecycle tests that used to live here now
live in ``test_alert_trigger_scan.py`` (Trigger 6 section), exercising the
SAME scenarios through ``alert_trigger_scan.handle()`` with payload-content
assertions proving the fold preserved firing conditions + payload content
byte-for-byte.

Pure tests (no DB, unaffected by the fold): cell/country bin keys, the
source-family fold, the score, the two-tier binning (country-centroid
geocodes NEVER cell-binned), and the formation/dissolution edge core — the
functions ``scan_geo_convergence`` still calls. Registry tests: the module's
``handle()`` is now a deprecation stub (dispatchable, but a no-op).
"""
from __future__ import annotations

import pytest

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
)
from legba.data.analysts.deterministic_handlers import (
    geo_convergence_scan as gcs,
)
from legba.data.provenance.kinds import (
    STRUCTURAL_VERIFY_EXEMPT_ANALYSTS,
    OutputKind,
)
from legba.runtime.analyst_method import AnalystMethodResult


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_registered_as_finding_sub_handler_and_structural_exempt():
    """Unchanged by the fold: the sub_handler name stays dispatchable (the
    deprecation stub's static registration is untouched), so the
    STRUCTURAL_VERIFY_EXEMPT drift guard's FINDING-set equality still holds
    even though the stub never actually emits a finding at runtime (it always
    reports ``force_trace_only=True`` — see ``test_stub_is_a_quiet_noop``)."""
    assert SUB_HANDLERS["geo_convergence_scan"] is gcs.handle
    assert (
        OUTPUT_KIND_BY_SUB_HANDLER["geo_convergence_scan"]
        is OutputKind.FINDING
    )
    assert "geo_convergence_scan" in STRUCTURAL_VERIFY_EXEMPT_ANALYSTS


async def test_stub_is_a_quiet_noop():
    """Post-fold, ``handle()`` is a deprecation stub: no pool required (it
    reads/writes nothing), always force_trace_only, and its data block names
    the fold destination — so it can never double-fire an alert alongside
    alert_trigger_scan's folded scan sharing the same watermark rows."""
    result = await gcs.handle(
        [], {"sub_handler": "geo_convergence_scan"}, None
    )
    assert isinstance(result, AnalystMethodResult)
    assert result.force_trace_only is True
    assert result.finding.data["folded_into"] == "alert_trigger_scan"
    assert result.finding.data["folded_trigger_class"] == gcs.TRIGGER_CLASS
    assert result.usage == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
    }

    # Deps with NO pg_pool at all — still a no-op, never raises (unlike the
    # pre-fold handler, which refused loud on a missing pool).
    class _NoPoolDeps:
        pass

    result2 = await gcs.handle(
        [], {"sub_handler": "geo_convergence_scan"}, _NoPoolDeps()
    )
    assert result2.force_trace_only is True


# ---------------------------------------------------------------------------
# Pure — bin keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lat,lon,expected",
    [
        (33.31, 44.36, "cell:33:44"),          # Baghdad-ish
        (-23.55, -46.63, "cell:-24:-47"),      # floor, not trunc, on negatives
        (0.0, 0.0, "cell:0:0"),
        (90.0, 180.0, "cell:89:179"),          # top edges fold into last cell
        (-90.0, -180.0, "cell:-90:-180"),
        ("33.5", "44.5", "cell:33:44"),        # numeric strings (JSONB text)
        (91.0, 0.0, None),                     # out of range
        (0.0, -180.1, None),
        (float("nan"), 10.0, None),
        (None, 10.0, None),
        ("junk", 10.0, None),
    ],
)
def test_cell_key(lat, lon, expected):
    assert gcs.cell_key(lat, lon) == expected


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("US", "country:US"),
        ("us", "country:US"),
        (" iq ", "country:IQ"),
        ("USA", None),       # not ISO2
        ("U1", None),
        ("", None),
        (None, None),
        (123, None),
    ],
)
def test_country_key(tag, expected):
    assert gcs.country_key(tag) == expected


# ---------------------------------------------------------------------------
# Pure — source family + score
# ---------------------------------------------------------------------------


def test_source_family_first_tag_wins_with_honest_fallback():
    assert gcs.source_family(["gis", "geospatial", "hazard"], "s") == "gis"
    assert gcs.source_family(["", "news"], "s") == "news"  # skip empties
    # No tags / junk tags → per-source fallback (counts once, never inflates
    # diversity across the source's own signals).
    assert gcs.source_family([], "source.x") == "src:source.x"
    assert gcs.source_family(None, "source.x") == "src:source.x"
    assert gcs.source_family("notalist", "source.x") == "src:source.x"


def test_convergence_score_families_plus_capped_volume_bonus():
    assert gcs.convergence_score(3, 5) == 3      # below the first volume step
    assert gcs.convergence_score(3, 10) == 4     # +1 at 10 signals
    assert gcs.convergence_score(4, 25) == 6     # +2 at 20+
    assert gcs.convergence_score(3, 1000) == 5   # bonus capped at +2
    assert gcs.convergence_score(5, 0) == 5


# ---------------------------------------------------------------------------
# Pure — two-tier binning
# ---------------------------------------------------------------------------

_FAMS = {
    "src.quake": ["gis"],
    "src.news_a": ["news"],
    "src.news_b": ["news"],
    "src.tg": ["social"],
}


def test_build_bins_cells_and_countries():
    point_rows = [
        {"id": "p1", "source_id": "src.quake", "lat": 33.2, "lon": 44.1,
         "iso2": "IQ"},
        {"id": "p2", "source_id": "src.news_a", "lat": 33.9, "lon": 44.9,
         "iso2": "IQ"},
        {"id": "p3", "source_id": "src.tg", "lat": 33.5, "lon": 44.5,
         "iso2": "SY"},
        {"id": "p4", "source_id": "src.news_b", "lat": 10.0, "lon": 10.0,
         "iso2": None},
        {"id": "p5", "source_id": "src.news_b", "lat": "junk", "lon": 44.0,
         "iso2": "IQ"},  # unusable point mints NO bin
    ]
    country_rows = [
        {"id": "p1", "source_id": "src.quake", "country": "IQ"},
        {"id": "c1", "source_id": "src.news_a", "country": "IQ"},
        {"id": "c2", "source_id": "src.tg", "country": "iq"},
        {"id": "c3", "source_id": "src.news_b", "country": "XXL"},  # skipped
    ]
    bins = gcs.build_bins(point_rows, country_rows, _FAMS)

    cell = bins["cell:33:44"]
    assert cell.bin_kind == "cell"
    assert {sid for sid, _, _ in cell.contributors} == {"p1", "p2", "p3"}
    assert cell.families == {"gis", "news", "social"}
    assert cell.country_iso2 == "IQ"  # modal contributor country

    iq = bins["country:IQ"]
    assert iq.bin_kind == "country"
    assert iq.country_iso2 == "IQ"
    assert iq.families == {"gis", "news", "social"}
    assert iq.signal_count == 3

    # The lone p4 point mints its own cell; the junk XXL tag minted nothing.
    assert "cell:10:10" in bins
    assert not any(k.startswith("country:XX") for k in bins)


def test_build_bins_same_family_pileon_stays_one_family():
    """Two different NEWS sources are still ONE family — same-family pile-ons
    can never manufacture diversity."""
    country_rows = [
        {"id": f"c{i}", "source_id": src, "country": "SY"}
        for i, src in enumerate(["src.news_a", "src.news_b"] * 10)
    ]
    bins = gcs.build_bins([], country_rows, _FAMS)
    assert bins["country:SY"].families == {"news"}
    assert bins["country:SY"].signal_count == 20


# ---------------------------------------------------------------------------
# Pure — formation/dissolution edge core
# ---------------------------------------------------------------------------


def test_edge_actions_first_scan_seeds_silently():
    formed, dissolved, seed = gcs.edge_actions(
        False, {}, ["country:US", "cell:33:44"]
    )
    assert formed == [] and dissolved == []
    assert seed == ["cell:33:44", "country:US"]


def test_edge_actions_formation_dissolution_and_steady_state():
    prev = {
        "country:US": {"active": True, "families": ["gis", "news", "social"]},
        "country:SY": {"active": True, "families": ["gis", "news", "osint"]},
        "cell:10:10": {"active": False, "families": []},
    }
    formed, dissolved, seed = gcs.edge_actions(
        True, prev, ["country:US", "cell:10:10", "cell:33:44"]
    )
    # US persists (no refire); cell:10:10 REFORMS from inactive; cell:33:44 is
    # brand new; SY dropped below the bar → one dissolution.
    assert formed == ["cell:10:10", "cell:33:44"]
    assert dissolved == ["country:SY"]
    assert seed == []


def test_bin_label_states_cell_extent_never_a_point():
    assert gcs.bin_label("country:IQ", "IQ") == "IQ"
    assert gcs.bin_label("cell:33:44", "IQ") == "cell(33..34°, 44..45°) IQ"
    assert gcs.bin_label("cell:-24:-47", None) == "cell(-24..-23°, -47..-46°)"
