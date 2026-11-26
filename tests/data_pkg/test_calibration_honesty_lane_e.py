"""Forecast/calibration honesty (signed confidence, own-n, staleness,
collection-volume exclusion, legacy label, one named threshold)."""
from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest
from datetime import datetime, timezone

from legba.data import calibration_honesty as h
from legba.data.analysts.deterministic_handlers import calibration_tracking as cal

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)


def test_signed_confidence_keeps_the_sign_and_clamps():
    assert h.hypothesis_claimed_confidence(-6) == 0.0   # clamped, not 0.92
    assert h.hypothesis_claimed_confidence(-3) == pytest.approx(0.05)
    assert h.hypothesis_claimed_confidence(0) == 0.5
    assert h.hypothesis_claimed_confidence(3) == pytest.approx(0.95)
    assert h.hypothesis_claimed_confidence(9) == 1.0
    assert h.hypothesis_claimed_confidence(None) == 0.5


def _row(analyst, conf, out, by="forecast_vs_actual", ts="2026-07-01T00:00:00+00:00"):
    return {"analyst_id": analyst, "claim_id": "c", "claim_kind": "prediction",
            "claimed_confidence": conf, "outcome": out, "resolved_by": by,
            "produced_at": ts}


def _run(rows):
    r = asyncio.run(cal.handle(rows, {}, None))
    return r.finding.data


def test_country_predictor_excluded_from_calibration_and_labelled():
    rows = (
        [_row("country_predictor", 0.9, 0)] * 6
        + [_row("some_forecaster", 0.8, 1, ts="2026-11-01T00:00:00+00:00")] * 6
    )
    d = _run(rows)
    assert d["collection_volume_sample_size"] == 6
    pops = d["populations"]
    assert pops["collection_volume_predictor"]["n"] == 6
    assert pops["collection_volume_predictor"]["brier"] > 0.8
    # nothing of it leaks into the headline / pooled figures
    assert d["exogenous_sample_size"] == 6 and d["sample_size"] == 6
    assert abs(d["brier_exogenous"] - 0.04) < 1e-9
    assert pops["headline_exogenous"]["newest_member_at"].startswith("2026-11-01")
    assert d["hypothesis_confidence_signed_from"] == h.SIGNED_CONFIDENCE_FROM


def test_reduce_names_each_brier_with_its_own_n_and_marks_stale():
    d = _run([_row("country_predictor", 0.9, 0)] * 6
             + [_row("f", 0.8, 1, ts="2026-07-01T00:00:00+00:00")] * 6)
    red = h.reduce_calibration(d, now=NOW)
    assert red["exogenous_sample_size"] == 6
    assert red["pooled_sample_size"] == 6
    assert "brier" not in red and "sample_size" not in red
    assert red["label"] == h.LEGACY_LABEL
    assert "sealed forecast ledger" in red["ledger_note"]
    assert red["stale"] is True and red["newest_member_at"].startswith("2026-07-01")
    vol = next(p for p in red["populations"] if p["key"] == h.POP_VOLUME)
    assert "collection-volume predictor" in vol["label"]
    fresh = h.reduce_calibration(d, now=datetime(2026, 7, 5, tzinfo=timezone.utc))
    assert fresh["stale"] is False


def test_pre_lane_e_finding_withholds_the_mixed_brier():
    red = h.reduce_calibration({"brier": 0.398, "brier_exogenous": 0.398,
                                "exogenous_sample_size": 537, "sample_size": 6180},
                               now=NOW)
    assert red["legacy_finding_format"] is True
    assert red["brier_exogenous"] is None and red["brier_pooled"] is None
    assert red["legacy_unscoped"]["pooled_sample_size"] == 6180
    assert red["stale"] is None


def test_one_named_threshold_everywhere():
    assert cal._FORECAST_ACUTE_MIN_SAMPLE == h.ACUTE_PILOT_MIN_SAMPLE == 30
    from legba.prompts.journal_assessor import JOURNAL_SYSTEM
    assert f"n<{h.ACUTE_PILOT_MIN_SAMPLE}" in JOURNAL_SYSTEM
    assert "untrusted" in JOURNAL_SYSTEM


def test_leaf_is_stdlib_only():
    code = (
        "import sys, importlib.util as u;"
        "s=u.spec_from_file_location('ch','src/legba/data/calibration_honesty.py');"
        "m=u.module_from_spec(s); s.loader.exec_module(m);"
        "print(sorted(k for k in sys.modules if k in ('numpy','asyncpg','fastapi')))"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
