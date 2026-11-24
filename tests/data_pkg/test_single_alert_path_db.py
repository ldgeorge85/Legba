# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-1 — the single alert path on the REAL driver, every claim read back from
the rows of a migrated database.

* migration 0250 is idempotent and additive;
* one raise opens an episode and writes one ``alert_events`` row with the
  decision; the same key again UPDATES the episode (one ``alerts`` row, two
  events) and does not page again; a severity rise pages; a recovery closes
  the episode;
* a kind outside the closed list is refused and writes nothing;
* every producer raises through the function: the escalation edge, the
  liveness watchdog (entry and recovery), the hard-fail DLQ, the forecast
  resolver, the trigger scan (paged and held rows), the reminder GC, the
  first-generation ``alert`` output kind, the host CLI, and the volume-surge
  detector (logged, never paged);
* the page rule decides: what pages carries the matching entry; nothing is
  delivered without a ledger row naming its episode and raise;
* the alert-log route reads it all back.

The push service is a stub (a recording HTTP client); nothing leaves the test.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from legba.data.alerts import AlertSinkDispatcher, NtfyAlertSink
from legba.data.alerts import raise_alert as RA
from legba.data.alerts.kinds import UnknownAlertKind
from legba.data.config import PostgresConfig

pytestmark = [pytest.mark.integration]

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src" / "legba" / "data" / "migrations" / "0250_alert_log.sql"
)
NTFY_URL = "http://ntfy.l1.test/legba-alerts"
TAG = "l1test"


class _Resp:
    status_code = 200
    text = ""


class _Http:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post(self, url, *, content=None, headers=None, timeout=None, **kw):  # noqa: ANN001
        self.calls.append({"url": url, "body": content.decode("utf-8"),
                           "headers": dict(headers or {})})
        return _Resp()


async def _noop_sleep(_s: float) -> None:
    return None


def _dispatcher(pool: Any, http: _Http) -> AlertSinkDispatcher:
    sink = NtfyAlertSink(url=NTFY_URL, http=http, sleep=_noop_sleep, min_severity="medium")
    return AlertSinkDispatcher(pg_pool=pool, sinks=[sink], cooldown_seconds=0)


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    started = datetime.now(timezone.utc) - timedelta(seconds=2)
    async with p.acquire() as conn:
        await conn.execute(_MIGRATION.read_text())
        await conn.execute("TRUNCATE alert_events, alerts")
    RA.reset_dispatchers()
    yield p
    async with p.acquire() as conn:
        await conn.execute("TRUNCATE alert_events, alerts")
        await conn.execute("DELETE FROM alert_sink_deliveries WHERE attempted_at >= $1", started)
    RA.reset_dispatchers()
    await p.close()


@pytest.fixture
def http() -> _Http:
    return _Http()


async def _alerts(pool: Any) -> list[dict]:
    return [dict(r) for r in await pool.fetch("SELECT * FROM alerts ORDER BY opened_at, id")]


async def _events(pool: Any) -> list[dict]:
    return [dict(r) for r in await pool.fetch("SELECT * FROM alert_events ORDER BY raised_at, id")]


async def _ledger(pool: Any) -> list[dict]:
    return [dict(r) for r in await pool.fetch(
        "SELECT * FROM alert_sink_deliveries WHERE alert_event_id IS NOT NULL ORDER BY attempted_at")]


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_is_idempotent_and_additive(pool) -> None:  # noqa: ANN001
    sql = _MIGRATION.read_text()
    assert "BEGIN;" not in sql.upper() and "DROP " not in sql.upper()
    async with pool.acquire() as conn:
        await conn.execute(sql)
        await conn.execute(sql)
        cols = {r["column_name"] for r in await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'alert_sink_deliveries'")}
    assert {"alert_id", "alert_event_id"} <= cols


# ---------------------------------------------------------------------------
# Episodes, dedupe, decisions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_episode_opens_updates_rises_and_closes(pool, http) -> None:  # noqa: ANN001
    disp = _dispatcher(pool, http)
    kw = dict(kind="source_degraded", subject_id="source.l1.test", raised_by=TAG,
              summary="Source degraded: source.l1.test", dispatcher=disp)
    r1 = await RA.raise_alert(pool, severity="high", **kw)
    assert (r1.action, r1.decision, r1.paged) == ("opened", "paged", True)
    assert r1.rule_entry == "source_degraded >= high"
    r2 = await RA.raise_alert(pool, severity="high", **kw)
    assert (r2.action, r2.decision) == ("updated", "deduped")
    r3 = await RA.raise_alert(pool, severity="critical", **kw)
    assert (r3.action, r3.decision) == ("updated", "paged")
    r4 = await RA.raise_alert(pool, severity="info", close=True, **kw)
    assert (r4.action, r4.decision) == ("closed", "logged")   # info: no rule entry

    (ep,) = await _alerts(pool)
    assert ep["state"] == "closed" and ep["occurrences"] == 4
    assert ep["paged_count"] == 2 and ep["paged_severity"] == "critical"
    assert ep["max_severity"] == "critical" and ep["closed_at"] is not None
    events = await _events(pool)
    assert [e["decision"] for e in events] == ["paged", "deduped", "paged", "logged"]
    assert all(e["alert_id"] == ep["id"] for e in events)
    assert len(http.calls) == 2

    # a new raise after the close opens a NEW episode
    r5 = await RA.raise_alert(pool, severity="high", **kw)
    assert r5.action == "opened" and r5.decision == "paged"
    assert len(await _alerts(pool)) == 2


@pytest.mark.asyncio
async def test_unknown_kind_writes_nothing(pool) -> None:  # noqa: ANN001
    with pytest.raises(UnknownAlertKind):
        await RA.raise_alert(pool, kind="disk_full", severity="high", summary="x",
                             subject_id="/", raised_by=TAG)
    assert await _alerts(pool) == [] and await _events(pool) == []


@pytest.mark.asyncio
async def test_an_event_episode_closes_after_its_quiet_window(pool, http) -> None:  # noqa: ANN001
    disp = _dispatcher(pool, http)
    t0 = datetime.now(timezone.utc) - timedelta(hours=30)
    kw = dict(kind="analyst_hard_fail", severity="high", subject_id="l1_analyst",
              summary="Analyst hard failure: l1_analyst", raised_by=TAG, dispatcher=disp)
    assert (await RA.raise_alert(pool, now=t0, **kw)).decision == "paged"
    assert (await RA.raise_alert(pool, now=t0 + timedelta(hours=1), **kw)).decision == "deduped"
    later = await RA.raise_alert(pool, now=t0 + timedelta(hours=8), **kw)  # quiet 6 h
    assert (later.action, later.decision) == ("opened", "paged")
    eps = await _alerts(pool)
    assert [e["state"] for e in eps] == ["closed", "open"]
    assert eps[0]["close_reason"].startswith("quiet")


@pytest.mark.asyncio
async def test_nothing_is_delivered_without_a_ledger_row(pool, http) -> None:  # noqa: ANN001
    disp = _dispatcher(pool, http)
    for i in range(3):
        await RA.raise_alert(pool, kind="escalations", severity="elevated",
                             summary=f"escalation {i}", subject_id=f"X{i}",
                             raised_by=TAG, dispatcher=disp)
    # rule 2026-11-14/1: a baseline deviation is logged only, at any severity
    await RA.raise_alert(pool, kind="baseline_deviation", severity="high", summary="deviation",
                         subject_id="US", raised_by=TAG, dispatcher=disp)
    paged = [e for e in await _events(pool) if e["decision"] == "paged"]
    ledger = await _ledger(pool)
    assert len(paged) == 3 == len(http.calls)
    assert {r["alert_event_id"] for r in ledger if r["status"] == "delivered"} == {
        e["id"] for e in paged}
    assert all(r["alert_id"] is not None for r in ledger)
    # the unit word 'elevated' paged as high (never folded to info)
    assert {e["severity"] for e in paged} == {"high"}
    dev = [e for e in await _events(pool) if e["kind"] == "baseline_deviation"]
    assert dev[0]["decision"] == "logged" and dev[0]["rule_entry"] is None


# ---------------------------------------------------------------------------
# Every producer raises through the function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_edge_raises(pool, http) -> None:  # noqa: ANN001
    from legba.data.analysts.agency.tools import ChannelEmitter
    from legba.data.schemas.action_pack import Channel

    disp = _dispatcher(pool, http)
    emitter = ChannelEmitter(pg_pool=pool, alert_sinks=disp)
    chan = Channel(name="escalations", kind="alert", config={})
    payload = {"title": "L1 escalation", "severity": "critical", "target_id": "ZZ",
               "output_id": str(uuid.uuid4()), "requested_by": "l1"}
    await emitter.emit(chan, payload)
    await emitter.emit(chan, payload)          # a second channel, the same finding
    (ep,) = await _alerts(pool)
    assert ep["kind"] == "escalations" and ep["subject_id"] == "ZZ"
    assert ep["occurrences"] == 2 and ep["paged_count"] == 1
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_watchdog_transitions_raise_and_recover(pool, http) -> None:  # noqa: ANN001
    from legba.runtime.liveness_watchdog import LivenessWatchdog, WatchdogConfig

    class _Nats:
        async def publish_core(self, *a: Any) -> None:
            return None

    wd = LivenessWatchdog(_Nats(), WatchdogConfig(), pg_store=pool,
                          alert_sinks=_dispatcher(pool, http))
    common = dict(channel="source_degraded", entity_id="source.l1.wd", kind="source_degraded")
    await wd._record_transition(state="entered", severity="high", title="degraded", body="b", **common)
    await wd._record_transition(state="recovered", severity="info", title="recovered", body="b", **common)
    (ep,) = await _alerts(pool)
    assert ep["state"] == "closed" and ep["paged_count"] == 1
    assert [e["action"] for e in await _events(pool)] == ["opened", "closed"]
    # a transient (medium) stall is logged, never paged
    await wd._record_transition(state="entered_transient", severity="medium", title="5xx",
                                body="b", channel="source_cadence_stall",
                                entity_id="source.l1.flaky", kind="source_cadence_stall",
                                fan_out=False)
    last = (await _events(pool))[-1]
    assert last["kind"] == "source_cadence_stall" and last["decision"] == "logged"
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_cursor_lag_leg_raises_through_the_path_and_recovers(pool, http) -> None:  # noqa: ANN001
    """N-Z x L-1: the cursor-lag leg's entry (``analyst_cursor_lag``) and
    recovery (payload kind ``analyst_cursor_lag_recovered``) both reach
    ``raise_alert`` under the channel kind in the closed list: entry opens and
    pages at high, recovery closes; nothing is refused."""
    from legba.runtime import analyst_cursor_lag as acl
    from legba.runtime.liveness_watchdog import LivenessWatchdog, WatchdogConfig

    class _Nats:
        async def publish_core(self, *a: Any) -> None:
            return None

    tclass, wkey = f"{TAG}_cursor", "signal_slice"
    entity = f"{tclass}:{wkey}"
    wd = LivenessWatchdog(_Nats(), WatchdogConfig(), pg_store=pool,
                          alert_sinks=_dispatcher(pool, http))
    wd._started_at = -1e9  # past the boot grace
    started = datetime.now(timezone.utc) - timedelta(seconds=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO alert_trigger_watermarks (trigger_class, watermark_key, state, updated_at) "
                "VALUES ($1, $2, $3::jsonb, now()) ON CONFLICT (trigger_class, watermark_key) "
                "DO UPDATE SET state = EXCLUDED.state, updated_at = now()",
                tclass, wkey, json.dumps({"lag_minutes": 1800.0}))
        assert await acl.check_once(wd, 0.0) == [entity]
        assert await acl.check_once(wd, 0.0) == []  # the ongoing condition is silent
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE alert_trigger_watermarks SET state = $3::jsonb, updated_at = now() "
                "WHERE trigger_class = $1 AND watermark_key = $2",
                tclass, wkey, json.dumps({"lag_minutes": 2.0}))
        await acl.check_once(wd, 0.0)
        eps = [a for a in await _alerts(pool) if a["kind"] == "analyst_cursor_lag"]
        (ep,) = eps
        assert ep["subject_id"] == entity and ep["state"] == "closed" and ep["paged_count"] == 1
        evs = [e for e in await _events(pool) if e["kind"] == "analyst_cursor_lag"]
        assert [e["action"] for e in evs] == ["opened", "closed"]
        assert evs[0]["decision"] == "paged"
        assert len(http.calls) == 1
        kinds = [json.loads(r["payload_summary"])["kind"] if isinstance(r["payload_summary"], str)
                 else r["payload_summary"]["kind"] for r in await pool.fetch(
            "SELECT payload_summary FROM alert_sink_deliveries WHERE sink_kind = 'liveness_watchdog' "
            "AND channel_name = 'analyst_cursor_lag' AND sink_target = $1 AND attempted_at >= $2 "
            "ORDER BY attempted_at", entity, started)]
        assert kinds == ["analyst_cursor_lag", "analyst_cursor_lag_recovered"]
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM alert_trigger_watermarks WHERE trigger_class = $1", tclass)


@pytest.mark.asyncio
async def test_hard_fail_raises_once_per_episode(pool, http) -> None:  # noqa: ANN001
    from legba.runtime.hard_fail_alert import dead_letter_and_alert

    disp = _dispatcher(pool, http)
    for _ in range(2):
        landed = await dead_letter_and_alert(
            pool, analyst_id="l1_flapper", version="v1", run_id=uuid.uuid4(),
            actor_id="analyst:l1", exc=RuntimeError("boom"), dispatcher=disp)
        assert landed == {"dlq": True, "alert": True}
    (ep,) = await _alerts(pool)
    assert ep["kind"] == "analyst_hard_fail" and ep["occurrences"] == 2
    assert len(http.calls) == 1
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM descriptor_dead_letter WHERE actor = 'analyst:l1'")


@pytest.mark.asyncio
async def test_forecast_resolver_raises(pool, http) -> None:  # noqa: ANN001
    from legba.data.analysts.deterministic_handlers import forecast_resolver as fr

    RA.register_dispatcher(pool, _dispatcher(pool, http))
    alert = fr._alert_fn(pool)
    await alert("Forecast l1-q: needs your judgement", "decide", "high")
    await alert("Forecast l1-q: proposal no", "confirm", "low")
    eps = await _alerts(pool)
    assert {e["kind"] for e in eps} == {"forecast_resolution"}
    assert {e["subject_id"] for e in eps} == {"l1-q"}
    assert [e["decision"] for e in await _events(pool)] == ["paged", "logged"]


@pytest.mark.asyncio
async def test_trigger_scan_raises_pushes_and_held_rows(pool, http) -> None:  # noqa: ANN001
    from legba.data.alerts.sinks import AlertSinkPayload
    from legba.data.analysts.deterministic_handlers._trigger_scan_raise import (
        raise_trigger_alerts,
    )

    disp = _dispatcher(pool, http)
    pushed_id, held_id = uuid.uuid4(), uuid.uuid4()

    def _cand(cls: str, data: dict) -> Any:
        return SimpleNamespace(trigger_class=cls, severity="high", title=f"{cls} title",
                               body="b", target_id="ZZ", data=data)

    def _payload(row_id: Any, cand: Any) -> AlertSinkPayload:
        return AlertSinkPayload(summary=cand.title, severity=cand.severity,
                                channel_name="trigger_scan", target_id=cand.target_id,
                                verify_state="unverified — test", kind=cand.trigger_class)

    ok, failed, _ = await raise_trigger_alerts(
        pool, disp,
        to_fan_out=[(pushed_id, _cand("band_crossing", {}))],
        held=[(held_id, _cand("verified_finding", {
            "guard_suppressed": True, "guard_suppression_reason": "steady_state_within_cooldown"}))],
        sink_payload=_payload,
    )
    assert (ok, failed) == (1, 0)
    events = {e["kind"]: e for e in await _events(pool)}
    assert events["band_crossing"]["decision"] == "paged"
    assert events["verified_finding"]["decision"] == "held"
    assert "steady-state guard" in events["verified_finding"]["decision_reason"]
    (row,) = [r for r in await _ledger(pool) if r["status"] == "delivered"]
    assert row["alert_event_id"] == events["band_crossing"]["id"]
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_reminder_gc_and_alert_output_kind_raise_logged(pool, http) -> None:  # noqa: ANN001
    from legba.data.outputs import alert as alert_kind
    from legba.data.outputs._contract import OutputContext, OutputDeps
    from legba.data.provenance.models import AlertPayload
    from legba.runtime import reminder_gc

    class _Rec:
        actor_id = "analyst:l1_retired"
        actor_kind = "analyst"
        descriptor_id = "l1_retired"
        source_cursors: dict = {}
        lifecycle = reminder_gc.RETIRED

    class _Store:
        async def list_by_lifecycle(self, _l: Any) -> list:
            return [_Rec()]

    async def _delete(*_a: Any) -> bool:
        return True

    result = await reminder_gc.sweep_orphan_reminders(
        state_store=_Store(), delete_reminder=_delete, pg_pool=pool)
    assert result.removed == 1
    await alert_kind.emit(
        AlertPayload(title="L1 output alert", body="b", confidence=0.9, severity="high"),
        deps=OutputDeps(), ctx=OutputContext(analyst_id="l1", target_id="ZZ"),
    )  # no pool: nothing raised
    await alert_kind._raise_on_alert_log(
        pool, AlertPayload(title="L1 output alert", body="b", confidence=0.9, severity="high"),
        OutputContext(analyst_id="l1", target_id="ZZ"))
    events = {e["kind"]: e for e in await _events(pool)}
    assert events["reminder_orphan_collected"]["decision"] == "logged"
    assert events["analyst_alert_output"]["decision"] == "logged"
    assert http.calls == []


@pytest.mark.asyncio
async def test_host_cli_raises_and_refuses(pool, migrated_pg, monkeypatch, capsys) -> None:  # noqa: ANN001
    from legba.data.alerts import cli

    for env in ("LEGBA_ALERT_NTFY_URL", "LEGBA_ALERT_WEBHOOK_URL"):
        monkeypatch.delenv(env, raising=False)      # no real push service, ever
    monkeypatch.setenv("LEGBA_DATA_PG_HOST", migrated_pg.host)
    monkeypatch.setenv("LEGBA_DATA_PG_PORT", str(migrated_pg.port))
    monkeypatch.setenv("LEGBA_DATA_PG_USER", migrated_pg.user)
    monkeypatch.setenv("LEGBA_DATA_PG_PASSWORD", migrated_pg.password)
    monkeypatch.setenv("LEGBA_DATA_PG_DB", migrated_pg.database)
    args = cli._parser().parse_args([
        "raise", "--kind", "host_disk", "--severity", "medium", "--subject", "/",
        "--summary", "legba disk 87% - warning", "--raised-by", "disk_floor_check.sh"])
    assert await cli._raise(args, "detail") == cli.EXIT_OK
    assert capsys.readouterr().out.startswith("paged:")
    (ep,) = await _alerts(pool)
    assert ep["kind"] == "host_disk" and ep["raised_by"] == "disk_floor_check.sh"
    rows = await _ledger(pool)
    assert rows and {r["status"] for r in rows} == {"skipped_unconfigured"}
    bad = cli._parser().parse_args([
        "raise", "--kind", "disk", "--severity", "medium", "--subject", "/",
        "--summary", "x", "--raised-by", "t"])
    assert await cli._raise(bad, "") == cli.EXIT_REFUSED


@pytest.mark.asyncio
async def test_volume_surge_detector_raises_and_pages_at_medium(pool, http) -> None:  # noqa: ANN001
    from legba.runtime import volume_surge_watch as vs

    now = datetime.now(timezone.utc)
    subject = "QZ"   # an unassigned ISO code: nothing else in the test DB uses it
    async with pool.acquire() as conn:
        for i in range(20):
            await conn.execute(
                "INSERT INTO signals (source_id, fetched_at, geo, payload, content_hash, origin_class) "
                "VALUES ($1, $2, $3, $4::jsonb, $5, 'live')",
                f"source.l1surge{i % 10}.news", now - timedelta(minutes=5 + i), [subject],
                json.dumps({"title": f"surge headline {i}",
                            "published_at": (now - timedelta(minutes=30)).isoformat()}),
                f"l1surge-{i}-{uuid.uuid4()}",
            )
    try:
        wd = SimpleNamespace(_pg=pool, _is_leader=None, _alert_sinks=_dispatcher(pool, http))
        vs._BASELINE_CACHE.clear()
        surges = await vs.check_volume_surge_once(wd, now=now, force=True)
        assert [s.subject for s in surges] == [subject]
        (ep,) = [e for e in await _alerts(pool) if e["kind"] == "volume_surge"]
        assert ep["subject_id"] == subject and ep["severity"] == "medium"
        (ev,) = [e for e in await _events(pool) if e["kind"] == "volume_surge"]
        # rule 2026-11-14/1 (the owner, "surge then"): a surge pages at medium
        assert ev["decision"] == "paged" and ev["rule_entry"] == "volume_surge >= medium"
        assert len(http.calls) == 1
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM signals WHERE source_id LIKE 'source.l1surge%'")


# ---------------------------------------------------------------------------
# The read route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alert_log_route_reads_it_back(pool, http) -> None:  # noqa: ANN001
    from legba.data.registry.alert_log_api import read_alert_log

    disp = _dispatcher(pool, http)
    await RA.raise_alert(pool, kind="consult_unhealthy", severity="high", summary="consult down",
                         subject_id="consult_default", raised_by=TAG, dispatcher=disp)
    await RA.raise_alert(pool, kind="baseline_deviation", severity="medium", summary="deviation",
                         subject_id="US", raised_by=TAG, dispatcher=disp)
    async with pool.acquire() as conn:
        out = await read_alert_log(conn, hours=24)
    assert out.measured
    assert {e.kind for e in out.open} == {"consult_unhealthy", "baseline_deviation"}
    (paged,) = out.paged
    assert paged.kind == "consult_unhealthy" and paged.rule_entry == "consult_unhealthy >= high"
    assert paged.delivered and paged.outcomes == ["delivered"]
    counts = {(c.kind, c.severity): c for c in out.counts}
    assert counts[("baseline_deviation", "medium")].logged == 1
    kinds = {k.kind: k for k in out.kinds}
    assert kinds["baseline_deviation"].pages_when == []
    assert kinds["volume_surge"].pages_when == ["volume_surge >= medium"]
    assert kinds["consult_unhealthy"].pages_when == ["consult_unhealthy >= high"]
