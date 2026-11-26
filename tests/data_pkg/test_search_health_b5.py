# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X1/B5 — typed absence tells the truth about a failed search; search health
is measured.

The defect: every ``searched_found_nothing`` absence on the us/cn/ua desks (70
of 70 sampled through ``/v3/absence``) carried a proof that said the search
plane was DEGRADED. Those were searches that could not look properly, typed as
"we looked and found nothing".

THE DONE LINE AS A TEST: no ``searched_found_nothing`` item ever carries a
DEGRADED proof — asserted as a property over the rule's whole input space
(:func:`test_no_found_nothing_item_ever_carries_a_degraded_proof`) and on rows
read back through the real ``/v3/absence`` app on the real driver.

Also here:
  * the ``web_search`` health block (engines asked/answered/errored, result
    count, elapsed) on every outcome, and carried through the REAL audit chain
    — tool → ``run_search`` → ``grade_claim`` → ``write_grade`` → asyncpg →
    ``/v3/absence`` (migration 0235's column read back);
  * ``GET /v3/system/search-health`` — per-provider and per-engine shares, with
    ``null`` (never 0) where there is no denominator;
  * the watchdog's ``search_degraded`` page firing on a fixture, once (the
    transition edge), and recovering;
  * the slim-import guard for the two registry modules.

``external_grades`` is append-only (migration 0190), so every row here uses a
throwaway provider / desk / engine id and the assertions read only those.
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
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data.analysts.agency.tools import ToolCall, ToolContext
from legba.data.analysts.agency.web_tools import web_search_tool
from legba.data.analysts.deterministic_handlers import (
    _external_audit_claims as width_claims,
)
from legba.data.analysts.deterministic_handlers import (
    _external_audit_grader as width_grader,
)
from legba.data.analysts.deterministic_handlers._external_audit_search import (
    run_search,
)
from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.provenance import external_grades as eg
from legba.data.registry import search_health as sh
from legba.data.registry.absence_api import audit_item, read_absences
from legba.data.registry.api import API_TOKEN_ENV
from legba.data.registry.search_health_api import (
    build_search_health_router,
    read_search_health_window,
)
from legba.data.schemas.action_pack import ActionPack
from legba.data.stack.search import (
    CONTROL_PROBE_QUERY,
    DEFAULT_LIVENESS_CACHE,
    TransientSearchFailure,
    parse_searxng_payload,
)
from legba.runtime import search_degraded_watch as sw
from legba.runtime.liveness_watchdog import LivenessWatchdog, WatchdogConfig

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC = str(_REPO_ROOT / "src")

#: The live shape (10-05 → 10-08 runtime log): 30 hits, reuters failing.
_LIVE_DEGRADED = {
    "query": "q",
    "results": [
        {"url": "https://a.example/1", "title": "A", "content": "c",
         "engine": "bing", "engines": ["bing", "yahoo"]},
        {"url": "https://a.example/2", "title": "B", "content": "c",
         "engine": "duckduckgo web", "engines": ["duckduckgo web"]},
    ],
    "unresponsive_engines": [["reuters", "HTTP error"]],
}


@pytest.fixture(autouse=True)
def _fresh_liveness_cache(monkeypatch):
    DEFAULT_LIVENESS_CACHE.reset()
    monkeypatch.delenv(sh.DEGRADED_MIN_UNRESPONSIVE_ENV, raising=False)
    yield
    DEFAULT_LIVENESS_CACHE.reset()


# ---------------------------------------------------------------------------
# 1) The rule
# ---------------------------------------------------------------------------


def _block(errored: list[tuple[str, str]], count: int, answered=("bing",)):
    """A health block as the tool builds it: ``count`` hits spread over the
    ``answered`` engines, ``errored`` reported unresponsive."""
    results = [
        {"engine": answered[i % len(answered)] if answered else None}
        for i in range(count)
    ]
    return sh.build_health_block(
        provider="search.b5.local", status="degraded" if errored else "ok",
        provider_degraded=bool(errored), results=results,
        unresponsive=[f"{n}: {w}" for n, w in errored], elapsed_ms=12,
    )


def test_a_pre_health_row_is_read_by_the_providers_flag_conservatively():
    h = sh.read_search_health(search_degraded=True, health={})
    assert h.degraded and not h.recorded
    assert "DEGRADED" in h.describe() and "not recorded" in h.describe()
    clean = sh.read_search_health(search_degraded=False, health={})
    assert not clean.degraded and "DEGRADED" not in clean.describe()


def test_engines_lost_at_the_threshold_is_degraded_and_below_it_is_partial():
    block = _block([("reuters", "HTTP error")], count=30)
    assert block["n_engines_errored"] == 1 and block["result_count"] == 30
    h1 = sh.read_search_health(search_degraded=True, health=block, threshold=1)
    assert h1.degraded and "reuters: HTTP error" in h1.describe()
    h2 = sh.read_search_health(search_degraded=True, health=block, threshold=2)
    assert not h2.degraded and h2.partial
    text = h2.describe()
    assert "DEGRADED" not in text and "reuters" in text and "threshold of 2" in text


def test_zero_results_with_any_engine_error_is_degraded_whatever_the_threshold():
    block = _block([("abcnyheter", "timeout")], count=0, answered=())
    h = sh.read_search_health(search_degraded=True, health=block, threshold=5)
    assert h.degraded
    assert "zero results with an engine error" in h.describe()


def test_the_threshold_is_a_tunable_and_a_nonsense_value_keeps_the_default(monkeypatch):
    monkeypatch.setenv(sh.DEGRADED_MIN_UNRESPONSIVE_ENV, "3")
    assert sh.degraded_min_unresponsive() == 3
    for bad in ("0", "-2", "x", ""):
        monkeypatch.setenv(sh.DEGRADED_MIN_UNRESPONSIVE_ENV, bad)
        assert sh.degraded_min_unresponsive() == sh.DEFAULT_DEGRADED_MIN_UNRESPONSIVE


def test_a_jsonb_string_block_reads_the_same_as_a_dict():
    block = _block([("reuters", "HTTP error")], count=3)
    a = sh.read_search_health(search_degraded=True, health=block, threshold=1)
    b = sh.read_search_health(search_degraded=True, health=json.dumps(block), threshold=1)
    assert a == b


def _grade_row(**over: Any) -> dict[str, Any]:
    row = {
        "claim_key": "k", "claim_text": "No strike was reported.",
        "analyst_id": "country_composition", "verdict": "NOT_FOUND",
        "unchecked_reason": None, "graded_at": datetime.now(timezone.utc),
        "graded_output_id": str(uuid4()), "search_provider": "search.searxng.local",
        "search_status": "degraded", "search_liveness": "unverified",
        "search_degraded": True, "search_health": {}, "n_urls": 3,
    }
    row.update(over)
    return row


def test_no_found_nothing_item_ever_carries_a_degraded_proof():
    """THE DONE LINE as a property: over every input shape the rule reads —
    pre-health and recorded rows, flag on/off, 0-3 lost engines, zero and
    non-zero results, thresholds 1-3 — an item typed ``searched_found_nothing``
    never says DEGRADED, and one whose proof says DEGRADED is ``search_failed``.
    """
    now = datetime.now(timezone.utc)
    engines = [("reuters", "HTTP error"), ("brave.news", "too many requests"),
               ("abcnyheter", "timeout")]
    seen = set()
    for threshold in (1, 2, 3):
        os.environ[sh.DEGRADED_MIN_UNRESPONSIVE_ENV] = str(threshold)
        try:
            for flag in (False, True):
                blocks: list[Any] = [{}]
                for k in range(4):
                    for count in (0, 2, 30):
                        blocks.append(_block(engines[:k], count=count))
                for block in blocks:
                    item = audit_item(
                        _grade_row(search_degraded=flag, search_health=block),
                        cadence_raw=None, now=now,
                    )
                    proof = item.proof.what_was_checked
                    seen.add(item.kind)
                    if item.kind == "searched_found_nothing":
                        assert "DEGRADED" not in proof, proof
                    if "DEGRADED" in proof:
                        assert item.kind == "search_failed", proof
        finally:
            os.environ.pop(sh.DEGRADED_MIN_UNRESPONSIVE_ENV, None)
    assert seen == {"searched_found_nothing", "search_failed"}


def test_the_live_specimen_flips_to_search_failed_with_its_reason():
    """The 70-of-70 shape: NOT_FOUND, provider flag on, no engine record."""
    item = audit_item(_grade_row(), cadence_raw=None, now=datetime.now(timezone.utc))
    assert item.kind == "search_failed"
    assert "did not answer (search plane DEGRADED" in item.reason
    assert "DEGRADED" in item.proof.what_was_checked


# ---------------------------------------------------------------------------
# 2) The health block on every web_search outcome
# ---------------------------------------------------------------------------


class _StubProvider:
    component_id = "search.searxng.local"

    def __init__(self, response: Any):
        self._response = response

    async def search(self, query, *, limit=5, **opts):
        if query == CONTROL_PROBE_QUERY:
            return parse_searxng_payload(
                {"results": [{"url": "https://un.org/", "title": "UN"}]},
                query=query,
            )
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _pack() -> ActionPack:
    return ActionPack.model_validate({
        "identity": {
            "id": "web_access", "name": "Web Access Tools",
            "schema_uri": "legba/action_pack/1.0.0", "version": "a" * 16,
            "state": "active", "owner": "s6_agency",
            "created": datetime.now(timezone.utc).isoformat(),
        },
        "tools": [{"name": "web_search", "config": {}}],
        "governor": {"budget_account": "web_access"},
    }, strict=False)


def _call(query: str = "q") -> ToolCall:
    return ToolCall(
        pack_id="web_access", tool_name="web_search", budget_account="acct",
        requested_by="analyst.test", args={"query": query, "limit": 30},
    )


async def test_an_answered_search_carries_the_engine_health_block():
    provider = _StubProvider(parse_searxng_payload(_LIVE_DEGRADED, query="q"))
    result = await web_search_tool(_call(), _pack(), ToolContext(search=provider))
    assert result.status == "completed"
    health = result.output["health"]
    assert health["version"] == sh.HEALTH_BLOCK_VERSION
    assert health["provider"] == "search.searxng.local"
    assert health["status"] == "degraded" and health["provider_degraded"] is True
    assert health["result_count"] == 2
    assert health["engines_answered"] == ["bing", "duckduckgo web", "yahoo"]
    assert health["engines_errored"] == [{"engine": "reuters", "error": "HTTP error"}]
    assert health["engines_asked"] == ["bing", "duckduckgo web", "reuters", "yahoo"]
    assert health["n_engines_asked"] == 4 and health["n_engines_errored"] == 1
    assert isinstance(health["elapsed_ms"], int) and health["elapsed_ms"] >= 0


async def test_a_search_the_provider_never_answered_has_no_result_count():
    provider = _StubProvider(TransientSearchFailure("timeout"))
    result = await web_search_tool(_call(), _pack(), ToolContext(search=provider))
    assert result.status == "failed"
    health = result.output["health"]
    assert health["error_class"] == "search_unavailable"
    assert health["result_count"] is None, "an unmeasured count is never 0"
    assert health["engines_answered"] == [] and health["status"] == "failed"


async def test_a_degraded_empty_carries_its_block_on_the_failure():
    body = {"results": [], "unresponsive_engines": [["reuters", "HTTP error"]]}
    provider = _StubProvider(parse_searxng_payload(body, query="q"))
    result = await web_search_tool(_call(), _pack(), ToolContext(search=provider))
    assert result.status == "failed"
    health = result.output["health"]
    assert health["result_count"] == 0 and health["status"] == "degraded_empty"
    assert sh.read_search_health(
        search_degraded=True, health=health, threshold=9,
    ).degraded


# ---------------------------------------------------------------------------
# 3) The page — pure halves
# ---------------------------------------------------------------------------


def test_evaluate_pages_over_the_floor_and_holds_a_thin_window():
    summary = {"threshold": 1, "engines": [], "providers": [
        {"provider": "a", "not_found": 25, "not_found_degraded": 10,
         "not_found_degraded_share": 0.4},
        {"provider": "b", "not_found": 25, "not_found_degraded": 5,
         "not_found_degraded_share": 0.2},
        {"provider": "c", "not_found": 3, "not_found_degraded": 3,
         "not_found_degraded_share": 1.0},
    ]}
    paging, thin = sw.evaluate(summary, share_floor=0.2, min_not_found=20)
    assert [p["provider"] for p in paging] == ["a"], "the floor is strict (> 20 %)"
    assert thin == {"c"}


def test_a_share_with_no_denominator_is_null_not_zero():
    s = sh.summarize([{
        "search_provider": "p", "verdict": "SUPPORTED", "search_degraded": False,
        "search_health": {}, "graded_at": datetime.now(timezone.utc),
    }], threshold=1)
    p = s["providers"][0]
    assert p["not_found"] == 0 and p["not_found_degraded_share"] is None
    assert s["engines"] == [] and s["rows_without_engine_record"] == 1


# ---------------------------------------------------------------------------
# 4) Slim import
# ---------------------------------------------------------------------------


def test_the_search_health_modules_import_without_the_runtime_stack() -> None:
    code = textwrap.dedent(
        """
        import sys
        import legba.data.registry.search_health as sh
        import legba.data.registry.search_health_api as api
        assert callable(api.build_search_health_router)
        bad = [m for m in sys.modules
               if m.startswith(("legba.runtime", "legba.data.analysts"))]
        assert not bad, bad
        """
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p)}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# 5) Real driver — migration 0235, the chain, the routes, the page
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg(migrated_pg: PostgresConfig):
    store = PostgresStore(migrated_pg)
    await store.connect()
    yield store
    await store.close()


async def _insert_grade(
    conn, *, provider: str, target: str, verdict: str = "NOT_FOUND",
    degraded: bool, health: dict | None = None, graded_at: datetime | None = None,
) -> str:
    key = f"b5-{uuid4().hex}"
    await conn.execute(
        """
        INSERT INTO public.external_grades
            (claim_key, population, graded_output_id, analyst_id, target_id,
             claim_text, verdict, search_provider, search_status,
             search_liveness, search_degraded, search_health, source_urls,
             grader_family, grader_component_id, grader_pipeline_version,
             rubric_version, rater_role, graded_at)
        VALUES ($1, 'assembly_span', $2, 'escalation', $3,
                'No cross-border strike was reported this week.',
                $4, $5, 'degraded', 'unverified', $6, $7::jsonb,
                ARRAY['https://example.test/a'],
                'b5-test', 'b5-test-component', 'b5/1', 'b5-rubric',
                'primary', $8)
        """,
        key, uuid4(), target, verdict, provider, degraded,
        json.dumps(health or {}), graded_at or datetime.now(timezone.utc),
    )
    return key


class _Binding:
    """The agency binding ``run_search`` drives — admitted, running the REAL
    ``web_search_tool`` over a provider double (the only external process)."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    async def run_tool(self, tool: str, args: dict) -> Any:
        result = await web_search_tool(
            ToolCall(pack_id="web_access", tool_name=tool, budget_account="acct",
                     requested_by="analyst.test", args=args),
            _pack(), ToolContext(search=self._provider),
        )
        return SimpleNamespace(admitted=True, tool_result=result)


class _Grader:
    _instance_id = "llm.judge.b5_stub"

    def __init__(self) -> None:
        self._cfg = SimpleNamespace(model_name=SimpleNamespace(raw="vendor/b5"))

    async def chat_complete(self, *a: Any, **kw: Any) -> Any:
        return SimpleNamespace(
            content='{"verdict":"NOT_FOUND","rationale":"r","evidence":[]}',
            usage=SimpleNamespace(model="", prompt_tokens=0, completion_tokens=0),
            raw_response={},
        )


@pytest.mark.integration
async def test_the_health_block_lands_on_the_grade_and_flips_the_absence(pg):
    """tool → run_search → grade_claim → write_grade → asyncpg → /v3/absence."""
    target = f"country_watch_b5{uuid4().hex[:6]}"
    envelope, unchecked = await run_search(
        _Binding(_StubProvider(parse_searxng_payload(_LIVE_DEGRADED, query="q"))),
        "q", limit=30, provider_order=["search.searxng.local"],
    )
    assert unchecked == "" and envelope.health["n_engines_errored"] == 1
    claim = width_claims.WidthClaim(
        claim_text="No strike on the border was reported this week.",
        population="assembly_span", graded_output_id=str(uuid4()),
        analyst_id="country_composition", target_id=target, desk_key=target,
        origin_head_id=str(uuid4()), start=0, end=10,
    )
    grade = await width_grader.grade_claim(
        _Grader(), claim, envelope, grader_family="b5",
        grader_component_id="llm.judge.b5_stub",
    )
    assert grade.verdict == "NOT_FOUND"
    async with pg.acquire() as conn:
        landed = await eg.write_grade(
            conn, grade.as_dict(), pipeline_version="b5/1",
            graded_at=datetime.now(timezone.utc),
        )
        assert landed is True
        stored = await conn.fetchval(
            "SELECT search_health FROM public.external_grades WHERE claim_key = $1",
            claim.key,
        )
        stored = json.loads(stored) if isinstance(stored, str) else dict(stored)
        assert stored["engines_errored"] == [
            {"engine": "reuters", "error": "HTTP error"}
        ]
        assert stored["result_count"] == 2
        assert stored["engines_answered"] == ["bing", "duckduckgo web", "yahoo"]

        out = await read_absences(
            conn, scope=target, now=datetime.now(timezone.utc), limit_per_kind=50,
        )
    items = [a for a in out.absences if a.subject == claim.key]
    assert len(items) == 1
    assert items[0].kind == "search_failed"
    assert "reuters: HTTP error" in items[0].proof.what_was_checked
    assert "engines answered: bing, duckduckgo web, yahoo" in (
        items[0].proof.what_was_checked
    )


@pytest.mark.integration
async def test_the_absence_route_never_types_a_degraded_search_as_found_nothing(
    pg, monkeypatch,
):
    """Four stored shapes, read through the real route app (ASGI)."""
    from legba.data.registry.absence_api import build_absence_router

    monkeypatch.setenv(sh.DEGRADED_MIN_UNRESPONSIVE_ENV, "2")
    target = f"country_watch_b5{uuid4().hex[:6]}"
    prov = "search.searxng.local"
    async with pg.acquire() as conn:
        legacy = await _insert_grade(conn, provider=prov, target=target, degraded=True)
        partial = await _insert_grade(
            conn, provider=prov, target=target, degraded=True,
            health=_block([("reuters", "HTTP error")], count=30),
        )
        lost_two = await _insert_grade(
            conn, provider=prov, target=target, degraded=True,
            health=_block([("reuters", "HTTP error"),
                           ("brave.news", "too many requests")], count=30),
        )
        clean = await _insert_grade(
            conn, provider=prov, target=target, degraded=False,
            health=_block([], count=30),
        )
    os.environ.pop(API_TOKEN_ENV, None)
    app = FastAPI()
    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=pg))
    app.state.registry_deps = deps
    app.include_router(build_absence_router(deps), prefix="/api/v1/v3")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://testserver") as client:
        r = await client.get("/api/v1/v3/absence",
                             params={"scope": target, "limit_per_kind": 50})
    assert r.status_code == 200, r.text
    items = {a["subject"]: a for a in r.json()["absences"]
             if a["kind"] in ("searched_found_nothing", "search_failed")}
    assert items[legacy]["kind"] == "search_failed"
    assert items[lost_two]["kind"] == "search_failed"
    assert "brave.news" in items[lost_two]["proof"]["what_was_checked"]
    assert items[partial]["kind"] == "searched_found_nothing"
    assert "reuters" in items[partial]["proof"]["what_was_checked"]
    assert items[clean]["kind"] == "searched_found_nothing"
    for item in items.values():
        if item["kind"] == "searched_found_nothing":
            assert "DEGRADED" not in item["proof"]["what_was_checked"]


@pytest.mark.integration
async def test_the_search_health_route_publishes_per_provider_and_per_engine(pg):
    prov = f"search.b5probe_{uuid4().hex[:8]}"
    dead, flaky = f"reuters_{uuid4().hex[:6]}", f"brave_{uuid4().hex[:6]}"
    async with pg.acquire() as conn:
        for i in range(4):
            errored = [(dead, "HTTP error")] + ([(flaky, "timeout")] if i == 0 else [])
            await _insert_grade(
                conn, provider=prov, target="b5_health", degraded=True,
                health=_block(errored, count=10), verdict="NOT_FOUND",
            )
        await _insert_grade(conn, provider=prov, target="b5_health",
                            degraded=True, verdict="SUPPORTED")
        out = await read_search_health_window(conn, days=7)
    assert out.measured is True and out.threshold == 1
    p = {x.provider: x for x in out.providers}[prov]
    assert (p.searches, p.degraded, p.not_found, p.not_found_degraded) == (5, 5, 4, 4)
    assert p.not_found_degraded_share == 1.0
    engines = {e.engine: e for e in out.engines}
    assert engines[dead].errored == 4 and engines[dead].errors == {"HTTP error": 4}
    assert engines[flaky].errored == 1 and engines[flaky].errors == {"timeout": 1}
    # The denominator is every row that recorded engines, not this provider's.
    assert engines[dead].searches == out.rows_with_engine_record >= 4

    os.environ.pop(API_TOKEN_ENV, None)
    app = FastAPI()
    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=pg))
    app.state.registry_deps = deps
    app.include_router(build_search_health_router(deps), prefix="/api/v1/v3")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://testserver") as client:
        r = await client.get("/api/v1/v3/system/search-health", params={"days": 7})
    assert r.status_code == 200
    body = r.json()
    assert body["measured"] is True and body["threshold_env"] == (
        sh.DEGRADED_MIN_UNRESPONSIVE_ENV
    )
    assert prov in {x["provider"] for x in body["providers"]}


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


@pytest.mark.integration
async def test_the_search_degraded_page_fires_once_and_recovers(pg, monkeypatch):
    monkeypatch.setenv(sw.PAGE_MIN_NOT_FOUND_ENV, "10")
    monkeypatch.delenv(sw.PAGE_SHARE_ENV, raising=False)
    started = datetime.now(timezone.utc) - timedelta(seconds=5)
    prov = f"search.b5page_{uuid4().hex[:8]}"
    engine = f"reuters_{uuid4().hex[:6]}"
    async with pg.acquire() as conn:
        for i in range(12):  # 4 of 12 degraded = 33 % > 20 %
            bad = i < 4
            await _insert_grade(
                conn, provider=prov, target="b5_page", degraded=bad,
                health=_block([(engine, "HTTP error")] if bad else [], count=10),
            )

    def watchdog(sinks: _Sinks) -> LivenessWatchdog:
        cfg = WatchdogConfig(stall_after_s=900.0, realert_every_s=1800.0,
                             check_interval_s=60.0)
        return LivenessWatchdog(_Nats(), cfg, pg_store=pg.pool, alert_sinks=sinks)

    async def rows() -> list[dict]:
        async with pg.acquire() as conn:
            got = await conn.fetch(
                "SELECT severity, payload_summary FROM alert_sink_deliveries "
                "WHERE channel_name = $1 AND sink_target = $2 ORDER BY attempted_at",
                sw.ALERT_CHANNEL_SEARCH_DEGRADED, prov,
            )
        out = []
        for g in got:
            p = g["payload_summary"]
            out.append({"severity": g["severity"],
                        "payload": json.loads(p) if isinstance(p, str) else dict(p)})
        return out

    try:
        sinks = _Sinks()
        wd = watchdog(sinks)
        fired = await sw.check_search_degraded_once(wd)
        assert prov in fired
        got = await rows()
        assert len(got) == 1 and got[0]["severity"] == "high"
        p = got[0]["payload"]
        assert p["state"] == "entered" and p["kind"] == "search_degraded"
        assert p["not_found"] == 12 and p["not_found_degraded"] == 4
        assert p["page_budget_exempt"] is True
        assert engine in p["body"] and "search_failed" in p["body"]
        assert [prov in s.summary for s in sinks.payloads].count(True) == 1

        # Next pass, still degraded: silence. A restarted watchdog re-seeds.
        assert prov not in await sw.check_search_degraded_once(wd)
        assert prov not in await sw.check_search_degraded_once(watchdog(_Sinks()))
        assert len(await rows()) == 1

        # Healthy grades dilute it under the floor (4 / 24 = 17 %): recovered.
        async with pg.acquire() as conn:
            for _ in range(12):
                await _insert_grade(conn, provider=prov, target="b5_page",
                                    degraded=False, health=_block([], count=10))
        await sw.check_search_degraded_once(wd)
        assert [g["payload"]["state"] for g in await rows()] == ["entered", "recovered"]
    finally:
        async with pg.acquire() as conn:
            await conn.execute(
                "DELETE FROM alert_sink_deliveries WHERE channel_name = $1 "
                "AND attempted_at >= $2",
                sw.ALERT_CHANNEL_SEARCH_DEGRADED, started,
            )


def test_search_degraded_page_is_budget_exempt() -> None:
    """The watch stamps ``page_budget_exempt`` on its entry page; the budget's
    own set is what decides, so the class must be in it (merge follow-up)."""
    from legba.data.analysts.deterministic_handlers import _daily_page_budget as dpb

    assert dpb.is_page_budget_exempt(sw.ALERT_CHANNEL_SEARCH_DEGRADED)
    assert sw.ALERT_CHANNEL_SEARCH_DEGRADED in dpb.LIVENESS_EXEMPT_CLASSES
