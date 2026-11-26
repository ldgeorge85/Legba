# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cadence-stall threshold follows the analyst's own SCHEDULE.

The watchdog once called ``correctness_grader`` stalled.
Its descriptor's ``fallback_schedule`` is ``20 1-9 * * *`` (hourly, 01:20-09:20
UTC); its runs started 01:21 … 09:21, and the next at 01:21 the following day.
The old evaluator took the gap between the next two fires after NOW (01:20 and
02:20 the next day = 1 h) as the interval and alerted at 2 x 1 h after 09:21.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from croniter import croniter

from legba.runtime.cadence_staleness import (
    evaluate_cadence_staleness,
    schedule_threshold_s,
)
from legba.runtime.liveness_watchdog import _evaluate_cadence_staleness

UTC = timezone.utc
GRADER_CRON = "20 1-9 * * *"            # correctness_grader, live descriptor
GRADER_LAST_RUN = datetime(2026, 11, 8, 9, 21, 0, 302203, tzinfo=UTC)
DEDUP_CRON = "*/15 * * * *"             # cross_source_dedup, live descriptor
FLOOR_S = 90 * 60.0                     # _CADENCE_MIN_THRESHOLD_S


def _rows(cron: str, last_run: datetime | None, aid: str = "a") -> list[dict]:
    return [{"analyst_id": aid, "cron": cron, "last_run": last_run}]


def _old_threshold_s(cron: str, now: datetime, factor: float = 2.0) -> float:
    """The previous rule: factor x the gap between the next two fires after NOW."""
    it = croniter(cron, start_time=now)
    first, second = it.get_next(datetime), it.get_next(datetime)
    return max(factor * (second - first).total_seconds(), FLOOR_S)


def test_the_watchdog_calls_the_extracted_evaluator() -> None:
    assert _evaluate_cadence_staleness is evaluate_cadence_staleness


def test_the_1121z_grader_alarm_was_the_flat_interval() -> None:
    now = datetime(2026, 11, 8, 11, 21, 30, tzinfo=UTC)
    age = (now - GRADER_LAST_RUN).total_seconds()
    assert age > _old_threshold_s(GRADER_CRON, now)  # the false positive, reproduced
    assert evaluate_cadence_staleness(
        _rows(GRADER_CRON, GRADER_LAST_RUN, "correctness_grader"),
        now=now, factor=2.0, min_threshold_s=FLOOR_S,
    ) == []


@pytest.mark.parametrize("now", [
    datetime(2026, 11, 8, 11, 21, 30, tzinfo=UTC),
    datetime(2026, 11, 8, 18, 0, tzinfo=UTC),
    datetime(2026, 11, 9, 0, 59, tzinfo=UTC),
    datetime(2026, 11, 9, 2, 19, tzinfo=UTC),  # 01:20 missed, 02:20 not yet due
])
def test_the_grader_is_never_stale_outside_its_window(now: datetime) -> None:
    assert evaluate_cadence_staleness(
        _rows(GRADER_CRON, GRADER_LAST_RUN), now=now, factor=2.0, min_threshold_s=FLOOR_S,
    ) == []


def test_the_grader_is_stale_once_two_window_fires_pass_without_a_run() -> None:
    now = datetime(2026, 11, 9, 2, 21, tzinfo=UTC)
    stale = evaluate_cadence_staleness(
        _rows(GRADER_CRON, GRADER_LAST_RUN), now=now, factor=2.0, min_threshold_s=FLOOR_S,
    )
    assert len(stale) == 1
    deadline = datetime(2026, 11, 9, 2, 20, tzinfo=UTC)
    assert stale[0][2] == pytest.approx((deadline - GRADER_LAST_RUN).total_seconds())


def test_a_grader_dying_inside_its_window_still_alerts_at_two_hours() -> None:
    last = datetime(2026, 11, 8, 5, 21, tzinfo=UTC)
    assert evaluate_cadence_staleness(
        _rows(GRADER_CRON, last), now=last + timedelta(hours=1, minutes=59),
        factor=2.0, min_threshold_s=FLOOR_S,
    ) == []
    stale = evaluate_cadence_staleness(
        _rows(GRADER_CRON, last), now=last + timedelta(hours=2, minutes=1),
        factor=2.0, min_threshold_s=FLOOR_S,
    )
    assert stale and stale[0][2] == pytest.approx(2 * 3600.0)


@pytest.mark.parametrize("age_min,stale", [(40, False), (89, False), (91, True), (300, True)])
def test_a_continuous_analyst_keeps_its_threshold(age_min: int, stale: bool) -> None:
    now = datetime(2026, 11, 8, 11, 21, 30, tzinfo=UTC)
    last = now - timedelta(minutes=age_min)
    out = evaluate_cadence_staleness(
        _rows(DEDUP_CRON, last, "cross_source_dedup"), now=now, factor=2.0, min_threshold_s=FLOOR_S,
    )
    assert bool(out) is stale
    if out:  # unchanged from the previous rule on a uniform cron
        assert out[0][2] == pytest.approx(_old_threshold_s(DEDUP_CRON, now))


@pytest.mark.parametrize("cron", ["0 */6 * * *", "0 1,13 * * *", "*/15 * * * *", "18 * * * *"])
def test_uniform_crons_get_factor_times_period_from_an_on_schedule_run(cron: str) -> None:
    last = croniter(cron, start_time=datetime(2026, 11, 8, 3, 7, tzinfo=UTC)).get_next(datetime)
    it = croniter(cron, start_time=last)
    first, second = it.get_next(datetime), it.get_next(datetime)
    period = (second - first).total_seconds()
    assert schedule_threshold_s(cron, last, 2.0) == pytest.approx(2 * period)


PROJECTOR_CRON = "7 * * * *"            # graph_projector, live descriptor


def test_a_projector_run_in_flight_after_one_missed_fire_is_not_a_stall() -> None:
    """P-7: the 2026-11-14 09:07Z page. Last success started 07:07; the 08:07
    run was lost to a roll; the 09:07 run is in flight and lands its trace at
    09:16. Without the in-flight grace the age is exactly 2 h at 09:07 and the
    watchdog paged; with the analyst's own recent run time (~9.5 min) it does
    not — and it still alerts once the grace is spent."""
    last = datetime(2026, 11, 14, 7, 7, 0, tzinfo=UTC)
    in_flight = datetime(2026, 11, 14, 9, 7, 26, tzinfo=UTC)
    rows = _rows(PROJECTOR_CRON, last, "graph_projector")
    assert evaluate_cadence_staleness(
        rows, now=in_flight, factor=2.0, min_threshold_s=FLOOR_S,
    ), "the false page, reproduced without the grace"
    rows[0]["recent_run_s"] = 577.75
    assert evaluate_cadence_staleness(
        rows, now=in_flight, factor=2.0, min_threshold_s=FLOOR_S,
    ) == []
    hung = evaluate_cadence_staleness(
        rows, now=last + timedelta(hours=2, minutes=10), factor=2.0,
        min_threshold_s=FLOOR_S,
    )
    assert hung and hung[0][2] == pytest.approx(2 * 3600.0 + 577.75)


def test_the_in_flight_grace_is_capped_at_one_period() -> None:
    last = datetime(2026, 11, 14, 7, 7, tzinfo=UTC)
    assert schedule_threshold_s(PROJECTOR_CRON, last, 2.0, 99_999.0) == pytest.approx(
        3 * 3600.0
    )
    assert schedule_threshold_s(PROJECTOR_CRON, last, 2.0, None) == pytest.approx(  # type: ignore[arg-type]
        2 * 3600.0
    )


def test_an_invalid_cron_is_skipped() -> None:
    assert schedule_threshold_s("not a cron", GRADER_LAST_RUN, 2.0) is None
    assert evaluate_cadence_staleness(
        _rows("not a cron", GRADER_LAST_RUN - timedelta(days=9)),
        now=GRADER_LAST_RUN, factor=2.0, min_threshold_s=FLOOR_S,
    ) == []
