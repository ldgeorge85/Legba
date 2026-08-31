# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the runtime + analyst telemetry API.

Exercises ``build_runtime_telemetry_router`` against the live substrate
via the ``migrated_pg`` fixture from ``conftest.py``. No mocks — same
hard rule as L-110 / L-111 / L-113.

Coverage:

  * Empty-result paths for all five endpoints.
  * Target with a known actor_state row — multi-source ``source_cursors``
    decomposition.
  * Analyst roster with zero traces → metrics zeroed (not omitted).
  * Analyst roster with non-zero traces → aggregates over the 7-day window.
  * Pagination over /runs, /outputs, /critiques (with three rows + limit=2).
  * /critiques filter — analyzed (not judge) analyst.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.config import NatsConfig, PostgresConfig
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.runtime_telemetry_api import (
    build_runtime_telemetry_router,
)
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache
from legba.runtime.state import ActorStateStore


_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "22" * 32)


def _fixed_identity() -> SigningIdentity:
    seed = b"runtime-telemetry-test-seed-deterministic"[:32]
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:runtime-telemetry-test",
    )


# ---------------------------------------------------------------------------
# Wiring fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api_app(migrated_pg: PostgresConfig):
    """Full app wired against the migrated test DB."""
    os.environ.pop(API_TOKEN_ENV, None)

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()

    # actor_state lives outside the migration set (it's created by the
    # runtime's ActorStateStore.ensure_schema on startup). The runtime
    # ships its own DDL in src/legba/runtime/state.py; we apply it here
    # so the telemetry endpoints have a table to read from.
    actor_store = ActorStateStore(pg_store.pool)
    await actor_store.ensure_schema()

    nats_store = NatsStore(NatsConfig.from_env())
    await nats_store.connect()

    identity = _fixed_identity()
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    vault = CredentialVault(pg_store)

    descriptor_registry = DescriptorRegistry(
        pg_store,
        nats_store=nats_store,
        vocabulary_cache=vocab,
        signing_identity=identity,
        audit_logger=audit,
        dead_letter=dlq,
    )
    await descriptor_registry.start()

    stack_registry = StackRegistry(
        pg_store,
        vault,
        audit=audit,
        dlq=dlq,
    )

    deps = RegistryAPIDeps(
        descriptor_registry=descriptor_registry,
        stack_registry=stack_registry,
        vault=vault,
        dlq=dlq,
        audit_logger=audit,
        vocabulary_cache=vocab,
        nats_store=nats_store,
        conversion_registry=None,
    )

    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(
        build_runtime_telemetry_router(deps),
        prefix="/api/v1",
    )

    yield app, deps, pg_store

    await descriptor_registry.stop()
    await nats_store.close()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(api_app):
    app, _, _ = api_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


@pytest_asyncio.fixture(autouse=True)
async def _clean_actor_state(clean_tables):
    """The confirmed root polluter behind the 2026-08-29 nightly's shuffled
    ``test_postgres_pool_search_path.py::
    test_actor_state_table_reachable_from_every_acquire`` failure
    (``assert 3 == 0``): ``_insert_actor_state`` writes directly to the
    session-shared ``actor_state`` table (3 call sites in this file) and
    nothing here ever cleaned them up. Autouse + setup-time TRUNCATE so
    EVERY test in this file (the 3 writers and the "empty" readers alike)
    starts from a table this file doesn't share evidence of with any other
    session-shared consumer — matches the ``clean_tables`` primitive's
    setup-only idiom in ``conftest.py``."""
    await clean_tables("actor_state")


# ---------------------------------------------------------------------------
# Insert helpers — write directly to the substrate.
# ---------------------------------------------------------------------------


async def _insert_actor_state(
    pg_store,
    *,
    actor_id: str,
    actor_kind: str,
    descriptor_id: str,
    descriptor_version: str = "v" * 16,
    lifecycle: str = "active",
    last_run_at: datetime | None = None,
    last_outcome: str | None = None,
    error_count: int = 0,
    last_error: str | None = None,
    source_cursors: dict | None = None,
) -> None:
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO public.actor_state (
                actor_id, actor_kind, descriptor_id, descriptor_version,
                lifecycle, last_run_at, last_outcome, error_count,
                last_error, source_cursors, extras
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, '{}'::jsonb
            )
            """,
            actor_id,
            actor_kind,
            descriptor_id,
            descriptor_version,
            lifecycle,
            last_run_at,
            last_outcome,
            error_count,
            last_error,
            json.dumps(source_cursors or {}),
        )


async def _insert_target_descriptor_head(
    pg_store,
    *,
    descriptor_id: str,
    version: str = "h" * 64,
    sources: list[dict] | None = None,
    name: str = "Test Target",
) -> None:
    body = {
        "identity": {
            "id": descriptor_id,
            "name": name,
            "schema_uri": "legba/target/2.0.0",
            "version": "0" * 16,
            "abstraction_level": "L1",
            "state": "active",
            "owner": "test@local",
            "created": "2026-05-20T00:00:00+00:00",
        },
        "scope": {
            "geo": ["BR"],
            "languages": ["en"],
            "entity_classes": ["organization"],
            "relationship_types": ["LocatedIn"],
            "time_horizon_days": 90,
        },
        "sources": sources or [],
    }
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO target_descriptors (
                descriptor_id, version, schema_uri, is_head,
                abstraction_level, state, owner, name, body, inherits
            ) VALUES (
                $1, $2, 'legba/target/2.0.0', TRUE, 'L1', 'active',
                'test@local', $3, $4::jsonb, '{}'::TEXT[]
            )
            """,
            descriptor_id,
            version,
            name,
            json.dumps(body),
        )


async def _insert_analyst_trace(
    pg_store,
    *,
    run_id: UUID,
    analyst_id: str,
    analyst_version: str = "av" * 8,
    target_id: str | None = None,
    status_: str = "success",
    run_started_at: datetime | None = None,
    run_ended_at: datetime | None = None,
    llm_calls: list[dict] | None = None,
    cadence_trigger: str = "scheduled",
) -> None:
    if run_started_at is None:
        run_started_at = datetime.now(tz=timezone.utc)
    if run_ended_at is None:
        run_ended_at = run_started_at + timedelta(seconds=2)
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_traces (
                run_id, analyst_id, analyst_version, target_id, cadence_trigger,
                input_row_refs, output_row_refs, intermediate_steps,
                llm_calls, tool_calls, status, run_started_at, run_ended_at,
                receipt_hash, schema_uri
            ) VALUES (
                $1, $2, $3, $4, $5,
                '{}'::UUID[], '{}'::UUID[],
                '[]'::jsonb, $6::jsonb, '[]'::jsonb,
                $7, $8, $9,
                $10,
                'iglu:legba/analyst_trace/jsonschema/1-0-0'
            )
            """,
            run_id,
            analyst_id,
            analyst_version,
            target_id,
            cadence_trigger,
            json.dumps(llm_calls or []),
            status_,
            run_started_at,
            run_ended_at,
            f"hash_{run_id.hex[:16]}",
        )


async def _insert_analyst_output(
    pg_store,
    *,
    analyst_id: str,
    kind: str = "finding",
    run_id: UUID | None = None,
    produced_at: datetime | None = None,
    title: str = "test finding",
    data: dict | None = None,
) -> UUID:
    output_id = uuid4()
    if produced_at is None:
        produced_at = datetime.now(tz=timezone.utc)
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity,
                data, target_id, target_version, analyst_id, analyst_version,
                produced_at, derived_from, schema_uri, run_id, created_at
            ) VALUES (
                $1, $2, $3, '', 0.7, NULL,
                $4::jsonb, NULL, NULL, $5, 'av_x',
                $6, '{}'::UUID[], 'iglu:legba/finding/jsonschema/1-0-0',
                $7, NOW()
            )
            """,
            output_id,
            kind,
            title,
            json.dumps(data or {}),
            analyst_id,
            produced_at,
            run_id,
        )
    return output_id


async def _insert_analyst_critique(
    pg_store,
    *,
    trace_id: UUID,
    judge_analyst_id: str,
    overall_score: float | None = 0.8,
    scores: dict | None = None,
    produced_at: datetime | None = None,
) -> UUID:
    cid = uuid4()
    if produced_at is None:
        produced_at = datetime.now(tz=timezone.utc)
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_critiques (
                id, trace_id, judge_analyst_id, judge_analyst_version,
                rubric_uri, scores, overall_score, revision_delta, produced_at,
                schema_uri
            ) VALUES (
                $1, $2, $3, 'jv_x',
                'iglu:legba/rubric/critic_default/1-0-0',
                $4::jsonb, $5, NULL, $6,
                'iglu:legba/analyst_critique/jsonschema/1-0-0'
            )
            """,
            cid,
            trace_id,
            judge_analyst_id,
            json.dumps(scores or {"accuracy": 0.9, "completeness": 0.7}),
            overall_score,
            produced_at,
        )
    return cid


# ---------------------------------------------------------------------------
# Empty-result paths
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_target_runtime_empty(api_app, client: AsyncClient):
    # No actor_state row exists for this id.
    desc_id = f"tgt_{uuid4().hex[:8]}"
    r = await client.get(f"/api/v1/targets/{desc_id}/runtime")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["descriptor_id"] == desc_id
    assert payload["active_descriptor"] is None
    assert payload["actors"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_analyst_runtime_empty(api_app, client: AsyncClient):
    # Nothing seeded — but the table may not be globally empty across
    # test-session ordering. We assert empty when we filter to a unique
    # descriptor_id below; here we just check the endpoint responds 200
    # and returns a list.
    r = await client.get("/api/v1/analysts/runtime")
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_analyst_runs_empty(api_app, client: AsyncClient):
    aid = f"a_{uuid4().hex[:8]}"
    r = await client.get(f"/api/v1/analysts/{aid}/runs")
    assert r.status_code == 200, r.text
    page = r.json()
    assert page["items"] == []
    assert page["next_cursor"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_analyst_outputs_empty(api_app, client: AsyncClient):
    aid = f"a_{uuid4().hex[:8]}"
    r = await client.get(f"/api/v1/analysts/{aid}/outputs")
    assert r.status_code == 200, r.text
    page = r.json()
    assert page["items"] == []
    assert page["next_cursor"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_analyst_critiques_empty(api_app, client: AsyncClient):
    aid = f"a_{uuid4().hex[:8]}"
    r = await client.get(f"/api/v1/analysts/{aid}/critiques")
    assert r.status_code == 200, r.text
    page = r.json()
    assert page["items"] == []
    assert page["next_cursor"] is None


# ---------------------------------------------------------------------------
# Target runtime — multi-source cursor decomposition + active descriptor.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_target_runtime_multi_source_cursors(
    api_app, client: AsyncClient,
):
    _, _, pg_store = api_app
    desc_id = f"tgt_{uuid4().hex[:8]}"
    last_pulled = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    cursors = {
        "agbrasil_econ_rss": {
            "source_id": "agbrasil_econ_rss",
            "cursor": {"last_seen_guid": "abc"},
            "last_pulled_at": last_pulled.isoformat(),
            "rows_pulled": 17,
            "last_error": None,
        },
        "valor_economic_rss": {
            "source_id": "valor_economic_rss",
            "cursor": {},
            "last_pulled_at": None,
            "rows_pulled": 0,
            "last_error": "DNS lookup failed",
        },
    }
    await _insert_target_descriptor_head(
        pg_store,
        descriptor_id=desc_id,
        sources=[
            {"id": "agbrasil_econ_rss", "kind": "rss",
             "config": {}, "enabled": True},
            {"id": "valor_economic_rss", "kind": "rss",
             "config": {}, "enabled": False},
            # extra binding without a cursor — should NOT generate a row.
            {"id": "future_source", "kind": "rss",
             "config": {}, "enabled": True},
        ],
    )
    await _insert_actor_state(
        pg_store,
        actor_id=f"target::{desc_id}::abc123",
        actor_kind="target",
        descriptor_id=desc_id,
        lifecycle="active",
        last_run_at=last_pulled,
        last_outcome="success",
        error_count=1,
        last_error="prior transient",
        source_cursors=cursors,
    )

    r = await client.get(f"/api/v1/targets/{desc_id}/runtime")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["descriptor_id"] == desc_id
    assert out["active_descriptor"] is not None
    ad = out["active_descriptor"]
    assert ad["descriptor_id"] == desc_id
    assert ad["source_count"] == 3
    assert ad["state"] == "active"
    assert ad["abstraction_level"] == "L1"

    assert len(out["actors"]) == 1
    actor = out["actors"][0]
    assert actor["actor_kind"] == "target"
    assert actor["lifecycle"] == "active"
    assert actor["error_count"] == 1
    assert actor["last_error"] == "prior transient"
    assert actor["last_outcome"] == "success"

    sources = actor["sources"]
    assert len(sources) == 2          # only cursor entries surfaced
    by_id = {s["source_id"]: s for s in sources}
    agb = by_id["agbrasil_econ_rss"]
    assert agb["rows_pulled"] == 17
    assert agb["last_error"] is None
    assert agb["descriptor_kind"] == "rss"
    assert agb["descriptor_enabled"] is True
    # Round-trip the ISO timestamp.
    assert agb["last_pulled_at"] is not None
    assert datetime.fromisoformat(
        agb["last_pulled_at"].replace("Z", "+00:00"),
    ) == last_pulled

    valor = by_id["valor_economic_rss"]
    assert valor["rows_pulled"] == 0
    assert valor["last_pulled_at"] is None
    assert valor["last_error"] == "DNS lookup failed"
    assert valor["descriptor_enabled"] is False


# ---------------------------------------------------------------------------
# Analyst roster — zero traces vs. windowed aggregates.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_analyst_roster_zero_traces_not_omitted(
    api_app, client: AsyncClient,
):
    _, _, pg_store = api_app
    aid = f"analyst_zero_{uuid4().hex[:8]}"
    await _insert_actor_state(
        pg_store,
        actor_id=f"analyst::{aid}::xxx",
        actor_kind="analyst",
        descriptor_id=aid,
        lifecycle="active",
    )

    r = await client.get("/api/v1/analysts/runtime")
    assert r.status_code == 200, r.text
    rows = r.json()
    match = [row for row in rows if row["descriptor_id"] == aid]
    assert len(match) == 1, "zero-trace analyst must still be in the roster"
    row = match[0]
    assert row["runs_7d"] == 0
    assert row["success_count_7d"] == 0
    assert row["failed_count_7d"] == 0
    assert row["avg_token_count_7d"] == 0.0
    assert row["last_trace_at"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_analyst_roster_aggregates_window(
    api_app, client: AsyncClient,
):
    _, _, pg_store = api_app
    aid = f"analyst_busy_{uuid4().hex[:8]}"
    await _insert_actor_state(
        pg_store,
        actor_id=f"analyst::{aid}::yyy",
        actor_kind="analyst",
        descriptor_id=aid,
        lifecycle="active",
    )
    now = datetime.now(tz=timezone.utc)
    # Three in-window: 2 success + 1 failed. Token sums: 100, 200, 300 → avg 200.
    await _insert_analyst_trace(
        pg_store,
        run_id=uuid4(), analyst_id=aid, status_="success",
        run_started_at=now - timedelta(hours=1),
        llm_calls=[{"prompt_tokens": 60, "completion_tokens": 40}],
    )
    await _insert_analyst_trace(
        pg_store,
        run_id=uuid4(), analyst_id=aid, status_="success",
        run_started_at=now - timedelta(hours=2),
        llm_calls=[
            {"prompt_tokens": 120, "completion_tokens": 80},
        ],
    )
    await _insert_analyst_trace(
        pg_store,
        run_id=uuid4(), analyst_id=aid, status_="error",
        run_started_at=now - timedelta(hours=3),
        llm_calls=[{"usage": {"prompt_tokens": 200, "completion_tokens": 100}}],
    )
    # One out of window — must NOT be counted.
    await _insert_analyst_trace(
        pg_store,
        run_id=uuid4(), analyst_id=aid, status_="success",
        run_started_at=now - timedelta(days=10),
        llm_calls=[{"prompt_tokens": 9999, "completion_tokens": 9999}],
    )

    r = await client.get("/api/v1/analysts/runtime")
    assert r.status_code == 200
    rows = r.json()
    row = next(row for row in rows if row["descriptor_id"] == aid)
    assert row["runs_7d"] == 3
    assert row["success_count_7d"] == 2
    assert row["failed_count_7d"] == 1
    assert row["avg_token_count_7d"] == 200.0
    assert row["last_trace_at"] is not None


# ---------------------------------------------------------------------------
# /runs — output_count subquery + duration_ms + token_count.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_analyst_runs_decomposition(api_app, client: AsyncClient):
    _, _, pg_store = api_app
    aid = f"analyst_runs_{uuid4().hex[:8]}"
    run_id = uuid4()
    started = datetime(2026, 5, 20, 10, 0, 0, tzinfo=timezone.utc)
    ended = started + timedelta(milliseconds=2500)
    await _insert_analyst_trace(
        pg_store,
        run_id=run_id,
        analyst_id=aid,
        run_started_at=started,
        run_ended_at=ended,
        llm_calls=[
            {"prompt_tokens": 100, "completion_tokens": 50,
             "reasoning_tokens": 10},
        ],
    )
    # Two outputs from this run.
    await _insert_analyst_output(pg_store, analyst_id=aid, run_id=run_id)
    await _insert_analyst_output(pg_store, analyst_id=aid, run_id=run_id)
    # One unrelated output (different run) — must NOT inflate count.
    await _insert_analyst_output(pg_store, analyst_id=aid, run_id=uuid4())

    r = await client.get(f"/api/v1/analysts/{aid}/runs")
    assert r.status_code == 200, r.text
    page = r.json()
    assert len(page["items"]) == 1
    row = page["items"][0]
    assert row["run_id"] == str(run_id)
    assert row["status"] == "success"
    assert row["duration_ms"] == 2500
    assert row["token_count"] == 160  # 100 + 50 + 10
    assert row["output_count"] == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_analyst_runs_pagination(api_app, client: AsyncClient):
    _, _, pg_store = api_app
    aid = f"analyst_page_{uuid4().hex[:8]}"
    base = datetime.now(tz=timezone.utc)
    ids = []
    for i in range(3):
        rid = uuid4()
        ids.append(rid)
        await _insert_analyst_trace(
            pg_store,
            run_id=rid,
            analyst_id=aid,
            run_started_at=base - timedelta(minutes=i),
        )

    # limit=2 returns first page + next_cursor.
    r = await client.get(f"/api/v1/analysts/{aid}/runs", params={"limit": 2})
    assert r.status_code == 200, r.text
    page1 = r.json()
    assert len(page1["items"]) == 2
    assert page1["next_cursor"] is not None
    first_ids = [item["run_id"] for item in page1["items"]]

    # Fetch page 2.
    r = await client.get(
        f"/api/v1/analysts/{aid}/runs",
        params={"limit": 2, "cursor": page1["next_cursor"]},
    )
    assert r.status_code == 200, r.text
    page2 = r.json()
    assert len(page2["items"]) == 1
    assert page2["next_cursor"] is None
    page2_id = page2["items"][0]["run_id"]
    # All three ids covered, no overlap.
    assert set(first_ids + [page2_id]) == {str(rid) for rid in ids}

    # Bad cursor → 400.
    r = await client.get(
        f"/api/v1/analysts/{aid}/runs",
        params={"cursor": "garbage!@#"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /outputs — pagination + kind filter.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_analyst_outputs_pagination_and_filter(
    api_app, client: AsyncClient,
):
    _, _, pg_store = api_app
    aid = f"analyst_out_{uuid4().hex[:8]}"
    base = datetime.now(tz=timezone.utc)
    findings = []
    for i in range(3):
        oid = await _insert_analyst_output(
            pg_store,
            analyst_id=aid,
            kind="finding",
            produced_at=base - timedelta(minutes=i),
        )
        findings.append(oid)
    alert_id = await _insert_analyst_output(
        pg_store,
        analyst_id=aid,
        kind="alert",
        produced_at=base - timedelta(minutes=5),
    )

    # All kinds, paginated.
    r = await client.get(
        f"/api/v1/analysts/{aid}/outputs", params={"limit": 2},
    )
    assert r.status_code == 200
    page1 = r.json()
    assert len(page1["items"]) == 2
    assert page1["next_cursor"] is not None
    r = await client.get(
        f"/api/v1/analysts/{aid}/outputs",
        params={"limit": 2, "cursor": page1["next_cursor"]},
    )
    page2 = r.json()
    # 4 total → page2 should hold the remaining 2.
    assert len(page2["items"]) == 2
    assert page2["next_cursor"] is None
    all_ids = (
        [item["id"] for item in page1["items"]]
        + [item["id"] for item in page2["items"]]
    )
    assert set(all_ids) == {
        str(fid) for fid in findings
    } | {str(alert_id)}

    # Kind filter excludes the alert.
    r = await client.get(
        f"/api/v1/analysts/{aid}/outputs", params={"kind": "finding"},
    )
    assert r.status_code == 200
    page = r.json()
    assert {item["id"] for item in page["items"]} == {
        str(fid) for fid in findings
    }


# ---------------------------------------------------------------------------
# /critiques — analyzed (not judge) filter + pagination + rubric breakdown.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_analyst_critiques_analyzed_not_judge(
    api_app, client: AsyncClient,
):
    _, _, pg_store = api_app
    analyzed = f"analyzed_{uuid4().hex[:8]}"
    other = f"other_{uuid4().hex[:8]}"
    judge = f"critic_{uuid4().hex[:8]}"

    # Trace owned by the analyzed analyst — critique attaches.
    trace_a = uuid4()
    await _insert_analyst_trace(
        pg_store, run_id=trace_a, analyst_id=analyzed,
    )
    crit_a = await _insert_analyst_critique(
        pg_store, trace_id=trace_a, judge_analyst_id=judge,
        scores={"accuracy": 0.9, "completeness": 0.85},
        overall_score=0.88,
    )

    # Trace owned by a *different* analyst — must NOT appear when
    # filtering by `analyzed`.
    trace_o = uuid4()
    await _insert_analyst_trace(
        pg_store, run_id=trace_o, analyst_id=other,
    )
    await _insert_analyst_critique(
        pg_store, trace_id=trace_o, judge_analyst_id=judge,
    )

    # Trace where the analyzed analyst is the *judge* — also must NOT appear
    # in the analyzed filter (this guards against the wrong column).
    trace_j = uuid4()
    await _insert_analyst_trace(
        pg_store, run_id=trace_j, analyst_id=other,
    )
    await _insert_analyst_critique(
        pg_store, trace_id=trace_j, judge_analyst_id=analyzed,
    )

    r = await client.get(f"/api/v1/analysts/{analyzed}/critiques")
    assert r.status_code == 200, r.text
    page = r.json()
    assert len(page["items"]) == 1
    item = page["items"][0]
    assert item["id"] == str(crit_a)
    assert item["analyzed_analyst_id"] == analyzed
    assert item["judge_analyst_id"] == judge
    assert item["overall_score"] == pytest.approx(0.88)
    assert item["scores"] == {"accuracy": 0.9, "completeness": 0.85}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_analyst_critiques_pagination(api_app, client: AsyncClient):
    _, _, pg_store = api_app
    aid = f"crit_pg_{uuid4().hex[:8]}"
    judge = f"judge_{uuid4().hex[:8]}"
    base = datetime.now(tz=timezone.utc)
    crit_ids = []
    for i in range(3):
        tid = uuid4()
        await _insert_analyst_trace(
            pg_store, run_id=tid, analyst_id=aid,
            run_started_at=base - timedelta(minutes=i),
        )
        cid = await _insert_analyst_critique(
            pg_store, trace_id=tid, judge_analyst_id=judge,
            produced_at=base - timedelta(minutes=i),
        )
        crit_ids.append(cid)

    r = await client.get(
        f"/api/v1/analysts/{aid}/critiques", params={"limit": 2},
    )
    assert r.status_code == 200
    page1 = r.json()
    assert len(page1["items"]) == 2
    assert page1["next_cursor"] is not None

    r = await client.get(
        f"/api/v1/analysts/{aid}/critiques",
        params={"limit": 2, "cursor": page1["next_cursor"]},
    )
    page2 = r.json()
    assert len(page2["items"]) == 1
    assert page2["next_cursor"] is None
    combined = {item["id"] for item in page1["items"]} | {
        item["id"] for item in page2["items"]
    }
    assert combined == {str(c) for c in crit_ids}
