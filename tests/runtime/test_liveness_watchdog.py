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
    """Conn double: routes fetches (the B0-12 alert-state seed reads
    ``alert_sink_deliveries``; every other fetch gets the check rows),
    records executes (the durable delivery-row INSERTs)."""

    def __init__(self, pg):
        self._pg = pg

    async def fetch(self, sql, *args, **_k):
        if "alert_sink_deliveries" in sql:
            return list(self._pg.state_rows)
        return list(self._pg.rows)

    async def execute(self, sql, *args):
        if self._pg.fail_execute:
            raise RuntimeError("boom-durable-write")
        self._pg.executed.append((sql, args))


class _FakePg:
    """Mutable pg-pool double. ``rows`` (the check-query result) and
    ``state_rows`` (the B0-12 alert-state seed result) are plain attributes —
    tests MUTATE them between checks to simulate condition changes on the
    same watchdog (the seeded state map lives on the watchdog, not the pool).
    ``executed`` accumulates every durable INSERT."""

    def __init__(self, rows, *, state_rows=None, fail_execute=False):
        self.rows = rows
        self.state_rows = state_rows or []
        self.executed: list[tuple] = []
        self.fail_execute = fail_execute

    def acquire(self):
        pg = self

        class _Acq:
            async def __aenter__(self_inner):
                return _FakeConn(pg)

            async def __aexit__(self_inner, *exc):
                return False

        return _Acq()


def _delivery_rows(pg: _FakePg) -> list[dict]:
    """Decode every captured durable alert_sink_deliveries INSERT by the
    column order in LivenessWatchdog._insert_delivery_row."""
    rows = []
    for sql, args in pg.executed:
        if "alert_sink_deliveries" in sql:
            rows.append(
                {
                    "channel": args[0],
                    "sink_kind": args[1],
                    "target": args[2],
                    "severity": args[3],
                    "status": args[4],
                    "payload": json.loads(args[5]),
                }
            )
    return rows


class _RecordingSinks:
    """P1-1 dispatcher double — records fan-out payloads."""

    def __init__(self):
        self.payloads = []

    async def fan_out(self, payload):
        self.payloads.append(payload)


def _cadence_watchdog(rows, *, is_leader=None, state_rows=None, alert_sinks=None):
    nats = _RecordingNats()
    cfg = WatchdogConfig(
        stall_after_s=900.0, realert_every_s=1800.0,
        check_interval_s=60.0, cadence_stall_factor=2.0,
    )
    pg = _FakePg(rows, state_rows=state_rows)
    wd = LivenessWatchdog(
        nats, cfg, pg_store=pg, is_leader=is_leader, alert_sinks=alert_sinks,
    )
    wd._started_at = 0.0  # type: ignore[attr-defined]
    return wd, nats, pg


@pytest.mark.asyncio
async def test_cadence_check_emits_per_analyst_alert_via_core() -> None:
    rows = [_row("world_assessor", "0 */6 * * *", _NOW - timedelta(hours=25))]
    wd, nats, _pg = _cadence_watchdog(rows)
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
async def test_cadence_check_boot_grace_and_transition_edge() -> None:
    # B0-12: alerting is a state-transition EDGE, not a level — an ongoing
    # condition fires exactly ONCE, including past the old heartbeat window.
    rows = [_row("world_assessor", "0 */6 * * *", _NOW - timedelta(hours=25))]
    wd, nats, _pg = _cadence_watchdog(rows)
    # Inside the boot grace (< stall_after_s) → no alert yet.
    assert await wd.check_analyst_cadence_once(now_monotonic=500.0) == []
    assert nats.published == []
    # Past grace → alert once on ENTRY...
    assert await wd.check_analyst_cadence_once(now_monotonic=1000.0) == ["world_assessor"]
    # ...then SILENT while the condition persists — even far past the global
    # realert window (the old heartbeat re-fired here; the durable edge does
    # not: the ~1.2k-ERROR-lines/day degraded-set spam class).
    assert await wd.check_analyst_cadence_once(now_monotonic=1500.0) == []
    assert await wd.check_analyst_cadence_once(now_monotonic=1000.0 + 1801.0) == []
    assert await wd.check_analyst_cadence_once(now_monotonic=1000.0 + 7200.0) == []
    assert len(nats.published) == 1


@pytest.mark.asyncio
async def test_cadence_check_leader_gated() -> None:
    rows = [_row("world_assessor", "0 */6 * * *", _NOW - timedelta(hours=25))]
    wd, nats, _pg = _cadence_watchdog(rows, is_leader=lambda: False)
    assert await wd.check_analyst_cadence_once(now_monotonic=1000.0) == []
    assert nats.published == []


@pytest.mark.asyncio
async def test_cadence_transition_entry_recovery_reentry() -> None:
    # B0-12: entry fires once (durable 'entered' row, severity high); recovery
    # fires once (durable 'recovered' row, severity info, NO streamless NATS
    # publish); a NEW episode after recovery alerts anew — immediately.
    stale = [_row("world_assessor", "0 */6 * * *", _NOW - timedelta(hours=25))]
    # The check evaluates staleness against the REAL wall clock — "fresh"
    # must be fresh relative to now(), not the fixed _NOW fixture time.
    fresh = [_row(
        "world_assessor", "0 */6 * * *",
        datetime.now(tz=timezone.utc) - timedelta(hours=3),
    )]
    sinks = _RecordingSinks()
    wd, nats, pg = _cadence_watchdog(stale, alert_sinks=sinks)

    # ENTRY
    assert await wd.check_analyst_cadence_once(now_monotonic=1000.0) == ["world_assessor"]
    rows = _delivery_rows(pg)
    assert len(rows) == 1
    assert rows[0]["channel"] == "analyst_cadence_stall"
    assert rows[0]["sink_kind"] == "liveness_watchdog"
    assert rows[0]["target"] == "world_assessor"
    assert rows[0]["severity"] == "high"
    assert rows[0]["status"] == "logged_only"
    assert rows[0]["payload"]["state"] == "entered"
    assert rows[0]["payload"]["kind"] == "analyst_cadence_stall"
    assert len(sinks.payloads) == 1
    assert sinks.payloads[0].channel_name == "analyst_cadence_stall"
    assert sinks.payloads[0].severity == "high"

    # RECOVERY — the analyst runs again (rows go fresh).
    pg.rows = fresh
    assert await wd.check_analyst_cadence_once(now_monotonic=1100.0) == []
    rows = _delivery_rows(pg)
    assert len(rows) == 2
    assert rows[1]["payload"]["state"] == "recovered"
    assert rows[1]["severity"] == "info"
    assert rows[1]["target"] == "world_assessor"
    assert len(sinks.payloads) == 2
    assert sinks.payloads[1].severity == "info"
    assert len(nats.published) == 1  # recovery does NOT publish streamless

    # Recovery is itself an edge — no repeat 'recovered' rows.
    assert await wd.check_analyst_cadence_once(now_monotonic=1200.0) == []
    assert len(_delivery_rows(pg)) == 2

    # RE-ENTRY — a new episode alerts immediately (pure edge, no flap floor).
    pg.rows = stale
    assert await wd.check_analyst_cadence_once(now_monotonic=1300.0) == ["world_assessor"]
    rows = _delivery_rows(pg)
    assert len(rows) == 3
    assert rows[2]["payload"]["state"] == "entered"
    assert len(nats.published) == 2


@pytest.mark.asyncio
async def test_cadence_transition_state_seeded_from_durable_ledger() -> None:
    # B0-12 restart survival: a fresh watchdog (a rebooted runtime) seeds its
    # last-alerted state from the durable ledger — an ongoing, already-alerted
    # condition must NOT re-fire; its RECOVERY must still fire.
    stale = [_row("world_assessor", "0 */6 * * *", _NOW - timedelta(hours=25))]
    seeded = [{
        "channel_name": "analyst_cadence_stall",
        "sink_target": "world_assessor",
        "state": "entered",
    }]
    wd, nats, pg = _cadence_watchdog(stale, state_rows=seeded)
    # Condition persists across the "restart" → no re-fire.
    assert await wd.check_analyst_cadence_once(now_monotonic=1000.0) == []
    assert nats.published == []
    assert _delivery_rows(pg) == []
    # The analyst recovers → the recovery edge fires off the SEEDED state.
    # (Fresh relative to the REAL wall clock the check evaluates against.)
    pg.rows = [_row(
        "world_assessor", "0 */6 * * *",
        datetime.now(tz=timezone.utc) - timedelta(hours=3),
    )]
    assert await wd.check_analyst_cadence_once(now_monotonic=1100.0) == []
    rows = _delivery_rows(pg)
    assert len(rows) == 1
    assert rows[0]["payload"]["state"] == "recovered"


@pytest.mark.asyncio
async def test_cadence_durable_write_failure_still_alerts() -> None:
    # Fail-safe: a broken durable write must neither raise nor kill the NATS
    # alert; the in-memory edge still holds (no 60s firehose this process).
    stale = [_row("world_assessor", "0 */6 * * *", _NOW - timedelta(hours=25))]
    wd, nats, pg = _cadence_watchdog(stale)
    pg.fail_execute = True
    assert await wd.check_analyst_cadence_once(now_monotonic=1000.0) == ["world_assessor"]
    assert len(nats.published) == 1
    # Ongoing condition stays silent on the in-memory state alone.
    assert await wd.check_analyst_cadence_once(now_monotonic=1100.0) == []
    assert len(nats.published) == 1


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
    wd, nats, pg = _cadence_watchdog(rows)
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
    # B0-12: the entry edge lands a durable row on its own channel.
    rows_d = _delivery_rows(pg)
    assert len(rows_d) == 1
    assert rows_d[0]["channel"] == "source_cadence_stall"
    assert rows_d[0]["target"] == "source.xinhua.world"
    assert rows_d[0]["severity"] == "high"
    assert rows_d[0]["payload"]["state"] == "entered"


@pytest.mark.asyncio
async def test_source_cadence_check_leader_gated_and_edge_deduped() -> None:
    rows = [_src_row("source.xinhua.world", "18 * * * *", _NOW - timedelta(days=12))]
    # Leader-gated off → silent.
    wd_off, nats_off, _pg_off = _cadence_watchdog(rows, is_leader=lambda: False)
    assert await wd_off.check_source_cadence_once(now_monotonic=1000.0) == []
    assert nats_off.published == []
    # Leader on → alert once on entry, then silent on the ongoing condition.
    wd, nats, _pg = _cadence_watchdog(rows)
    assert await wd.check_source_cadence_once(now_monotonic=1000.0) == ["source.xinhua.world"]
    assert await wd.check_source_cadence_once(now_monotonic=1500.0) == []
    assert await wd.check_source_cadence_once(now_monotonic=1000.0 + 1801.0) == []
    assert len(nats.published) == 1


# ---------------------------------------------------------------------------
# B0-12: honest-quiet discriminator gate on the source cadence check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_cadence_honest_quiet_stays_silent() -> None:
    # A cadence-stale source whose freshest poll evidence shows the FEED is
    # quiet (clean empty poll; newest observed upstream entry <= our newest
    # ingested signal) is a slow news day, NOT a fault → no alert. This was
    # 7/8 of the B0-11 "stalled cluster" false positives (weekly feeds).
    last_signal = _NOW - timedelta(days=12)
    rows = [
        _src_row_full(
            "source.weekly.review", "18 * * * *", last_signal,
            last_poll_outcome="empty", last_poll_health="healthy",
            last_poll_error=None, last_poll_at=_NOW - timedelta(hours=1),
            last_poll_newest_entry_ts=last_signal - timedelta(days=1),
        )
    ]
    wd, nats, pg = _cadence_watchdog(rows)
    assert await wd.check_source_cadence_once(now_monotonic=1000.0) == []
    assert nats.published == []
    assert _delivery_rows(pg) == []


@pytest.mark.asyncio
async def test_source_cadence_newer_upstream_still_alerts() -> None:
    # Same stale source, but the feed OBSERVABLY carries entries newer than
    # our last ingest → not honest-quiet → the stall alert stands.
    last_signal = _NOW - timedelta(days=12)
    rows = [
        _src_row_full(
            "source.eaten.feed", "18 * * * *", last_signal,
            last_poll_outcome="empty", last_poll_health="healthy",
            last_poll_error=None, last_poll_at=_NOW - timedelta(hours=1),
            last_poll_newest_entry_ts=_NOW - timedelta(hours=2),
        )
    ]
    wd, nats, _pg = _cadence_watchdog(rows)
    assert await wd.check_source_cadence_once(now_monotonic=1000.0) == ["source.eaten.feed"]
    assert len(nats.published) == 1


@pytest.mark.asyncio
async def test_source_cadence_honest_quiet_counts_as_recovery() -> None:
    # A previously-alerted stalled source whose evidence now classifies
    # honest-quiet closes its episode (recovery edge fires once).
    last_signal = _NOW - timedelta(days=12)
    stale_no_evidence = [
        _src_row("source.weekly.review", "18 * * * *", last_signal)
    ]
    wd, nats, pg = _cadence_watchdog(stale_no_evidence)
    assert await wd.check_source_cadence_once(now_monotonic=1000.0) == ["source.weekly.review"]
    pg.rows = [
        _src_row_full(
            "source.weekly.review", "18 * * * *", last_signal,
            last_poll_outcome="empty", last_poll_health="healthy",
            last_poll_error=None, last_poll_at=_NOW,
            last_poll_newest_entry_ts=last_signal - timedelta(days=1),
        )
    ]
    assert await wd.check_source_cadence_once(now_monotonic=1100.0) == []
    rows_d = _delivery_rows(pg)
    assert rows_d[-1]["payload"]["state"] == "recovered"
    assert rows_d[-1]["severity"] == "info"


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
    wd, nats, _pg = _cadence_watchdog(rows)
    assert await wd.check_source_cadence_once(now_monotonic=1000.0) == ["source.xinhua.world"]
    env = json.loads(nats.published_core[0][1])
    assert "FAILED" in env["body"]
    assert "HTTP 500" in env["body"]


@pytest.mark.asyncio
async def test_source_cadence_alert_body_flags_missing_outcome_row() -> None:
    # Stale source with NO recorded poll outcome → body points at the reminder.
    rows = [_src_row("source.xinhua.world", "18 * * * *", _NOW - timedelta(days=12))]
    wd, nats, _pg = _cadence_watchdog(rows)
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
    # No newest_entry_ts evidence on any row → fault_class 'unknown' (the
    # pre-B0-12 degraded escalation is preserved).
    rows = _empty_run("source.xinhua.world", 8)
    degraded = _evaluate_empty_streaks(rows, threshold=5)
    assert [d[0] for d in degraded] == ["source.xinhua.world"]
    sid, streak, newest_at, fault_class = degraded[0]
    assert streak == 8
    assert newest_at == _NOW  # the most-recent empty row's timestamp
    assert fault_class == "unknown"


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


# ---------------------------------------------------------------------------
# B0-12: the newest_entry_ts precision discriminator — truth table
# (quiet vs cursor-fault vs no-evidence vs healthy)
# ---------------------------------------------------------------------------


def _with_newest_entry(rows, newest_entry_ts):
    return [{**r, "newest_entry_ts": newest_entry_ts} for r in rows]


def test_discriminator_honest_quiet_stays_silent() -> None:
    # Feed 200s, streak over threshold, but every observed upstream entry is
    # <= our newest ingested signal → the source is just QUIET. NO alert —
    # this kills the false-quiet class (7/8 of the B0-11 cluster).
    last_signal = _NOW - timedelta(days=20)
    rows = _with_last_signal(
        _with_newest_entry(
            _empty_run("source.weekly.review", 8),
            newest_entry_ts=last_signal - timedelta(days=2),
        ),
        last_signal=last_signal,
    )
    assert _evaluate_empty_streaks(rows, threshold=5) == []


def test_discriminator_cursor_fault_escalates() -> None:
    # Feed 200s AND carries entries NEWER than our last ingest, yet 0 yielded
    # across the run → the cursor/filter is eating them (the B0-11 stategov
    # cursor-poison class) → escalate as 'cursor_fault'.
    last_signal = _NOW - timedelta(days=20)
    rows = _with_last_signal(
        _with_newest_entry(
            _empty_run("source.stategov.press", 8),
            newest_entry_ts=_NOW - timedelta(hours=3),  # newer than last ingest
        ),
        last_signal=last_signal,
    )
    degraded = _evaluate_empty_streaks(rows, threshold=5)
    assert [(d[0], d[3]) for d in degraded] == [("source.stategov.press", "cursor_fault")]
    assert degraded[0][1] == 8


def test_discriminator_no_evidence_keeps_degraded() -> None:
    # newest_entry_ts absent/None on every row (handler doesn't record it,
    # pre-B0-12 rows) → 'unknown' → the pre-existing degraded escalation.
    last_signal = _NOW - timedelta(days=20)
    rows = _with_last_signal(
        _with_newest_entry(_empty_run("source.dead.feed", 6), newest_entry_ts=None),
        last_signal=last_signal,
    )
    degraded = _evaluate_empty_streaks(rows, threshold=5)
    assert [(d[0], d[3]) for d in degraded] == [("source.dead.feed", "unknown")]


def test_discriminator_never_ingested_with_upstream_entries_is_cursor_fault() -> None:
    # A source that has NEVER produced a signal while the feed observably
    # carries dated entries: the filter chain is eating everything → fault.
    rows = _with_last_signal(
        _with_newest_entry(
            _empty_run("source.never.yielded", 6),
            newest_entry_ts=_NOW - timedelta(hours=2),
        ),
        last_signal=None,
    )
    degraded = _evaluate_empty_streaks(rows, threshold=5)
    assert [(d[0], d[3]) for d in degraded] == [("source.never.yielded", "cursor_fault")]


def test_discriminator_healthy_producing_source_not_flagged() -> None:
    # Producing again (signal newer than the whole empty run) → no flag at
    # all, regardless of the observation evidence.
    rows = _with_last_signal(
        _with_newest_entry(
            _empty_run("source.reuters.world", 8),
            newest_entry_ts=_NOW - timedelta(hours=1),
        ),
        last_signal=_NOW + timedelta(hours=1),
    )
    assert _evaluate_empty_streaks(rows, threshold=5) == []


def test_discriminator_uses_newest_observation_in_the_run() -> None:
    # Evidence is aggregated across the run: one poll observing a fresh
    # upstream entry is enough to classify the run cursor_fault even when the
    # other polls carry older/no observations.
    last_signal = _NOW - timedelta(days=20)
    base = _empty_run("source.mixed.evidence", 6)
    base[0]["newest_entry_ts"] = None
    base[1]["newest_entry_ts"] = last_signal - timedelta(days=1)   # quiet-looking
    base[2]["newest_entry_ts"] = _NOW - timedelta(hours=4)         # the fresh one
    rows = _with_last_signal(base, last_signal=last_signal)
    degraded = _evaluate_empty_streaks(rows, threshold=5)
    assert [(d[0], d[3]) for d in degraded] == [("source.mixed.evidence", "cursor_fault")]


def _streak_watchdog(rows, *, is_leader=None, state_rows=None, alert_sinks=None):
    nats = _RecordingNats()
    cfg = WatchdogConfig(
        stall_after_s=900.0, realert_every_s=1800.0,
        check_interval_s=60.0, empty_streak_threshold=5,
    )
    pg = _FakePg(rows, state_rows=state_rows)
    wd = LivenessWatchdog(
        nats, cfg, pg_store=pg, is_leader=is_leader, alert_sinks=alert_sinks,
    )
    wd._started_at = 0.0  # type: ignore[attr-defined]
    return wd, nats, pg


@pytest.mark.asyncio
async def test_empty_streak_check_emits_degraded_alert() -> None:
    rows = _empty_run("source.aljazeera.arabic", 6)
    wd, nats, pg = _streak_watchdog(rows)
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
    assert env["fault_class"] == "unknown"
    assert env["stale_source_id"] == "source.aljazeera.arabic"
    # Must go out via core publish (legba.alerts.* has no JetStream stream).
    assert nats.published_json == []
    # B0-12: the durable entry row on the source_degraded channel.
    rows_d = _delivery_rows(pg)
    assert len(rows_d) == 1
    assert rows_d[0]["channel"] == "source_degraded"
    assert rows_d[0]["target"] == "source.aljazeera.arabic"
    assert rows_d[0]["severity"] == "high"
    assert rows_d[0]["status"] == "logged_only"
    assert rows_d[0]["payload"]["state"] == "entered"
    assert rows_d[0]["payload"]["kind"] == "source_empty_degraded"
    assert rows_d[0]["payload"]["empty_streak"] == 6


@pytest.mark.asyncio
async def test_empty_streak_cursor_fault_alert_shape() -> None:
    # A cursor-fault run escalates with the discriminating anatomy: the
    # cursor_fault tag/kind and a body naming the eaten-fresh-content fact.
    last_signal = _NOW - timedelta(days=20)
    rows = _with_last_signal(
        [{**r, "newest_entry_ts": _NOW - timedelta(hours=3)}
         for r in _empty_run("source.stategov.press", 6)],
        last_signal=last_signal,
    )
    sinks = _RecordingSinks()
    wd, nats, pg = _streak_watchdog(rows, alert_sinks=sinks)
    assert await wd.check_source_empty_streak_once(now_monotonic=1000.0) == ["source.stategov.press"]
    env = json.loads(nats.published_core[0][1])
    assert env["fault_class"] == "cursor_fault"
    assert "cursor_fault" in env["tags"]
    assert "cursor/filter fault" in env["title"]
    rows_d = _delivery_rows(pg)
    assert rows_d[0]["payload"]["kind"] == "source_cursor_fault"
    assert rows_d[0]["payload"]["fault_class"] == "cursor_fault"
    assert rows_d[0]["severity"] == "high"
    assert len(sinks.payloads) == 1
    assert sinks.payloads[0].severity == "high"


@pytest.mark.asyncio
async def test_empty_streak_honest_quiet_no_alert_at_check_level() -> None:
    # End-to-end through the check: an honest-quiet run produces NOTHING —
    # no NATS publish, no durable row, no fan-out.
    last_signal = _NOW - timedelta(days=20)
    rows = _with_last_signal(
        [{**r, "newest_entry_ts": last_signal - timedelta(days=1)}
         for r in _empty_run("source.weekly.review", 6)],
        last_signal=last_signal,
    )
    sinks = _RecordingSinks()
    wd, nats, pg = _streak_watchdog(rows, alert_sinks=sinks)
    assert await wd.check_source_empty_streak_once(now_monotonic=1000.0) == []
    assert nats.published == []
    assert _delivery_rows(pg) == []
    assert sinks.payloads == []


@pytest.mark.asyncio
async def test_empty_streak_check_boot_grace_leader_and_transition_edge() -> None:
    rows = _empty_run("source.xinhua.world", 6)
    # Leader-gated off → silent.
    wd_off, nats_off, _pg_off = _streak_watchdog(rows, is_leader=lambda: False)
    assert await wd_off.check_source_empty_streak_once(now_monotonic=1000.0) == []
    assert nats_off.published == []

    wd, nats, pg = _streak_watchdog(rows)
    # Inside boot grace → no alert.
    assert await wd.check_source_empty_streak_once(now_monotonic=500.0) == []
    # Past grace → alert once on ENTRY, then silence for the CONTINUING
    # episode — there is NO heartbeat any more (B0-12: the durable row is the
    # standing record; re-escalating a slow-moving fact every interval was
    # the ~1.2k-ERROR-lines/day spam class).
    assert await wd.check_source_empty_streak_once(now_monotonic=1000.0) == ["source.xinhua.world"]
    assert await wd.check_source_empty_streak_once(now_monotonic=1500.0) == []
    assert await wd.check_source_empty_streak_once(now_monotonic=1000.0 + 1801.0) == []
    assert await wd.check_source_empty_streak_once(now_monotonic=1000.0 + 7200.0) == []
    assert len(nats.published) == 1
    # EPISODE CLOSE: the source produces again → the recovery edge fires one
    # durable 'recovered' row (info), no streamless publish.
    pg.rows = _with_last_signal(rows, last_signal=_NOW + timedelta(hours=1))
    assert await wd.check_source_empty_streak_once(now_monotonic=1000.0 + 7300.0) == []
    rows_d = _delivery_rows(pg)
    assert rows_d[-1]["payload"]["state"] == "recovered"
    assert rows_d[-1]["severity"] == "info"
    assert len(nats.published) == 1
    # RE-degradation = a NEW episode → alerts immediately (edge, no floor).
    pg.rows = rows
    assert await wd.check_source_empty_streak_once(now_monotonic=1000.0 + 7400.0) == ["source.xinhua.world"]
    assert len(nats.published) == 2


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
