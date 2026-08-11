# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M-1 — ``GET /api/v1/v3/eval/correctness``, the operator gold-set surface.

The 2026-08-02 engine review's finding was not that the number was wrong but
that it existed nowhere a reader would meet it: eight operator verdicts scoring
0.625 against a same-window faithfulness of 0.92, visible only inside one API
overlay's badge string. This route is where the axis lives, and these tests pin
the two things that make it trustworthy — the tiny-n display never lets a bare
ratio out, and the axis is served SEPARATELY from calibration (the standing
never-pool rule made structural).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from legba.data import correctness_axis
from legba.data.registry.v3_api import UnitCorrectnessBoard, build_v3_router


# ---------------------------------------------------------------------------
# Stub substrate (the test_v3_eval_country_scorecard harness)
# ---------------------------------------------------------------------------


class _StubConn:
    def __init__(self, labels, scorer_row=None, weeks=None) -> None:
        self._labels = labels
        self._scorer_row = scorer_row
        self._weeks = weeks or []
        self.queries: list[str] = []

    async def fetch(self, sql: str, *args: Any) -> Any:
        self.queries.append(sql)
        if "FROM correctness_labels" in sql:
            if isinstance(self._labels, Exception):
                raise self._labels
            return self._labels
        if "goldset_week_samples" in sql:
            return self._weeks
        return []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.queries.append(sql)
        return self._scorer_row


class _StubPool:
    def __init__(self, conn: _StubConn) -> None:
        self._conn = conn

    def acquire(self) -> Any:
        conn = self._conn

        class _Ctx:
            async def __aenter__(self) -> _StubConn:
                return conn

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()


class _StubDeps:
    def __init__(self, conn: _StubConn) -> None:
        class _Reg:
            pass

        self.descriptor_registry = _Reg()
        self.descriptor_registry.pg = _StubPool(conn)  # type: ignore[attr-defined]


def _endpoint(conn: _StubConn) -> Any:
    router = build_v3_router(deps=_StubDeps(conn))  # type: ignore[arg-type]
    return next(
        r for r in router.routes  # type: ignore[attr-defined]
        if r.path == "/eval/correctness"
    ).endpoint


def _call(conn: _StubConn) -> UnitCorrectnessBoard:
    return asyncio.run(_endpoint(conn)(principal="tester"))


def _label(unit: str, label: str) -> dict[str, Any]:
    return {"unit_analyst_id": unit, "label": label}


#: The eight verdicts that actually exist, read from the live table 2026-08-03.
_LIVE_GOLD_SET = [
    _label("economic_coercion", "incorrect"),
    _label("energy_security", "partially_correct"),
    _label("escalation", "correct"),
    _label("internal_stability", "correct"),
    _label("leadership_transition", "partially_correct"),
    _label("military_posture", "partially_correct"),
    _label("narrative_coordination", "partially_correct"),
    _label("narrative_coordination", "correct"),
]


def _scorer_row(units: dict[str, Any]) -> dict[str, Any]:
    return {
        "produced_at": datetime(2026, 8, 3, tzinfo=timezone.utc),
        "data": {
            "title": "t",
            "body": "b",
            "data": {"sub_handler": "unit_correctness_scorer", "units": units},
        },
    }


# ---------------------------------------------------------------------------
# Registration + separation
# ---------------------------------------------------------------------------


def test_route_is_registered_and_separate_from_calibration() -> None:
    router = build_v3_router(deps=object())  # type: ignore[arg-type]
    paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
    assert "/eval/correctness" in paths
    # It is its OWN route, NOT a section of the calibration scoreboard — the
    # never-pool rule made structural rather than documented.
    assert "/eval/calibration" in paths


def test_correctness_is_not_a_key_of_the_calibration_scoreboard() -> None:
    """Enforcement of the standing rule at the model boundary."""
    from legba.data.registry.v3_api import CalibrationScoreboard

    correctness_axis.assert_not_pooled(
        CalibrationScoreboard(available=False).model_dump(),
        what="the v3 calibration scoreboard",
    )


# ---------------------------------------------------------------------------
# The number, and the tiny-n display
# ---------------------------------------------------------------------------


def test_the_live_gold_set_surfaces_as_0_625_with_its_n() -> None:
    board = _call(_StubConn(_LIVE_GOLD_SET))
    assert board.available is True
    assert board.fleet is not None
    assert round(board.fleet.correctness, 3) == 0.625
    assert board.fleet.n_scored == 8
    # ...and is never called measured at n=8.
    assert board.fleet.sufficient is False
    assert board.fleet.min_labels == correctness_axis.MIN_FLEET_LABELS
    assert "indicative only" in board.fleet.status


def test_every_row_carries_its_mix_so_no_bare_ratio_escapes() -> None:
    board = _call(_StubConn(_LIVE_GOLD_SET))
    for row in board.units:
        if row.correctness is None:
            continue
        assert row.n_scored > 0
        assert sum(row.mix.values()) == row.n_labels
        assert row.status  # a sentence naming the n, always
        assert "correctness" in row.display and "n=" in row.display


def test_a_single_verdict_unit_is_marked_insufficient() -> None:
    board = _call(_StubConn(_LIVE_GOLD_SET))
    esc = next(r for r in board.units if r.unit == "escalation")
    assert esc.correctness == 1.0          # one 'correct' verdict
    assert esc.n_scored == 1
    assert esc.sufficient is False
    assert "1 correct" in esc.display


def test_fleet_pools_verdicts_rather_than_averaging_unit_means() -> None:
    board = _call(_StubConn(_LIVE_GOLD_SET))
    unit_means = [r.correctness for r in board.units if r.correctness is not None]
    mean_of_means = sum(unit_means) / len(unit_means)
    # narrative_coordination carries two verdicts; a mean of means would give it
    # the same weight as a unit with one.
    assert board.fleet.correctness != mean_of_means


def test_honesty_note_states_the_never_pool_rule_and_the_floors() -> None:
    board = _call(_StubConn(_LIVE_GOLD_SET))
    assert "never pooled" in board.honesty_note
    assert "JUDGE-INDEPENDENT" in board.honesty_note
    assert str(correctness_axis.MIN_UNIT_LABELS) in board.honesty_note
    assert str(correctness_axis.MIN_FLEET_LABELS) in board.honesty_note
    assert "unresolvable" in board.honesty_note


# ---------------------------------------------------------------------------
# The second axis rides along, segregated
# ---------------------------------------------------------------------------


def test_the_diagnostic_axis_and_faithfulness_ride_along_unpooled() -> None:
    scorer = _scorer_row({
        "escalation": {
            "unit": "escalation",
            "faithfulness": 0.92,
            "faithfulness_population": {"judge_pipeline_version": "2026-08-03/1"},
            "correctness_vs_reference": None,
            "n_labeled": 0,
            "status": "no gold labels",
        },
    })
    board = _call(_StubConn(_LIVE_GOLD_SET, scorer_row=scorer))
    esc = next(r for r in board.units if r.unit == "escalation")
    # The operator axis says 1.0 (n=1) while the deterministic axis is null and
    # faithfulness is 0.92 — three separate numbers, none averaged.
    assert esc.correctness == 1.0
    assert esc.correctness_vs_reference is None
    assert esc.reference_status == "no gold labels"
    assert esc.faithfulness == 0.92
    # Faithfulness never appears without naming the judge population it covers.
    assert esc.judge_pipeline_version == "2026-08-03/1"
    assert board.scored_at is not None


def test_a_unit_the_scorer_knows_but_the_gold_set_does_not_still_gets_a_row() -> None:
    scorer = _scorer_row({
        "disruption_status": {
            "unit": "disruption_status", "faithfulness": 0.88,
            "correctness_vs_reference": None, "n_labeled": 0,
        },
    })
    board = _call(_StubConn(_LIVE_GOLD_SET, scorer_row=scorer))
    row = next(r for r in board.units if r.unit == "disruption_status")
    assert row.correctness is None
    assert row.n_labels == 0
    assert row.status == "no operator verdicts"
    assert row.faithfulness == 0.88


# ---------------------------------------------------------------------------
# Loop health + degradation
# ---------------------------------------------------------------------------


def test_labeling_loop_health_rides_beside_the_number() -> None:
    weeks = [
        {"week": "2026-W31", "sampled": 8, "labeled": 8},
        {"week": "2026-W30", "sampled": 8, "labeled": 0},
    ]
    board = _call(_StubConn(_LIVE_GOLD_SET, weeks=weeks))
    assert board.labeling["weeks_pinned"] == 2
    assert board.labeling["weeks"][0] == {
        "week": "2026-W31", "sampled": 8, "labeled": 8
    }
    # A week pinned but unlabelled is visible — a stalled loop is why n stops
    # growing, and a flat number must not read as a stable measurement.
    assert board.labeling["weeks"][1]["labeled"] == 0


def test_a_missing_gold_table_degrades_to_an_honest_empty_board() -> None:
    board = _call(_StubConn(RuntimeError("relation does not exist")))
    assert board.available is False
    assert board.fleet is None
    assert board.units == []
    # The honesty note is ALWAYS present — the empty state still says what the
    # axis is and why it is empty-capable.
    assert "never pooled" in board.honesty_note


def test_no_scorer_run_yet_still_serves_the_operator_axis() -> None:
    board = _call(_StubConn(_LIVE_GOLD_SET, scorer_row=None))
    assert board.available is True
    assert board.scored_at is None
    esc = next(r for r in board.units if r.unit == "escalation")
    assert esc.correctness == 1.0
    assert esc.faithfulness is None          # honest absence, not 0.0
    assert esc.correctness_vs_reference is None
    assert uuid4  # keep the import meaningful for the harness parity
