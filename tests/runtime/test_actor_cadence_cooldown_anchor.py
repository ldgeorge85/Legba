# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R3 — the per-(analyst, target) cadence cooldown is anchored on run START.

The defect, observed live: ``AnalystActor.run`` stamped
``cooldown_by_target[key] = run_END + cooldown_seconds``. That makes the
effective mute window ``cooldown_seconds + run_duration``, so ANY run slower
than ``(cadence_period − cooldown_seconds)`` pushes the expiry past the
analyst's OWN next scheduled tick and eats it — silently, with no
``analyst_traces`` row, only a bare ``noop reason=cooldown``.

The proven instance is reproduced verbatim below: signal_salience fired at
20:07:01Z, ran 14m15s, and its 21:07:01Z tick was dropped because
``20:21:16 + 3000s = 21:11:16`` was still in the future. Ten analysts sat 16-41h
dark on that mechanism (journal_assessor / world_assessor losing their 12:00Z
leg two days running, desk_baseline + source_track_record 41h, …).

What these tests pin:

  * the live case RUNS under start-anchoring, and the OLD end-anchored stamp is
    asserted to have blocked it — so a regression to end-anchoring fails here
    rather than a day later in the substrate;
  * the window is EXACTLY ``cooldown_seconds`` regardless of run duration;
  * burst suppression is UNCHANGED — a rapid re-trigger inside the window still
    NOOPs, including every instant the run itself is still in flight;
  * the NOOP is classifiable: a suppressed SCHEDULE tick (the alertable
    "cadence was eaten" case) is distinguishable from a suppressed reactive
    ``coalesced_fire`` (the cooldown working as designed).

The helpers under test are the ones ``AnalystActor.run`` itself calls at both
the check site and the stamp site — there is no second copy of this arithmetic.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from legba.runtime import dapr_actors
from legba.runtime.dapr_actors import (
    ACTIVE,
    _cadence_cooldown_blocks,
    _cadence_cooldown_slack,
    _cadence_cooldown_until,
    _is_organic_trigger,
    _is_schedule_trigger,
    _worker_actor_id,
)

# Reuse the A2 concurrent-workers test harness (fake actor / deps / state
# manager) rather than duplicating it — house style, see
# tests.runtime.test_critic_descriptor_e2e's cross-file import of
# test_spike_integration fixtures.
from tests.runtime.test_analyst_concurrent_workers import _build_descriptor, _make_actor, _make_deps


@pytest.fixture(autouse=True)
def _reset_deps_registry():
    dapr_actors.clear_deps_registry()
    yield
    dapr_actors.clear_deps_registry()


# The live signal_salience incident, to the second.
_COOLDOWN_S = 3000                                     # descriptor cooldown_seconds
_RUN_START = datetime(2026, 7, 29, 20, 7, 1, tzinfo=timezone.utc)
_RUN_END = datetime(2026, 7, 29, 20, 21, 16, tzinfo=timezone.utc)   # +14m15s
_NEXT_TICK = datetime(2026, 7, 29, 21, 7, 1, tzinfo=timezone.utc)   # +1h cadence


def _end_anchored(run_end: datetime, cooldown_seconds: int) -> datetime:
    """The OLD (buggy) stamp — kept here ONLY as the regression's negative pole."""
    return run_end + timedelta(seconds=cooldown_seconds)


# ---------------------------------------------------------------------------
# 1) The live incident: the next scheduled tick must RUN.
# ---------------------------------------------------------------------------


def test_slow_run_no_longer_eats_the_next_cadence_tick():
    """signal_salience 21:07:01Z — the tick that was dropped now runs."""
    stamped = _cadence_cooldown_until(_RUN_START, _COOLDOWN_S)
    assert stamped == datetime(2026, 7, 29, 20, 57, 1, tzinfo=timezone.utc)
    assert not _cadence_cooldown_blocks(
        stamped.isoformat(), now=_NEXT_TICK, cooldown_seconds=_COOLDOWN_S,
    )


def test_end_anchoring_would_still_eat_it_regression_pole():
    """Pin the defect itself: the OLD anchor DOES suppress the 21:07 tick.

    Without this the fix is untestable — a silent revert to ``_utcnow()`` at the
    stamp site would leave every other assertion in this file green.
    """
    old_stamp = _end_anchored(_RUN_END, _COOLDOWN_S)
    assert old_stamp == datetime(2026, 7, 29, 21, 11, 16, tzinfo=timezone.utc)
    assert old_stamp > _NEXT_TICK + _cadence_cooldown_slack(_COOLDOWN_S)
    assert _cadence_cooldown_blocks(
        old_stamp.isoformat(), now=_NEXT_TICK, cooldown_seconds=_COOLDOWN_S,
    )


# ---------------------------------------------------------------------------
# 2) The window is cooldown_seconds — full stop.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "run_duration_s",
    [0, 1, 60, 855, 3000, 7200, 86_400],
)
def test_window_is_independent_of_run_duration(run_duration_s):
    """However long the run takes, the next tick lands on the same schedule.

    This is the whole point of the change: the cooldown must be a property of
    the DESCRIPTOR, not of how slow the model happened to be that hour.
    """
    stamped = _cadence_cooldown_until(_RUN_START, _COOLDOWN_S)
    assert stamped - _RUN_START == timedelta(seconds=_COOLDOWN_S)
    # The run's own end is irrelevant to the arithmetic.
    run_end = _RUN_START + timedelta(seconds=run_duration_s)
    assert _cadence_cooldown_until(_RUN_START, _COOLDOWN_S) == stamped
    assert run_end >= _RUN_START  # the duration is only a fixture here


def test_a_run_longer_than_its_own_cooldown_still_frees_the_next_tick():
    """The worst case end-anchoring produced: run_duration > cooldown_seconds.

    Under the old anchor this analyst could NEVER hold its cadence — every run
    pushed the expiry a full run-duration past the next tick, compounding.
    """
    long_start = _RUN_START
    long_end = _RUN_START + timedelta(seconds=_COOLDOWN_S * 2)
    tick = _RUN_START + timedelta(hours=1)
    assert not _cadence_cooldown_blocks(
        _cadence_cooldown_until(long_start, _COOLDOWN_S).isoformat(),
        now=tick, cooldown_seconds=_COOLDOWN_S,
    )
    assert _cadence_cooldown_blocks(
        _end_anchored(long_end, _COOLDOWN_S).isoformat(),
        now=tick, cooldown_seconds=_COOLDOWN_S,
    )


# ---------------------------------------------------------------------------
# 3) Burst suppression + in-flight protection are UNCHANGED.
# ---------------------------------------------------------------------------


def test_rapid_double_trigger_within_the_cooldown_still_noops():
    """A coalesced re-fire a minute after the run started is still suppressed."""
    stamped = _cadence_cooldown_until(_RUN_START, _COOLDOWN_S).isoformat()
    for delay_s in (1, 60, 600, 1800, _COOLDOWN_S - 200):
        now = _RUN_START + timedelta(seconds=delay_s)
        assert _cadence_cooldown_blocks(
            stamped, now=now, cooldown_seconds=_COOLDOWN_S,
        ), f"re-trigger at +{delay_s}s should be suppressed"


def test_every_instant_the_run_is_in_flight_is_suppressed():
    """In-flight protection at this layer is unchanged by the re-anchoring.

    Dapr actors are turn-based (reentrancy is enabled nowhere in this
    deployment), so a second invocation of the same actor id queues rather than
    overlapping — that is the real in-flight guard and this change does not
    touch it. What the COOLDOWN contributes is that every instant between run
    start and run end still falls inside the mute window, so the queued call
    NOOPs when it is finally serviced. Assert that across the live 14m15s run.
    """
    stamped = _cadence_cooldown_until(_RUN_START, _COOLDOWN_S).isoformat()
    assert (_RUN_END - _RUN_START).total_seconds() < _COOLDOWN_S
    t = _RUN_START
    while t <= _RUN_END:
        assert _cadence_cooldown_blocks(
            stamped, now=t, cooldown_seconds=_COOLDOWN_S,
        ), f"in-flight instant {t.isoformat()} must stay suppressed"
        t += timedelta(seconds=60)


def test_cooldown_releases_after_the_window_plus_slack():
    stamped = _cadence_cooldown_until(_RUN_START, _COOLDOWN_S).isoformat()
    slack = _cadence_cooldown_slack(_COOLDOWN_S)
    assert slack == timedelta(seconds=150)                   # 5% of 3000s
    just_inside = _RUN_START + timedelta(seconds=_COOLDOWN_S) - slack - timedelta(seconds=1)
    just_outside = _RUN_START + timedelta(seconds=_COOLDOWN_S) - slack + timedelta(seconds=1)
    assert _cadence_cooldown_blocks(stamped, now=just_inside, cooldown_seconds=_COOLDOWN_S)
    assert not _cadence_cooldown_blocks(stamped, now=just_outside, cooldown_seconds=_COOLDOWN_S)


def test_slack_is_capped_at_ten_minutes():
    """A 24h cooldown must not buy itself a 72-minute slack."""
    assert _cadence_cooldown_slack(86_400) == timedelta(seconds=600)
    assert _cadence_cooldown_slack(0) == timedelta(0)


# ---------------------------------------------------------------------------
# 4) Shape tolerance — the stamp round-trips through Dapr state.
# ---------------------------------------------------------------------------


def test_absent_stamp_never_blocks():
    for empty in (None, ""):
        assert not _cadence_cooldown_blocks(
            empty, now=_RUN_START, cooldown_seconds=_COOLDOWN_S,
        )


def test_datetime_and_iso_string_stamps_agree():
    """Dapr's DefaultJSONSerializer may hand back a datetime, not the str we wrote."""
    stamped = _cadence_cooldown_until(_RUN_START, _COOLDOWN_S)
    at = _RUN_START + timedelta(seconds=60)
    assert _cadence_cooldown_blocks(stamped, now=at, cooldown_seconds=_COOLDOWN_S)
    assert _cadence_cooldown_blocks(
        stamped.isoformat(), now=at, cooldown_seconds=_COOLDOWN_S,
    )


# ---------------------------------------------------------------------------
# 5) The NOOP is alertable — a missed SCHEDULE tick is not a throttled reactive.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trigger", ["cadence", "reminder"])
def test_schedule_triggers_classify_as_missed_cadence(trigger):
    assert _is_schedule_trigger(trigger)


@pytest.mark.parametrize("trigger", ["coalesced_fire", "method", "whatever"])
def test_non_schedule_triggers_are_not_missed_cadence(trigger):
    assert not _is_schedule_trigger(trigger)


def test_organic_trigger_classification_is_untouched():
    """The cooldown-STAMPING gate is unchanged: only 'method' is forced."""
    for organic in ("cadence", "reminder", "coalesced_fire"):
        assert _is_organic_trigger(organic)
    assert not _is_organic_trigger("method")


# ---------------------------------------------------------------------------
# 6) R4 — per-target WORKER schedule-tick suppression is DESIGNED, not a
# missed heartbeat. Live: 432 missed_cadence WARNINGs/9h, 369 from
# cross_source_dedup workers whose fan-out tick landed inside the cooldown a
# just-completed coalesced_fire on the SAME target had stamped.
#
# These exercise ``AnalystActor.run`` end to end (fake state manager + a
# registered deps resolver, no live daprd) rather than the pure cooldown-math
# helpers above — the classification lives at the call site, keyed off
# ``AnalystActor._is_worker``, which only a real actor instance can answer.
# ---------------------------------------------------------------------------


def _future_stamp(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


async def _seed_record(actor, *, descriptor, cooldown_key: str, worker_target: str | None = None):
    rec = {
        "actor_id": actor.id.id,
        "actor_kind": "analyst",
        "descriptor_id": descriptor.identity.id,
        "descriptor_version": descriptor.identity.version,
        "lifecycle": ACTIVE,
        "last_run_at": None,
        "last_outcome": None,
        "cooldown_until": None,
        "cooldown_by_target": {cooldown_key: _future_stamp()},
        "error_count": 0,
        "last_error": None,
    }
    if worker_target is not None:
        rec["worker_target"] = worker_target
    await actor._set_record(rec)


async def test_worker_schedule_tick_inside_cooldown_is_info_worker_suppressed(caplog):
    """A per-target WORKER's cadence fan-out tick landing inside a cooldown a
    recent run on the SAME target stamped is reclassified: INFO,
    worker_suppressed=true, missed_cadence=false (NOT the WARNING)."""
    descriptor = _build_descriptor()  # cooldown_seconds=300, cadence-bearing
    deps = _make_deps(descriptor)

    async def resolver(actor_id: str):
        return deps

    dapr_actors.register_analyst_deps_resolver(resolver)

    worker_id = _worker_actor_id(descriptor.identity.id, "brazil")
    actor = _make_actor(worker_id)
    await _seed_record(
        actor, descriptor=descriptor, cooldown_key="brazil", worker_target="brazil",
    )

    with caplog.at_level(logging.INFO, logger="legba.runtime.dapr_actors"):
        result = await actor.run({"trigger_kind": "cadence", "target_filter": "brazil"})

    assert result["outcome"] == dapr_actors.ActorRunOutcome.NOOP.value
    assert result["reason"] == "cooldown"
    assert result["missed_cadence"] is False
    assert result["worker_suppressed"] is True
    # No WARNING was raised for this designed-suppression case.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "worker_suppressed=true" in r.message and "missed_cadence=false" in r.message
        for r in caplog.records
    )


async def test_primary_schedule_tick_inside_cooldown_stays_warning_missed_cadence(caplog):
    """A PRIMARY / meta actor's OWN cadence tick landing inside its cooldown
    is the true "went dark a full period" case — unchanged: WARNING,
    missed_cadence=true, worker_suppressed=false."""
    descriptor = _build_descriptor()  # default version "0"*64 -> primary tail "0"*16
    deps = _make_deps(descriptor)

    async def resolver(actor_id: str):
        return deps

    dapr_actors.register_analyst_deps_resolver(resolver)

    primary_id = "analyst::" + descriptor.identity.id + "::" + "0" * 16
    actor = _make_actor(primary_id)
    await _seed_record(actor, descriptor=descriptor, cooldown_key="_global")

    with caplog.at_level(logging.INFO, logger="legba.runtime.dapr_actors"):
        result = await actor.run({"trigger_kind": "cadence"})  # no target_filter

    assert result["outcome"] == dapr_actors.ActorRunOutcome.NOOP.value
    assert result["reason"] == "cooldown"
    assert result["missed_cadence"] is True
    assert result["worker_suppressed"] is False
    assert any(
        r.levelno == logging.WARNING and "missed_cadence=true" in r.message
        for r in caplog.records
    )


async def test_worker_reactive_trigger_inside_cooldown_still_info_not_missed():
    """Control: a worker's REACTIVE re-fire ('coalesced_fire') inside its own
    cooldown was already INFO/missed_cadence=false pre-fix — unaffected by
    the worker-scoping change."""
    descriptor = _build_descriptor()
    deps = _make_deps(descriptor)

    async def resolver(actor_id: str):
        return deps

    dapr_actors.register_analyst_deps_resolver(resolver)

    worker_id = _worker_actor_id(descriptor.identity.id, "brazil")
    actor = _make_actor(worker_id)
    await _seed_record(
        actor, descriptor=descriptor, cooldown_key="brazil", worker_target="brazil",
    )

    result = await actor.run({"trigger_kind": "coalesced_fire", "target_filter": "brazil"})
    assert result["missed_cadence"] is False
    assert result["worker_suppressed"] is False
