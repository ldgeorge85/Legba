# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the honest skill-scoreboard route on the v3 telemetry API (P4-T4).

Covers the read route added to :mod:`legba.data.registry.v3_api`:

  * ``GET /api/v1/v3/eval/calibration`` -> ``CalibrationScoreboard``

Like the System Status tests, these are pure registration + model-shape checks:
``build_v3_router`` only touches ``deps`` lazily inside the async handler, so the
router can be constructed against a trivial stub and its registered paths
introspected without a live substrate. The load-bearing HONESTY contract (absence
of proof is not proof of skill) is asserted on the pydantic model's defaults +
round-trip — the reduction itself mirrors ``SubstrateQueryPort.get_calibration``.
"""

from __future__ import annotations

from legba.data.registry.v3_api import (
    CalibrationScoreboard,
    build_v3_router,
)


def test_eval_calibration_route_registered() -> None:
    """The /eval/calibration route registers on the v3 router (resolves under the
    /api/v1/v3 mount prefix the panel polls)."""
    router = build_v3_router(deps=object())  # type: ignore[arg-type]
    paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
    assert "/eval/calibration" in paths


def test_calibration_scoreboard_absent_defaults() -> None:
    """No calibration finding yet -> available False, and BOTH legs read unproven.

    Absence of proof is not proof of skill: the honesty verdict fields default to
    the conservative (unproven / thin) state, refs empty, no numbers.
    """
    sb = CalibrationScoreboard(available=False)
    assert sb.available is False
    assert sb.forecast_unproven is True
    assert sb.calibration_thin is True
    assert sb.refs == []
    assert sb.produced_at is None
    assert sb.brier is None
    assert sb.brier_skill_score is None
    assert sb.forecast_acute_ready is False
    assert sb.forecast_acute_degenerate is False


def test_calibration_scoreboard_honest_field_roundtrip() -> None:
    """The full honest field set survives construction — the exogenous headline,
    the segregated acute-forecast keys, and the deterministic verdict flags."""
    sb = CalibrationScoreboard(
        available=True,
        produced_at="2026-06-30T00:00:00+00:00",
        brier=0.20,
        brier_exogenous=0.18,
        exogenous_sample_size=12,
        sample_size=40,
        insufficient_exogenous=False,
        self_consistency_only=False,
        brier_forecast_acute=0.11,
        brier_skill_score=0.25,
        forecast_acute_sample_size=18,
        forecast_acute_ready=True,
        forecast_acute_degenerate=False,
        forecast_acute_status="ready",
        forecast_unproven=False,
        calibration_thin=False,
        refs=["cal-1"],
    )
    assert sb.available is True
    assert sb.brier_exogenous == 0.18
    assert sb.exogenous_sample_size == 12
    # acute pilot lives in its OWN keys, never pooled into the headline.
    assert sb.brier_skill_score == 0.25
    assert sb.forecast_acute_status == "ready"
    assert sb.forecast_unproven is False
    assert sb.refs == ["cal-1"]


def test_calibration_scoreboard_degenerate_pilot_shape() -> None:
    """A degenerate acute pilot: the model still carries a (raw) BSS but the
    honesty flags mark it unproven — the UI reducer, not the model, withholds the
    number; the route's job is to surface the flags faithfully."""
    sb = CalibrationScoreboard(
        available=True,
        brier_forecast_acute=0.0,
        brier_skill_score=0.9,
        forecast_acute_sample_size=6,
        forecast_acute_ready=True,
        forecast_acute_degenerate=True,
        forecast_acute_status="degenerate",
        forecast_unproven=True,
        calibration_thin=True,
        refs=["cal-2"],
    )
    assert sb.forecast_acute_degenerate is True
    assert sb.forecast_unproven is True
