# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resilience-observability W-1b §2 — silent-stall liveness watchdog.

Exercises ``legba.runtime.liveness_watchdog.LivenessWatchdog.check_once`` (the
pure stall evaluator, driven by an injected ``now`` so no real timers are
needed):

  * a fresh rig with recent activity does NOT alert;
  * idle past the threshold fires exactly one ``high`` stall alert on
    ``legba.alerts.high`` with the standard alert envelope shape;
  * re-alerts are rate-limited to a heartbeat (no flood while still stalled);
  * a re-alert is allowed once ``realert_every`` has elapsed;
  * activity after a stall clears the latch (recovery), and a later stall
    alerts again;
  * the boot-grace: a cold rig (no activity yet) doesn't alert until
    ``stall_after`` has elapsed since construction.
"""

from __future__ import annotations

import json

import pytest

from datetime import datetime, timedelta, timezone

from legba.runtime.liveness_watchdog import (
    STALL_ALERT_SUBJECT,
    LivenessWatchdog,
    WatchdogConfig,
    _evaluate_cadence_staleness,
    _evaluate_empty_streaks,
    _source_stall_diagnosis,
)


class _RecordingNats:
    """Minimal NatsStore double — records publish calls.

    Alerts go out via ``publish_core`` (the streamless alert subject); both
    methods record to the same list so existing assertions hold and a
    regression that reverts to ``publish_json`` for alerts is caught.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []
        self.published_json: list[tuple[str, bytes]] = []
        self.published_core: list[tuple[str, bytes]] = []

    async def publish_json(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, payload))
        self.published_json.append((subject, payload))

    async def publish_core(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, payload))
        self.published_core.append((subject, payload))


def _watchdog(start_at: float = 0.0):
    nats = _RecordingNats()
    cfg = WatchdogConfig(stall_after_s=900.0, realert_every_s=1800.0, check_interval_s=60.0)
    wd = LivenessWatchdog(nats, cfg)
    # Pin the boot reference so the boot-grace math is deterministic.
    wd._started_at = start_at  # type: ignore[attr-defined]
    return wd, nats


@pytest.mark.asyncio
async def test_recent_activity_does_not_alert() -> None:
    wd, nats = _watchdog(start_at=0.0)
    await wd._on_signal(None)  # records activity at the (mocked) monotonic clock
    # Force a known last-activity time, then check shortly after.
    wd._last_signal_at = 100.0  # type: ignore[attr-defined]
    fired = await wd.check_once(now=200.0)  # 100s idle < 900s threshold
    assert fired is False
    assert nats.published == []


@pytest.mark.asyncio
async def test_stall_fires_one_high_alert() -> None:
    wd, nats = _watchdog(start_at=0.0)
    wd._last_finding_at = 100.0  # type: ignore[attr-defined]
    fired = await wd.check_once(now=100.0 + 901.0)  # 901s idle > 900s threshold
    assert fired is True
    assert len(nats.published) == 1
    subject, payload = nats.published[0]
    assert subject == STALL_ALERT_SUBJECT
    env = json.loads(payload)
    assert env["kind"] == "alert"
    assert env["severity"] == "high"
    assert "watchdog" in env["tags"]
    assert env["analyst_id"] == "system.liveness_watchdog"
    assert env["target_id"] is None
    assert env["idle_seconds"] >= 900.0
    # The alert MUST go out via core publish — legba.alerts.* has no JetStream
    # stream, so publish_json would NoStreamResponseError and drop it silently.
    assert nats.published_core == nats.published
    assert nats.published_json == []


@pytest.mark.asyncio
async def test_realert_rate_limited_then_allowed() -> None:
    wd, nats = _watchdog(start_at=0.0)
    wd._last_signal_at = 0.0  # type: ignore[attr-defined]
    # First stall alert.
    assert await wd.check_once(now=901.0) is True
    # Still stalled, but within the re-alert window → suppressed.
    assert await wd.check_once(now=901.0 + 1799.0) is False
    assert len(nats.published) == 1
    # Past the re-alert window → heartbeat alert allowed.
    assert await wd.check_once(now=901.0 + 1801.0) is True
    assert len(nats.published) == 2


@pytest.mark.asyncio
async def test_recovery_clears_latch_then_realerts() -> None:
    wd, nats = _watchdog(start_at=0.0)
    wd._last_signal_at = 0.0  # type: ignore[attr-defined]
    assert await wd.check_once(now=901.0) is True
    assert wd._stalled is True  # type: ignore[attr-defined]
    # A finding arrives → recovery (clears stall + alert latch).
    await wd._on_finding(None)
    assert wd._stalled is False  # type: ignore[attr-defined]
    # Pin a fresh last-activity, then stall again much later → alerts anew.
    wd._last_finding_at = 5000.0  # type: ignore[attr-defined]
    wd._last_signal_at = 5000.0  # type: ignore[attr-defined]
    assert await wd.check_once(now=5000.0 + 901.0) is True
    assert len(nats.published) == 2


@pytest.mark.asyncio
async def test_cold_rig_boot_grace() -> None:
    # No activity ever seen → reference is the boot time. Before stall_after
    # elapses since boot, the cold rig must NOT self-alert.
    wd, nats = _watchdog(start_at=0.0)
    assert await wd.check_once(now=899.0) is False
    assert nats.published == []
    # Once the boot grace passes with still no activity, it alerts.
    assert await wd.check_once(now=901.0) is True
    assert len(nats.published) == 1


# ---------------------------------------------------------------------------
# OBS — per-analyst cadence-liveness
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def _row(analyst_id: str, cron: str, last_run):
    return {"analyst_id": analyst_id, "cron": cron, "last_run": last_run}


def test_cadence_eval_flags_a_stale_6h_analyst() -> None:
    # 6h cron, factor 2 → 12h threshold. last_run 25h ago → STALE (the world_
    # assessor 24h-outage class). A fresh 6h analyst (3h ago) is NOT stale.
    rows = [
        _row("world_assessor", "0 */6 * * *", _NOW - timedelta(hours=25)),
        _row("country_assessor", "0 */6 * * *", _NOW - timedelta(hours=3)),
    ]
    stale = _evaluate_cadence_staleness(
        rows, now=_NOW, factor=2.0, min_threshold_s=90 * 60.0
    )
    ids = [s[0] for s in stale]
    assert ids == ["world_assessor"]
    aid, age_s, threshold_s = stale[0]
    assert age_s == pytest.approx(25 * 3600.0, abs=1.0)
    assert threshold_s == pytest.approx(12 * 3600.0, abs=1.0)


def test_cadence_eval_skips_never_run_and_bad_cron() -> None:
    rows = [
        _row("fresh_analyst", "0 */6 * * *", None),       # never ran → skip
        _row("broken_cron", "not a cron", _NOW - timedelta(days=9)),  # skip
        _row("", "0 */6 * * *", _NOW - timedelta(days=9)),  # no id → skip
        _row("no_cron", "", _NOW - timedelta(days=9)),     # no cron → skip
    ]
    assert _evaluate_cadence_staleness(
        rows, now=_NOW, factor=2.0, min_threshold_s=90 * 60.0
    ) == []


def test_cadence_eval_min_threshold_floor_protects_fast_cadence() -> None:
    # A */5 analyst: 5min × factor 2 = 10min, but the 90-min floor applies so a
    # single missed tick (e.g. 30 min) does NOT alert; 2h old DOES.
    rows_recent = [_row("fast", "*/5 * * * *", _NOW - timedelta(minutes=30))]
    rows_old = [_row("fast", "*/5 * * * *", _NOW - timedelta(hours=2))]
    assert _evaluate_cadence_staleness(
        rows_recent, now=_NOW, factor=2.0, min_threshold_s=90 * 60.0
    ) == []
    stale = _evaluate_cadence_staleness(
        rows_old, now=_NOW, factor=2.0, min_threshold_s=90 * 60.0
    )
    assert [s[0] for s in stale] == ["fast"]
    assert stale[0][2] == pytest.approx(90 * 60.0, abs=1.0)  # floored


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, *_a, **_k):
        return self._rows


class _FakePg:
    def __init__(self, rows):
        self._rows = rows

    def acquire(self):
        rows = self._rows

        class _Acq:
            async def __aenter__(self_inner):
                return _FakeConn(rows)

            async def __aexit__(self_inner, *exc):
                return False

        return _Acq()


def _cadence_watchdog(rows, *, is_leader=None):
    nats = _RecordingNats()
    cfg = WatchdogConfig(
        stall_after_s=900.0, realert_every_s=1800.0,
        check_interval_s=60.0, cadence_stall_factor=2.0,
    )
    wd = LivenessWatchdog(nats, cfg, pg_store=_FakePg(rows), is_leader=is_leader)
    wd._started_at = 0.0  # type: ignore[attr-defined]
    return wd, nats


@pytest.mark.asyncio
async def test_cadence_check_emits_per_analyst_alert_via_core() -> None:
    rows = [_row("world_assessor", "0 */6 * * *", _NOW - timedelta(hours=25))]
    wd, nats = _cadence_watchdog(rows)
    # now_monotonic past the boot grace (900s) so the cadence check runs.
    alerted = await wd.check_analyst_cadence_once(now_monotonic=1000.0)
    assert alerted == ["world_assessor"]
    assert len(nats.published_core) == 1
    subject, payload = nats.published_core[0]
    assert subject == STALL_ALERT_SUBJECT
    env = json.loads(payload)
    assert env["severity"] == "high"
    assert "cadence_stall" in env["tags"]
    assert env["stale_analyst_id"] == "world_assessor"
    assert nats.published_json == []


@pytest.mark.asyncio
async def test_cadence_check_boot_grace_and_rate_limit() -> None:
    rows = [_row("world_assessor", "0 */6 * * *", _NOW - timedelta(hours=25))]
    wd, nats = _cadence_watchdog(rows)
    # Inside the boot grace (< stall_after_s) → no alert yet.
    assert await wd.check_analyst_cadence_once(now_monotonic=500.0) == []
    assert nats.published == []
    # Past grace → alert once, then rate-limited within realert_every_s.
    assert await wd.check_analyst_cadence_once(now_monotonic=1000.0) == ["world_assessor"]
    assert await wd.check_analyst_cadence_once(now_monotonic=1500.0) == []  # within 1800s
    assert len(nats.published) == 1
    # Past the realert window → heartbeat alert again.
    assert await wd.check_analyst_cadence_once(now_monotonic=1000.0 + 1801.0) == ["world_assessor"]
    assert len(nats.published) == 2


@pytest.mark.asyncio
async def test_cadence_check_leader_gated() -> None:
    rows = [_row("world_assessor", "0 */6 * * *", _NOW - timedelta(hours=25))]
    wd, nats = _cadence_watchdog(rows, is_leader=lambda: False)
    assert await wd.check_analyst_cadence_once(now_monotonic=1000.0) == []
    assert nats.published == []


@pytest.mark.asyncio
async def test_cadence_realert_anchor_only_stamped_on_emit() -> None:
    # T-4(b) regression guard (the TEST_DEBT_RECON realert-anchor class): the
    # per-analyst realert anchor must be stamped ONLY when an alert is actually
    # emitted, never on a rate-limited (suppressed) check. Otherwise a
    # persistently-stalled analyst re-arms its window on every check and alerts
    # exactly ONCE ever. Sequence: alert at t=1000 (anchor→1000); a rate-limited
    # check at t=1500 must NOT touch the anchor; so at t=2801 (2801-1000=1801 ≥
    # 1800 realert) the heartbeat MUST fire against the ORIGINAL t=1000.
    rows = [_row("world_assessor", "0 */6 * * *", _NOW - timedelta(hours=25))]
    wd, nats = _cadence_watchdog(rows)
    assert await wd.check_analyst_cadence_once(now_monotonic=1000.0) == ["world_assessor"]
    # a suppressed check between the two alerts (must not reset the anchor to 1500)
    assert await wd.check_analyst_cadence_once(now_monotonic=1500.0) == []
    # 1801s after the ORIGINAL alert → heartbeat fires (would be starved forever
    # if the 1500 check had moved the anchor).
    assert await wd.check_analyst_cadence_once(now_monotonic=2801.0) == ["world_assessor"]
    assert len(nats.published) == 2


# ---------------------------------------------------------------------------
# DQ-H5: per-source cadence-liveness (generic evaluator + check)
# ---------------------------------------------------------------------------


def _src_row(source_id: str, cron: str, last_signal):
    return {"source_id": source_id, "cron": cron, "last_signal": last_signal}


def test_source_cadence_eval_flags_silent_source() -> None:
    # An hourly source silent 12d → STALE (the 10-silent-sources class). A fresh
    # one (30 min ago) is NOT — the 3h source floor protects a single missed poll.
    rows = [
        _src_row("source.xinhua.world", "18 * * * *", _NOW - timedelta(days=12)),
        _src_row("source.bbc.world", "5 * * * *", _NOW - timedelta(minutes=30)),
        _src_row("source.voa.africa", "47 * * * *", None),  # never produced → skip
    ]
    stale = _evaluate_cadence_staleness(
        rows, now=_NOW, factor=2.0, min_threshold_s=3 * 3600.0,
        id_key="source_id", ts_key="last_signal",
    )
    assert [s[0] for s in stale] == ["source.xinhua.world"]


@pytest.mark.asyncio
async def test_source_cadence_check_emits_source_alert() -> None:
    rows = [_src_row("source.xinhua.world", "18 * * * *", _NOW - timedelta(days=12))]
    wd, nats = _cadence_watchdog(rows)
    alerted = await wd.check_source_cadence_once(now_monotonic=1000.0)
    assert alerted == ["source.xinhua.world"]
    assert len(nats.published_core) == 1
    subject, payload = nats.published_core[0]
    assert subject == STALL_ALERT_SUBJECT
    env = json.loads(payload)
    assert env["severity"] == "high"
    assert "source_stall" in env["tags"]
    assert env["stale_source_id"] == "source.xinhua.world"
    assert nats.published_json == []


@pytest.mark.asyncio
async def test_source_cadence_check_leader_gated_and_rate_limited() -> None:
    rows = [_src_row("source.xinhua.world", "18 * * * *", _NOW - timedelta(days=12))]
    # Leader-gated off → silent.
    wd_off, nats_off = _cadence_watchdog(rows, is_leader=lambda: False)
    assert await wd_off.check_source_cadence_once(now_monotonic=1000.0) == []
    assert nats_off.published == []
    # Leader on → alert once, then rate-limited within the realert window.
    wd, nats = _cadence_watchdog(rows)
    assert await wd.check_source_cadence_once(now_monotonic=1000.0) == ["source.xinhua.world"]
    assert await wd.check_source_cadence_once(now_monotonic=1500.0) == []
    assert len(nats.published) == 1


# ---------------------------------------------------------------------------
# DQ-H5b (#88): poll-outcome diagnosis in the source-stall alert body
# ---------------------------------------------------------------------------


def test_source_stall_diagnosis_error() -> None:
    when = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
    msg = _source_stall_diagnosis(
        {
            "last_poll_outcome": "error",
            "last_poll_health": "unhealthy",
            "last_poll_error": "HTTP 503",
            "last_poll_at": when,
        }
    )
    assert "FAILED" in msg
    assert "unhealthy" in msg
    assert "HTTP 503" in msg
    assert "2026-06-22T09:00:00" in msg


def test_source_stall_diagnosis_empty() -> None:
    msg = _source_stall_diagnosis(
        {"last_poll_outcome": "empty", "last_poll_health": "healthy",
         "last_poll_error": None, "last_poll_at": None}
    )
    assert "0 signals" in msg
    assert "FAILED" not in msg


def test_source_stall_diagnosis_no_outcome_row() -> None:
    # No provenance row at all → the poll reminder itself may be dead.
    msg = _source_stall_diagnosis({})
    assert "reminder" in msg.lower()
    assert "may be dead" in msg


def _src_row_full(source_id, cron, last_signal, **poll):
    row = _src_row(source_id, cron, last_signal)
    row.update(poll)
    return row


@pytest.mark.asyncio
async def test_source_cadence_alert_body_carries_error_diagnosis() -> None:
    rows = [
        _src_row_full(
            "source.xinhua.world", "18 * * * *", _NOW - timedelta(days=12),
            last_poll_outcome="error", last_poll_health="unhealthy",
            last_poll_error="HTTP 500", last_poll_at=_NOW - timedelta(hours=1),
        )
    ]
    wd, nats = _cadence_watchdog(rows)
    assert await wd.check_source_cadence_once(now_monotonic=1000.0) == ["source.xinhua.world"]
    env = json.loads(nats.published_core[0][1])
    assert "FAILED" in env["body"]
    assert "HTTP 500" in env["body"]


@pytest.mark.asyncio
async def test_source_cadence_alert_body_flags_missing_outcome_row() -> None:
    # Stale source with NO recorded poll outcome → body points at the reminder.
    rows = [_src_row("source.xinhua.world", "18 * * * *", _NOW - timedelta(days=12))]
    wd, nats = _cadence_watchdog(rows)
    assert await wd.check_source_cadence_once(now_monotonic=1000.0) == ["source.xinhua.world"]
    env = json.loads(nats.published_core[0][1])
    assert "may be dead" in env["body"]


# ---------------------------------------------------------------------------
# D19: per-source empty-200 streak escalation to 'degraded'
# ---------------------------------------------------------------------------


def _outcome(source_id: str, outcome: str, occurred_at):
    return {"source_id": source_id, "outcome": outcome, "occurred_at": occurred_at}


def _empty_run(source_id: str, n: int, *, start=_NOW):
    # n consecutive 'empty' outcome rows, newest first (most recent at index 0).
    return [
        _outcome(source_id, "empty", start - timedelta(hours=i))
        for i in range(n)
    ]


def test_empty_streak_eval_flags_a_long_run() -> None:
    # xinhua/aljazeera class: a long run of clean-but-empty polls → degraded.
    rows = _empty_run("source.xinhua.world", 8)
    degraded = _evaluate_empty_streaks(rows, threshold=5)
    assert [d[0] for d in degraded] == ["source.xinhua.world"]
    sid, streak, newest_at = degraded[0]
    assert streak == 8
    assert newest_at == _NOW  # the most-recent empty row's timestamp


def test_empty_streak_eval_below_threshold_does_not_flag() -> None:
    rows = _empty_run("source.bbc.world", 4)
    assert _evaluate_empty_streaks(rows, threshold=5) == []


def test_empty_streak_eval_breaks_on_error_row() -> None:
    # The leading run is only counted up to the first non-'empty' (error) row.
    rows = [
        _outcome("s", "empty", _NOW),
        _outcome("s", "empty", _NOW - timedelta(hours=1)),
        _outcome("s", "error", _NOW - timedelta(hours=2)),  # breaks the run
        _outcome("s", "empty", _NOW - timedelta(hours=3)),
        _outcome("s", "empty", _NOW - timedelta(hours=4)),
    ]
    # Leading run = 2 empties → below threshold 5 → not flagged.
    assert _evaluate_empty_streaks(rows, threshold=5) == []
    # And with threshold 2 it IS flagged, counting only the leading 2.
    degraded = _evaluate_empty_streaks(rows, threshold=2)
    assert degraded[0][1] == 2


def test_empty_streak_eval_groups_per_source() -> None:
    rows = _empty_run("source.a", 6) + _empty_run("source.b", 3)
    degraded = _evaluate_empty_streaks(rows, threshold=5)
    assert [d[0] for d in degraded] == ["source.a"]


def _with_last_signal(rows, last_signal):
    # Stamp the per-source newest-signal timestamp the SQL now returns on each
    # row (a source's rows all carry the same value).
    return [{**r, "last_signal": last_signal} for r in rows]


def test_empty_streak_eval_resets_when_producing_again() -> None:
    # OBS fix: an actively-producing source whose last recorded outcome rows are
    # empty (a productive poll writes NO outcome row) must NOT be flagged. A
    # signal produced AFTER the most-recent empty poll resets the run.
    rows = _with_last_signal(
        _empty_run("source.reuters.world", 8),  # newest empty at _NOW
        last_signal=_NOW + timedelta(hours=1),  # produced AFTER the newest empty
    )
    assert _evaluate_empty_streaks(rows, threshold=5) == []


def test_empty_streak_eval_still_flags_when_signal_older_than_run() -> None:
    # A stale signal OLDER than the leading empty run does NOT reset it: the
    # source went (and stayed) empty after it last produced → genuinely dead.
    rows = _with_last_signal(
        _empty_run("source.xinhua.world", 8),   # newest empty at _NOW
        last_signal=_NOW - timedelta(hours=20),  # last production predates the run
    )
    degraded = _evaluate_empty_streaks(rows, threshold=5)
    assert [d[0] for d in degraded] == ["source.xinhua.world"]
    assert degraded[0][1] == 8


def test_empty_streak_eval_bounded_to_since_last_signal() -> None:
    # foreignaffairs 2026-07-21 false alarm: a source that produces daily but
    # polls hourly interleaves productive polls (which write NO outcome row)
    # among empties, so the raw leading run grows without bound. Only the
    # empties SINCE the newest produced signal count.
    rows = _with_last_signal(
        _empty_run("source.foreignaffairs.all", 20),        # hourly empties
        last_signal=_NOW - timedelta(hours=3, minutes=30),  # produced 3.5h ago
    )
    # empties at _NOW, -1h, -2h, -3h postdate the signal → streak 4 < 5.
    assert _evaluate_empty_streaks(rows, threshold=5) == []
    # with a lower threshold the streak is exactly the since-signal count.
    degraded = _evaluate_empty_streaks(rows, threshold=3)
    assert degraded[0][1] == 4


def test_empty_streak_eval_no_last_signal_key_unchanged() -> None:
    # Back-compat: rows without a last_signal key behave exactly as before
    # (contiguous-empty run alone decides) — the xinhua/aljazeera dead case.
    rows = _empty_run("source.dead.feed", 6)
    degraded = _evaluate_empty_streaks(rows, threshold=5)
    assert [d[0] for d in degraded] == ["source.dead.feed"]


@pytest.mark.asyncio
async def test_empty_streak_check_emits_degraded_alert() -> None:
    rows = _empty_run("source.aljazeera.arabic", 6)
    nats = _RecordingNats()
    cfg = WatchdogConfig(
        stall_after_s=900.0, realert_every_s=1800.0,
        check_interval_s=60.0, empty_streak_threshold=5,
    )
    wd = LivenessWatchdog(nats, cfg, pg_store=_FakePg(rows))
    wd._started_at = 0.0  # type: ignore[attr-defined]
    alerted = await wd.check_source_empty_streak_once(now_monotonic=1000.0)
    assert alerted == ["source.aljazeera.arabic"]
    assert len(nats.published_core) == 1
    subject, payload = nats.published_core[0]
    assert subject == STALL_ALERT_SUBJECT
    env = json.loads(payload)
    assert env["severity"] == "high"
    assert env["health_state"] == "degraded"
    assert "source_degraded" in env["tags"]
    assert "empty_streak" in env["tags"]
    assert env["empty_streak"] == 6
    assert env["stale_source_id"] == "source.aljazeera.arabic"
    # Must go out via core publish (legba.alerts.* has no JetStream stream).
    assert nats.published_json == []


@pytest.mark.asyncio
async def test_empty_streak_check_boot_grace_leader_and_rate_limit() -> None:
    rows = _empty_run("source.xinhua.world", 6)
    # Streak re-alerts now ride their OWN (much longer) interval — the global
    # realert_every_s no longer governs them (R-5, 2026-07-21: the shared
    # 30-min interval re-escalated a dozen degraded sources ~1.2k times/day).
    cfg = WatchdogConfig(
        stall_after_s=900.0, realert_every_s=1800.0,
        check_interval_s=60.0, empty_streak_threshold=5,
        empty_streak_realert_every_s=3600.0,
    )
    # Leader-gated off → silent.
    nats_off = _RecordingNats()
    wd_off = LivenessWatchdog(nats_off, cfg, pg_store=_FakePg(rows), is_leader=lambda: False)
    wd_off._started_at = 0.0  # type: ignore[attr-defined]
    assert await wd_off.check_source_empty_streak_once(now_monotonic=1000.0) == []
    assert nats_off.published == []

    nats = _RecordingNats()
    wd = LivenessWatchdog(nats, cfg, pg_store=_FakePg(rows))
    wd._started_at = 0.0  # type: ignore[attr-defined]
    # Inside boot grace → no alert.
    assert await wd.check_source_empty_streak_once(now_monotonic=500.0) == []
    # Past grace → alert once, then rate-limited for the CONTINUING episode —
    # including past the (shorter) global realert window.
    assert await wd.check_source_empty_streak_once(now_monotonic=1000.0) == ["source.xinhua.world"]
    assert await wd.check_source_empty_streak_once(now_monotonic=1500.0) == []
    assert await wd.check_source_empty_streak_once(now_monotonic=1000.0 + 1801.0) == []
    assert len(nats.published) == 1
    # Past the streak realert window → heartbeat alert again.
    assert await wd.check_source_empty_streak_once(now_monotonic=1000.0 + 3601.0) == ["source.xinhua.world"]
    assert len(nats.published) == 2
    # EPISODE RESET: the source recovers (produces newer than its empties) →
    # its rate-limit stamp is dropped, so a RE-degradation alerts immediately.
    recovered = _with_last_signal(rows, last_signal=_NOW + timedelta(hours=1))
    wd._pg = _FakePg(recovered)  # type: ignore[attr-defined]
    assert await wd.check_source_empty_streak_once(now_monotonic=1000.0 + 3700.0) == []
    wd._pg = _FakePg(rows)  # type: ignore[attr-defined]  # degrades again
    assert await wd.check_source_empty_streak_once(now_monotonic=1000.0 + 3800.0) == ["source.xinhua.world"]
    assert len(nats.published) == 3


# ---------------------------------------------------------------------------
# B0-12 (2026-07-14): a GLOBAL stall now ALSO persists a durable
# alert_sink_deliveries row so the ~13h silent-outage class is operator-visible
# (escalations panel + W1-T3 canary) instead of evaporating on the streamless
# NATS subject.
# ---------------------------------------------------------------------------


class _RecConn:
    def __init__(self, pg) -> None:
        self._pg = pg

    async def execute(self, sql, *args):
        self._pg.executed.append((sql, args))
        if self._pg.raise_on_execute:
            raise RuntimeError("boom")


class _RecAcquire:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _RecordingPg:
    """Fake pg pool exposing .acquire() → conn.execute (records the INSERT)."""

    def __init__(self, raise_on_execute: bool = False) -> None:
        self.executed: list[tuple] = []
        self.raise_on_execute = raise_on_execute

    def acquire(self):
        return _RecAcquire(_RecConn(self))


def _watchdog_with_pg(pg, start_at: float = 0.0):
    nats = _RecordingNats()
    cfg = WatchdogConfig(stall_after_s=900.0, realert_every_s=1800.0, check_interval_s=60.0)
    wd = LivenessWatchdog(nats, cfg, pg_store=pg)
    wd._started_at = start_at  # type: ignore[attr-defined]
    return wd, nats


@pytest.mark.asyncio
async def test_stall_writes_durable_delivery_row() -> None:
    pg = _RecordingPg()
    wd, nats = _watchdog_with_pg(pg)
    wd._last_finding_at = 100.0  # type: ignore[attr-defined]
    fired = await wd.check_once(now=100.0 + 901.0)
    assert fired is True
    assert len(nats.published) == 1                 # NATS alert still fires
    assert len(pg.executed) == 1                    # + one durable row
    sql, args = pg.executed[0]
    assert "INSERT INTO alert_sink_deliveries" in sql
    assert args[1] == "liveness_watchdog"           # sink_kind
    assert args[3] == "high"                        # severity
    assert args[4] == "logged_only"                 # status (durable, not delivered)
    payload = json.loads(args[5])
    assert payload["kind"] == "pipeline_stall"
    assert payload["idle_minutes"] >= 15.0


@pytest.mark.asyncio
async def test_stall_delivery_write_is_fail_safe() -> None:
    # A DB write error must NEVER crash the watchdog loop — the whole point is to
    # observe stalls, not add one.
    pg = _RecordingPg(raise_on_execute=True)
    wd, nats = _watchdog_with_pg(pg)
    wd._last_finding_at = 100.0  # type: ignore[attr-defined]
    fired = await wd.check_once(now=100.0 + 901.0)   # must not raise
    assert fired is True
    assert len(nats.published) == 1                  # NATS alert still fires


@pytest.mark.asyncio
async def test_stall_without_pg_still_alerts_no_durable_write() -> None:
    wd, nats = _watchdog(start_at=0.0)               # no pg wired
    wd._last_finding_at = 100.0  # type: ignore[attr-defined]
    fired = await wd.check_once(now=100.0 + 901.0)
    assert fired is True and len(nats.published) == 1  # NATS-only path unchanged
