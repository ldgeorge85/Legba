# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X4 ALERTS on the REAL driver — every claim read back from the rows.

The lane's done test, one test per line:

* a trigger tick with 9 band moves yields ONE push (9 alert rows, one ledger
  row carrying all 9 ids, one HTTP publish on ``world``), and the daily page
  budget counts it as ONE page;
* a level-only escalation yields no push (its ledger row says
  ``skipped_level_only``); a transition yields one on ``world``;
* a source stall yields one on ``engine``;
* the volume route counts them per topic and kind (pre-split rows report as
  ``pre_split``);
* the watchdog pages ``alert_volume_high`` once above the declared cap, on
  ``engine``, and recovers;
* the registry modules import in the slim image.

The ntfy server is the only double (a recording HTTP client); the dispatcher,
the sink, the trigger scan, the watchdog and the route are the real ones.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data.alerts import AlertSinkDispatcher, NtfyAlertSink
from legba.data.alerts import topics as T
from legba.data.alerts.sinks import (
    ENV_SUPPRESS_LEVEL_ONLY,
    AlertSinkPayload,
    runtime_alert_payload,
)
from legba.data.analysts.deterministic_handlers import _daily_page_budget as dpb
from legba.data.analysts.deterministic_handlers import alert_trigger_scan as ats
from legba.data.config import PostgresConfig
from legba.data.registry import alert_volume as av
from legba.data.registry.alert_volume_api import register_alert_volume_routes
from legba.data.registry.api import API_TOKEN_ENV
from legba.runtime import alert_volume_watch as vw
from legba.runtime.liveness_watchdog import LivenessWatchdog, WatchdogConfig

pytestmark = [pytest.mark.integration]

_SRC = str(pathlib.Path(__file__).resolve().parents[2] / "src")
NTFY_URL = "http://ntfy.x4.test/legba-alerts"


# ---------------------------------------------------------------------------
# Rig
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    started = datetime.now(timezone.utc) - timedelta(seconds=2)
    async with p.acquire() as conn:
        await conn.execute("TRUNCATE alert_trigger_watermarks")
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id = 'alert_trigger_scan'"
        )
    yield p
    async with p.acquire() as conn:
        await conn.execute(
            "DELETE FROM alert_sink_deliveries WHERE attempted_at >= $1", started
        )
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id = 'alert_trigger_scan'"
        )
    await p.close()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for env in (T.ENV_TOPIC_SPLIT, T.ENV_TOPIC_WORLD, T.ENV_TOPIC_ENGINE,
                ENV_SUPPRESS_LEVEL_ONLY, *av.CAP_ENV_BY_TOPIC.values()):
        monkeypatch.delenv(env, raising=False)


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


class _Deps:
    def __init__(self, pool: Any, dispatcher: Any) -> None:
        self.pg_pool = pool
        self.extras = {"alert_sink_dispatcher": dispatcher}


async def _scan(pool: Any, dispatcher: Any, **opts: Any):
    options = {
        "sub_handler": "alert_trigger_scan", "analyst_id": "alert_trigger_scan",
        "run_id": str(uuid4()), "daily_page_budget": 10_000, **opts,
    }
    return await ats.handle([], options, _Deps(pool, dispatcher))


async def _scorecard(conn: Any, desk: str, band: str) -> UUID:
    row_id = uuid4()
    data = {
        "kind_marker": "scorecard", "tags": ["deterministic", "scorecard"],
        "data": {"sub_handler": "scorecard_producer", "bands": {
            "target_id": desk,
            "dimensions": {"escalation": {
                "band": band, "basis": [], "reason": "qualified",
                "effective_confidence": 0.8,
            }},
        }},
    }
    await conn.execute(
        "UPDATE analyst_outputs SET superseded_by = $1 "
        "WHERE kind = 'scorecard' AND target_id = $2 AND superseded_by IS NULL",
        row_id, desk,
    )
    await conn.execute(
        "INSERT INTO analyst_outputs (id, kind, title, body, confidence, data, "
        "target_id, analyst_id, schema_uri) VALUES ($1, 'scorecard', $2, '', 1.0, "
        "$3::jsonb, $4, 'scorecard_producer', 'iglu:legba/scorecard/jsonschema/1-0-0')",
        row_id, f"Scorecard {desk}", json.dumps(data), desk,
    )
    return row_id


async def _nine_band_moves(pool: Any) -> list[str]:
    """Nine desks, one dimension each: seeded at 'watch', then 7 move worse
    (→ high) and 2 better (→ low) in ONE tick."""
    tag = uuid4().hex[:6]
    desks = [f"desk_x4_{tag}_{i}" for i in range(9)]
    async with pool.acquire() as conn:
        for d in desks:
            await _scorecard(conn, d, "watch")
    await _scan(pool, _dispatcher(pool, _Http()))  # first-ever scan: seeds silently
    async with pool.acquire() as conn:
        for i, d in enumerate(desks):
            await _scorecard(conn, d, "high" if i < 7 else "low")
    return desks


async def _ledger(pool: Any, where: str, *args: Any) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT alert_row_id, channel_name, sink_kind, sink_target, status, "
            f"payload_summary FROM alert_sink_deliveries WHERE {where} "
            "ORDER BY attempted_at", *args,
        )
    out = []
    for r in rows:
        d = dict(r)
        ps = d["payload_summary"]
        d["payload_summary"] = json.loads(ps) if isinstance(ps, str) else dict(ps)
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# 1) Nine band moves in one tick → ONE push
# ---------------------------------------------------------------------------


async def test_a_tick_with_nine_band_moves_yields_one_push(pool) -> None:
    desks = await _nine_band_moves(pool)
    http = _Http()
    result = await _scan(pool, _dispatcher(pool, http))

    # Every move still has its OWN alert row (the morning read folds them per desk).
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, target_id, data FROM analyst_outputs WHERE kind = 'alert' "
            "AND analyst_id = 'alert_trigger_scan' AND target_id = ANY($1::text[])",
            desks,
        )
    assert len(rows) == 9
    batch_keys = {json.loads(r["data"])["data"]["push_batch"] for r in rows}
    assert len(batch_keys) == 1  # one tick, one batch

    # ONE publish, on world, naming its size and listing the moves worst-first.
    band_calls = [c for c in http.calls if "band moves" in c["headers"]["X-Title"]]
    assert len(band_calls) == 1
    [call] = band_calls
    assert call["url"] == "http://ntfy.x4.test/world"
    assert call["headers"]["X-Title"] == "Legba: high - 9 band moves"
    body = call["body"].split("\n")
    assert body[0] == "9 band moves: 7 worse, 2 better"
    assert body[2].endswith("watch→high (worse)")
    assert any(line.endswith("watch→low (better)") for line in body)
    assert "… and 1 more" in call["body"]  # 9 moves over the default cap of 8

    # ONE ledger row for the push, carrying all nine ids.
    [ledger] = await _ledger(
        pool, "sink_kind = 'ntfy' AND payload_summary->>'kind' = 'band_crossing'"
    )
    ps = ledger["payload_summary"]
    assert ledger["status"] == "delivered"
    assert (ps["topic"], ps["batch_count"]) == ("world", 9)
    assert sorted(ps["batch_alert_ids"]) == sorted(str(r["id"]) for r in rows)
    assert result.finding.data["band_push_batch"] == {"members": 9, "pushes": 1}


async def test_a_batch_shaping_failure_falls_back_to_one_push_per_row(
    pool, monkeypatch
) -> None:
    """Shaping the batch must never cost a page: on a failure the tick pushes
    each row on its own, exactly as before X4."""
    desks = await _nine_band_moves(pool)

    def _boom(*a, **k):
        raise RuntimeError("shaping bug")

    monkeypatch.setattr(ats._band_push_batch, "group_pushes", _boom)
    http = _Http()
    result = await _scan(pool, _dispatcher(pool, http))
    band = [c for c in http.calls if any(d in c["headers"]["X-Title"] for d in desks)]
    assert len(band) == 9
    assert result.finding.data["fanout_failed"] == 0
    assert "band_push_batch" not in result.finding.data


async def test_the_budget_counts_one_batch_as_one_page(pool) -> None:
    desks = await _nine_band_moves(pool)
    http = _Http()
    # A budget of ONE page and ONE per kind: before X4 eight of these nine
    # moves were deferred; now the batch is one unit and takes the one slot.
    await _scan(pool, _dispatcher(pool, http),
                daily_page_budget=1, budget_per_kind_cap=1)
    async with pool.acquire() as conn:
        deferred = await conn.fetch(
            "SELECT (data->'data'->>'budget_deferred')::boolean AS d "
            "FROM analyst_outputs WHERE kind = 'alert' AND target_id = ANY($1::text[])",
            desks,
        )
        day_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        paged = await dpb.count_paged_today(conn, "alert_trigger_scan", day_start)
        by_kind = await dpb.count_paged_today_by_kind(conn, "alert_trigger_scan", day_start)
    assert [r["d"] for r in deferred] == [False] * 9
    assert by_kind.get("band_crossing") == 1
    assert paged == sum(by_kind.values())
    assert len([c for c in http.calls if "band moves" in c["headers"]["X-Title"]]) == 1


# ---------------------------------------------------------------------------
# 2) Escalations — a level never pushes, a transition pushes once on world
# ---------------------------------------------------------------------------


async def _finding(conn: Any, *, analyst: str, desk: str, severity: str,
                   at: datetime) -> UUID:
    fid = uuid4()
    await conn.execute(
        "INSERT INTO analyst_outputs (id, kind, title, body, confidence, severity, "
        "data, target_id, analyst_id, produced_at, schema_uri) VALUES ($1, "
        "'finding', $2, '', 0.8, $3, $4::jsonb, $5, $6, $7, "
        "'iglu:legba/finding/jsonschema/1-0-0')",
        fid, f"{analyst} on {desk}: {severity}", severity,
        json.dumps({"tags": [f"severity:{severity}", "severity_delta:steady"]}),
        desk, analyst, at,
    )
    return fid


async def _escalate(dispatcher: AlertSinkDispatcher, fid: UUID, desk: str) -> Any:
    payload = await dispatcher.payload_for_finding(
        channel_name="escalations", alert_row_id=str(fid), target_id=desk,
        severity="high", effective_confidence=0.8, title="Posture holds",
        faithfulness_score=0.9,
    )
    return payload, await dispatcher.fan_out(payload)


async def test_a_level_only_escalation_yields_no_push(pool) -> None:
    desk, now = f"desk_x4lvl_{uuid4().hex[:6]}", datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        await _finding(conn, analyst="military_posture", desk=desk,
                       severity="high", at=now - timedelta(hours=12))
        fid = await _finding(conn, analyst="military_posture", desk=desk,
                             severity="high", at=now)
    http = _Http()
    payload, [res] = await _escalate(_dispatcher(pool, http), fid, desk)

    assert payload.transition == "high → high (level-only)"
    assert res.outcome == "skipped_level_only" and http.calls == []
    [row] = await _ledger(pool, "alert_row_id = $1", fid)
    assert row["status"] == "skipped_level_only" and row["sink_kind"] == "ntfy"
    assert row["payload_summary"]["topic"] == "world"
    assert row["payload_summary"]["transition"] == "high → high (level-only)"


async def test_a_transition_yields_one_push_on_world(pool) -> None:
    desk, now = f"desk_x4tr_{uuid4().hex[:6]}", datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        await _finding(conn, analyst="military_posture", desk=desk,
                       severity="elevated", at=now - timedelta(hours=12))
        # Another unit's read on the same desk is NOT this unit's previous read.
        await _finding(conn, analyst="escalation", desk=desk,
                       severity="high", at=now - timedelta(hours=1))
        fid = await _finding(conn, analyst="military_posture", desk=desk,
                             severity="high", at=now)
    http = _Http()
    payload, [res] = await _escalate(_dispatcher(pool, http), fid, desk)

    assert payload.transition == "elevated → high" and res.delivered
    assert [c["url"] for c in http.calls] == ["http://ntfy.x4.test/world"]
    [row] = await _ledger(pool, "alert_row_id = $1", fid)
    assert row["status"] == "delivered"
    assert row["payload_summary"]["topic"] == "world"


# ---------------------------------------------------------------------------
# 3) A source stall → one push on engine
# ---------------------------------------------------------------------------


async def test_a_source_stall_yields_one_push_on_engine(pool) -> None:
    http = _Http()
    wd = LivenessWatchdog(
        _Nats(), WatchdogConfig(stall_after_s=900.0, realert_every_s=1800.0,
                                check_interval_s=60.0),
        pg_store=pool, alert_sinks=_dispatcher(pool, http),
    )
    source = f"source.x4_{uuid4().hex[:6]}"
    await wd._record_transition(
        channel="source_cadence_stall", entity_id=source, state="entered",
        severity="high", kind="source_cadence_stall",
        title=f"Source poll stall: {source} silent 4.0h", body="silent",
    )
    assert [c["url"] for c in http.calls] == ["http://ntfy.x4.test/engine"]
    rows = await _ledger(
        pool, "channel_name = 'source_cadence_stall' AND ("
        "sink_target = $1 OR payload_summary->>'summary' LIKE '%' || $1 || '%')",
        source,
    )
    # The watchdog's own transition record (internal) + ONE outward push.
    assert sorted(r["sink_kind"] for r in rows) == ["liveness_watchdog", "ntfy"]
    ntfy = [r for r in rows if r["sink_kind"] == "ntfy"]
    assert ntfy[0]["status"] == "delivered"
    assert ntfy[0]["payload_summary"]["topic"] == "engine"
    assert ntfy[0]["payload_summary"]["kind"] == "source_cadence_stall"


class _Nats:
    async def publish_core(self, *a, **k):
        return None

    async def publish_json(self, *a, **k):
        return None


# ---------------------------------------------------------------------------
# 4) The volume route counts them
# ---------------------------------------------------------------------------


async def test_the_volume_route_counts_per_topic_and_kind(pool) -> None:
    tag = uuid4().hex[:6]
    world_kind, engine_kind, level_kind = f"x4w_{tag}", f"x4e_{tag}", f"x4l_{tag}"
    http = _Http()
    d = _dispatcher(pool, http)
    # One batched world push carrying 3 alerts, two engine pushes, one level.
    await d.fan_out(AlertSinkPayload(
        summary="3 band moves: 3 worse", severity="high", channel_name="trigger_scan",
        kind=world_kind, topic="world", verify_state="unverified — x",
        batch_count=3, batch_alert_ids=("a", "b", "c"),
    ))
    for i in range(2):
        await d.fan_out(runtime_alert_payload(channel_name=engine_kind, summary=f"s{i}"))
    await d.fan_out(AlertSinkPayload(
        summary="held", severity="high", channel_name="escalations", kind=level_kind,
        topic="world", verify_state="faithfulness=0.90", level_only=True,
        transition="high → high (level-only)",
    ))
    # A pre-split row: no topic recorded (written before X4).
    legacy = f"x4legacy_{tag}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO alert_sink_deliveries (channel_name, sink_kind, status, "
            "payload_summary) VALUES ($1, 'ntfy', 'delivered', '{}'::jsonb)", legacy,
        )
        # An internal row is never a push.
        await conn.execute(
            "INSERT INTO alert_sink_deliveries (channel_name, sink_kind, status, "
            "payload_summary) VALUES ($1, 'alert', 'delivered', '{}'::jsonb)", legacy,
        )

    os.environ.pop(API_TOKEN_ENV, None)
    app = FastAPI()
    router = APIRouter()
    register_alert_volume_routes(router, SimpleNamespace(
        descriptor_registry=SimpleNamespace(pg=pool)))
    app.include_router(router, prefix="/api/v1/v3")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://testserver") as client:
        r = await client.get("/api/v1/v3/system/alerts/volume", params={"days": 1})
        bad = await client.get("/api/v1/v3/system/alerts/volume", params={"days": 0})
    assert r.status_code == 200, r.text
    assert bad.status_code == 422
    body = r.json()
    assert body["measured"] is True and body["window_days"] == 1
    assert body["caps"] == {"world": 50, "engine": 50, "nightly": 50}  # Nightly is ledgered now
    assert body["cap_env"]["world"] == "LEGBA_ALERT_VOLUME_DAILY_CAP_WORLD"
    kinds = {(k["topic"], k["kind"]): k for k in body["kinds"]}
    w = kinds[("world", world_kind)]
    assert (w["pushed"], w["alerts_carried"]) == (1, 3)
    assert kinds[("engine", engine_kind)]["pushed"] == 2
    lv = kinds[("world", level_kind)]
    assert (lv["pushed"], lv["level_only"]) == (0, 1)
    pre = kinds[("pre_split", legacy)]
    assert pre["pushed"] == 1  # the internal 'alert' row is not counted
    assert {t["topic"] for t in body["topics"]} >= {"world", "engine", "pre_split"}
    assert all(t["per_day"] is not None for t in body["topics"])
    assert body["as_of"] is not None


# ---------------------------------------------------------------------------
# 5) The watchdog pages alert_volume_high once, on engine, and recovers
# ---------------------------------------------------------------------------


class _Sinks:
    def __init__(self) -> None:
        self.payloads: list = []

    async def fan_out(self, payload) -> None:
        self.payloads.append(payload)


async def test_alert_volume_high_pages_once_on_engine_and_recovers(pool, monkeypatch) -> None:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            av.VOLUME_WINDOW_SQL, datetime.now(timezone.utc) - timedelta(hours=24),
            list(av.OUTWARD_SINK_KINDS),
        )
    baseline = av.pushed_by_topic(av.summarize([dict(r) for r in rows])).get("world", 0)
    monkeypatch.setenv("LEGBA_ALERT_VOLUME_DAILY_CAP_WORLD", str(baseline + 2))
    async with pool.acquire() as conn:
        for _ in range(3):  # baseline + 3 > cap
            await conn.execute(
                "INSERT INTO alert_sink_deliveries (channel_name, sink_kind, status, "
                "payload_summary) VALUES ('x4vol_probe', 'ntfy', 'delivered', "
                "'{\"topic\": \"world\", \"kind\": \"x4vol_probe\"}'::jsonb)"
            )
    sinks = _Sinks()
    wd = LivenessWatchdog(
        _Nats(), WatchdogConfig(stall_after_s=900.0, realert_every_s=1800.0,
                                check_interval_s=60.0),
        pg_store=pool, alert_sinks=sinks,
    )
    assert await vw.check_alert_volume_once(wd, force=True) == ["world"]
    [page] = sinks.payloads
    assert page.topic == "engine" and page.channel_name == "alert_volume_high"
    assert f"cap {baseline + 2}" in page.summary and "x4vol_probe 3" in page.detail

    # Still above: silence. A restarted watchdog re-seeds from the ledger.
    assert await vw.check_alert_volume_once(wd, force=True) == []
    wd2 = LivenessWatchdog(_Nats(), pg_store=pool, alert_sinks=_Sinks())
    assert await vw.check_alert_volume_once(wd2, force=True) == []

    # The cap rises over the count: recovered.
    monkeypatch.setenv("LEGBA_ALERT_VOLUME_DAILY_CAP_WORLD", str(baseline + 10))
    await vw.check_alert_volume_once(wd, force=True)
    states = [
        r["payload_summary"]["state"]
        for r in await _ledger(pool, "channel_name = 'alert_volume_high' "
                               "AND sink_target = 'world'")
    ]
    assert states == ["entered", "recovered"]
    # The rate limit holds without force.
    assert await vw.check_alert_volume_once(wd) == []


def test_alert_volume_high_is_budget_exempt() -> None:
    assert dpb.is_page_budget_exempt(vw.ALERT_CHANNEL_ALERT_VOLUME)


# ---------------------------------------------------------------------------
# 6) Registry-slim
# ---------------------------------------------------------------------------


def test_the_alert_volume_modules_import_in_the_slim_image() -> None:
    code = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None
        import legba.data.registry.alert_volume as av
        import legba.data.registry.alert_volume_api as api
        import legba.data.registry.v3_api as v3
        assert callable(api.register_alert_volume_routes)
        bad = [m for m in sys.modules
               if m.startswith(("legba.runtime", "legba.data.analysts"))]
        assert not bad, bad
        print("OK")
        """
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p)}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, env=env, timeout=120)
    assert result.returncode == 0 and "OK" in result.stdout, result.stderr
