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
    # The SQL is re-pointed at the real writer output, live head only.
    (sql,) = conn.queries
    assert "kind = 'finding'" in sql
    assert "analyst_id = 'calibration_tracking'" in sql
    assert "superseded_by IS NULL" in sql
    assert "kind = 'calibration'" not in sql


async def test_eval_calibration_available_false_when_absent() -> None:
    conn = _FakeConn(cal_row=None)
    sb = await _eval_calibration_endpoint(_fake_deps(conn))(principal="t")
    assert sb.available is False
    assert sb.forecast_unproven is True
    assert sb.calibration_thin is True


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
