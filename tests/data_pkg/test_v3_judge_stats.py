# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/system/judge-stats`` — verdict mix by SERVING PROVIDER (GLASS-3).

The route makes ``served_by`` operator-visible for the first time: it was written
onto every LLM receipt from 2026-08-16 and read by nothing, while a provider
change was measured to flip 13.6% of verdicts. These tests hold the two things
that make such a number trustworthy rather than merely present.

**The attribution must not inflate.** The critique↔receipt join is many-to-many
(one run yields several critiques; one finding partitions into several judge
calls). A naive join multiplies every verdict by its receipt count. The provider
is therefore resolved per RUN before being attached to that run's critiques, and
``test_receipt_fanout_does_not_multiply_verdicts`` is the regression that would
catch its removal — it is the whole reason the SQL has a ``run_provider`` CTE.

**The unknown must stay unknown.** Four sentinels (``(mixed)``, ``(unrouted)``,
``(no receipt)``, ``(unknown)``) each mean a different specific thing, and none
of them means "fine". The tests below pin that a run flipping provider mid-way
is bucketed rather than assigned to whichever call came first; that a
deterministic verdict — which never called an LLM and so CANNOT have a provider —
lands in ``(no receipt)`` rather than being folded into a real provider's mix;
and that a legacy NULL ``judge_status`` is reported as ``(unknown)`` rather than
relabelled ``deterministic`` the way the health gauge deliberately does.

The arithmetic tests run WITHOUT a substrate (``build_payload`` is pure), so the
denominators are pinned even on a rig with no Postgres; the route tests run
through the REAL FastAPI app over ASGI, so the wiring, the bearer gate and the
SQL are exercised as mounted, not as called.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry import judge_stats_api
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import MASTER_KEY_ENV, CredentialVault
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.judge_stats_api import (
    SENTINEL_MIXED,
    SENTINEL_NO_RECEIPT,
    SENTINEL_UNKNOWN_STATUS,
    SENTINEL_UNROUTED,
    JudgeStatsOut,
    build_judge_stats_router,
    build_payload,
)
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "66" * 32)

_ROUTE = "/api/v1/v3/system/judge-stats"
_DAY = date(2026, 8, 20)


# ---------------------------------------------------------------------------
# Pure — registration + contract, no DB
# ---------------------------------------------------------------------------


def test_route_is_registered():
    router = build_judge_stats_router(deps=object())  # type: ignore[arg-type]
    paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
    assert "/system/judge-stats" in paths


def test_empty_payload_is_honestly_unmeasured():
    """The degraded return must be distinguishable from a judge that judged
    nothing — an empty table that SAYS it measured nothing, never one that
    reads as all-clear."""
    out = JudgeStatsOut()
    assert out.measured is False
    assert out.cells == []
    assert out.providers == []
    assert out.totals.critiques == 0
    assert out.totals.faithfulness_mean is None


def test_sentinel_glossary_is_published_on_the_wire():
    """A reader must not have to hardcode what `(mixed)` means — the same reason
    the production gauge publishes its own alert floor."""
    out = JudgeStatsOut()
    for sentinel in (
        SENTINEL_MIXED, SENTINEL_UNROUTED, SENTINEL_NO_RECEIPT,
        SENTINEL_UNKNOWN_STATUS,
    ):
        assert sentinel in out.sentinels
        assert out.sentinels[sentinel].strip()


# ---------------------------------------------------------------------------
# The read-cost rewrite: same answer, cheaper SQL, never a cold wait
# ---------------------------------------------------------------------------


def _fields(model) -> set[str]:
    return set(model.model_fields)


def test_p3_response_shape_is_unchanged():
    """Changed how the numbers are read, never what is returned: every
    field of every model on the wire, pinned as it was before the rewrite."""
    from legba.data.registry.judge_stats_api import (
        JudgeComponentHealth, JudgeHealthOut, JudgePipelineVersionRow,
        JudgeStatsCell, JudgeStatsProvider, JudgeStatsTotals, JudgeUncitedOut,
        JudgeUncitedUnitDay,
    )

    assert _fields(JudgeStatsOut) == {
        "generated_at", "window_days", "measured",
        "pools_across_pipeline_versions", "totals", "providers",
        "pipeline_versions", "cells", "sentinels", "judge_statuses", "health",
        "uncited",
    }
    assert _fields(JudgeStatsCell) == {
        "day", "served_by", "judge_status", "judge_pipeline_version", "n",
        "faithfulness_n", "faithfulness_mean", "aligned_by_id",
        "aligned_positionally", "miscount_n",
    }
    assert _fields(JudgeStatsProvider) == {
        "served_by", "is_sentinel", "n", "by_status", "adjudicated_n",
        "adjudicated_share", "faithfulness_n", "faithfulness_mean",
        "positional_share", "miscount_rate", "judge_calls", "judge_call_errors",
        "latency_p95_ms", "first_call_at", "last_call_at",
    }
    assert _fields(JudgePipelineVersionRow) == {
        "judge_pipeline_version", "n", "providers", "faithfulness_n",
        "faithfulness_mean", "positional_share", "miscount_rate",
    }
    assert _fields(JudgeStatsTotals) == {
        "critiques", "by_status", "attributed", "unattributed", "providers",
        "adjudicated_n", "adjudicated_share", "faithfulness_n",
        "faithfulness_mean", "positional_share", "miscount_rate", "judge_calls",
        "judge_call_errors",
    }
    assert _fields(JudgeHealthOut) == {
        "measured", "unverified_since", "unverified_n", "open_at_window_start",
        "components",
    }
    assert _fields(JudgeComponentHealth) == {
        "component_id", "model", "calls", "last_success_at", "last_failure_at",
        "failure_class", "last_failure_http_status", "consecutive_failures",
        "unverified_since", "unverified_n", "open_at_window_start",
    }
    assert _fields(JudgeUncitedOut) == {"measured", "rows"}
    assert _fields(JudgeUncitedUnitDay) == {
        "day", "analyst_id", "reads", "fact_sentences", "uncited_fact_sentences",
        "uncited_share", "bluf_reads", "bluf_uncited_reads", "bluf_uncited_share",
    }


def _ws(sql: str) -> str:
    return " ".join(sql.split())


def test_p3_critique_data_is_detoasted_once_per_row():
    """Each ``o.data->...`` detoasts the whole critique document again; the cube
    had eight (22.5 s cold / 2.4 s warm on live). The verification block is
    extracted ONCE, behind ``OFFSET 0`` so the planner cannot inline it back."""
    js = judge_stats_api
    for name in ("_CUBE_SQL", "_HEALTH_STREAK_SQL", "_UNCITED_SQL"):
        sql = _ws(getattr(js, name))
        assert sql.count("o.data->'data'->'verification'") == 1, name
        assert "o.data->'data'->'verification' AS v OFFSET 0" in sql or (
            "o.data->'data'->'verification' AS v," in sql and "OFFSET 0" in sql
        ), name


def test_p3_trace_reads_never_join_the_receipts_array():
    """A lateral ``jsonb_array_elements`` join drew a Memoize keyed on the whole
    ``llm_calls`` document (detoast + hash per trace). The verify-judge calls
    are a set-returning ``jsonb_path_query`` in the select list instead, with
    the leg filter in STRICT mode (equal to ``c->>'leg' = 'verify_judge'``)."""
    js = judge_stats_api
    for name in ("_CUBE_SQL", "_RECEIPTS_SQL", "_HEALTH_CALLS_SQL"):
        sql = getattr(js, name)
        assert "jsonb_array_elements" not in sql, name
        assert """'strict $[*] ? (@.leg == "verify_judge")'""" in sql, name


def test_p3_cube_reads_only_the_runs_it_can_join():
    """Only a run that HAS a critique can be joined to one; the cube's trace
    side is narrowed to those runs (a primary-key lookup), and ``crit`` is
    materialized so a bad row estimate cannot re-run the trace aggregate per
    critique (the old plan did, 194 times, on a 3-day single-analyst read)."""
    sql = _ws(judge_stats_api._CUBE_SQL)
    assert sql.startswith("WITH crit AS MATERIALIZED (")
    assert "AND t.run_id IN (SELECT run_id FROM crit)" in sql
    assert sql.index("crit AS MATERIALIZED") < sql.index("judge_calls AS (")
    # The receipts read keeps the whole window: calls are counted at their own
    # grain, including runs whose critique is not in the window.
    assert "run_id IN" not in _ws(judge_stats_api._RECEIPTS_SQL)


class _FakePool:
    """Answers every statement with no rows; counts what it was asked."""

    def __init__(self) -> None:
        self.fetches: list[str] = []
        self.gate: asyncio.Event | None = None

    def acquire(self):
        pool = self

        class _Conn:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def fetch(self, sql, *args):
                pool.fetches.append(sql)
                if pool.gate is not None:
                    await pool.gate.wait()
                return []

        return _Conn()


def _fake_app(pool: _FakePool) -> FastAPI:
    from types import SimpleNamespace

    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=pool))
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_judge_stats_router(deps), prefix="/api/v1/v3")  # type: ignore[arg-type]
    return app


@pytest.fixture
def _open_bearer(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    monkeypatch.setenv("LEGBA_DEV_MODE", "1")


@pytest.mark.asyncio
async def test_p3_an_expired_payload_is_served_at_once_while_it_refreshes(
    _open_bearer, monkeypatch,
):
    """Past the TTL the last good payload answers immediately (its
    ``generated_at`` says how old it is) while ONE refresh runs behind it."""

    monkeypatch.setattr(judge_stats_api, "JUDGE_STATS_TTL_S", 0.05)
    pool = _FakePool()
    app = _fake_app(pool)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        first = (await c.get(_ROUTE)).json()
        assert len(pool.fetches) == 5
        await asyncio.sleep(0.06)
        pool.gate = asyncio.Event()  # the refresh now blocks on the database
        stale = await asyncio.wait_for(c.get(_ROUTE), timeout=2)
        assert stale.status_code == 200
        assert stale.json() == first  # same payload, same as-of, no wait
        await asyncio.sleep(0.01)
        # ONE refresh is already reading (cube, receipts, health calls,
        # uncited; the health streak follows its calls on the same connection).
        assert len(pool.fetches) == 9
        pool.gate.set()
        await asyncio.sleep(0.05)
        assert len(pool.fetches) == 10
        fresh = (await c.get(_ROUTE)).json()
        assert len(pool.fetches) == 10  # the refreshed payload, from the cache
    assert fresh["generated_at"] > first["generated_at"]
    assert set(fresh) == set(first)


@pytest.mark.asyncio
async def test_p3_the_default_read_is_warmed_at_startup(_open_bearer):
    """The router's lifespan computes the console's default read in the
    background, so the first landing after a roll is served from the cache."""

    pool = _FakePool()
    app = _fake_app(pool)
    async with app.router.lifespan_context(app):
        for _ in range(50):
            if len(pool.fetches) == 5:
                break
            await asyncio.sleep(0.01)
        assert len(pool.fetches) == 5
        await asyncio.sleep(0.01)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(_ROUTE)
        assert r.status_code == 200 and r.json()["measured"] is True
        assert len(pool.fetches) == 5  # served from the warmed entry


@pytest.mark.asyncio
async def test_p3_a_warm_up_still_running_at_shutdown_is_cancelled(_open_bearer):

    pool = _FakePool()
    pool.gate = asyncio.Event()  # the database never answers
    app = _fake_app(pool)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.01)
    # leaving the lifespan did not hang and did not raise


def _cube(
    *, provider: str, status: str, n: int,
    f_n: int = 0, f_sum: float | None = None,
    version: str = "2026-08-20/1", day: date = _DAY,
) -> dict:
    return {
        "day": day, "served_by": provider, "judge_status": status,
        "pipeline_version": version, "n": n,
        "faithfulness_n": f_n, "faithfulness_sum": f_sum,
    }


def _built(cube, receipts=None, *, days: int = 14) -> JudgeStatsOut:
    return build_payload(
        cube, receipts or [], window_days=days,
        generated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )


def test_every_metric_carries_its_n_and_its_provider():
    """The standing constraint, checked structurally: no rate or mean is
    reported without the count it was computed over, keyed by provider."""
    out = _built([
        _cube(provider="DeepInfra", status="llm", n=8, f_n=8, f_sum=6.4),
        _cube(provider="DeepInfra", status="deterministic", n=2),
    ])
    p = out.providers[0]
    assert p.served_by == "DeepInfra"
    assert p.n == 10
    assert p.faithfulness_n == 8
    assert p.faithfulness_mean == pytest.approx(0.8)
    # 8 llm of 10 adjudicable (nothing unsampled here).
    assert p.adjudicated_n == 10
    assert p.adjudicated_share == pytest.approx(0.8)


def test_a_mean_over_zero_rows_is_absent_never_zero():
    """`unassessable` verdicts carry no score. Reporting 0.0 would put a
    fabricated perfect-failure into every average that reads it."""
    out = _built([_cube(provider="Nvidia", status="llm", n=5, f_n=0, f_sum=None)])
    p = out.providers[0]
    assert p.n == 5
    assert p.faithfulness_n == 0
    assert p.faithfulness_mean is None
    assert out.totals.faithfulness_mean is None


def test_unsampled_leaves_the_adjudicated_denominator_but_stays_visible():
    """The J2 gate deliberately never offered these to a judge, so counting them
    against the judge would report a permanent fake shortfall — but they are a
    first-class bucket here, unlike in the health gauge."""
    out = _built([
        _cube(provider="DeepInfra", status="llm", n=6),
        _cube(provider="DeepInfra", status="unsampled", n=94),
    ])
    p = out.providers[0]
    assert p.by_status["unsampled"] == 94          # visible, not dropped
    assert p.n == 100
    assert p.adjudicated_n == 6                    # unsampled OUT of the denom
    assert p.adjudicated_share == pytest.approx(1.0)


def test_legacy_null_status_is_unknown_not_deterministic():
    """The health gauge folds NULL into `deterministic` on purpose. This is the
    measurement instrument, and relabelling legacy rows as a grader they may not
    have used would put fiction in the denominator."""
    out = _built([
        _cube(provider="DeepInfra", status="llm", n=3),
        _cube(provider="DeepInfra", status=SENTINEL_UNKNOWN_STATUS, n=1),
    ])
    p = out.providers[0]
    assert p.by_status.get("deterministic") is None
    assert p.by_status[SENTINEL_UNKNOWN_STATUS] == 1
    # `(unknown)` STAYS in the adjudicated denominator: it is a real verdict
    # that was not LLM-adjudicated.
    assert p.adjudicated_n == 4
    assert p.adjudicated_share == pytest.approx(0.75)


def test_sentinels_are_flagged_and_sorted_after_real_providers():
    """The drift comparison an operator came for belongs at the top of the
    table; the buckets that mean 'we could not say' belong under it."""
    out = _built([
        _cube(provider=SENTINEL_NO_RECEIPT, status="deterministic", n=500),
        _cube(provider="DeepInfra", status="llm", n=10),
    ])
    assert [p.served_by for p in out.providers] == [
        "DeepInfra", SENTINEL_NO_RECEIPT,
    ]
    assert out.providers[0].is_sentinel is False
    assert out.providers[1].is_sentinel is True
    # Attributed vs unattributed are reported SEPARATELY and must partition.
    assert out.totals.attributed == 10
    assert out.totals.unattributed == 500
    assert out.totals.attributed + out.totals.unattributed == out.totals.critiques
    assert out.totals.providers == 1  # sentinels are not providers


def test_pooling_across_a_judge_swap_is_flagged():
    """Pooling faithfulness across judge-pipeline stamps launders a regression
    into a flat line. The cube's grain makes the swap visible; this flag makes
    it unmissable."""
    out = _built([
        _cube(provider="DeepInfra", status="llm", n=5, f_n=5, f_sum=4.5,
              version="2026-08-05/1"),
        _cube(provider="DeepInfra", status="llm", n=5, f_n=5, f_sum=3.0,
              version="2026-08-20/1"),
    ])
    assert out.pools_across_pipeline_versions is True
    per_version = {v.judge_pipeline_version: v for v in out.pipeline_versions}
    assert per_version["2026-08-05/1"].faithfulness_mean == pytest.approx(0.9)
    assert per_version["2026-08-20/1"].faithfulness_mean == pytest.approx(0.6)
    # The pooled mean (0.75) is still reported, but never ALONE.
    assert out.totals.faithfulness_mean == pytest.approx(0.75)


def test_a_single_stamp_is_not_flagged_as_pooling():
    out = _built([_cube(provider="DeepInfra", status="llm", n=5)])
    assert out.pools_across_pipeline_versions is False
    assert len(out.pipeline_versions) == 1


def test_provider_with_calls_but_no_attributed_critiques_still_appears():
    """A provider that served judge calls whose runs all ended `(mixed)` would
    otherwise vanish from the table that exists to show provider drift."""
    out = _built(
        [_cube(provider=SENTINEL_MIXED, status="llm", n=4)],
        [{"served_by": "Nvidia", "calls": 9, "call_errors": 1,
          "latency_p95_ms": 1234.5, "first_call_at": None, "last_call_at": None}],
    )
    labels = {p.served_by for p in out.providers}
    assert "Nvidia" in labels
    nvidia = next(p for p in out.providers if p.served_by == "Nvidia")
    assert nvidia.judge_calls == 9
    assert nvidia.judge_call_errors == 1
    assert nvidia.latency_p95_ms == pytest.approx(1234.5)
    assert nvidia.n == 0                    # honest: no verdict attributable
    assert nvidia.faithfulness_mean is None


# ---------------------------------------------------------------------------
# Rig — the REAL app, mounted as it is in production
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api_app(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()

    identity = SigningIdentity(
        signing_key=SigningKey(b"glass3-judge-stats-route-seed-001"[:32]),
        signer_did="did:legba:registry:glass3-judge-stats-test",
    )
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    vault = CredentialVault(pg_store)
    descriptor_registry = DescriptorRegistry(
        pg_store,
        vocabulary_cache=vocab,
        signing_identity=identity,
        audit_logger=audit,
        dead_letter=dlq,
    )
    await descriptor_registry.start()
    deps = RegistryAPIDeps(
        descriptor_registry=descriptor_registry,
        stack_registry=StackRegistry(pg_store, vault, audit=audit, dlq=dlq),
        vault=vault,
        dlq=dlq,
        audit_logger=audit,
        vocabulary_cache=vocab,
        nats_store=None,
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_judge_stats_router(deps), prefix="/api/v1/v3")

    yield app, pg_store

    await descriptor_registry.stop()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(api_app):
    app, _ = api_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


#: Rows this module writes, torn down by id/run_id so nothing global is
#: truncated — the gauge module's blast radius lesson (a truncating fixture that
#: moved another module's denominators and only failed under a shuffled run).
@pytest_asyncio.fixture
async def seeded(api_app):
    _, pg_store = api_app
    run_ids: list = []
    output_ids: list = []

    async def _trace(*, calls: list[dict], started: datetime):
        run_id = uuid4()
        async with pg_store.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO analyst_traces
                    (run_id, analyst_id, analyst_version, cadence_trigger,
                     llm_calls, status, run_started_at, receipt_hash)
                VALUES ($1, 'glass3_probe', 'v1', 'test', $2::jsonb,
                        'success', $3, $4)
                """,
                run_id, json.dumps(calls), started, f"h-{run_id}",
            )
        run_ids.append(run_id)
        return run_id

    async def _critique(
        *, run_id, judge_status: str | None, score: float | None = None,
        created: datetime, version: str | None = "2026-08-20/1",
        title: str = "Faithfulness verify (score 0.87)",
    ):
        verification: dict = {}
        if judge_status is not None:
            verification["judge_status"] = judge_status
        if score is not None:
            verification["faithfulness_score"] = score
        if version is not None:
            verification["judge_pipeline_version"] = version
        payload = {
            "analyzed_analyst_id": "glass3_analyst",
            "data": {"verification": verification},
        }
        oid = uuid4()
        async with pg_store.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO analyst_outputs
                    (id, kind, title, body, confidence, data, schema_uri,
                     run_id, produced_at, created_at)
                VALUES ($1, 'critique', $2, '', 0.5, $3::jsonb,
                        'iglu:legba/analyst_output/jsonschema/1-0-0',
                        $4, $5, $5)
                """,
                oid, title, json.dumps(payload), run_id, created,
            )
        output_ids.append(oid)
        return oid

    yield _trace, _critique

    async with pg_store.pool.acquire() as conn:
        if output_ids:
            await conn.execute(
                "DELETE FROM analyst_outputs WHERE id = ANY($1::uuid[])",
                output_ids,
            )
        if run_ids:
            await conn.execute(
                "DELETE FROM analyst_traces WHERE run_id = ANY($1::uuid[])",
                run_ids,
            )


def _providers(body: dict) -> dict[str, dict]:
    return {p["served_by"]: p for p in body["providers"]}


@pytest.mark.asyncio
async def test_route_answers_200_and_measures(client):
    r = await client.get(_ROUTE)
    assert r.status_code == 200
    body = r.json()
    assert body["measured"] is True
    assert body["window_days"] == judge_stats_api.DEFAULT_WINDOW_DAYS
    assert SENTINEL_MIXED in body["sentinels"]


@pytest.mark.asyncio
async def test_window_is_bounded(client):
    """The lateral unnest over `llm_calls` has no index to lean on; an unbounded
    window would table-scan a 600MB table from a polling panel."""
    assert (await client.get(_ROUTE, params={"days": 0})).status_code == 422
    over = judge_stats_api.MAX_WINDOW_DAYS + 1
    assert (await client.get(_ROUTE, params={"days": over})).status_code == 422


@pytest.mark.asyncio
async def test_served_by_reaches_the_wire(client, seeded):
    """The point of the whole route: a provider name recorded on a judge receipt
    becomes an operator-visible number."""
    trace, critique = seeded
    now = datetime.now(timezone.utc)
    run = await trace(
        calls=[{"leg": "verify_judge", "served_by": "DeepInfra",
                "status": "success", "duration_ms": 900}],
        started=now - timedelta(hours=2),
    )
    await critique(run_id=run, judge_status="llm", score=0.9, created=now - timedelta(hours=1))

    body = (await client.get(_ROUTE, params={"analyst_id": "glass3_analyst"})).json()
    p = _providers(body)["DeepInfra"]
    assert p["n"] == 1
    assert p["is_sentinel"] is False
    assert p["judge_calls"] == 1
    assert p["faithfulness_n"] == 1
    assert p["faithfulness_mean"] == pytest.approx(0.9)
    assert body["totals"]["attributed"] == 1


@pytest.mark.asyncio
async def test_receipt_fanout_does_not_multiply_verdicts(client, seeded):
    """THE regression this route's `run_provider` CTE exists to prevent.

    One run, THREE judge receipts from the same provider, TWO critiques. A join
    that attached receipts to critiques directly would report 6 verdicts. The
    truth is 2."""
    trace, critique = seeded
    now = datetime.now(timezone.utc)
    run = await trace(
        calls=[
            {"leg": "verify_judge", "served_by": "DeepInfra", "status": "success",
             "duration_ms": 100},
            {"leg": "verify_judge", "served_by": "DeepInfra", "status": "success",
             "duration_ms": 200},
            {"leg": "verify_judge", "served_by": "DeepInfra", "status": "success",
             "duration_ms": 300},
        ],
        started=now - timedelta(hours=2),
    )
    await critique(run_id=run, judge_status="llm", score=0.8, created=now - timedelta(hours=1))
    await critique(run_id=run, judge_status="llm", score=0.6, created=now - timedelta(hours=1))

    body = (await client.get(_ROUTE, params={"analyst_id": "glass3_analyst"})).json()
    p = _providers(body)["DeepInfra"]
    assert p["n"] == 2, "receipt fan-out inflated the verdict count"
    assert p["judge_calls"] == 3, "calls are counted at their own grain"
    assert p["faithfulness_mean"] == pytest.approx(0.7)
    assert body["totals"]["critiques"] == 2


@pytest.mark.asyncio
async def test_a_run_that_flips_provider_is_bucketed_not_guessed(client, seeded):
    """There is no per-critique receipt id, so a mid-run router flip makes
    per-critique attribution genuinely impossible. It must say so."""
    trace, critique = seeded
    now = datetime.now(timezone.utc)
    run = await trace(
        calls=[
            {"leg": "verify_judge", "served_by": "DeepInfra", "status": "success"},
            {"leg": "verify_judge", "served_by": "Nvidia", "status": "success"},
        ],
        started=now - timedelta(hours=2),
    )
    await critique(run_id=run, judge_status="llm", score=0.5, created=now - timedelta(hours=1))

    body = (await client.get(_ROUTE, params={"analyst_id": "glass3_analyst"})).json()
    provs = _providers(body)
    assert provs[SENTINEL_MIXED]["n"] == 1
    assert provs[SENTINEL_MIXED]["is_sentinel"] is True
    # Neither real provider may claim the verdict...
    assert provs.get("DeepInfra", {}).get("n", 0) == 0
    assert provs.get("Nvidia", {}).get("n", 0) == 0
    # ...but both really did serve a CALL, and that is reported at its own grain.
    assert provs["DeepInfra"]["judge_calls"] == 1
    assert provs["Nvidia"]["judge_calls"] == 1
    assert body["totals"]["unattributed"] == 1


@pytest.mark.asyncio
async def test_a_verdict_with_no_judge_call_cannot_have_a_provider(client, seeded):
    """`deterministic` and `unsampled` never called an LLM. Folding them into a
    real provider's mix would attribute verdicts to a provider that was never
    asked — the single most misleading thing this route could do."""
    trace, critique = seeded
    now = datetime.now(timezone.utc)
    run = await trace(calls=[], started=now - timedelta(hours=2))
    await critique(run_id=run, judge_status="deterministic", created=now - timedelta(hours=1))
    await critique(run_id=run, judge_status="unsampled", created=now - timedelta(hours=1))

    body = (await client.get(_ROUTE, params={"analyst_id": "glass3_analyst"})).json()
    p = _providers(body)[SENTINEL_NO_RECEIPT]
    assert p["by_status"]["deterministic"] == 1
    assert p["by_status"]["unsampled"] == 1
    assert p["is_sentinel"] is True
    # unsampled is out of the denominator; deterministic stays in it, unjudged.
    assert p["adjudicated_n"] == 1
    assert p["adjudicated_share"] == pytest.approx(0.0)
    assert body["totals"]["attributed"] == 0


@pytest.mark.asyncio
async def test_an_unrouted_receipt_is_not_an_error(client, seeded):
    """A direct (non-router) provider never reports who ultimately served. That
    is expected, and must not read as a fault."""
    trace, critique = seeded
    now = datetime.now(timezone.utc)
    run = await trace(
        calls=[{"leg": "verify_judge", "status": "success"}],  # no served_by key
        started=now - timedelta(hours=2),
    )
    await critique(run_id=run, judge_status="llm", score=0.75, created=now - timedelta(hours=1))

    body = (await client.get(_ROUTE, params={"analyst_id": "glass3_analyst"})).json()
    p = _providers(body)[SENTINEL_UNROUTED]
    assert p["n"] == 1
    assert p["judge_calls"] == 1
    assert p["faithfulness_mean"] == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_non_judge_receipts_are_not_counted(client, seeded):
    """`leg` is the ONLY discriminator — a generation call on the same run must
    not be read as a judge call, however it was routed."""
    trace, critique = seeded
    now = datetime.now(timezone.utc)
    run = await trace(
        calls=[
            {"served_by": "SomeGenerationProvider", "status": "success"},
            {"leg": "verify_judge", "served_by": "DeepInfra", "status": "success"},
        ],
        started=now - timedelta(hours=2),
    )
    await critique(run_id=run, judge_status="llm", score=0.9, created=now - timedelta(hours=1))

    body = (await client.get(_ROUTE, params={"analyst_id": "glass3_analyst"})).json()
    provs = _providers(body)
    assert "SomeGenerationProvider" not in provs
    # ...and the run resolves to ONE provider, not `(mixed)`.
    assert provs["DeepInfra"]["n"] == 1
    assert SENTINEL_MIXED not in provs or provs[SENTINEL_MIXED]["n"] == 0


@pytest.mark.asyncio
async def test_structural_verify_family_is_excluded(client, seeded):
    """`Structural verify%` is a different instrument. Counting it here would
    pool two populations under one faithfulness mean."""
    trace, critique = seeded
    now = datetime.now(timezone.utc)
    run = await trace(
        calls=[{"leg": "verify_judge", "served_by": "DeepInfra", "status": "success"}],
        started=now - timedelta(hours=2),
    )
    await critique(
        run_id=run, judge_status="llm", score=0.1,
        created=now - timedelta(hours=1), title="Structural verify (score 0.10)",
    )

    body = (await client.get(_ROUTE, params={"analyst_id": "glass3_analyst"})).json()
    assert body["totals"]["critiques"] == 0


@pytest.mark.asyncio
async def test_p3_the_strict_path_filter_matches_exactly_what_leg_equality_did(
    client, seeded,
):
    """Swapped ``c->>'leg' = 'verify_judge'`` over ``jsonb_array_elements``
    for ``jsonb_path_query('strict $[*] ? (@.leg == "verify_judge")')``. The
    shapes where the two could part: a non-object element, an element with no
    ``leg``, a NESTED array holding a judge call, and a ``leg`` that is an
    array. ``->>`` matched none of them; lax mode would unwrap the two arrays
    and match. Strict must match only the plain judge call."""
    trace, critique = seeded
    now = datetime.now(timezone.utc)
    run = await trace(
        calls=[
            "not an object",
            {"served_by": "P3EdgeNoLeg", "status": "success"},
            [{"leg": "verify_judge", "served_by": "P3EdgeNested", "status": "success"}],
            {"leg": ["verify_judge"], "served_by": "P3EdgeArrayLeg", "status": "success"},
            {"leg": "verify_judge", "served_by": "P3EdgeReal", "status": "success"},
        ],
        started=now - timedelta(hours=2),
    )
    await critique(run_id=run, judge_status="llm", score=0.5, created=now - timedelta(hours=1))

    body = (await client.get(_ROUTE, params={"analyst_id": "glass3_analyst"})).json()
    provs = _providers(body)
    assert provs["P3EdgeReal"]["n"] == 1
    assert provs["P3EdgeReal"]["judge_calls"] == 1
    for ghost in ("P3EdgeNoLeg", "P3EdgeNested", "P3EdgeArrayLeg"):
        assert ghost not in provs, ghost
    assert SENTINEL_MIXED not in provs or provs[SENTINEL_MIXED]["n"] == 0


@pytest.mark.asyncio
async def test_rows_outside_the_window_are_not_counted(client, seeded):
    trace, critique = seeded
    now = datetime.now(timezone.utc)
    run = await trace(
        calls=[{"leg": "verify_judge", "served_by": "DeepInfra", "status": "success"}],
        started=now - timedelta(days=40),
    )
    await critique(run_id=run, judge_status="llm", score=0.9, created=now - timedelta(days=40))

    body = (await client.get(
        _ROUTE, params={"analyst_id": "glass3_analyst", "days": 7},
    )).json()
    assert body["totals"]["critiques"] == 0

    wide = (await client.get(
        _ROUTE, params={"analyst_id": "glass3_analyst", "days": 60},
    )).json()
    assert wide["totals"]["critiques"] == 1
