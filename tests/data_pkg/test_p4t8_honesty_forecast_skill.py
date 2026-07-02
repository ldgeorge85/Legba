# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P4-T8 honesty contract #2 — a degenerate / under-sample pilot never asserts
bare positive skill.

Two layers, both grounded and pure (no DB):

  * PRODUCER / TAG layer (``calibration_tracking._forecast_acute_metrics`` L610-705
    + the ``_build_finding`` tag conjunction L826-849): a geography-dominated
    (degenerate) or under-sample pilot emits NO ``forecast_skill_positive`` tag and
    WITHHOLDS the headline ``brier_forecast_acute`` (None). The earned tag requires
    the exact three-way conjunction ready AND non-degenerate AND BSS>0.
  * ROUTE / SCOREBOARD layer (``v3_api.eval_calibration`` L1132-1198): the
    ``/eval/calibration`` reducer reports ``forecast_unproven=True`` /
    ``calibration_thin=True`` for a degenerate pilot or a thin exogenous sample —
    never a bare positive number.

The tests FAIL the instant a degenerate/thin/under-sample pilot surfaces bare
positive skill.

Selectable via ``pytest -k p4t8_honesty``.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from legba.data.analysts.deterministic_handlers import calibration_tracking as cal
from legba.data.analysts.deterministic_handlers import forecast_acute as fa  # noqa: F401
from legba.data.registry.v3_api import CalibrationScoreboard, build_v3_router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _finding_tags(m: dict) -> list[str]:
    """Build the calibration finding from a forecast-acute metrics blob and return
    its tags — the exact ``_build_finding`` call block from
    tests/data_pkg/test_forecast_acute.py L116-125."""
    finding = cal._build_finding(
        brier=None, sample_size=0, reliability_bins=[], per_analyst={},
        rolling=[], drift_z=None, drift_threshold=2.0, resolution_sources={},
        self_consistency_only=False, brier_exogenous=None,
        brier_self_consistency=None, brier_pooled=None, exogenous_sample_size=0,
        self_consistency_fraction=0.0, insufficient_exogenous=True,
        forecast_acute=m, warnings=[], target_id=None,
    )
    return finding.tags


def _run(coro):
    # Fresh loop per call — robust when prior tests created/closed loops.
    return asyncio.run(coro)


class _FakeConn:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, query, *args):  # noqa: ANN001
        return self._row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, row):
        self._conn = _FakeConn(row)

    def acquire(self):
        return _FakeAcquire(self._conn)


def _calibration_endpoint(fake_deps):
    router = build_v3_router(fake_deps)
    return next(r.endpoint for r in router.routes if r.path == "/eval/calibration")


def _fake_deps(data: dict):
    row = {"id": "cal-1", "produced_at": "2026-06-30T00:00:00+00:00", "data": data}
    return SimpleNamespace(
        descriptor_registry=SimpleNamespace(pg=_FakePool(row))
    )


# ---------------------------------------------------------------------------
# PRODUCER / TAG layer
# ---------------------------------------------------------------------------


def test_degenerate_pilot_never_tagged_skill_positive():
    """40 rows of 0/1 certainties, base rate 0.2 (geography-dominated) → the
    producer flags degeneracy and refuses the earned-skill tag."""
    rows = [
        {"claimed_confidence": float(1 if i < 8 else 0),
         "p_base": 0.2,
         "outcome": (1 if i < 8 else 0)}
        for i in range(40)
    ]
    m = cal._forecast_acute_metrics(rows, min_sample=30)
    assert m["forecast_acute_degenerate"] is True

    tags = _finding_tags(m)
    assert "forecast_skill_positive" not in tags
    assert "forecast_pilot_degenerate" in tags


def test_undersample_pilot_withholds_headline_and_skill_tag():
    """A genuinely-probabilistic but UNDER-sample pilot (n=10 < 30) withholds the
    headline Brier (None; raw only under ``brier_forecast_acute_raw``) and earns no
    skill tag."""
    rows = [
        {"claimed_confidence": 0.7 if i % 2 else 0.3, "p_base": 0.5, "outcome": i % 2}
        for i in range(10)
    ]
    m = cal._forecast_acute_metrics(rows, min_sample=30)
    assert m["forecast_acute_ready"] is False
    assert m["brier_forecast_acute"] is None            # headline withheld
    assert m["brier_forecast_acute_raw"] is not None    # raw diagnostic only
    assert m["forecast_acute_status"].startswith("accumulating")

    assert "forecast_skill_positive" not in _finding_tags(m)


def test_skill_tag_requires_ready_AND_nondegenerate_AND_positive_bss():
    """The ONLY earned path (n>=30, non-degenerate, BSS>0) sets the tag; flipping
    ANY single precondition drops it — pins the L831-836 three-way conjunction."""
    rows = [
        {"claimed_confidence": 0.7 if i % 2 else 0.3, "p_base": 0.5, "outcome": i % 2}
        for i in range(40)
    ]
    m = cal._forecast_acute_metrics(rows, min_sample=30)
    assert m["forecast_acute_ready"] is True
    assert m["forecast_acute_degenerate"] is False
    assert m["brier_skill_score"] > 0.0
    assert "forecast_skill_positive" in _finding_tags(m)

    # Flip each precondition independently → the tag must disappear each time.
    not_ready = dict(m, forecast_acute_ready=False)
    assert "forecast_skill_positive" not in _finding_tags(not_ready)

    degenerate = dict(m, forecast_acute_degenerate=True)
    assert "forecast_skill_positive" not in _finding_tags(degenerate)

    no_skill = dict(m, brier_skill_score=0.0)
    assert "forecast_skill_positive" not in _finding_tags(no_skill)


def test_bss_none_on_degenerate_all_same_outcome():
    """30 rows all outcome 0, p_base 0 → climatology Brier 0 ⟹ BSS undefined
    (None), never a spurious positive; the tag stays off."""
    rows = [
        {"claimed_confidence": 0.0, "p_base": 0.0, "outcome": 0}
        for _ in range(30)
    ]
    m = cal._forecast_acute_metrics(rows, min_sample=30)
    assert m["brier_skill_score"] is None
    assert "forecast_skill_positive" not in _finding_tags(m)


# ---------------------------------------------------------------------------
# ROUTE / SCOREBOARD layer
# ---------------------------------------------------------------------------


def test_eval_calibration_degenerate_reads_unproven():
    """A degenerate pilot with a LARGE raw BSS is NEVER surfaced as proven."""
    ep = _calibration_endpoint(_fake_deps({
        "forecast_acute_ready": True,
        "forecast_acute_degenerate": True,
        "brier_skill_score": 0.9,
        "exogenous_sample_size": 3,
    }))
    result = _run(ep(principal="test"))
    assert isinstance(result, CalibrationScoreboard)
    assert result.forecast_unproven is True
    assert result.forecast_acute_degenerate is True
    assert result.calibration_thin is True


def test_eval_calibration_thin_exogenous_reads_thin():
    """exogenous_sample_size 3 (<5) ⟹ calibration_thin, even with an exogenous
    Brier present (L1168)."""
    ep = _calibration_endpoint(_fake_deps({
        "exogenous_sample_size": 3,
        "brier_exogenous": 0.12,
    }))
    result = _run(ep(principal="test"))
    assert result.calibration_thin is True


def test_eval_calibration_proven_only_when_ready_nondegenerate_positive():
    """The single proven case: ready AND non-degenerate AND BSS>0 AND a
    non-thin exogenous sample ⟹ forecast_unproven False / calibration_thin False."""
    ep = _calibration_endpoint(_fake_deps({
        "forecast_acute_ready": True,
        "forecast_acute_degenerate": False,
        "brier_skill_score": 0.25,
        "exogenous_sample_size": 12,
    }))
    result = _run(ep(principal="test"))
    assert result.forecast_unproven is False
    assert result.calibration_thin is False


def test_eval_calibration_no_finding_reads_unproven():
    """Before any calibration finding exists the scoreboard is unavailable AND
    both legs read unproven — a distinct honest 'no pilot yet' state, never a
    bare positive number."""
    ep = _calibration_endpoint(SimpleNamespace(
        descriptor_registry=SimpleNamespace(pg=_FakePool(None))
    ))
    result = _run(ep(principal="test"))
    assert result.available is False
    assert result.forecast_unproven is True
    assert result.calibration_thin is True
