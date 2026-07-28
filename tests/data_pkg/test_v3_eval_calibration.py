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

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

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


# ---------------------------------------------------------------------------
# B0-3 (read-truth) — the calibration read is RE-POINTED at what the writer
# actually produces: calibration_tracking emits kind='finding' +
# analyst_id='calibration_tracking' (NOTHING writes kind='calibration'), with
# the metrics one JSONB level down at data.data (the FindingPayload dump).
# Covered here for ALL THREE registry-side call-sites (this module's
# /eval/calibration route + journal_api._read_calibration +
# journal_proposals_api._journal_calibration_evidence) via a duck-typed conn —
# the port-side site is covered DB-backed in tests/journal_w1.
# ---------------------------------------------------------------------------


class _FakeConn:
    """Duck-typed asyncpg conn: records every fetchrow SQL, answers the
    analyst_outputs calibration read and (proposals only) the analyst_critiques
    rollup with canned rows."""

    def __init__(self, cal_row=None, crit_row=None):
        self.cal_row = cal_row
        self.crit_row = crit_row
        self.queries: list[str] = []

    async def fetchrow(self, sql: str, *args):
        self.queries.append(sql)
        if "analyst_critiques" in sql:
            return self.crit_row
        return self.cal_row


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


def _fake_deps(conn):
    pg = SimpleNamespace(acquire=lambda: _FakeAcquire(conn))
    return SimpleNamespace(descriptor_registry=SimpleNamespace(pg=pg))


def _writer_shaped_row():
    """An analyst_outputs row EXACTLY as calibration_tracking writes it: the
    ``data`` column is the whole FindingPayload dump, metrics nested at
    ``data.data`` (live numbers: brier_exogenous 0.3976 / n_exo=537)."""
    return {
        "id": uuid4(),
        "produced_at": datetime(2026, 7, 9, tzinfo=timezone.utc),
        "data": json.dumps({
            "title": "Calibration",
            "body": "…",
            "confidence": 1.0,
            "data": {
                "sub_handler": "calibration_tracking",
                "brier": 0.3976,
                "brier_exogenous": 0.3976,
                "exogenous_sample_size": 537,
                "sample_size": 600,
                "brier_skill_score": None,
                "forecast_acute_ready": False,
                "forecast_acute_degenerate": False,
                "forecast_acute_sample_size": 3,
                "forecast_acute_status": "accumulating (n=3/30)",
            },
        }),
    }


def _eval_calibration_endpoint(deps):
    router = build_v3_router(deps=deps)
    (route,) = [
        r for r in router.routes  # type: ignore[attr-defined]
        if r.path == "/eval/calibration"
    ]
    return route.endpoint


async def test_eval_calibration_reads_writer_shape_nested() -> None:
    conn = _FakeConn(cal_row=_writer_shaped_row())
    sb = await _eval_calibration_endpoint(_fake_deps(conn))(principal="t")
    assert sb.available is True
    assert sb.brier == 0.3976
    assert sb.brier_exogenous == 0.3976
    assert sb.exogenous_sample_size == 537
    assert sb.calibration_thin is False
    assert sb.forecast_unproven is True
    assert len(sb.refs) == 1
    # The SQL is re-pointed at the real writer output, live head only. P2-3
    # adds a SECOND (additive) read for the band_calibration_tracker finding.
    cal_sql, band_sql = conn.queries
    assert "kind = 'finding'" in cal_sql
    assert "analyst_id = 'calibration_tracking'" in cal_sql
    assert "superseded_by IS NULL" in cal_sql
    assert "kind = 'calibration'" not in cal_sql
    assert "analyst_id = 'band_calibration_tracker'" in band_sql
    assert "superseded_by IS NULL" in band_sql
    # The fake answered the band read with a calibration_tracking-shaped row
    # (no data.data.band_calibration) — the section reads honestly unavailable,
    # never a fabricated aggregate.
    assert sb.band_calibration is not None
    assert sb.band_calibration.available is False
    assert sb.band_calibration.no_brier is True


async def test_eval_calibration_available_false_when_absent() -> None:
    conn = _FakeConn(cal_row=None)
    sb = await _eval_calibration_endpoint(_fake_deps(conn))(principal="t")
    assert sb.available is False
    assert sb.forecast_unproven is True
    assert sb.calibration_thin is True
    # P2-3: the band section is still carried (honest absent state).
    assert sb.band_calibration is not None
    assert sb.band_calibration.available is False


# ---------------------------------------------------------------------------
# P2-3 — the ADDITIVE band_calibration section (band-persistence harness).
# ---------------------------------------------------------------------------


class _RoutedFakeConn(_FakeConn):
    """Dispatches the band_calibration_tracker read to its own canned row so
    the two eval_calibration fetchrows can be answered independently."""

    def __init__(self, cal_row=None, band_row=None):
        super().__init__(cal_row=cal_row)
        self.band_row = band_row

    async def fetchrow(self, sql: str, *args):
        if "band_calibration_tracker" in sql:
            self.queries.append(sql)
            return self.band_row
        return await super().fetchrow(sql, *args)


def _band_writer_shaped_row():
    """An analyst_outputs row EXACTLY as band_calibration_tracker writes it:
    the aggregate one JSONB level down at data.data.band_calibration."""
    return {
        "id": uuid4(),
        "produced_at": datetime(2026, 7, 23, tzinfo=timezone.utc),
        "data": json.dumps({
            "title": "Band calibration: logged=1 resolved=4 this run (claims=9)",
            "body": "…",
            "confidence": 1.0,
            "data": {
                "sub_handler": "band_calibration_tracker",
                "band_calibration": {
                    "claims_total": 9,
                    "lookback_days": 365,
                    "resolution_spec": "hard_band_at_horizon_v1",
                    "horizons": {
                        "14d": {
                            "resolved": 4,
                            "open": 5,
                            "confirmed": 3,
                            "reverted": 1,
                            "scored": 4,
                            "persistence_rate": 0.75,
                            "reversal_rate": 0.25,
                        },
                        "28d": {
                            "resolved": 0,
                            "open": 9,
                            "confirmed": 0,
                            "reverted": 0,
                            "scored": 0,
                            "persistence_rate": None,
                            "reversal_rate": None,
                        },
                    },
                    "by_direction": {"deterioration": {"claims": 9}},
                    "by_dimension": {"escalation": {"claims": 9}},
                    "no_brier": True,
                    "honesty_note": "bands are not probabilities",
                },
            },
        }),
    }


def test_band_calibration_section_defaults_are_honest() -> None:
    from legba.data.registry.v3_api import BandCalibrationSection

    sec = BandCalibrationSection(available=False)
    assert sec.available is False
    assert sec.claims_total is None
    assert sec.horizons == {}
    assert sec.no_brier is True
    assert sec.refs == []


async def test_eval_calibration_band_section_populated() -> None:
    """The additive band_calibration section projects the tracker finding's
    aggregate — its OWN keys, no Brier — while the main scoreboard fields are
    byte-identical to the pre-P2-3 reduction."""
    conn = _RoutedFakeConn(
        cal_row=_writer_shaped_row(), band_row=_band_writer_shaped_row()
    )
    sb = await _eval_calibration_endpoint(_fake_deps(conn))(principal="t")
    # Main scoreboard untouched.
    assert sb.available is True and sb.brier == 0.3976
    # Band section: populated from the writer-shaped nested block.
    bc = sb.band_calibration
    assert bc is not None and bc.available is True
    assert bc.claims_total == 9
    assert bc.resolution_spec == "hard_band_at_horizon_v1"
    assert bc.horizons["14d"]["persistence_rate"] == 0.75
    assert bc.horizons["28d"]["persistence_rate"] is None
    assert bc.no_brier is True
    assert bc.honesty_note == "bands are not probabilities"
    assert len(bc.refs) == 1
    # The serialized response stays additive: every pre-P2-3 key survives.
    dumped = sb.model_dump()
    for key in (
        "brier", "brier_exogenous", "brier_skill_score", "forecast_unproven",
        "calibration_thin", "refs",
    ):
        assert key in dumped
    assert "band_calibration" in dumped


async def test_eval_calibration_band_section_absent_when_no_finding() -> None:
    conn = _RoutedFakeConn(cal_row=_writer_shaped_row(), band_row=None)
    sb = await _eval_calibration_endpoint(_fake_deps(conn))(principal="t")
    assert sb.available is True
    assert sb.band_calibration is not None
    assert sb.band_calibration.available is False


async def test_journal_api_read_calibration_nested_and_repointed() -> None:
    from legba.data.registry.journal_api import _read_calibration

    conn = _FakeConn(cal_row=_writer_shaped_row())
    verdict = await _read_calibration(conn)
    assert verdict.available is True
    assert verdict.exogenous_sample_size == 537
    assert verdict.calibration_thin is False
    assert verdict.forecast_unproven is True
    (sql,) = conn.queries
    assert "analyst_id = 'calibration_tracking'" in sql
    assert "superseded_by IS NULL" in sql

    verdict_absent = await _read_calibration(_FakeConn(cal_row=None))
    assert verdict_absent.available is False
    assert verdict_absent.forecast_unproven is True


async def test_journal_proposals_calibration_evidence_nested_and_repointed() -> None:
    from legba.data.registry.journal_proposals_api import (
        _journal_calibration_evidence,
    )

    row = _writer_shaped_row()
    # a PROVEN acute pilot, nested exactly where the writer puts it
    payload = json.loads(row["data"])
    payload["data"].update({
        "brier_skill_score": 0.25,
        "forecast_acute_ready": True,
        "forecast_acute_degenerate": False,
    })
    row["data"] = json.dumps(payload)
    conn = _FakeConn(cal_row=row, crit_row={"mean": 0.7, "n": 4})
    ev = await _journal_calibration_evidence(conn)
    assert ev.available is True
    assert ev.brier_skill_score == 0.25
    assert ev.forecast_unproven is False   # nested read reaches the pilot keys
    assert ev.calibration_thin is False    # nested n_exo=537 reaches the guard
    assert ev.journal_critic_mean == 0.7
    cal_sql = conn.queries[0]
    assert "analyst_id = 'calibration_tracking'" in cal_sql
    assert "superseded_by IS NULL" in cal_sql

    ev_absent = await _journal_calibration_evidence(
        _FakeConn(cal_row=None, crit_row={"mean": None, "n": 0})
    )
    assert ev_absent.available is False
    assert ev_absent.forecast_unproven is True
