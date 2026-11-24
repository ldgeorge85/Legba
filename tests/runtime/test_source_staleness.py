# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W3 SRC-2 — the staleness tripwire.

* ``staleness_state`` — every state boundary, ``unknown`` (never delivered / no
  cadence), the weekday-only business-time rule (a Monday morning after a quiet
  weekend reads fresh), the reason text.
* the honest-quiet discriminator and the trip policy (``should_alert``).
* the publishing profile (observed interval, weekday-only) and the expected
  interval.
* the TRIP: a stale-feed fixture trips the watchdog once per transition (not
  per poll), a dead one trips its own tier, recovery fires once, absence is not
  recovery, a leader-gated / boot-graced watchdog is silent.
* the real-driver read: ``get_source_health`` rows carry ``staleness`` and
  ``feed_has_no_new_items`` agrees with it; the ``/v3/source-quality`` route
  attaches the same block to an active source.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from legba.runtime import source_staleness as st
from legba.runtime import source_staleness_watch as watch
from legba.runtime.liveness_watchdog import LivenessWatchdog, WatchdogConfig

H = 3600.0
NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)  # a Saturday
MONDAY_0100 = datetime(2026, 10, 5, 1, 0, 0, tzinfo=timezone.utc)
FRIDAY_2200 = datetime(2026, 10, 2, 22, 0, 0, tzinfo=timezone.utc)


def _state(age_h: float, expected_s: float = H, **kw):
    return st.staleness_state(
        NOW - timedelta(hours=age_h), NOW - timedelta(minutes=5), expected_s, NOW, **kw
    )


# ---------------------------------------------------------------------------
# State boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "age_h,expected_state",
    [
        (0.0, "fresh"),
        (2.99, "fresh"),
        (3.0, "quiet"),
        (7.99, "quiet"),
        (8.0, "stale"),
        (23.99, "stale"),
        (24.0, "dead"),
        (500.0, "dead"),
    ],
)
def test_every_state_boundary(age_h: float, expected_state: str) -> None:
    out = _state(age_h)
    assert out["state"] == expected_state
    assert out["factor"] == pytest.approx(age_h, abs=0.06)


def test_constants_are_the_documented_multiples() -> None:
    assert (st.QUIET_FACTOR, st.STALE_FACTOR, st.DEAD_FACTOR) == (3.0, 8.0, 24.0)


def test_factor_scales_with_the_expected_interval() -> None:
    # 9 h old against a 1 h interval is 9x (stale); against a 6 h interval it is
    # 1.5x (fresh) — the same age, a different feed.
    assert _state(9.0, expected_s=H)["state"] == "stale"
    assert _state(9.0, expected_s=6 * H)["state"] == "fresh"


def test_stale_reason_names_factor_instant_and_polling_health() -> None:
    out = _state(9.2)
    assert out["state"] == "stale"
    assert out["factor"] == 9.2
    assert "9.2×" in out["reason"]
    assert out["last_new_item_at"] in out["reason"]
    assert "polling is clean — the upstream is not delivering" in out["reason"]


def test_reason_distinguishes_a_poller_that_is_itself_behind() -> None:
    out = st.staleness_state(
        NOW - timedelta(hours=12), NOW - timedelta(hours=11), H, NOW
    )
    assert out["state"] == "stale"
    assert "the poller itself is behind" in out["reason"]


def test_reason_when_no_clean_poll_is_on_record() -> None:
    out = st.staleness_state(NOW - timedelta(hours=12), None, H, NOW)
    assert "no clean poll on record" in out["reason"]


def test_reason_when_upstream_is_honestly_quiet() -> None:
    out = _state(12.0, upstream_quiet=True)
    assert out["state"] == "stale"
    assert "upstream carries nothing newer" in out["reason"]
    assert out["upstream_quiet"] is True


def test_naive_datetimes_are_read_as_utc() -> None:
    out = st.staleness_state(
        (NOW - timedelta(hours=9)).replace(tzinfo=None), None, H, NOW.replace(tzinfo=None)
    )
    assert out["state"] == "stale"


# ---------------------------------------------------------------------------
# unknown — absence is absence, never 0
# ---------------------------------------------------------------------------


def test_unknown_when_the_source_never_delivered() -> None:
    out = st.staleness_state(None, NOW, H, NOW)
    assert out["state"] == "unknown"
    assert out["factor"] is None
    assert out["age_s"] is None
    assert out["last_new_item_at"] is None
    assert "ever been recorded" in out["reason"]


@pytest.mark.parametrize("expected", [None, 0, -5.0])
def test_unknown_when_there_is_no_cadence_to_measure_against(expected) -> None:
    out = st.staleness_state(NOW - timedelta(hours=40), NOW, expected, NOW)
    assert out["state"] == "unknown"
    assert out["factor"] is None
    assert out["expected_interval_s"] is None


# ---------------------------------------------------------------------------
# weekday_only — business time
# ---------------------------------------------------------------------------


def test_business_elapsed_skips_the_weekend() -> None:
    # Friday 22:00 -> Monday 01:00 = 2 h of Friday + 1 h of Monday.
    assert st.business_elapsed_s(FRIDAY_2200, MONDAY_0100) == pytest.approx(3 * H)
    # Entirely inside the weekend: nothing elapsed.
    sat_noon = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    sun_noon = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    assert st.business_elapsed_s(sat_noon, sun_noon) == 0.0
    # An ordinary Tuesday -> Wednesday is the full 24 h.
    tue = datetime(2026, 10, 6, 9, tzinfo=timezone.utc)
    assert st.business_elapsed_s(tue, tue + timedelta(days=1)) == pytest.approx(24 * H)
    # Reversed / equal spans are zero, never negative.
    assert st.business_elapsed_s(MONDAY_0100, FRIDAY_2200) == 0.0


def test_weekday_only_feed_on_a_monday_morning_after_a_quiet_weekend_is_fresh() -> None:
    # Last item Friday 22:00; a 2 h-interval weekday publisher; Monday 01:00 UTC.
    kw = dict(expected_interval_s=2 * H, now=MONDAY_0100)
    weekday = st.staleness_state(FRIDAY_2200, MONDAY_0100, weekday_only=True, **kw)
    assert weekday["state"] == "fresh"
    assert weekday["factor"] == 1.5
    assert weekday["age_s"] == pytest.approx(3 * H)
    assert "weekday time" in weekday["reason"]
    # The same instants on the wall clock (75 h = 37.5x) would be DEAD — the
    # flag is what keeps the weekend from tripping it.
    wall = st.staleness_state(FRIDAY_2200, MONDAY_0100, weekday_only=False, **kw)
    assert wall["state"] == "dead"


def test_weekday_only_still_trips_a_genuine_weekday_outage() -> None:
    # Last item Monday 09:00, now Wednesday 09:00: 48 h of weekday against 2 h.
    mon = datetime(2026, 10, 5, 9, tzinfo=timezone.utc)
    wed = datetime(2026, 10, 7, 9, tzinfo=timezone.utc)
    out = st.staleness_state(mon, wed, 2 * H, wed, weekday_only=True)
    assert out["state"] == "dead"


# ---------------------------------------------------------------------------
# honest-quiet + the trip policy
# ---------------------------------------------------------------------------


def test_feed_has_no_new_items_matches_the_b9_rule() -> None:
    last = NOW - timedelta(hours=60)
    assert st.feed_has_no_new_items("empty", last - timedelta(seconds=30), last) is True
    # within the 5 min clock-skew slack
    assert st.feed_has_no_new_items("empty", last + timedelta(minutes=4), last) is True
    # upstream carries newer entries -> an ingestion fault, not a quiet feed
    assert st.feed_has_no_new_items("empty", last + timedelta(hours=2), last) is False
    assert st.feed_has_no_new_items("error", last, last) is False
    assert st.feed_has_no_new_items("empty", None, last) is False
    assert st.feed_has_no_new_items("empty", last, None) is False


def test_should_alert_policy() -> None:
    assert st.should_alert({"state": "dead", "upstream_quiet": True}) == "dead"
    assert st.should_alert({"state": "dead", "upstream_quiet": False}) == "dead"
    assert st.should_alert({"state": "stale", "upstream_quiet": False}) == "stale"
    assert st.should_alert({"state": "stale", "upstream_quiet": None}) == "stale"
    # an honestly-quiet feed does not trip at stale (it escalates at dead)
    assert st.should_alert({"state": "stale", "upstream_quiet": True}) is None
    for state in ("fresh", "quiet", "unknown"):
        assert st.should_alert({"state": state, "upstream_quiet": False}) is None


# ---------------------------------------------------------------------------
# publishing profile + expected interval
# ---------------------------------------------------------------------------


def _profile_row(n, hours, weekend, span_h):
    first = NOW - timedelta(hours=span_h)
    return {
        "n_items": n, "active_hours": hours, "weekend_items": weekend,
        "first_at": first, "last_at": NOW,
    }


def test_profile_observed_interval_is_the_gap_between_active_hours() -> None:
    p = st.profile_from_row(_profile_row(n=120, hours=101, weekend=30, span_h=100))
    assert p["observed_interval_s"] == pytest.approx(H, rel=0.02)
    # a burst publisher: twenty items in one hour, once a day for 28 days
    burst = st.profile_from_row(_profile_row(n=560, hours=28, weekend=160, span_h=27 * 24))
    assert burst["observed_interval_s"] == pytest.approx(24 * H, rel=0.02)


def test_profile_sparse_and_single_item_feeds() -> None:
    # two items two weeks apart: a two-week rhythm, not the 6 h poll
    sparse = st.profile_from_row(_profile_row(n=2, hours=2, weekend=0, span_h=14 * 24))
    assert sparse["observed_interval_s"] == pytest.approx(14 * 24 * H)
    one = st.profile_from_row(_profile_row(n=1, hours=1, weekend=0, span_h=0))
    assert one["observed_interval_s"] == pytest.approx(28 * 24 * H)
    none = st.profile_from_row(_profile_row(n=0, hours=0, weekend=0, span_h=0))
    assert none["observed_interval_s"] is None
    # several items inside ONE hour: a backlog burst, no rhythm to read
    burst = st.profile_from_row(_profile_row(n=5, hours=1, weekend=0, span_h=0))
    assert burst["observed_interval_s"] is None


def test_weekday_only_detection() -> None:
    # 24/7 feed: ~28% weekend share
    assert st.profile_from_row(_profile_row(100, 90, 28, 600))["weekday_only"] is False
    # weekday publisher: 2% weekend share
    assert st.profile_from_row(_profile_row(100, 90, 2, 600))["weekday_only"] is True
    # too few items to claim it
    assert st.profile_from_row(_profile_row(8, 8, 0, 600))["weekday_only"] is False


def test_expected_interval_is_the_larger_of_poll_period_and_observed() -> None:
    hourly = "7 * * * *"
    assert st.poll_period_s(hourly, NOW) == pytest.approx(H)
    assert st.poll_period_s("*/15 * * * *", NOW) == pytest.approx(900)
    # a thin feed polled every 15 min but publishing every 6 h -> 6 h
    assert st.expected_interval_s("*/15 * * * *", {"observed_interval_s": 6 * H}, NOW) == 6 * H
    # a busy feed polled hourly -> the poll period
    assert st.expected_interval_s(hourly, {"observed_interval_s": 600.0}, NOW) == pytest.approx(H)
    # invalid / absent cron falls back to the observed rhythm; nothing -> None
    assert st.expected_interval_s("not a cron", {"observed_interval_s": 5 * H}, NOW) == 5 * H
    assert st.expected_interval_s(None, None, NOW) is None


# ---------------------------------------------------------------------------
# THE TRIP — the watchdog alerts once per transition
# ---------------------------------------------------------------------------


class _Nats:
    def __init__(self) -> None:
        self.published_core: list[tuple[str, bytes]] = []

    async def publish_core(self, subject: str, payload: bytes) -> None:
        self.published_core.append((subject, payload))


class _Conn:
    def __init__(self, pg) -> None:
        self._pg = pg

    async def fetch(self, sql, *args, **_k):
        assert "alert_sink_deliveries" in sql, "the trip's only direct read is its own ledger"
        return list(self._pg.state_rows)

    async def execute(self, sql, *args):
        self._pg.executed.append((sql, args))


class _Pg:
    def __init__(self, state_rows=None) -> None:
        self.state_rows = state_rows or []
        self.executed: list[tuple] = []

    def acquire(self):
        pg = self

        class _A:
            async def __aenter__(self_inner):
                return _Conn(pg)

            async def __aexit__(self_inner, *exc):
                return False

        return _A()


def _delivery_rows(pg: _Pg) -> list[dict]:
    rows = []
    for sql, args in pg.executed:
        if "alert_sink_deliveries" in sql:
            rows.append({
                "channel": args[0], "target": args[2], "severity": args[3],
                "payload": json.loads(args[5]),
            })
    return rows


def _health_row(source_id: str, state: str, factor: float | None, *, upstream_quiet=False):
    staleness = {
        "state": state, "factor": factor,
        "reason": f"{state} · {factor}× its 1.0h expected interval",
        "age_s": None if factor is None else factor * H,
        "expected_interval_s": H, "weekday_only": False,
        "upstream_quiet": upstream_quiet,
        "last_new_item_at": "2026-10-01T06:12:00+00:00", "last_poll_ok_at": None,
    }
    return {"source_id": source_id, "staleness": staleness}


def _watchdog(rows_holder: dict, *, is_leader=None, state_rows=None, monkeypatch=None):
    async def fake_health(pool, *, silent_only=False, **_k):
        return {"rows": rows_holder["rows"]}

    monkeypatch.setattr(watch, "get_source_health", fake_health)
    nats = _Nats()
    pg = _Pg(state_rows)
    cfg = WatchdogConfig(stall_after_s=900.0, realert_every_s=1800.0, check_interval_s=60.0)
    wd = LivenessWatchdog(nats, cfg, pg_store=pg, is_leader=is_leader)
    wd._started_at = 0.0  # type: ignore[attr-defined]
    return wd, nats, pg


@pytest.mark.asyncio
async def test_a_stale_feed_trips_once_not_per_poll(monkeypatch) -> None:
    rows = {"rows": [_health_row("source.x", "stale", 9.2)]}
    wd, nats, pg = _watchdog(rows, monkeypatch=monkeypatch)
    assert await watch.check_source_staleness_once(wd, 1000.0) == ["source.x:stale"]
    # the condition persists across many polls: silence
    for t in (1300.0, 1600.0, 5000.0):
        assert await watch.check_source_staleness_once(wd, t) == []
    assert len(nats.published_core) == 1
    env = json.loads(nats.published_core[0][1])
    assert env["severity"] == "high"
    assert "source_staleness" in env["tags"] and "stale" in env["tags"]
    assert env["stale_source_id"] == "source.x"
    assert env["staleness_factor"] == 9.2
    delivered = _delivery_rows(pg)
    assert len(delivered) == 1
    assert delivered[0]["channel"] == watch.ALERT_CHANNEL_STALE
    assert delivered[0]["target"] == "source.x"
    assert delivered[0]["payload"]["state"] == "entered"
    assert delivered[0]["payload"]["last_new_item_at"] == "2026-10-01T06:12:00+00:00"


@pytest.mark.asyncio
async def test_stale_then_dead_alerts_once_per_tier_and_recovers_once(monkeypatch) -> None:
    rows = {"rows": [_health_row("source.x", "stale", 9.0)]}
    wd, nats, pg = _watchdog(rows, monkeypatch=monkeypatch)
    assert await watch.check_source_staleness_once(wd, 1000.0) == ["source.x:stale"]
    rows["rows"] = [_health_row("source.x", "dead", 30.0)]
    assert await watch.check_source_staleness_once(wd, 1100.0) == ["source.x:dead"]
    assert await watch.check_source_staleness_once(wd, 1200.0) == []
    # dead -> stale: the dead episode closes, the stale one is still open
    rows["rows"] = [_health_row("source.x", "stale", 12.0)]
    assert await watch.check_source_staleness_once(wd, 1300.0) == []
    # back to fresh: the stale episode closes
    rows["rows"] = [_health_row("source.x", "fresh", 0.5)]
    assert await watch.check_source_staleness_once(wd, 1400.0) == []
    transitions = [
        (r["channel"], r["payload"]["state"]) for r in _delivery_rows(pg)
    ]
    assert transitions == [
        (watch.ALERT_CHANNEL_STALE, "entered"),
        (watch.ALERT_CHANNEL_DEAD, "entered"),
        (watch.ALERT_CHANNEL_DEAD, "recovered"),
        (watch.ALERT_CHANNEL_STALE, "recovered"),
    ]
    assert len(nats.published_core) == 2  # entries only; recoveries are durable-row only
    # a recurrence alerts anew
    rows["rows"] = [_health_row("source.x", "stale", 9.0)]
    assert await watch.check_source_staleness_once(wd, 1500.0) == ["source.x:stale"]


@pytest.mark.asyncio
async def test_a_source_that_jumps_straight_to_dead_fires_only_the_dead_alert(monkeypatch) -> None:
    rows = {"rows": [_health_row("source.x", "dead", 40.0)]}
    wd, nats, pg = _watchdog(rows, monkeypatch=monkeypatch)
    assert await watch.check_source_staleness_once(wd, 1000.0) == ["source.x:dead"]
    assert [r["channel"] for r in _delivery_rows(pg)] == [watch.ALERT_CHANNEL_DEAD]


@pytest.mark.asyncio
async def test_honestly_quiet_stale_is_silent_but_dead_still_trips(monkeypatch) -> None:
    rows = {"rows": [_health_row("source.weekly", "stale", 9.0, upstream_quiet=True)]}
    wd, nats, pg = _watchdog(rows, monkeypatch=monkeypatch)
    assert await watch.check_source_staleness_once(wd, 1000.0) == []
    assert nats.published_core == []
    rows["rows"] = [_health_row("source.weekly", "dead", 30.0, upstream_quiet=True)]
    assert await watch.check_source_staleness_once(wd, 1100.0) == ["source.weekly:dead"]


@pytest.mark.asyncio
async def test_fresh_quiet_and_unknown_never_trip(monkeypatch) -> None:
    rows = {"rows": [
        _health_row("a", "fresh", 0.2),
        _health_row("b", "quiet", 4.0),
        _health_row("c", "unknown", None),
    ]}
    wd, nats, pg = _watchdog(rows, monkeypatch=monkeypatch)
    assert await watch.check_source_staleness_once(wd, 1000.0) == []
    assert nats.published_core == [] and pg.executed == []


@pytest.mark.asyncio
async def test_unknown_or_missing_source_is_not_recovery(monkeypatch) -> None:
    rows = {"rows": [_health_row("source.x", "stale", 9.0)]}
    wd, nats, pg = _watchdog(rows, monkeypatch=monkeypatch)
    await watch.check_source_staleness_once(wd, 1000.0)
    n_before = len(pg.executed)
    rows["rows"] = [_health_row("source.x", "unknown", None)]
    await watch.check_source_staleness_once(wd, 1100.0)
    rows["rows"] = []  # paused / retired: gone from the active roster
    await watch.check_source_staleness_once(wd, 1200.0)
    assert len(pg.executed) == n_before  # no "recovered" claimed on no evidence


@pytest.mark.asyncio
async def test_restart_does_not_re_fire_an_ongoing_episode(monkeypatch) -> None:
    rows = {"rows": [_health_row("source.x", "stale", 9.0)]}
    seeded = [{
        "channel_name": watch.ALERT_CHANNEL_STALE, "sink_target": "source.x",
        "state": "entered",
    }]
    wd, nats, pg = _watchdog(rows, state_rows=seeded, monkeypatch=monkeypatch)
    assert await watch.check_source_staleness_once(wd, 1000.0) == []
    assert nats.published_core == []


@pytest.mark.asyncio
async def test_leader_gate_boot_grace_and_throttle(monkeypatch) -> None:
    rows = {"rows": [_health_row("source.x", "stale", 9.0)]}
    wd_off, nats_off, _ = _watchdog(rows, is_leader=lambda: False, monkeypatch=monkeypatch)
    assert await watch.check_source_staleness_once(wd_off, 1000.0) == []
    wd, nats, _ = _watchdog(rows, monkeypatch=monkeypatch)
    assert await watch.check_source_staleness_once(wd, 100.0) == []  # inside boot grace
    # throttle: the second call inside min_interval_s does not even read
    assert await watch.check_source_staleness_once(wd, 1000.0, min_interval_s=300.0) == ["source.x:stale"]
    rows["rows"] = [_health_row("source.y", "stale", 9.0)]
    assert await watch.check_source_staleness_once(wd, 1100.0, min_interval_s=300.0) == []
    assert await watch.check_source_staleness_once(wd, 1400.0, min_interval_s=300.0) == ["source.y:stale"]


def test_the_tripwire_channels_are_seeded_on_boot() -> None:
    from legba.runtime import liveness_watchdog as lw

    for channel in watch.CHANNELS:
        assert channel in lw._TRANSITION_CHANNELS


def test_source_stall_diagnosis_is_still_importable_from_the_watchdog() -> None:
    from legba.runtime import liveness_watchdog as lw
    from legba.runtime import source_stall_diagnosis as diag

    assert lw._source_stall_diagnosis is diag._source_stall_diagnosis


# ---------------------------------------------------------------------------
# Slim-image import guard (the /v3/source-quality route imports the leaf)
# ---------------------------------------------------------------------------


def test_staleness_leaf_imports_nothing_heavy() -> None:
    import subprocess
    import textwrap
    from pathlib import Path

    src = str(Path(__file__).resolve().parents[2] / "src")
    script = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client", "dapr"):
            sys.modules[blocked] = None
        import legba.runtime.source_staleness
        import legba.runtime.substrate_source_health
        leaked = [m for m in sys.modules
                  if m.startswith(("legba.runtime.", "legba.data.analysts"))
                  and m not in ("legba.runtime.source_staleness",
                                "legba.runtime.substrate_source_health",
                                "legba.runtime.enrichment_timing")]
        assert not leaked, leaked
        import legba.data.registry.source_quality_api  # the route, slim
        print("OK")
        """
    )
    import os

    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (src, os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# Migration 0226 (W3-F8): a gated poll is a clean poll, never a new item
# ---------------------------------------------------------------------------


def _for(outcome: str, *, last_signal_h: float = 12.0, ok_min: float = 5.0):
    return st.staleness_for(
        last_signal_at=NOW - timedelta(hours=last_signal_h),
        cadence_raw="7 * * * *",
        profile=None,
        last_poll_outcome=outcome,
        newest_entry_ts=NOW - timedelta(hours=1),
        last_ok_poll_at=NOW - timedelta(minutes=ok_min),
        now=NOW,
    )


def test_a_gated_poll_is_not_a_new_item_and_does_not_move_the_state() -> None:
    gated, success = _for("gated"), _for("success")
    # last_new_item_at is the newest SIGNAL, untouched by a gated poll
    assert gated["last_new_item_at"] == success["last_new_item_at"]
    assert gated["state"] == success["state"] == "stale"      # 12 h vs the 1 h poll
    assert gated["factor"] == success["factor"]
    assert gated["last_poll_gated"] is True and success["last_poll_gated"] is False


def test_a_gated_poll_is_a_clean_poll_and_says_why_nothing_arrived() -> None:
    reason = _for("gated")["reason"]
    assert "gated" in reason and "keyword gate dropped every entry" in reason
    assert "poller itself is behind" not in reason
    assert "upstream is not delivering" not in reason
    # the poller WAS clean (last_ok_poll_at counted it): not "no clean poll"
    assert "no clean poll" not in reason
    # a gated poll never makes upstream "quiet" (it carried newer entries)
    assert _for("gated")["upstream_quiet"] is False
    assert st.feed_has_no_new_items("gated", NOW - timedelta(hours=1), NOW) is False


def test_an_empty_or_success_poll_leaves_the_gated_flag_off() -> None:
    for outcome in ("empty", "success", "error", None):
        assert _for(outcome)["last_poll_gated"] is False


# ---------------------------------------------------------------------------
# Real-driver reads
# ---------------------------------------------------------------------------

import pytest_asyncio  # noqa: E402

from legba.runtime.substrate_source_health import get_source_health  # noqa: E402
from tests.runtime.test_substrate_query_port import pg_pool  # noqa: E402,F401


async def _seed_active_source(pool, *, source_id, cron, signal_ages_h, poll_outcome="empty",
                              newest_entry_age_h=None):
    """One ACTIVE head source with ``cadence.schedule.raw``, a signal per entry of
    ``signal_ages_h`` (hours ago) and one poll outcome row."""
    now = datetime.now(tz=timezone.utc)
    body = {
        "identity": {"id": source_id, "kind": "rss", "state": "active"},
        "cadence": {"schedule": {"raw": cron}},
    }
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO source_descriptors
                (descriptor_id, version, schema_uri, is_head, kind, state, owner,
                 name, body, created_at)
            VALUES ($1, 'v1', 'legba/source/1.0.0', TRUE, 'rss', 'active', 'test',
                    $1, $2::jsonb, $3)
            """,
            source_id, json.dumps(body), now - timedelta(days=30),
        )
        for age_h in signal_ages_h:
            await conn.execute(
                """
                INSERT INTO signals
                    (id, source_id, source_version, payload, content_hash,
                     fetched_at, created_at, updated_at, schema_uri)
                VALUES (gen_random_uuid(), $1, 'v1', '{}'::jsonb, $2, $3, $3, $3,
                        'iglu:legba/signal/jsonschema/1-0-0')
                """,
                source_id, uuid4().hex, now - timedelta(hours=age_h),
            )
        await conn.execute(
            """
            INSERT INTO source_poll_outcomes
                (source_id, source_version, outcome, health_state,
                 signals_written, occurred_at, newest_entry_ts)
            VALUES ($1, 'v1', $2, 'healthy', 0, $3, $4)
            """,
            source_id, poll_outcome, now,
            None if newest_entry_age_h is None else now - timedelta(hours=newest_entry_age_h),
        )


@pytest.mark.asyncio
async def test_get_source_health_rows_carry_staleness(pg_pool) -> None:
    st.reset_profile_cache()
    stale_id = f"source.stale.tripwire.{uuid4().hex[:8]}"
    fresh_id = f"source.stale.fresh.{uuid4().hex[:8]}"
    never_id = f"source.stale.never.{uuid4().hex[:8]}"
    # hourly poll; twelve signals an hour apart, the newest 200 h old. 200 h
    # is far enough that the weekend discount (<= 48 h) cannot change the
    # verdict whatever day of the week this runs on.
    await _seed_active_source(
        pg_pool, source_id=stale_id, cron="7 * * * *",
        signal_ages_h=[200.0 + i for i in range(0, 12)],
        poll_outcome="empty", newest_entry_age_h=201.0,
    )
    await _seed_active_source(
        pg_pool, source_id=fresh_id, cron="7 * * * *",
        signal_ages_h=[0.2, 1.2, 2.2, 3.2], poll_outcome="success",
    )
    await _seed_active_source(
        pg_pool, source_id=never_id, cron="7 * * * *", signal_ages_h=[],
        poll_outcome="empty",
    )
    st.reset_profile_cache()
    health = await get_source_health(pg_pool, silent_hours=48)
    rows = {r["source_id"]: r for r in health["rows"]}

    s = rows[stale_id]["staleness"]
    assert s["state"] == "dead"            # ~200 h old vs a 1 h rhythm
    assert s["factor"] >= 100
    assert s["last_new_item_at"] is not None
    # ONE source of staleness: feed_has_no_new_items IS the staleness input
    assert rows[stale_id]["feed_has_no_new_items"] is True
    assert s["upstream_quiet"] is True

    assert rows[fresh_id]["staleness"]["state"] == "fresh"
    assert rows[fresh_id]["feed_has_no_new_items"] is False

    never = rows[never_id]["staleness"]
    assert never["state"] == "unknown" and never["factor"] is None   # absence, not 0

    hist = health["summary"]["staleness"]
    assert set(hist) == set(st.STATES)
    assert sum(hist.values()) == health["scanned"]
    assert hist["unknown"] >= 1 and hist["fresh"] >= 1 and hist["dead"] >= 1


@pytest.mark.asyncio
async def test_source_quality_route_attaches_the_same_staleness(pg_pool) -> None:
    from legba.data.registry import source_quality_api as sq

    st.reset_profile_cache()
    source_id = f"source.stale.route.{uuid4().hex[:8]}"
    await _seed_active_source(
        pg_pool, source_id=source_id, cron="7 * * * *",
        signal_ages_h=[10.0 + i for i in range(0, 10)],
        poll_outcome="empty", newest_entry_age_h=None,
    )
    st.reset_profile_cache()
    # the view's row shape is unavailable on a bare pool; build the projected
    # row the route builds and attach, which is the unit the route calls.
    row = sq.SourceQualityRow(
        source_id=source_id, registered=True, declared_state="active",
        asserted=sq.AssertedQualityOut(),
        computed=sq.ComputedQualityOut(
            cadence_raw="7 * * * *",
            last_signal_at=datetime.now(tz=timezone.utc) - timedelta(hours=10),
        ),
    )
    inactive = sq.SourceQualityRow(
        source_id="source.stale.paused", registered=True, declared_state="paused",
        asserted=sq.AssertedQualityOut(), computed=sq.ComputedQualityOut(),
    )
    await sq._attach_staleness(pg_pool, [row, inactive])
    assert row.computed.staleness is not None
    assert row.computed.staleness.state == "stale"      # 10 h vs 1 h rhythm
    assert row.computed.staleness.factor == pytest.approx(10.0, abs=1.0)
    assert inactive.computed.staleness is None          # NOT MEASURED, not unknown


@pytest.mark.asyncio
async def test_gated_poll_counts_as_ok_and_leaves_last_new_item_on_the_real_driver(
    pg_pool,
) -> None:
    """The evidence read counts a gated poll as a clean poll (``outcome <>
    'error'``), and ``get_source_health`` reports the newest SIGNAL as the last
    new item, the gated word on the row, and the stalled source under
    ``silent_feed_gated`` -- not the error-or-none remainder."""
    st.reset_profile_cache()
    source_id = f"source.stale.gated.{uuid4().hex[:8]}"
    await _seed_active_source(
        pg_pool, source_id=source_id, cron="7 * * * *",
        signal_ages_h=[200.0 + i for i in range(0, 12)], poll_outcome="gated",
        newest_entry_age_h=1.0,
    )
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE source_poll_outcomes SET keyword_filtered = 9 WHERE source_id = $1",
            source_id,
        )
    evidence = await st.fetch_poll_evidence(pg_pool, [source_id])
    assert evidence[source_id]["last_poll_outcome"] == "gated"
    assert isinstance(evidence[source_id]["last_ok_poll_at"], datetime)

    st.reset_profile_cache()
    health = await get_source_health(pg_pool, silent_hours=48)
    row = next(r for r in health["rows"] if r["source_id"] == source_id)
    assert row["last_poll_outcome"] == "gated"
    assert row["keyword_filtered"] == 9
    assert row["feed_has_no_new_items"] is False
    s = row["staleness"]
    assert s["last_poll_gated"] is True and s["state"] == "dead"
    # last_new_item_at is the newest signal (~200 h ago), not the gated poll
    newest_signal = datetime.fromisoformat(s["last_new_item_at"])
    assert (datetime.now(tz=timezone.utc) - newest_signal) > timedelta(hours=190)
    summary = health["summary"]
    assert summary["silent_feed_gated"] >= 1
    assert (
        summary["silent_poll_error_or_none"]
        == summary["active_stalled"] - summary["silent_feed_empty"] - summary["silent_feed_gated"]
    )
