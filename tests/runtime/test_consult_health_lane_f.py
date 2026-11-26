"""dlq_and_alert, consult run health, consult health alarm."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from legba.runtime import consult_health as ch
from legba.runtime import hard_fail_alert as hfa
from legba.runtime.liveness_watchdog import LivenessWatchdog, WatchdogConfig

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- doubles
class _Conn:
    def __init__(self, pg):
        self.pg = pg

    async def execute(self, sql, *args):
        if self.pg.fail_execute:
            raise RuntimeError("boom")
        self.pg.executed.append((sql, args))

    async def fetch(self, sql, *args, **_k):
        if "alert_sink_deliveries" in sql:
            return []
        if self.pg.fail_fetch:
            raise RuntimeError("boom-read")
        return list(self.pg.outcomes)


class _Pg:
    def __init__(self, outcomes=(), fail_execute=False, fail_fetch=False):
        self.outcomes = list(outcomes)
        self.executed: list[tuple] = []
        self.fail_execute = fail_execute
        self.fail_fetch = fail_fetch

    def acquire(self):
        pg = self

        class _A:
            async def __aenter__(self_i):
                return _Conn(pg)

            async def __aexit__(self_i, *e):
                return False

        return _A()


class _Disp:
    def __init__(self, fail=False):
        self.payloads = []
        self.fail = fail

    async def fan_out(self, payload):
        if self.fail:
            raise RuntimeError("ntfy down")
        self.payloads.append(payload)


class _Nats:
    async def publish_core(self, *a, **k):
        return None


def _o(mode, hours_ago, outcome):
    return {"mode": mode, "at": NOW - timedelta(hours=hours_ago), "outcome": outcome}


# ------------------------------------------------- item 1: dlq_and_alert
@pytest.mark.asyncio
async def test_dlq_and_alert_writes_row_and_pushes_alert() -> None:
    pg, disp = _Pg(), _Disp()
    exc = RuntimeError("vllm 400: maximum context length is 262144 tokens")
    landed = await hfa.dead_letter_and_alert(
        pg, analyst_id="consult_default", version="41c77678aa", run_id="bd1a8e8a-x",
        actor_id="analyst::consult_default", exc=exc, dispatcher=disp,
    )
    assert landed == {"dlq": True, "alert": True}
    sql, args = pg.executed[0]
    assert "INSERT INTO descriptor_dead_letter" in sql
    assert args[0] == "analyst::consult_default" and args[1] == "analyst_run"
    assert json.loads(args[2])["run_id"] == "bd1a8e8a-x"
    assert "maximum context length" in json.loads(args[3])["message"]
    (payload,) = disp.payloads
    assert payload.severity == "high"
    assert payload.channel_name == "analyst_hard_fail"
    assert "consult_default" in payload.summary
    assert "maximum context length" in payload.detail


@pytest.mark.asyncio
async def test_dlq_and_alert_legs_are_independent_and_never_raise() -> None:
    exc = RuntimeError("x")
    kw = dict(analyst_id="a", version="v", run_id="r", actor_id="act", exc=exc)
    # DLQ write dead, alert still goes.
    disp = _Disp()
    landed = await hfa.dead_letter_and_alert(
        _Pg(fail_execute=True), dispatcher=disp, **kw)
    assert landed == {"dlq": False, "alert": True} and len(disp.payloads) == 1
    # ntfy dead, DLQ row still written.
    pg = _Pg()
    landed = await hfa.dead_letter_and_alert(pg, dispatcher=_Disp(fail=True), **kw)
    assert landed == {"dlq": True, "alert": False} and len(pg.executed) == 1


def test_actor_hard_branch_calls_dead_letter_and_alert_only_for_dlq_strategy() -> None:
    import inspect

    from legba.runtime import dapr_actors

    src = inspect.getsource(dapr_actors)
    # pause / drop keep their behaviour; the default branch calls the helper.
    tail = src.split('elif hard_strategy == "drop":', 1)[1].split("await self._set_record", 1)[0]
    assert "_hard_fail_alert.dead_letter_and_alert" in tail
    head = src.split('if hard_strategy == "pause":', 1)[1].split('elif hard_strategy == "drop":', 1)[0]
    assert "dead_letter_and_alert" not in head


# ------------------------------------------- item 2: run health off turns
def test_summarize_idle_last_succeeded_vs_failing() -> None:
    idle = ch.summarize([
        _o("chat", 100, "succeeded"), _o("chat", 400, "failed"),
    ])["consult_default"]
    assert idle["run_state"] == "idle_last_succeeded" and idle["consecutive_bad"] == 0
    bad = ch.summarize([
        _o("chat", 1, "unfinished"), _o("chat", 2, "failed"), _o("chat", 3, "succeeded"),
    ])["consult_default"]
    assert bad["run_state"] == "failing" and bad["consecutive_bad"] == 2


@pytest.mark.asyncio
async def test_overlay_replaces_the_stale_10_29_failure_trace() -> None:
    """The real shape: one failed trace 303h ago, a later success on turns."""
    pg = _Pg([_o("chat", 150, "succeeded"), _o("chat", 303, "failed")])
    result = {
        "rows": [{
            "analyst_id": "consult_default", "run_id": "bd1a8e8a", "status": "failed",
            "had_error": True, "cadence_trigger": "method",
            "last_run_at": (NOW - timedelta(hours=303)).isoformat(),
            "hours_ago": 303.0, "quiet": True,
        }],
        "count": 1, "quiet_analysts": ["consult_default"],
    }
    await ch.overlay_run_health(pg, result)
    row = result["rows"][0]
    assert row["status"] == "succeeded" and row["had_error"] is False
    assert row["run_state"] == "idle_last_succeeded"
    assert row["quiet"] is False and row["idle_since"]
    assert result["quiet_analysts"] == []


@pytest.mark.asyncio
async def test_overlay_appends_row_when_no_trace_and_keeps_failing_visible() -> None:
    pg = _Pg([_o("deep", 2, "failed"), _o("deep", 3, "failed")])
    result = {"rows": [], "count": 0, "quiet_analysts": []}
    await ch.overlay_run_health(pg, result)
    (row,) = result["rows"]
    assert row["analyst_id"] == "deep_consult" and row["run_state"] == "failing"
    assert row["status"] == "failed" and row["consecutive_bad"] == 2


@pytest.mark.asyncio
async def test_overlay_is_failsafe_and_per_analyst_read_gets_a_block() -> None:
    result = {"rows": [], "count": 0, "quiet_analysts": []}
    await ch.overlay_run_health(_Pg(fail_fetch=True), result)
    assert result["rows"] == []
    out = {"rows": []}
    await ch.overlay_run_health(
        _Pg([_o("chat", 5, "succeeded")]), out, analyst_id="consult_default")
    assert out["on_demand_health"]["run_state"] == "idle_last_succeeded"


# ------------------------------------------------ item 3: the alarm
def test_evaluate_needs_n_consecutive_and_n_is_a_knob(monkeypatch) -> None:
    two_bad = [_o("chat", 1, "failed"), _o("chat", 2, "unfinished"), _o("chat", 3, "succeeded")]
    assert ch.evaluate(two_bad) == []                      # default N=3
    assert [e["analyst_id"] for e in ch.evaluate(two_bad, threshold=2)] == ["consult_default"]
    monkeypatch.setenv(ch.CONSECUTIVE_BAD_ENV, "2")
    assert len(ch.evaluate(two_bad)) == 1
    monkeypatch.setenv(ch.CONSECUTIVE_BAD_ENV, "junk")
    assert ch.consecutive_bad_threshold() == 3
    # a success in the middle resets the run; modes do not mix
    mixed = [_o("chat", 1, "failed"), _o("deep", 2, "failed"), _o("chat", 3, "succeeded"),
             _o("chat", 4, "failed"), _o("chat", 5, "failed")]
    assert ch.evaluate(mixed, threshold=2) == []


def _wd(pg):
    wd = LivenessWatchdog(
        _Nats(),
        WatchdogConfig(stall_after_s=900.0, realert_every_s=1800.0, check_interval_s=60.0),
        pg_store=pg,
    )
    wd._started_at = 0.0
    return wd


def _delivered(pg):
    return [a for s, a in pg.executed if "alert_sink_deliveries" in s]


@pytest.mark.asyncio
async def test_watchdog_alerts_once_on_entry_then_recovers() -> None:
    pg = _Pg([_o("chat", 1, "failed"), _o("chat", 2, "unfinished"), _o("chat", 3, "failed")])
    wd = _wd(pg)
    assert await wd.check_consult_health_once(2000.0) == ["consult_default"]
    rows = _delivered(pg)
    assert len(rows) == 1 and rows[0][0] == "consult_unhealthy" and rows[0][3] == "high"
    assert json.loads(rows[0][5])["consecutive_bad"] == 3
    assert await wd.check_consult_health_once(3000.0) == []       # silent while ongoing
    assert len(_delivered(pg)) == 1
    pg.outcomes.insert(0, _o("chat", 0, "succeeded"))              # a turn completes
    assert await wd.check_consult_health_once(4000.0) == []
    rec = _delivered(pg)
    assert len(rec) == 2 and json.loads(rec[1][5])["state"] == "recovered"


@pytest.mark.asyncio
async def test_watchdog_quiet_when_healthy_or_in_boot_grace() -> None:
    pg = _Pg([_o("chat", 1, "succeeded"), _o("chat", 2, "failed")])
    assert await _wd(pg).check_consult_health_once(2000.0) == []
    bad = _Pg([_o("chat", i, "failed") for i in range(1, 5)])
    assert await _wd(bad).check_consult_health_once(10.0) == []   # boot grace
    assert _delivered(bad) == []


def test_alarm_channel_is_a_transition_channel_and_sql_is_parseable_shape() -> None:
    from legba.runtime.liveness_watchdog import _TRANSITION_CHANNELS

    assert ch.ALERT_CHANNEL_CONSULT in _TRANSITION_CHANNELS
    assert "consult_turns" in ch.OUTCOMES_SQL and "$1" in ch.OUTCOMES_SQL
