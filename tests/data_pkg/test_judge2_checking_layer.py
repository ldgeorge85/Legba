# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""JUDGE-2 (W11b) — the checking layer never dies silently again. REAL DRIVER.

The 2026-10-02 23:08Z → 10-05 02:03Z OpenRouter 402 outage published 641 + 628
floor-only critiques on 10-03/10-04 under a green run counter; the only page
came 14 h late and nothing on a reader surface said "unverified". Every value
these tests claim is PERSISTED is read back from the test database:

  1. the critic, through ``verify_inline_target_finding`` on a real connection,
     writes a critique whose row says WHICH chain rung judged it (rung 1's route
     gone → rung 2) — and, when every rung fails, ``unverified_reason``;
  2. ``GET /v3/system/judge-stats/health`` (through the real FastAPI app over
     ASGI) reports ``unverified_since`` = the earliest critique of the CURRENT
     floor-only streak, NULL once an adjudicated critique lands after it;
  3. the watchdog's at-once page (``llm_route_auth_failed``) writes ONE durable
     ``alert_sink_deliveries`` row per component per 6 h, never for an in-house
     host, and closes the episode on the first success;
  4. ``scripts/rejudge_floor_critiques.py`` dry-run counts a fixture by day, and
     ``--apply`` refuses without an explicit ``--limit`` (SEAMS #69, built in
     W11-F102 — the apply itself is ``tests/runtime/test_rejudge_floor.py``).

The judge rungs are the only doubles (the only external process); no paid or
hosted model is called. Every row is scoped by a throwaway id and deleted at
teardown (the W8 rule).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.provenance.models import FindingPayload
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import MASTER_KEY_ENV, CredentialVault
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.judge_stats_api import build_judge_stats_router
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache
from legba.data.stack.llm.base import HardLLMFailure
from legba.runtime import judge_fallback_chain as JC
from legba.runtime import llm_route_liveness as L
from legba.runtime.actor_critic import verify_inline_target_finding
from legba.runtime.liveness_watchdog import LivenessWatchdog, WatchdogConfig

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "66" * 32)

_HEALTH = "/api/v1/v3/system/judge-stats/health"
_STATS = "/api/v1/v3/system/judge-stats"
_CLAIM_NUM = re.compile(r"^\s*(\d+)\.\s", re.M)

R1 = "llm.judge.judge2probe_hosted_a"
R2 = "llm.judge.judge2probe_hosted_b"
R3 = "llm.judge2probe.inhouse_small"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg(migrated_pg: PostgresConfig):
    store = PostgresStore(migrated_pg)
    await store.connect()
    yield store
    await store.close()


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    reasoning_tokens = 0


class _Answer:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()
        self.raw_response = {"choices": [{}]}


class _Rung:
    """A judge rung double. ``fail`` → raise it on every call; else answer one
    ``supported`` verdict per claim it was actually asked to grade."""

    def __init__(self, name: str, *, fail: BaseException | None = None) -> None:
        self.subprovider = f"sub-{name}"
        self._cfg = SimpleNamespace(model_name=SimpleNamespace(raw=f"vendor/{name}"))
        self.fail = fail
        self.calls = 0

    async def chat_complete(self, messages, **kw):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        tail = str(messages[-1]["content"]).split("CLAIMS:\n")[-1]
        n = len({int(m) for m in _CLAIM_NUM.findall(tail)})
        return _Answer(json.dumps({"verdicts": ["supported"] * n}))


def _chain(*rungs: _Rung) -> JC.JudgeChain:
    refs = (R1, R2, R3)[: len(rungs)]
    by_ref = dict(zip(refs, rungs))

    async def factory(ref: str):
        return by_ref[ref]

    return JC.JudgeChain(refs, first_handler=rungs[0], factory=factory,
                         route_class="configured")


def _deps(judge: Any, analyst_id: str) -> Any:
    descriptor = SimpleNamespace(
        identity=SimpleNamespace(kind="inline_target", id=analyst_id, version="1.0.0"),
        method=SimpleNamespace(llm={"primary": {"raw": "llm.primary.openai_compat"},
                                    "verify": {"raw": R1}}),
    )
    return SimpleNamespace(
        descriptor=descriptor, verify_judge=judge, verify_judge_ref=R1,
        verify_judge_route="configured",
    )


def _finding() -> FindingPayload:
    return FindingPayload(
        title="Lira",
        body="## Key developments\n- The lira fell three percent on Monday [1].\n",
        confidence=0.8,
        data={"citations": [{
            "marker": "[1]", "signal_id": str(uuid4()),
            "title": "Lira drops 3% on the day",
            "source_text": "The lira fell three percent on Monday.",
        }]},
    )


async def _critique_row(pg: PostgresStore, fid) -> dict:
    async with pg.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT data FROM analyst_outputs WHERE kind = 'critique' "
            "AND data->>'analyzed_output_id' = $1", str(fid),
        )
    assert row is not None, "the critique row was not persisted"
    data = row["data"]
    return json.loads(data) if isinstance(data, str) else dict(data)


@pytest_asyncio.fixture
async def critic_rows(pg):
    fids: list = []
    yield fids
    async with pg.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE kind = 'critique' "
            "AND data->>'analyzed_output_id' = ANY($1::text[])",
            [str(f) for f in fids],
        )


# ---------------------------------------------------------------------------
# 1. The critic writes WHICH rung judged — and why the floor, when it is one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_402_on_rung_one_is_judged_by_rung_two_and_the_row_says_so(
    pg, critic_rows, monkeypatch,
):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    monkeypatch.delenv(JC.JUDGE_WEAK_RUNGS_ENV, raising=False)
    r1 = _Rung("hosted-a", fail=HardLLMFailure("payment required", status=402))
    r2 = _Rung("hosted-b")
    fid = uuid4()
    critic_rows.append(fid)
    async with pg.pool.acquire() as conn:
        result = await verify_inline_target_finding(
            conn, deps=_deps(_chain(r1, r2), "judge2_probe_analyst"),
            finding_id=fid, finding_payload=_finding(), run_id=uuid4(),
        )
    assert result is not None and result["judge_status"] == "llm"
    assert r1.calls == 1 and r2.calls >= 1
    data = await _critique_row(pg, fid)
    assert data["judge_route"] == f"chain:{R2}"
    assert data["judge_llm_ref"] == R2
    assert data["judge_model"] == "vendor/hosted-b"
    v = data["data"]["verification"]
    assert v["judge_status"] == "llm"
    assert v["judge_route"] == f"chain:{R2}"
    assert v["judge_chain"]["used"] == R2 and v["judge_chain"]["rung"] == 2
    assert v["judge_chain"]["tried"][0]["http_status"] == "402"
    assert "unverified_reason" not in v


@pytest.mark.asyncio
async def test_every_rung_failing_floors_with_unverified_reason_persisted(
    pg, critic_rows, monkeypatch,
):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    rungs = [_Rung(n, fail=HardLLMFailure("x", status=402)) for n in ("a", "b", "c")]
    fid = uuid4()
    critic_rows.append(fid)
    async with pg.pool.acquire() as conn:
        result = await verify_inline_target_finding(
            conn, deps=_deps(_chain(*rungs), "judge2_probe_analyst"),
            finding_id=fid, finding_payload=_finding(), run_id=uuid4(),
        )
    assert result is not None and result["judge_status"] == "deterministic"
    assert all(r.calls >= 1 for r in rungs)
    data = await _critique_row(pg, fid)
    # The floor keeps today's stamps (rung 1, the ladder's class) …
    assert data["judge_llm_ref"] == R1 and data["judge_route"] == "configured"
    v = data["data"]["verification"]
    assert v["judge_unavailable_reason"] == "judge_error"
    # … and SAYS why it is the floor: the LAST rung's class, status and time.
    reason = v["unverified_reason"]
    assert reason["class"] == "credit" and reason["http_status"] == "402"
    assert reason["component_id"] == R3 and reason["rungs_tried"] == 3
    assert datetime.fromisoformat(reason["at"]).tzinfo is not None


@pytest.mark.asyncio
async def test_rung_one_answering_writes_todays_row_byte_for_byte(
    pg, critic_rows, monkeypatch,
):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    first = _Rung("hosted-a")
    fid = uuid4()
    critic_rows.append(fid)
    async with pg.pool.acquire() as conn:
        await verify_inline_target_finding(
            conn, deps=_deps(_chain(first, _Rung("b"), _Rung("c")), "judge2_probe_analyst"),
            finding_id=fid, finding_payload=_finding(), run_id=uuid4(),
        )
    data = await _critique_row(pg, fid)
    assert (data["judge_route"], data["judge_llm_ref"], data["judge_model"]) == (
        "configured", R1, "sub-hosted-a",
    )
    v = data["data"]["verification"]
    assert "judge_chain" not in v and "unverified_reason" not in v


# ---------------------------------------------------------------------------
# 2. The health fact over the real route
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)
    store = PostgresStore(migrated_pg)
    await store.connect()
    identity = SigningIdentity(
        signing_key=SigningKey(b"judge2-health-route-seed-0000001"[:32]),
        signer_did="did:legba:registry:judge2-health-test",
    )
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(store)
    vocab = VocabularyCache(store)
    vault = CredentialVault(store)
    registry = DescriptorRegistry(
        store, vocabulary_cache=vocab, signing_identity=identity,
        audit_logger=audit, dead_letter=dlq,
    )
    await registry.start()
    deps = RegistryAPIDeps(
        descriptor_registry=registry,
        stack_registry=StackRegistry(store, vault, audit=audit, dlq=dlq),
        vault=vault, dlq=dlq, audit_logger=audit, vocabulary_cache=vocab,
        nats_store=None,
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_judge_stats_router(deps), prefix="/api/v1/v3")
    run_ids: list = []
    output_ids: list = []

    async def trace(*, analyst: str, calls: list[dict], started: datetime):
        run_id = uuid4()
        async with store.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO analyst_traces (run_id, analyst_id, analyst_version, "
                "cadence_trigger, llm_calls, status, run_started_at, receipt_hash) "
                "VALUES ($1, $2, 'v1', 'test', $3::jsonb, 'success', $4, $5)",
                run_id, analyst, json.dumps(calls), started, f"h-{run_id}",
            )
        run_ids.append(run_id)
        return run_id

    async def critique(*, analyst: str, status: str, reason: str | None, ref: str,
                       created: datetime, analyzed: str | None = None):
        oid = uuid4()
        payload = {
            "analyzed_analyst_id": analyst,
            "analyzed_output_id": analyzed or str(uuid4()),
            "data": {"verification": {
                "judge_status": status, "judge_unavailable_reason": reason,
                "judge_llm_ref": ref,
            }},
        }
        async with store.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO analyst_outputs (id, kind, title, body, confidence, data, "
                "schema_uri, run_id, produced_at, created_at) VALUES ($1, 'critique', "
                "'Faithfulness verify (score 0.85)', '', 0.5, $2::jsonb, "
                "'iglu:legba/analyst_output/jsonschema/1-0-0', $3, $4, $4)",
                oid, json.dumps(payload), uuid4(), created,
            )
        output_ids.append(oid)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, trace, critique
    async with store.pool.acquire() as conn:
        await conn.execute("DELETE FROM analyst_outputs WHERE id = ANY($1::uuid[])", output_ids)
        await conn.execute("DELETE FROM analyst_traces WHERE run_id = ANY($1::uuid[])", run_ids)
    await registry.stop()
    await store.close()


@pytest.mark.asyncio
async def test_health_reports_unverified_since_for_the_current_floor_streak(api):
    client, trace, critique = api
    analyst = f"judge2_health_{uuid4().hex[:8]}"
    t0 = datetime.now(timezone.utc) - timedelta(hours=10)
    # Adjudicated → then three floor-only (judge asked, no answer) → the streak.
    await critique(analyst=analyst, status="llm", reason=None, ref=R1, created=t0)
    for h in (1, 2, 3):
        await critique(analyst=analyst, status="deterministic", reason="judge_error",
                       ref=R1, created=t0 + timedelta(hours=h))
    # An unsampled floor never asked a judge: it neither joins nor breaks a streak.
    await critique(analyst=analyst, status="unsampled", reason=None, ref="",
                   created=t0 + timedelta(hours=4))
    ok_at, fail_at = t0 + timedelta(minutes=5), t0 + timedelta(hours=3)
    await trace(analyst=analyst, started=t0, calls=[
        {"leg": "verify_judge", "component_id": R1, "status": "success",
         "model": "vendor/a", "at": ok_at.isoformat()},
        {"leg": "verify_judge", "component_id": R1, "status": "hard_fail",
         "http_status": 402, "model": "vendor/a",
         "at": (t0 + timedelta(hours=1)).isoformat()},
        {"leg": "verify_judge", "component_id": R1, "status": "hard_fail",
         "http_status": 402, "model": "vendor/a", "at": fail_at.isoformat()},
        {"leg": "primary", "component_id": "llm.primary.x", "status": "success"},
    ])
    r = await client.get(_HEALTH, params={"analyst_id": analyst})
    assert r.status_code == 200
    body = r.json()
    assert body["measured"] is True
    assert datetime.fromisoformat(body["unverified_since"]) == t0 + timedelta(hours=1)
    assert body["unverified_n"] == 3 and body["open_at_window_start"] is False
    comp = {c["component_id"]: c for c in body["components"]}[R1]
    assert comp["failure_class"] == "credit"
    assert comp["last_failure_http_status"] == "402"
    assert comp["consecutive_failures"] == 2
    assert datetime.fromisoformat(comp["last_success_at"]) == ok_at
    assert datetime.fromisoformat(comp["last_failure_at"]) == fail_at
    assert datetime.fromisoformat(comp["unverified_since"]) == t0 + timedelta(hours=1)
    assert "llm.primary.x" not in {c["component_id"] for c in body["components"]}
    # The judge-stats payload carries the same block.
    stats = (await client.get(_STATS, params={"analyst_id": analyst, "days": 2})).json()
    assert stats["health"]["unverified_n"] == 3

    # The judge answers again → the streak closes → NULL, never a stale time.
    await critique(analyst=analyst, status="llm", reason=None, ref=R1,
                   created=t0 + timedelta(hours=5))
    body = (await client.get(_HEALTH, params={"analyst_id": analyst})).json()
    assert body["unverified_since"] is None and body["unverified_n"] == 0
    comp = {c["component_id"]: c for c in body["components"]}[R1]
    assert comp["unverified_since"] is None


# ---------------------------------------------------------------------------
# 3. The at-once page on a hosted route's 401/402/403/404
# ---------------------------------------------------------------------------


class _Nats:
    async def publish_core(self, *a, **k):
        return None

    async def publish_json(self, *a, **k):
        return None


class _Sinks:
    def __init__(self) -> None:
        self.payloads: list = []

    async def fan_out(self, payload) -> None:
        self.payloads.append(payload)


@pytest_asyncio.fixture
async def auth_rig(pg, monkeypatch):
    monkeypatch.setenv(L.INHOUSE_HOSTS_ENV, "inhouse.judge2probe.test")
    monkeypatch.delenv(L.AUTH_PAGE_DEDUP_ENV, raising=False)
    monkeypatch.delenv(L.AUTH_RECEIPT_LAG_ENV, raising=False)
    started = datetime.now(timezone.utc) - timedelta(seconds=5)
    hosted = f"llm.judge2probe.hosted_{uuid4().hex[:8]}"
    inhouse = f"llm.judge2probe.inhouse_{uuid4().hex[:8]}"
    run_ids: list = []
    async with pg.pool.acquire() as conn:
        for cid, endpoint in ((hosted, "https://router.example.com/api/v1"),
                              (inhouse, "https://slm.inhouse.judge2probe.test")):
            await conn.execute(
                "INSERT INTO stack_components (component_id, version, schema_uri, kind, "
                "is_head, state, owner, name, body) VALUES ($1, $2, 'legba/stack/1.0.0', "
                "'llm_provider', true, 'active', 'test', $1, $3::jsonb)",
                cid, "0" * 64,
                json.dumps({"config": {"api_endpoint": {"raw": endpoint},
                                       "model_name": {"raw": "vendor/m"}}}),
            )

    async def receipt(component: str, *, at: datetime, ok: bool, http: int = 402):
        run_id = uuid4()
        call = {"component_id": component, "model": "vendor/m", "at": at.isoformat(),
                "status": "success" if ok else "hard_fail"}
        if not ok:
            call["http_status"] = http
        async with pg.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO analyst_traces (run_id, analyst_id, analyst_version, "
                "cadence_trigger, llm_calls, status, run_started_at, receipt_hash) "
                "VALUES ($1, 'judge2_auth_probe', 'v1', 'test', $2::jsonb, 'success', "
                "$3, $4)",
                run_id, json.dumps([call]), at - timedelta(minutes=5), f"h-{run_id}",
            )
        run_ids.append(run_id)

    def watchdog(sinks: _Sinks) -> LivenessWatchdog:
        cfg = WatchdogConfig(stall_after_s=900.0, realert_every_s=1800.0,
                             check_interval_s=60.0)
        return LivenessWatchdog(_Nats(), cfg, pg_store=pg.pool, alert_sinks=sinks)

    async def rows(component: str) -> list[dict]:
        async with pg.pool.acquire() as conn:
            got = await conn.fetch(
                "SELECT severity, payload_summary FROM alert_sink_deliveries "
                "WHERE channel_name = $1 AND sink_target = $2 ORDER BY attempted_at",
                L.ALERT_CHANNEL_LLM_ROUTE_AUTH, component,
            )
        out = []
        for g in got:
            p = g["payload_summary"]
            out.append({"severity": g["severity"],
                        "payload": json.loads(p) if isinstance(p, str) else dict(p)})
        return out

    yield SimpleNamespace(hosted=hosted, inhouse=inhouse, receipt=receipt,
                          watchdog=watchdog, rows=rows)
    async with pg.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM alert_sink_deliveries WHERE channel_name = $1 AND attempted_at >= $2",
            L.ALERT_CHANNEL_LLM_ROUTE_AUTH, started,
        )
        await conn.execute("DELETE FROM analyst_traces WHERE run_id = ANY($1::uuid[])", run_ids)
        await conn.execute(
            "DELETE FROM stack_components WHERE component_id = ANY($1::text[])",
            [hosted, inhouse],
        )


@pytest.mark.asyncio
async def test_a_hosted_402_pages_at_once_once_per_6h_and_clears_on_success(auth_rig):
    rig = auth_rig
    now = datetime.now(timezone.utc)
    await rig.receipt(rig.hosted, at=now - timedelta(minutes=3), ok=False)
    await rig.receipt(rig.inhouse, at=now - timedelta(minutes=3), ok=False)
    sinks = _Sinks()
    wd = rig.watchdog(sinks)
    paged = await L.check_auth_failed_once(wd, now=now)
    assert rig.hosted in paged and rig.inhouse not in paged
    got = await rig.rows(rig.hosted)
    assert len(got) == 1
    p = got[0]["payload"]
    assert got[0]["severity"] == "high"
    assert p["state"] == "entered" and p["kind"] == "llm_route_auth_failed"
    assert p["http_status"] == "402" and p["model"] == "vendor/m"
    assert p["page_budget_exempt"] is True and p["reminder"] is False
    assert rig.hosted in p["body"] and "OUT OF CREDIT" in p["body"]
    assert p["first_failure_at"]
    # The SAME outward fan-out (ntfy / webhook) every watchdog channel rides.
    assert [rig.hosted in s.summary for s in sinks.payloads].count(True) == 1
    assert await rig.rows(rig.inhouse) == []

    # Next tick, still failing: silence (the edge already fired).
    assert await L.check_auth_failed_once(wd, now=now + timedelta(minutes=1)) == []
    # A RESTARTED watchdog re-seeds from the ledger: still silence inside 6 h.
    wd2 = rig.watchdog(_Sinks())
    assert await L.check_auth_failed_once(wd2, now=now + timedelta(minutes=2)) == []
    assert len(await rig.rows(rig.hosted)) == 1

    # Still failing 7 h later → ONE reminder page.
    later = now + timedelta(hours=7)
    await rig.receipt(rig.hosted, at=later - timedelta(minutes=2), ok=False)
    assert await L.check_auth_failed_once(wd2, now=later) == [rig.hosted]
    got = await rig.rows(rig.hosted)
    assert [g["payload"]["state"] for g in got] == ["entered", "entered"]
    assert got[-1]["payload"]["reminder"] is True

    # A success newer than the last failure closes the episode.
    await rig.receipt(rig.hosted, at=later + timedelta(minutes=1), ok=True)
    assert await L.check_auth_failed_once(wd2, now=later + timedelta(minutes=2)) == []
    got = await rig.rows(rig.hosted)
    assert got[-1]["payload"]["state"] == "recovered" and got[-1]["severity"] == "info"


def test_the_inhouse_default_is_derived_from_the_core_plane_host(monkeypatch):
    monkeypatch.delenv(L.INHOUSE_HOSTS_ENV, raising=False)
    sfx = L.inhouse_suffixes("api.plane.example.net")
    assert sfx == ("plane.example.net",)
    assert not L.is_hosted("slm.plane.example.net", sfx)
    assert L.is_hosted("openrouter.ai", sfx)
    assert L.is_hosted(None, sfx)  # unknown endpoint → hosted (fail loud)
    assert L.endpoint_host({"api_endpoint": {"raw": "https://API.X.org/v1"}}) == "api.x.org"


def test_the_auth_channel_is_seeded_on_restart_and_budget_exempt():
    from legba.data.analysts.deterministic_handlers._daily_page_budget import (
        is_page_budget_exempt,
    )
    from legba.runtime import liveness_watchdog as lw

    assert L.ALERT_CHANNEL_LLM_ROUTE_AUTH in lw._TRANSITION_CHANNELS
    assert is_page_budget_exempt(L.ALERT_CHANNEL_LLM_ROUTE_AUTH)


# ---------------------------------------------------------------------------
# 4. The rejudge inventory (dry run) — and --apply needs an explicit --limit
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rejudge_floor_critiques.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("rejudge_floor_critiques", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["rejudge_floor_critiques"] = module
    spec.loader.exec_module(module)
    return module


REJUDGE = _load_script()


@pytest.mark.asyncio
async def test_rejudge_dry_run_counts_the_fixture_by_day(api, pg):
    _client, _trace, critique = api
    analyst = f"judge2_rejudge_{uuid4().hex[:8]}"
    d1 = datetime(2026, 10, 3, 6, tzinfo=timezone.utc)
    d2 = datetime(2026, 10, 4, 6, tzinfo=timezone.utc)
    rejudged = str(uuid4())
    for i, analyzed in enumerate((rejudged, None, None)):
        await critique(analyst=analyst, status="deterministic", reason="judge_error",
                       ref=R1, created=d1 + timedelta(minutes=i), analyzed=analyzed)
    await critique(analyst=analyst, status="deterministic", reason="judge_empty",
                   ref=R1, created=d2)
    # A LATER adjudicated critique of the first finding: listed, but no longer
    # floor-only. Not listed at all: an unsampled floor, a flag-off floor.
    await critique(analyst=analyst, status="llm", reason=None, ref=R1, created=d2,
                   analyzed=rejudged)
    await critique(analyst=analyst, status="unsampled", reason=None, ref="", created=d2)
    await critique(analyst=analyst, status="deterministic", reason="flag_off", ref="",
                   created=d2)
    async with pg.pool.acquire() as conn:
        out = await REJUDGE.run(
            conn, since=d1 - timedelta(hours=1), until=d2 + timedelta(hours=1),
            analyst_id=analyst, list_n=2,
        )
    assert out["days"] == [
        {"day": "2026-10-03", "floor_critiques": 3, "still_floor_only": 2},
        {"day": "2026-10-04", "floor_critiques": 1, "still_floor_only": 1},
    ]
    assert out["floor_critiques"] == 4 and out["still_floor_only"] == 3
    assert len(out["listed"]) == 2
    assert "SEAMS #69" in REJUDGE.render(out)


@pytest.mark.asyncio
async def test_rejudge_apply_without_an_explicit_limit_refuses_before_any_read():
    with pytest.raises(SystemExit) as exc:
        await REJUDGE.main(["--apply"])
    assert exc.value.code == 2
