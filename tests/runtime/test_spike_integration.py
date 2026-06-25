# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 5a vertical-slice integration test — DAPRD VARIANT (L-002a §6).

Test isolation (Wave B prereq #3)
---------------------------------
This file routes every actor invocation through the SHARED production
daprd sidecar (configured via ``dapr/components/statestore.yaml`` against
the production ``legba`` DB).  To keep concurrent test sessions (and
re-runs of this session) from colliding on dapr_state rows, every
target_actor_id / analyst_actor_id in this file embeds a session-scoped
prefix from the ``dapr_actor_session_prefix`` fixture (see
``tests/runtime/conftest.py``).  Format:

    target::<prefix>::<descriptor_id>::<vfrag>
    analyst::<prefix>::<descriptor_id>::<vfrag>

The pre-test ``DELETE FROM dapr_state WHERE key LIKE '%' || $1 || '%'``
guard then matches only this session's rows.

Full per-session sidecar bring-up (separate daprd subprocess against a
per-session legba_test_<uuid> DB) is the L-002a follow-up tracked in
DESIGN §19 — the ``dapr_test_statestore_component`` fixture renders the
YAML for that path so it's ready to wire up.

This is the acceptance test for gates 1-7 of the Phase 5a runtime
validation. UNLIKE the embedded version, every actor invocation in this
file goes through a real ``daprd`` sidecar:

    test --ActorProxy--> daprd (sidecar @ localhost:3500/50001)
                            |
                            v
                FastAPI app (built by build_dapr_host_app, hosted
                 in-process on port 6090 in a background asyncio task)
                            |
                            v
                  Dapr Python SDK ActorRuntime dispatcher
                            |
                            v
                 legba.runtime.dapr_actors.TargetActor / AnalystActor

Prereqs (the test will fail-fast / skip when missing):

  1. ``docker compose --profile dapr up -d`` has started both
     ``dapr-placement`` (port 50005) and ``dapr-sidecar`` (ports
     3500 / 50001) containers.
  2. Substrate containers (Postgres, NATS, Redis) running.

The test session:

  * brings up the FastAPI app on port 6090,
  * registers stack components + descriptors,
  * populates the per-actor deps registry (so the actor methods can
    resolve their source factories / pipelines / LLM handler),
  * exercises every actor method through ``ActorProxy.create()``,
  * verifies signals + finding land in substrate with provenance,
  * verifies state survives a fresh ActorProxy lookup (restart
    survival proxy — the actor is deactivated and rehydrated by Dapr
    via the state store, no in-process retention).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import urllib.request
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Mapping
from uuid import UUID

import asyncpg
import httpx
import pytest
import pytest_asyncio
import uvicorn
from dapr.actor import ActorId, ActorProxy
from dapr.actor.runtime.runtime import ActorRuntime
from pydantic import BaseModel

from legba.data.config import NatsConfig, PostgresConfig
from legba.data.filters._contract import FilterContext
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault
from legba.data.registry.descriptor import DescriptorRegistry, Family
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import load_default_identity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache
from legba.data.schemas.analyst import (
    AnalystDescriptor,
    AnalystIdentity,
    AnalystKind,
    CadenceBlock,
    EvalBlock,
    MappingBlock,
    MethodBlock,
    SubscriptionBlock,
    SubscriptionTargets,
    TypeSignature,
)
from legba.data.schemas.lifecycle import AbstractionLevel, LifecycleState
from legba.data.schemas.properties import Property
from legba.data.schemas.stack import (
    EmbeddingService,
    EmbeddingServiceConfig,
    LLMProvider,
    LLMProviderConfig,
    NATSCluster,
    NATSClusterConfig,
    PostgresCluster,
    PostgresClusterConfig,
    VectorStore,
    VectorStoreConfig,
)
from legba.data.schemas.source import SourceRef
from legba.data.schemas.target import (
    GeoScope,
    InlineAnalystBlock,
    OutputBinding,
    PipelineStage,
    TargetDescriptor,
    TargetIdentity,
    TargetPipeline,
)
from legba.runtime import dapr_actors
from legba.runtime.dapr_actors import _unwrap_factory_dict
from legba.runtime.analyst_method import LLMAnalystRunner
from legba.runtime.budget import BudgetEnforcer
from legba.runtime.dapr_host import build_dapr_host_app
from legba.runtime.deps import StandardDeps
from legba.runtime.pipeline import PipelineRunner, build_filter_handler


# ---------------------------------------------------------------------------
# Lineage-walk helper (test-local). The old runtime/lineage.py module was
# deleted by C-3 (zero src callers; its SELECT targeted pre-pivot signals
# columns). This walks derived_from backward across analyst_outputs +
# signals on the POST-pivot schema and returns the reachable signal ids.
# ---------------------------------------------------------------------------


async def _walk_finding_to_signal_ids(
    conn, finding_id: UUID, *, max_depth: int = 16,
) -> set[UUID]:
    root = await conn.fetchrow(
        "SELECT id, derived_from FROM analyst_outputs WHERE id = $1",
        finding_id,
    )
    assert root is not None, f"finding {finding_id} not in analyst_outputs"
    visited: set[UUID] = {finding_id}
    frontier = [UUID(str(u)) for u in (root["derived_from"] or [])]
    signal_hits: set[UUID] = set()
    for _ in range(max_depth):
        ids = [u for u in frontier if u not in visited]
        if not ids:
            break
        visited.update(ids)
        sig_rows = await conn.fetch(
            "SELECT id, derived_from FROM signals WHERE id = ANY($1::uuid[])",
            ids,
        )
        out_rows = await conn.fetch(
            "SELECT id, derived_from FROM analyst_outputs "
            "WHERE id = ANY($1::uuid[])",
            ids,
        )
        frontier = []
        for row in sig_rows:
            signal_hits.add(UUID(str(row["id"])))
            frontier.extend(UUID(str(u)) for u in (row["derived_from"] or []))
        for row in out_rows:
            frontier.extend(UUID(str(u)) for u in (row["derived_from"] or []))
    return signal_hits


# ---------------------------------------------------------------------------
# Pre-flight: daprd reachability gates the whole module
# ---------------------------------------------------------------------------


DAPR_HTTP_PORT = int(os.getenv("DAPR_HTTP_PORT", "3500"))
DAPR_GRPC_PORT = int(os.getenv("DAPR_GRPC_PORT", "50001"))
DAPR_PLACEMENT_PORT = int(os.getenv("DAPR_PLACEMENT_PORT", "50005"))
APP_PORT = int(os.getenv("LEGBA_RUNTIME_HTTP_PORT", "6090"))


def _port_open(port: int, host: str = "localhost", timeout: float = 2.0) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _dapr_sidecar_outbound_ok() -> bool:
    """Daprd's /v1.0/healthz/outbound returns 204 once components load —
    independent of whether our app channel is up. Use this to assert
    'placement + sidecar healthy' for gate 1."""
    try:
        req = urllib.request.Request(f"http://localhost:{DAPR_HTTP_PORT}/v1.0/healthz/outbound")
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 204
    except urllib.error.HTTPError as e:
        return e.code == 204
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not (_port_open(DAPR_HTTP_PORT) and _port_open(DAPR_GRPC_PORT) and _port_open(DAPR_PLACEMENT_PORT)),
    reason=(
        f"daprd not running on localhost:{DAPR_HTTP_PORT}/{DAPR_GRPC_PORT}/"
        f"{DAPR_PLACEMENT_PORT} — bring up with `docker compose --profile dapr up -d`"
    ),
)


# ---------------------------------------------------------------------------
# Test stand-ins
# ---------------------------------------------------------------------------


SAMPLE_RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>EPE Brazil Energy News</title>
    <link>https://www.epe.gov.br</link>
    <description>Energy infrastructure briefings</description>
    <item>
      <title>Itaipu hydro plant upgrade complete</title>
      <link>https://www.epe.gov.br/news/itaipu-upgrade-2026</link>
      <guid isPermaLink="false">epe-itaipu-2026-05-19</guid>
      <pubDate>Mon, 19 May 2026 14:00:00 GMT</pubDate>
      <description>Brazil's Itaipu hydroelectric plant completes its third-generation turbine upgrade.</description>
    </item>
    <item>
      <title>Northeast wind capacity hits record</title>
      <link>https://www.epe.gov.br/news/wind-capacity-2026</link>
      <guid isPermaLink="false">epe-wind-2026-05-18</guid>
      <pubDate>Sun, 18 May 2026 09:30:00 GMT</pubDate>
      <description>Northeast Brazil wind farms hit a new generation peak last week.</description>
    </item>
    <item>
      <title>Petrobras reports Q1 refinery throughput</title>
      <link>https://www.epe.gov.br/news/petrobras-q1-2026</link>
      <guid isPermaLink="false">epe-petrobras-q1-2026</guid>
      <pubDate>Sat, 17 May 2026 10:00:00 GMT</pubDate>
      <description>Petrobras Q1 refinery utilization across Brazilian sites.</description>
    </item>
  </channel>
</rss>
"""


def _build_mock_rss_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=SAMPLE_RSS_FEED.encode("utf-8"),
            headers={
                "Content-Type": "application/rss+xml",
                "ETag": '"deterministic-etag-001"',
                "Last-Modified": "Mon, 19 May 2026 14:00:01 GMT",
            },
        )
    return httpx.MockTransport(handler)


class TestLLMHandler:
    """Canned-JSON LLM handler. Same shape as the embedded test."""

    subprovider = "openai"

    class _Cfg:
        class _ModelName:
            raw = "gpt-oss-120b"
        model_name = _ModelName()

    _cfg = _Cfg()
    _instance_id = "llm.primary.openai_compat"
    _instance_version = "ff" * 8

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        finding = {
            "title": "Brazil Energy Infrastructure Update (May 17-19, 2026)",
            "body": (
                "Three observations from EPE feed: Itaipu turbine upgrade complete; "
                "Northeast wind farm peak generation; Petrobras Q1 refinery "
                "utilization figures published."
            ),
            "confidence": 0.85,
            "evidence": [
                "Itaipu upgrade May 19",
                "Wind capacity record May 18",
                "Petrobras Q1 figures",
            ],
            "tags": ["energy", "brazil", "infrastructure"],
        }

        class _Usage:
            prompt_tokens = 412
            completion_tokens = 187
            reasoning_tokens = 0
            cache_read_tokens = 0
            cache_write_tokens = 0
            total_tokens = 599
            cost_estimate_usd = 0.0
            model = "gpt-oss-120b"

        class _Response:
            content = json.dumps(finding)
            finish_reason = "stop"
            tool_calls: list = []
            usage = _Usage()
            raw_response: dict[str, Any] | None = None

        return _Response()


# ---------------------------------------------------------------------------
# Pytest fixtures — substrate
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg):
    pool = await asyncpg.create_pool(
        host=migrated_pg.host,
        port=migrated_pg.port,
        user=migrated_pg.user,
        password=migrated_pg.password,
        database=migrated_pg.database,
        min_size=1,
        max_size=4,
    )
    # The Dapr-native actors store their lifecycle/cursors in Dapr's
    # state component (NOT the legacy ``actor_state`` table). But the
    # filter handlers still use ``actor_filter_state`` (per L-102) for
    # per-pipeline-instance state (dedupe seen-hashes etc.). Ensure that
    # table exists in the test DB — the runtime ``ActorStateStore.
    # ensure_schema()`` writes both ``actor_state`` and
    # ``actor_filter_state``, and we re-use it here.
    from legba.runtime.state import ActorStateStore
    await ActorStateStore(pool).ensure_schema()
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def pg_store(migrated_pg):
    store = PostgresStore(migrated_pg)
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def nats_store():
    cfg = NatsConfig.from_env()
    store = NatsStore(cfg)
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def vault(pg_store):
    v = CredentialVault(pg_store)
    secrets_seed = {
        "llm.primary.api_key": os.getenv(
            "OPENAI_API_KEY",
            "test-only-not-real-a884498c-45e6-4bcc-a128-680f0c04a74d",
        ),
        "pg.cluster_main.password": "legba",
        "gcp.bigquery.legba_497003.service_account": "{}",
    }
    for sid, plaintext in secrets_seed.items():
        with suppress(Exception):
            await v.store_secret(sid, plaintext, actor="phase5a_dapr_test", notes="seeded")
    yield v


@pytest_asyncio.fixture
async def descriptor_registry(pg_store, nats_store):
    identity = load_default_identity()
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    await vocab.refresh()
    reg = DescriptorRegistry(
        pg_store,
        nats_store=nats_store,
        vocabulary_cache=vocab,
        signing_identity=identity,
        audit_logger=audit,
        dead_letter=dlq,
    )
    await reg.start()
    yield reg
    await reg.stop()


@pytest_asyncio.fixture
async def stack_registry(pg_store, vault):
    reg = StackRegistry(pg_store, vault)
    yield reg


# ---------------------------------------------------------------------------
# Pytest fixture — Dapr host (FastAPI + DaprActor) running in the test process
# ---------------------------------------------------------------------------


class _BackgroundUvicorn:
    """uvicorn.Server wrapped to start in an asyncio task and stop cleanly."""

    def __init__(self, app, port: int):
        self._cfg = uvicorn.Config(
            app, host="0.0.0.0", port=port,
            log_level="warning", log_config=None,
        )
        self._server = uvicorn.Server(self._cfg)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve(), name="dapr-host-test")
        # Wait until uvicorn signals "started" so daprd's app-channel
        # health probe stops returning 500.
        for _ in range(40):  # ~4s
            if self._server.started:
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("uvicorn did not start within 4s")

    async def stop(self) -> None:
        self._server.should_exit = True
        if self._task is not None:
            with suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(self._task, timeout=3)


@pytest_asyncio.fixture
async def dapr_host_app():
    """Bring up the FastAPI dapr-host on port 6090 inside this test session.

    daprd routes ActorProxy invocations to this app via host.docker.internal.
    The fixture also clears the per-actor deps registry on teardown.
    """
    # The LIVE runtime-dapr host already holds APP_PORT (6090) on the shared
    # ``--network host`` rig — binding it here errors with "address already
    # in use". This test needs an isolated runtime port; skip rather than
    # collide with the live runtime.
    if _port_open(APP_PORT):
        pytest.skip(
            "needs an isolated runtime port; collides with live runtime on "
            "--network host"
        )

    # Force the SDK's runtime ports to match the running daprd sidecar.
    os.environ["DAPR_HTTP_PORT"] = str(DAPR_HTTP_PORT)
    os.environ["DAPR_GRPC_PORT"] = str(DAPR_GRPC_PORT)

    app = build_dapr_host_app(app_port=APP_PORT)
    server = _BackgroundUvicorn(app, port=APP_PORT)
    await server.start()

    # Give daprd a moment to notice the app is up (its app-channel health
    # probe polls every 5s by default). 2s is enough for most cases; if the
    # full healthz still returns 500 after that we proceed anyway — the
    # outbound-only health gate is what we need.
    await asyncio.sleep(2)

    yield app

    dapr_actors.clear_deps_registry()
    await server.stop()


# ---------------------------------------------------------------------------
# Descriptor builders (identical to embedded test)
# ---------------------------------------------------------------------------


def _placeholder_version() -> str:
    return "0" * 64


def _build_stack_components() -> list[BaseModel]:
    return [
        LLMProvider(
            id="llm.primary.openai_compat",
            name="gpt-oss-120b via vLLM (OpenAI-compatible)",
            schema_uri="legba/stack/llm_provider/1.0.0",
            version=_placeholder_version(),
            owner="phase5a_dapr_test",
            config=LLMProviderConfig(
                api_endpoint=Property.Text.of("https://llm.example.internal/v1"),
                api_key=Property.Secret.of("llm.primary.api_key"),
                model_name=Property.Text.of("gpt-oss-120b"),
                max_tokens=Property.Number.of(16384, minimum=1, maximum=131072),
            ),
        ),
        EmbeddingService(
            id="embed.primary.openai_compat",
            name="self-hosted bge-m3 embeddings",
            schema_uri="legba/stack/embedding/1.0.0",
            version=_placeholder_version(),
            owner="phase5a_dapr_test",
            config=EmbeddingServiceConfig(
                endpoint=Property.Text.of("https://llm.example.internal/v1"),
                model_name=Property.Text.of("bge-m3"),
                dim=Property.Number.of(1024),
            ),
        ),
        NATSCluster(
            id="nats.cluster_main",
            name="Primary NATS JetStream cluster",
            schema_uri="legba/stack/nats/1.0.0",
            version=_placeholder_version(),
            owner="phase5a_dapr_test",
            config=NATSClusterConfig(
                servers=Property.List(raw=["nats://localhost:4222"], item_kind="text"),
            ),
        ),
        PostgresCluster(
            id="pg.cluster_main",
            name="Primary Postgres+AGE cluster",
            schema_uri="legba/stack/postgres/1.0.0",
            version=_placeholder_version(),
            owner="phase5a_dapr_test",
            config=PostgresClusterConfig(
                host=Property.Text.of("localhost"),
                database=Property.Text.of("legba"),
                user=Property.Text.of("legba"),
                password=Property.Secret.of("pg.cluster_main.password"),
            ),
        ),
        VectorStore(
            id="vector.qdrant.cluster_main",
            name="Primary Qdrant cluster",
            schema_uri="legba/stack/vector_store/1.0.0",
            version=_placeholder_version(),
            owner="phase5a_dapr_test",
            config=VectorStoreConfig(
                endpoint=Property.Text.of("http://localhost:6333"),
                collection_prefix=Property.Text.of("legba_signals"),
            ),
        ),
    ]


def _build_target_descriptor() -> TargetDescriptor:
    return TargetDescriptor(
        identity=TargetIdentity(
            id="india_energy_infra_dapr",
            name="Brazil Energy Infrastructure (dapr-test)",
            schema_uri="legba/target/2.0.0",
            version=_placeholder_version(),
            abstraction_level=AbstractionLevel.L1,
            state=LifecycleState.ACTIVE,
            owner="phase5a_dapr_test",
            created=datetime.now(tz=timezone.utc),
        ),
        scope=GeoScope(
            geo=["BR"],
            languages=["pt-BR", "en"],
            entity_classes=["organization", "corporation"],
            predicate=None,
        ),
        # Source-first pivot: targets reference SHARED sources via SourceRef
        # (explicit id or selector) — the RSS pull config now lives on the
        # SourceDescriptor, not inline on the target.
        sources=[
            SourceRef(source_id="source.epe.rss"),
        ],
        pipeline=TargetPipeline(
            ingestion_filters=[
                PipelineStage(kind="language_detect", config={}),
                PipelineStage(kind="dedupe_tier_1", config={}),
                PipelineStage(kind="dedupe_tier_2", config={}),
            ],
        ),
        analyst=InlineAnalystBlock(
            use="inline_target",
            cadence={"fallback_schedule": "*/10 * * * *"},
            method={
                "kind": "llm_planner",
                "llm": {"primary": "llm.primary.openai_compat"},
            },
        ),
        outputs=[
            OutputBinding(
                kind="a2a_skill",
                config={"skill_id": "intelligence.india_energy_assessment_dapr"},
            ),
        ],
    )


def _build_analyst_descriptor(*, target_id: str) -> AnalystDescriptor:
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id="india_energy_inline_dapr",
            name="Brazil Energy Inline Analyst (dapr-test)",
            schema_uri="legba/analyst/1.0.0",
            version=_placeholder_version(),
            kind=AnalystKind.INLINE_TARGET,
            type_signature=TypeSignature(
                input_type="legba.runtime.SignalList",
                output_type="legba.runtime.Finding",
            ),
            state=LifecycleState.ACTIVE,
            owner="phase5a_dapr_test",
        ),
        subscription=SubscriptionBlock(
            targets=SubscriptionTargets(
                predicate=f'target_id() == "{target_id}"',
                data_types=["signal"],
                time_window="24h",
            ),
        ),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="llm_single_turn",
            prompt_module="legba.runtime.analyst_method:_DEFAULT_SYSTEM",
            llm={
                "primary": Property.StackRef(
                    raw="llm.primary.openai_compat",
                    expected_family="llm_provider",
                ).model_dump(),
                "max_tokens": 1024,
            },
            budget_tokens_per_day=200_000,
        ),
        cadence=CadenceBlock(
            fallback_schedule="*/10 * * * *",
            cooldown_seconds=300,
        ),
        outputs=[
            OutputBinding(
                kind="a2a_skill",
                config={"skill_id": "intelligence.india_energy_assessment_dapr"},
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Gate tests (registry-only, no daprd needed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate3_registry_can_register_stack_components(
    pg_pool,
    stack_registry: StackRegistry,
    descriptor_registry: DescriptorRegistry,
) -> None:
    actor = "phase5a_dapr_test"
    expected_ids = [
        "llm.primary.openai_compat",
        "embed.primary.openai_compat",
        "nats.cluster_main",
        "pg.cluster_main",
        "vector.qdrant.cluster_main",
    ]
    # Test isolation (B4): the stack_registry writes into the SHARED session
    # `migrated_pg` DB. A sibling test (the gates-2-through-7 e2e) registers
    # the SAME component_ids into that DB earlier in the session, so a clean
    # `register()` here would otherwise hit VersionConflict purely on order.
    # Clear these specific rows first so this gate proves a from-scratch
    # registration deterministically, without weakening the assertion.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM stack_components WHERE component_id = ANY($1::text[])",
            expected_ids,
        )
    registered_ids: list[str] = []
    for component in _build_stack_components():
        body = component.model_dump(mode="json", by_alias=True)
        row = await stack_registry.register(body, actor)
        registered_ids.append(row.component_id)
    assert registered_ids == expected_ids


@pytest.mark.asyncio
async def test_gate5_register_target_descriptor(
    descriptor_registry: DescriptorRegistry,
) -> None:
    target = _build_target_descriptor()
    row = await descriptor_registry.register(target, actor="phase5a_dapr_test")
    assert row.descriptor_id == "india_energy_infra_dapr"
    assert row.is_head
    assert row.state == "active"


@pytest.mark.asyncio
async def test_gate6_register_analyst_descriptor(
    descriptor_registry: DescriptorRegistry,
) -> None:
    analyst = _build_analyst_descriptor(target_id="india_energy_infra_dapr")
    row = await descriptor_registry.register(analyst, actor="phase5a_dapr_test")
    assert row.descriptor_id == "india_energy_inline_dapr"
    assert row.kind == "inline_target"


# ---------------------------------------------------------------------------
# Gate 1: daprd is actually running and healthy on its outbound path
# ---------------------------------------------------------------------------


def test_gate1_daprd_outbound_healthy() -> None:
    """Sidecar's `/v1.0/healthz/outbound` should be 204 once its components load."""
    assert _dapr_sidecar_outbound_ok(), (
        "daprd outbound healthz did not return 204 — check `docker compose --profile dapr ps`"
    )


# ---------------------------------------------------------------------------
# Gates 2-7: end-to-end run through daprd
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gates_2_through_7_end_to_end_through_daprd(
    pg_pool,
    pg_store: PostgresStore,
    nats_store: NatsStore,
    descriptor_registry: DescriptorRegistry,
    stack_registry: StackRegistry,
    vault: CredentialVault,
    dapr_host_app,
    dapr_actor_session_prefix: str,
) -> None:
    """The headline test — every actor invocation routed through daprd.

    Steps:

      1. Register stack components + descriptors (gate 3 / 5 / 6 pre-reqs).
      2. Build the dependencies bundle for each actor, populate the
         process-global deps registry the Dapr-native actors read on
         activation.
      3. ActorProxy.create(...) for each actor and:
         a. invoke ``activate`` — assert lifecycle transitions to active
         b. invoke ``run``      — for target: pulls + writes signals;
                                  for analyst: reads slice + writes finding
         c. invoke ``get_state`` — verify Dapr-state holds the cursors +
            last_outcome (gate 3 + 4 pass)
      4. Walk lineage from finding → source signals (gate 5).
      5. Restart simulation: tell Dapr to deactivate the actor, then
         re-acquire the proxy and invoke ``get_state`` — Dapr should
         re-hydrate from its state store and return the persisted record
         (gate 6).
    """
    actor = "phase5a_dapr_test"

    # ---- Register stack components + descriptors ----------------------
    for component in _build_stack_components():
        body = component.model_dump(mode="json", by_alias=True)
        with suppress(Exception):
            await stack_registry.register(body, actor)
    with suppress(Exception):
        await descriptor_registry.register(
            _build_target_descriptor(), actor=actor,
        )
    with suppress(Exception):
        await descriptor_registry.register(
            _build_analyst_descriptor(target_id="india_energy_infra_dapr"),
            actor=actor,
        )

    target_descriptor = await descriptor_registry.get_typed(
        "india_energy_infra_dapr", family=Family.TARGET,
    )
    analyst_descriptor = await descriptor_registry.get_typed(
        "india_energy_inline_dapr", family=Family.ANALYST,
    )

    # ---- Build mocked RSS handler + redis client for dedupe ----------
    from legba.data.sources.rss import RSSConfig, RSSSourceHandler

    rss_transport = _build_mock_rss_transport()
    rss_client = httpx.AsyncClient(transport=rss_transport, timeout=5.0)
    rss_config = RSSConfig(url="https://www.epe.gov.br/feed")
    rss_handler = RSSSourceHandler(config=rss_config, http_client=rss_client)

    import redis.asyncio as redis_async

    redis_url = os.getenv("LEGBA_DATA_REDIS_URL", "redis://localhost:6379")
    redis_client = redis_async.from_url(redis_url, decode_responses=True)
    # Flush dedupe keys that match our target so the test isn't dependent on
    # the redis prior state. We only flush keys carrying our target id —
    # other tests' state is untouched.
    target_pat = f"*{target_descriptor.identity.id}*"
    async for key in redis_client.scan_iter(match=target_pat):
        await redis_client.delete(key)

    # Source-first (L-205 / B5): the TargetActor pull loop + its
    # source_factory / pipeline_factory deps were retired. SourceActor owns
    # acquisition; a TargetActor's deps are just descriptor + StandardDeps.
    deps = StandardDeps(
        pg_pool=pg_pool,
        nats_publish=nats_store.publish_json,
        secrets_resolve=vault.resolve,
    )

    # ---- Populate the actor deps registry ----------------------------
    # Wave B prereq #3: embed session prefix so dapr_state rows from
    # parallel / re-run sessions stay byte-disjoint.
    target_actor_id = (
        f"target::{dapr_actor_session_prefix}::"
        f"{target_descriptor.identity.id}::"
        f"{target_descriptor.identity.version[:8]}"
    )
    analyst_actor_id = (
        f"analyst::{dapr_actor_session_prefix}::"
        f"{analyst_descriptor.identity.id}::"
        f"{analyst_descriptor.identity.version[:8]}"
    )

    # Dapr's state component points at the production ``legba`` Postgres DB
    # (its connectionString is fixed in dapr/components/statestore.yaml). Test
    # isolation requires we clear any stale rows from prior test runs for
    # these exact actor_ids before invoking. We do this via a direct asyncpg
    # connection to the production DB — out-of-band of the Dapr SDK, which
    # is fine because the SDK reads its own writes.
    import asyncpg as _asyncpg
    _prod = await _asyncpg.connect(
        host="127.0.0.1", port=5432, user="legba", password="legba",
        database="legba",
    )
    try:
        await _prod.execute(
            "DELETE FROM dapr_state WHERE key LIKE '%' || $1 || '%' OR key LIKE '%' || $2 || '%'",
            target_actor_id, analyst_actor_id,
        )
    finally:
        await _prod.close()

    dapr_actors.register_target_deps(
        target_actor_id,
        dapr_actors._TargetDeps(
            descriptor=target_descriptor,
            deps=deps,
        ),
    )

    llm = TestLLMHandler()
    runner = LLMAnalystRunner(llm, max_tokens=1024)
    budget = BudgetEnforcer(
        analyst_id=analyst_descriptor.identity.id,
        analyst_version=analyst_descriptor.identity.version,
        budget_tokens_per_day=200_000,
        provider="openai",
        model="gpt-oss-120b",
    )
    dapr_actors.register_analyst_deps(
        analyst_actor_id,
        dapr_actors._AnalystDeps(
            descriptor=analyst_descriptor,
            deps=deps,
            run_method=runner,
            budget=budget,
        ),
    )

    # ---- ActorProxy: invoke target activate, then run ---------------
    target_proxy = ActorProxy.create(
        "TargetActor",
        ActorId(target_actor_id),
        TargetActorInterface,
    )

    activated = await _await_actor_call(target_proxy, "activate", None)
    assert activated.get("lifecycle") == "active", (
        f"target activation did not reach active: {activated}"
    )

    run_result = await _await_actor_call(target_proxy, "run", {"trigger_kind": "method"})
    assert run_result.get("outcome") in {"success", "noop"}, (
        f"target run unexpected outcome: {run_result}"
    )

    async with pg_pool.acquire() as conn:
        signal_rows = await conn.fetch(
            "SELECT id, title, target_id, target_version, schema_uri, derived_from "
            "FROM signals WHERE target_id = $1 ORDER BY produced_at DESC",
            target_descriptor.identity.id,
        )
    assert len(signal_rows) >= 1, (
        f"expected at least one signal row, got {len(signal_rows)}. "
        "If redis was warm from a prior test, run `redis-cli FLUSHDB`."
    )
    signal_ids = [UUID(str(r["id"])) for r in signal_rows]
    for r in signal_rows:
        assert r["target_id"] == target_descriptor.identity.id
        assert r["target_version"] == target_descriptor.identity.version

    # ---- ActorProxy: invoke analyst activate + run ------------------
    analyst_proxy = ActorProxy.create(
        "AnalystActor",
        ActorId(analyst_actor_id),
        AnalystActorInterface,
    )
    activated_a = await _await_actor_call(analyst_proxy, "activate", None)
    assert activated_a.get("lifecycle") == "active"

    run_a = await _await_actor_call(
        analyst_proxy, "run",
        {"trigger_kind": "method", "target_filter": target_descriptor.identity.id},
    )
    assert run_a.get("outcome") == "success", f"analyst run did not succeed: {run_a}"
    finding_id = UUID(run_a["finding_id"])

    # ---- Substrate side-effect assertions -----------------------------
    async with pg_pool.acquire() as conn:
        finding_row = await conn.fetchrow(
            "SELECT id, title, body, analyst_id, analyst_version, target_id, "
            "derived_from, schema_uri, kind FROM analyst_outputs WHERE id = $1",
            finding_id,
        )
    assert finding_row is not None, "finding row not in analyst_outputs"
    derived = [UUID(str(u)) for u in (finding_row["derived_from"] or [])]
    assert derived, "finding has empty derived_from — provenance chain broken"
    assert set(derived).issubset(set(signal_ids))

    async with pg_pool.acquire() as conn:
        walked_signal_ids = await _walk_finding_to_signal_ids(conn, finding_id)
    assert len(walked_signal_ids) >= 1, "lineage walk surfaced no source signals"
    # Every walked signal must be one of the rows this test seeded — the
    # provenance chain points at real substrate rows, not invented ids.
    assert walked_signal_ids.issubset(set(signal_ids)), (
        f"lineage walk left the seeded signal set: {walked_signal_ids}"
    )

    async with pg_pool.acquire() as conn:
        budget_row = await conn.fetchrow(
            "SELECT tokens_used, runs FROM budget_ledger "
            "WHERE analyst_id = $1 AND analyst_version = $2 AND bucket = CURRENT_DATE",
            analyst_descriptor.identity.id, analyst_descriptor.identity.version,
        )
    assert budget_row is not None
    assert int(budget_row["tokens_used"]) >= 599

    # ---- get_state through ActorProxy verifies Dapr state -----------
    target_state = await _await_actor_call(target_proxy, "get_state", None)
    assert target_state.get("lifecycle") == "active"
    cursors = target_state.get("source_cursors") or {}
    assert "epe_rss" in cursors, f"epe_rss cursor missing from Dapr state: {cursors}"

    analyst_state = await _await_actor_call(analyst_proxy, "get_state", None)
    assert analyst_state.get("lifecycle") == "active"
    assert analyst_state.get("last_outcome") == "success"

    # ---- Restart-survival simulation -------------------------------------
    # Tell daprd to deactivate the actor (puts it through _on_deactivate +
    # drops the in-memory instance). Next method call re-activates via
    # _on_activate, which reads back from the Dapr state store.
    await _deactivate_actor("TargetActor", target_actor_id)
    await _deactivate_actor("AnalystActor", analyst_actor_id)

    # Re-acquire proxies (no per-process retention) and read state back.
    target_proxy2 = ActorProxy.create(
        "TargetActor", ActorId(target_actor_id), TargetActorInterface,
    )
    target_state2 = await _await_actor_call(target_proxy2, "get_state", None)
    assert target_state2.get("lifecycle") == "active", (
        "lifecycle not preserved across Dapr deactivation/reactivation"
    )
    cursors2 = target_state2.get("source_cursors") or {}
    assert "epe_rss" in cursors2, (
        f"source cursors lost across Dapr deactivation: {cursors2}"
    )

    analyst_proxy2 = ActorProxy.create(
        "AnalystActor", ActorId(analyst_actor_id), AnalystActorInterface,
    )
    analyst_state2 = await _await_actor_call(analyst_proxy2, "get_state", None)
    assert analyst_state2.get("lifecycle") == "active"
    assert analyst_state2.get("last_outcome") == "success", (
        "last_outcome did not survive Dapr deactivation"
    )

    # Cleanup
    await redis_client.aclose()
    await rss_client.aclose()


# ---------------------------------------------------------------------------
# Helpers — interface stubs + raw HTTP fallback for actor invoke
# ---------------------------------------------------------------------------


# The TargetActorInterface / AnalystActorInterface live in dapr_actors, but
# the SDK uses interface type-hinted methods only for serializer hints.
# Importing them here so ActorProxy.create can take the interface arg.
from legba.runtime.dapr_actors import (
    AnalystActorInterface,
    TargetActorInterface,
)


async def _await_actor_call(proxy: ActorProxy, method: str, payload) -> dict[str, Any]:
    """Invoke an actor method through the SDK and decode the JSON reply.

    ``proxy.invoke_method`` requires ``bytes`` (or ``None``) for raw_body
    and returns ``bytes``. We JSON-encode dict payloads on the way in
    and JSON-decode the response on the way out so the test surface deals
    in plain Python dicts.
    """
    if payload is None:
        body: bytes | None = None
    elif isinstance(payload, (bytes, bytearray)):
        body = bytes(payload)
    else:
        body = json.dumps(payload).encode("utf-8")
    raw = await proxy.invoke_method(method, body)
    if isinstance(raw, (bytes, bytearray)):
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise AssertionError(
                f"actor method {method!r} returned non-JSON: {raw[:200]!r}"
            ) from e
    if isinstance(raw, dict):
        return raw
    return {"raw": str(raw)}


async def _deactivate_actor(actor_type: str, actor_id: str) -> None:
    """Hit daprd's actor-deactivation endpoint directly via the management API."""
    url = (
        f"http://localhost:{DAPR_HTTP_PORT}/v1.0/actors/"
        f"{actor_type}/{actor_id}"
    )
    req = urllib.request.Request(url, method="DELETE")
    try:
        urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as e:
        # 200 / 204 fine, 4xx not fatal here (actor may not be active).
        if e.code >= 500:
            raise


# ---------------------------------------------------------------------------
# Gates 8 + 9 — consult_on_demand + predictor through daprd
# ---------------------------------------------------------------------------
#
# These exercise the Wave-A integration pass: per-kind ``OUTPUT_KIND`` +
# ``READ_SLICE`` + 3-arg ``run_method(inputs, options, deps)`` flow
# through the Dapr-native AnalystActor against a real daprd sidecar.


def _build_predictor_analyst_descriptor(*, target_id: str) -> AnalystDescriptor:
    """Predictor descriptor — same shape as the spike's inline analyst
    but registers as kind PREDICTOR.  Uses the same LLM-stack ref so the
    descriptor passes registry validation."""
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id="india_energy_predictor_dapr",
            name="Brazil Energy Predictor (dapr-test)",
            schema_uri="legba/analyst/1.0.0",
            version=_placeholder_version(),
            kind=AnalystKind.PREDICTOR,
            type_signature=TypeSignature(
                input_type="legba.runtime.SignalList",
                output_type="legba.runtime.Prediction",
            ),
            state=LifecycleState.ACTIVE,
            owner="phase5a_dapr_test",
        ),
        subscription=SubscriptionBlock(
            targets=SubscriptionTargets(
                predicate=f'target_id() == "{target_id}"',
                data_types=["signal"],
                time_window="336h",  # 14 days
            ),
        ),
        mapping=MappingBlock(),
        method=MethodBlock(
            # Wave B prereq #1: ``stat_forecaster`` is the proper kind for the
            # AutoARIMA + optional-LLM-narrative shape.  ``hybrid`` was the
            # Wave A workaround when the Literal set didn't yet include it.
            kind="stat_forecaster",
            prompt_module="legba.prompts.predictor.v1",
            llm={
                "primary": Property.StackRef(
                    raw="llm.primary.openai_compat",
                    expected_family="llm_provider",
                ).model_dump(),
                "max_tokens": 512,
            },
            budget_tokens_per_day=50_000,
        ),
        cadence=CadenceBlock(
            fallback_schedule="0 */6 * * *",
            cooldown_seconds=900,
        ),
        outputs=[
            OutputBinding(
                kind="a2a_skill",
                config={"skill_id": "intelligence.india_energy_forecast_dapr"},
            ),
        ],
    )


def _build_consult_analyst_descriptor() -> AnalystDescriptor:
    """consult_on_demand descriptor — no cadence-driven schedule (the
    kind is on-demand only).  The registry still requires a cadence
    block; we use a long fallback that we never expect to fire because
    the actor is invoked directly via ActorProxy.
    """
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id="consult_on_demand_dapr",
            name="Consult-on-Demand (dapr-test)",
            schema_uri="legba/analyst/1.0.0",
            version=_placeholder_version(),
            kind=AnalystKind.CONSULT_ON_DEMAND,
            type_signature=TypeSignature(
                input_type="legba.runtime.ConsultRequest",
                output_type="legba.runtime.ConsultResponse",
            ),
            state=LifecycleState.ACTIVE,
            owner="phase5a_dapr_test",
        ),
        subscription=SubscriptionBlock(
            targets=SubscriptionTargets(
                predicate="True",
                data_types=["signal", "finding"],
                time_window="168h",
            ),
        ),
        mapping=MappingBlock(),
        method=MethodBlock(
            # Wave B prereq #1: ``react_loop`` is the proper kind for the
            # consult_on_demand multi-round tool-using loop.  ``llm_planner``
            # was the Wave A workaround when the Literal set didn't include
            # the ReAct shape.
            kind="react_loop",
            prompt_module="legba.prompts.consult_on_demand.v1",
            llm={
                "primary": Property.StackRef(
                    raw="llm.primary.openai_compat",
                    expected_family="llm_provider",
                ).model_dump(),
                "max_tokens": 1024,
            },
            budget_tokens_per_day=50_000,
        ),
        cadence=CadenceBlock(
            fallback_schedule="0 0 1 1 *",  # 1 Jan, effectively never
            cooldown_seconds=60,
        ),
        outputs=[
            OutputBinding(
                kind="a2a_skill",
                config={"skill_id": "intelligence.consult_on_demand_dapr"},
            ),
        ],
    )


class _StubSubstrate:
    """In-memory ``SubstrateQueryPort`` stub for the consult kind.

    The L-178 consult kind doesn't talk to Postgres directly — it goes
    through a substrate-tool surface (search_signals / query_facts /
    inspect_entity / vector_search).  For the integration test we
    return a deterministic set of refs so the ConsultResponsePayload's
    ``cited_substrate_refs`` walks back to known UUIDs.
    """

    def __init__(self, signal_id: UUID):
        self._signal_id = signal_id

    async def search_signals(
        self, *, query: str, category=None, limit: int = 20,
        scope_predicate=None,
    ) -> dict[str, Any]:
        return {
            "rows": [
                {
                    "id": str(self._signal_id),
                    "title": "Itaipu hydro plant upgrade complete",
                    "snippet": "Brazil Itaipu turbine upgrade completed May 19.",
                }
            ],
            "refs": [str(self._signal_id)],
        }

    async def query_facts(self, **_kwargs) -> dict[str, Any]:
        return {"rows": [], "refs": []}

    async def inspect_entity(self, *, name: str) -> dict[str, Any]:
        return {"entity": name, "facts": [], "refs": []}

    async def vector_search(self, **_kwargs) -> dict[str, Any]:
        return {"rows": [], "refs": [], "unavailable": False}


class _ConsultLLMHandler:
    """Canned-JSON LLM handler emitting a single-turn final-answer reply.

    Round 1: emits a search_signals tool call.
    Round 2: emits the final JSON citing the substrate ref.
    """

    subprovider = "openai"

    def __init__(self, signal_id: UUID):
        self._signal_id = signal_id
        self._round = 0

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None,
        **kwargs,
    ):
        self._round += 1

        if self._round == 1:
            content = json.dumps({
                "tool": "search_signals",
                "args": {"query": "Itaipu upgrade", "limit": 5},
            })
        else:
            content = json.dumps({
                "final": True,
                "answer": (
                    "Itaipu hydro plant completed its third-generation "
                    "turbine upgrade on 19 May 2026 (substrate ref cited)."
                ),
                "uncertainty": 0.15,
                "cited_refs": [str(self._signal_id)],
                "unanswered_aspects": [],
            })

        class _Usage:
            prompt_tokens = 320
            completion_tokens = 75
            reasoning_tokens = 0
            total_tokens = 395

        class _Response:
            tool_calls: list = []
            usage = _Usage()
            finish_reason = "stop"
            raw_response = None

        _Response.content = content
        return _Response()


class _PredictorLLMHandler:
    """LLM handler returning a terse narrative for the predictor's
    optional narrative LLM call.  Returns the same canned text regardless
    of input — the predictor reads ``response.content`` directly."""

    subprovider = "openai"

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None,
        **kwargs,
    ):
        class _Usage:
            prompt_tokens = 180
            completion_tokens = 45
            reasoning_tokens = 0
            total_tokens = 225

        class _Response:
            content = (
                "Daily event counts are trending up at ~1 event/day with "
                "mild variance.  The seven-day-ahead forecast is consistent "
                "with the observed mean; expect similar volume."
            )
            tool_calls: list = []
            usage = _Usage()
            finish_reason = "stop"
            raw_response = None

        return _Response()


async def _seed_predictor_signals(
    pool, *, target_id: str, target_version: str, days: int = 14,
) -> list[UUID]:
    """Write ``days`` synthetic signal rows spaced one per day.

    Each row carries ``produced_by = target_id``, a sentiment value,
    and a unique title/url so the predictor can aggregate them into
    daily buckets.  Returns the inserted row uuids in insertion order.

    We INSERT directly with a per-row ``produced_at`` because the predictor
    needs the full 14-day window present in the column (a now()-clamped
    write path could not spread the rows across history).
    """
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4 as _uuid4
    import json as _json
    from legba.data.provenance.kinds import OutputKind as _OK, spec_for_kind as _sf

    spec = _sf(_OK.SIGNAL)
    schema_uri = spec.schema_uri

    ids: list[UUID] = []
    now = datetime.now(tz=timezone.utc)
    async with pool.acquire() as conn:
        for d in range(days):
            day = now - timedelta(days=days - 1 - d)
            new_id = _uuid4()
            data = {
                "summary": f"Synthetic signal for day -{days - 1 - d}",
                "sentiment": 0.1 * (d - days / 2),
                "descriptor_source_id": "synthetic",
            }
            await conn.execute(
                """
                INSERT INTO signals (
                    id, data, title, source_id, source_url, guid, category,
                    event_timestamp, language, confidence,
                    classification_scores,
                    target_id, target_version, analyst_id, analyst_version,
                    produced_at, derived_from, schema_uri, run_id
                ) VALUES (
                    $1, $2::jsonb, $3, NULL, $4, $5, '',
                    NULL, 'en', 0.5, NULL,
                    $6, $7, NULL, NULL,
                    $8, '{}'::uuid[], $9, NULL
                )
                """,
                new_id,
                _json.dumps(data),
                f"Brazil Energy Synthetic Signal day -{days - 1 - d}",
                f"https://example.invalid/synthetic/{d}",
                f"predictor-synth-{target_id}-{d}",
                target_id,
                target_version,
                day,
                schema_uri,
            )
            ids.append(new_id)
    return ids


@pytest.mark.asyncio
async def test_gate8_consult_on_demand_through_daprd(
    pg_pool,
    pg_store: PostgresStore,
    nats_store: NatsStore,
    descriptor_registry: DescriptorRegistry,
    stack_registry: StackRegistry,
    vault: CredentialVault,
    dapr_host_app,
    dapr_actor_session_prefix: str,
) -> None:
    """Register a consult_on_demand analyst, invoke it via ActorProxy with
    a question + scope predicate, assert a ConsultResponsePayload lands
    in ``analyst_outputs.data`` and the lineage walks back to the
    cited substrate refs."""
    actor = "phase5a_dapr_test"

    # ---- Register stack components, target, analyst ------------------
    for component in _build_stack_components():
        body = component.model_dump(mode="json", by_alias=True)
        with suppress(Exception):
            await stack_registry.register(body, actor)

    target = _build_target_descriptor()
    with suppress(Exception):
        await descriptor_registry.register(target, actor=actor)

    consult_descriptor = _build_consult_analyst_descriptor()
    with suppress(Exception):
        await descriptor_registry.register(consult_descriptor, actor=actor)

    target_descriptor = await descriptor_registry.get_typed(
        target.identity.id, family=Family.TARGET,
    )
    analyst_descriptor = await descriptor_registry.get_typed(
        consult_descriptor.identity.id, family=Family.ANALYST,
    )

    # ---- Seed one substrate signal so the cited ref is real ----------
    # The legacy target-owned writer (write_target_signal) was retired with
    # L-205; SourceActor now writes canonical, target-agnostic signals. The
    # consult lineage only needs a real signal row to cite back to, so we
    # INSERT one directly into the canonical post-pivot `signals` columns
    # (mirrors _seed_predictor_signals above).
    from datetime import datetime as _dt, timezone as _tz
    from uuid import uuid4 as _uuid4
    import json as _json
    from legba.data.provenance.kinds import OutputKind as _OK, spec_for_kind as _sf

    _spec = _sf(_OK.SIGNAL)
    seed_signal_id: UUID = _uuid4()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO signals (
                id, data, title, source_id, source_url, guid, category,
                event_timestamp, language, confidence, classification_scores,
                target_id, target_version, analyst_id, analyst_version,
                produced_at, derived_from, schema_uri, run_id
            ) VALUES (
                $1, $2::jsonb, $3, NULL, $4, $5, 'other',
                NULL, 'en', 0.5, NULL,
                $6, $7, NULL, NULL,
                $8, '{}'::uuid[], $9, NULL
            )
            """,
            seed_signal_id,
            _json.dumps({"summary": "Itaipu turbine upgrade complete",
                         "descriptor_source_id": "epe_rss_consult"}),
            "Itaipu hydro plant upgrade complete",
            "https://www.epe.gov.br/news/itaipu-upgrade-2026",
            "epe-itaipu-consult-2026",
            target_descriptor.identity.id,
            target_descriptor.identity.version,
            _dt.now(tz=_tz.utc),
            _spec.schema_uri,
        )

    # ---- Wire deps + ActorProxy ---------------------------------------
    from legba.data.analysts.consult_on_demand import (
        ConsultOnDemandDeps,
        run_method as consult_run_method,
    )

    deps = StandardDeps(
        pg_pool=pg_pool,
        nats_publish=nats_store.publish_json,
        secrets_resolve=vault.resolve,
    )

    # Wave B prereq #3: session-prefixed actor_id keeps dapr_state
    # disjoint across concurrent / re-run sessions.
    analyst_actor_id = (
        f"analyst::{dapr_actor_session_prefix}::"
        f"{analyst_descriptor.identity.id}::"
        f"{analyst_descriptor.identity.version[:8]}"
    )

    # Clear stale dapr_state for this actor so the lifecycle starts fresh.
    import asyncpg as _asyncpg
    _prod = await _asyncpg.connect(
        host="127.0.0.1", port=5432, user="legba", password="legba",
        database="legba",
    )
    try:
        await _prod.execute(
            "DELETE FROM dapr_state WHERE key LIKE '%' || $1 || '%'",
            analyst_actor_id,
        )
    finally:
        await _prod.close()

    consult_llm = _ConsultLLMHandler(seed_signal_id)
    consult_substrate = _StubSubstrate(seed_signal_id)
    kind_deps = ConsultOnDemandDeps(
        llm=consult_llm,
        substrate=consult_substrate,
        max_tokens=1024,
        temperature=0.2,
        max_rounds=6,
    )

    dapr_actors.register_analyst_deps(
        analyst_actor_id,
        dapr_actors._AnalystDeps(
            descriptor=analyst_descriptor,
            deps=deps,
            run_method=consult_run_method,
            kind_deps=kind_deps,
            output_kind=dapr_actors.OutputKind.FINDING,
            read_slice=None,
            budget=None,
        ),
    )

    consult_proxy = ActorProxy.create(
        "AnalystActor", ActorId(analyst_actor_id), AnalystActorInterface,
    )

    # Activate then invoke with an inputs override carrying the question.
    activated = await _await_actor_call(consult_proxy, "activate", None)
    assert activated.get("lifecycle") == "active", activated

    run_result = await _await_actor_call(
        consult_proxy, "run",
        {
            "trigger_kind": "method",
            "inputs": [{
                "question": "What's the latest with Brazil's Itaipu plant?",
                "scope_predicate": "target_id() == 'india_energy_infra_dapr'",
            }],
        },
    )
    assert run_result.get("outcome") == "success", (
        f"consult_on_demand run failed: {run_result}"
    )
    output_id = UUID(run_result.get("output_id") or run_result["finding_id"])

    # ---- Assert ConsultResponsePayload landed in analyst_outputs.data
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, kind, title, body, data, derived_from "
            "FROM analyst_outputs WHERE id = $1",
            output_id,
        )
    assert row is not None, "consult output row missing"
    assert row["kind"] == "finding"

    # Some Postgres asyncpg installs return JSONB as str; normalize.
    data_blob = row["data"]
    if isinstance(data_blob, str):
        data_blob = json.loads(data_blob)
    # The finding's payload model dump puts the FindingPayload's own
    # ``data`` under ``data->'data'`` (writes._insert_analyst_output
    # stores model_dump(mode='json')).
    nested = data_blob.get("data") if isinstance(data_blob, dict) else None
    if nested is None:
        nested = data_blob
    consult_resp = (nested or {}).get("consult_response")
    if not consult_resp and isinstance(nested, dict):
        # Some encoders preserve top-level vs nested differently; check
        # the outer blob too before giving up.
        consult_resp = data_blob.get("consult_response")
    assert consult_resp is not None, (
        f"consult_response not found in analyst_outputs.data: {data_blob!r}"
    )
    cited = consult_resp.get("cited_substrate_refs") or []
    assert str(seed_signal_id) in [str(c) for c in cited], (
        f"seed signal {seed_signal_id} not in cited_substrate_refs: {cited}"
    )

    # ---- Lineage walk: finding -> seed signal -------------------------
    async with pg_pool.acquire() as conn:
        walked_ids = await _walk_finding_to_signal_ids(conn, output_id)
    # consult_on_demand's derived_from carries the cited refs — lineage
    # walk should surface the seed signal as a contributing row.
    assert seed_signal_id in walked_ids, (
        f"lineage walk did not return seed signal: walked={walked_ids}"
    )


@pytest.mark.asyncio
async def test_gate8b_consult_chat_mode_writes_no_finding(
    pg_pool,
    pg_store: PostgresStore,
    nats_store: NatsStore,
    descriptor_registry: DescriptorRegistry,
    stack_registry: StackRegistry,
    vault: CredentialVault,
    dapr_host_app,
    dapr_actor_session_prefix: str,
) -> None:
    """Piece 1 (T5): a consult run invoked with ``mode='chat'`` returns its
    ConsultResponsePayload IN the envelope and writes NO ``analyst_outputs``
    row. ``mode='deep'`` (gate8) is the persist path; this is the guard.

    Budget metering is exercised by the kind unit tests (the actor records
    budget ABOVE the chat guard); here we focus on the no-write contract +
    the envelope shape, which is the new behavior."""
    actor = "phase5a_dapr_test"

    for component in _build_stack_components():
        body = component.model_dump(mode="json", by_alias=True)
        with suppress(Exception):
            await stack_registry.register(body, actor)

    target = _build_target_descriptor()
    with suppress(Exception):
        await descriptor_registry.register(target, actor=actor)

    consult_descriptor = _build_consult_analyst_descriptor()
    with suppress(Exception):
        await descriptor_registry.register(consult_descriptor, actor=actor)

    analyst_descriptor = await descriptor_registry.get_typed(
        consult_descriptor.identity.id, family=Family.ANALYST,
    )

    from uuid import uuid4 as _uuid4
    from legba.data.analysts.consult_on_demand import (
        ConsultOnDemandDeps,
        run_method as consult_run_method,
    )

    deps = StandardDeps(
        pg_pool=pg_pool,
        nats_publish=nats_store.publish_json,
        secrets_resolve=vault.resolve,
    )

    # Distinct session-scoped actor id so this run's dapr_state is isolated
    # from gate8's.
    analyst_actor_id = (
        f"analyst::{dapr_actor_session_prefix}::chat::"
        f"{analyst_descriptor.identity.id}::"
        f"{analyst_descriptor.identity.version[:8]}"
    )
    import asyncpg as _asyncpg
    _prod = await _asyncpg.connect(
        host="127.0.0.1", port=5432, user="legba", password="legba",
        database="legba",
    )
    try:
        await _prod.execute(
            "DELETE FROM dapr_state WHERE key LIKE '%' || $1 || '%'",
            analyst_actor_id,
        )
    finally:
        await _prod.close()

    seed_signal_id = _uuid4()
    kind_deps = ConsultOnDemandDeps(
        llm=_ConsultLLMHandler(seed_signal_id),
        substrate=_StubSubstrate(seed_signal_id),
        max_tokens=1024,
        temperature=0.2,
        max_rounds=6,
    )
    dapr_actors.register_analyst_deps(
        analyst_actor_id,
        dapr_actors._AnalystDeps(
            descriptor=analyst_descriptor,
            deps=deps,
            run_method=consult_run_method,
            kind_deps=kind_deps,
            output_kind=dapr_actors.OutputKind.FINDING,
            read_slice=None,
            budget=None,
        ),
    )

    consult_proxy = ActorProxy.create(
        "AnalystActor", ActorId(analyst_actor_id), AnalystActorInterface,
    )
    activated = await _await_actor_call(consult_proxy, "activate", None)
    assert activated.get("lifecycle") == "active", activated

    async with pg_pool.acquire() as conn:
        before = await conn.fetchval(
            "SELECT count(*) FROM analyst_outputs WHERE analyst_id = $1",
            analyst_descriptor.identity.id,
        )

    run_result = await _await_actor_call(
        consult_proxy, "run",
        {
            "trigger_kind": "method",
            "inputs": [{
                "question": "What's the latest with Brazil's Itaipu plant?",
                "mode": "chat",
                "request_id": _uuid4().hex,
            }],
        },
    )
    assert run_result.get("outcome") == "success", run_result
    # Chat envelope: mode=chat, the typed response present, NO finding id.
    assert run_result.get("mode") == "chat", run_result
    cr = run_result.get("consult_response")
    assert isinstance(cr, dict) and cr.get("answer"), run_result
    assert "finding_id" not in run_result

    # No row written.
    async with pg_pool.acquire() as conn:
        after = await conn.fetchval(
            "SELECT count(*) FROM analyst_outputs WHERE analyst_id = $1",
            analyst_descriptor.identity.id,
        )
    assert after == before, (
        f"chat mode must not write a row: before={before} after={after}"
    )


@pytest.mark.asyncio
async def test_gate9_predictor_writes_prediction_kind_through_daprd(
    pg_pool,
    pg_store: PostgresStore,
    nats_store: NatsStore,
    descriptor_registry: DescriptorRegistry,
    stack_registry: StackRegistry,
    vault: CredentialVault,
    dapr_host_app,
    dapr_actor_session_prefix: str,
) -> None:
    """Register a predictor analyst, seed 14d of synthetic signals,
    invoke via ActorProxy, assert the row lands in the predictions table
    (the dispatch route for OutputKind.PREDICTION) with provenance."""
    actor = "phase5a_dapr_test"

    # ---- Register stack components + target + predictor descriptor ----
    for component in _build_stack_components():
        body = component.model_dump(mode="json", by_alias=True)
        with suppress(Exception):
            await stack_registry.register(body, actor)

    target = _build_target_descriptor()
    with suppress(Exception):
        await descriptor_registry.register(target, actor=actor)

    predictor_descriptor = _build_predictor_analyst_descriptor(
        target_id=target.identity.id,
    )
    with suppress(Exception):
        await descriptor_registry.register(predictor_descriptor, actor=actor)

    target_descriptor = await descriptor_registry.get_typed(
        target.identity.id, family=Family.TARGET,
    )
    analyst_descriptor = await descriptor_registry.get_typed(
        predictor_descriptor.identity.id, family=Family.ANALYST,
    )

    # ---- Seed 14 days of synthetic signals ---------------------------
    seeded_ids = await _seed_predictor_signals(
        pg_pool,
        target_id=target_descriptor.identity.id,
        target_version=target_descriptor.identity.version,
        days=14,
    )
    assert len(seeded_ids) == 14

    # ---- Build dep wiring + ActorProxy --------------------------------
    from legba.data.analysts.predictor import (
        PredictorDeps,
        run_method as predictor_run_method,
    )

    deps = StandardDeps(
        pg_pool=pg_pool,
        nats_publish=nats_store.publish_json,
        secrets_resolve=vault.resolve,
    )

    # Wave B prereq #3: session-prefixed actor_id keeps dapr_state
    # disjoint across concurrent / re-run sessions.
    analyst_actor_id = (
        f"analyst::{dapr_actor_session_prefix}::"
        f"{analyst_descriptor.identity.id}::"
        f"{analyst_descriptor.identity.version[:8]}"
    )
    import asyncpg as _asyncpg
    _prod = await _asyncpg.connect(
        host="127.0.0.1", port=5432, user="legba", password="legba",
        database="legba",
    )
    try:
        await _prod.execute(
            "DELETE FROM dapr_state WHERE key LIKE '%' || $1 || '%'",
            analyst_actor_id,
        )
    finally:
        await _prod.close()

    kind_deps = PredictorDeps(llm=_PredictorLLMHandler())

    dapr_actors.register_analyst_deps(
        analyst_actor_id,
        dapr_actors._AnalystDeps(
            descriptor=analyst_descriptor,
            deps=deps,
            run_method=predictor_run_method,
            kind_deps=kind_deps,
            output_kind=dapr_actors.OutputKind.PREDICTION,
            read_slice=None,  # use default signals reader
            budget=None,
        ),
    )

    pred_proxy = ActorProxy.create(
        "AnalystActor", ActorId(analyst_actor_id), AnalystActorInterface,
    )

    activated = await _await_actor_call(pred_proxy, "activate", None)
    assert activated.get("lifecycle") == "active", activated

    run_result = await _await_actor_call(
        pred_proxy, "run",
        {"trigger_kind": "method", "target_filter": target_descriptor.identity.id},
    )
    assert run_result.get("outcome") == "success", (
        f"predictor run failed: {run_result}"
    )
    assert run_result.get("kind") == "prediction", (
        f"predictor wrote unexpected kind: {run_result}"
    )
    pred_id = UUID(run_result.get("output_id") or run_result["finding_id"])

    # ---- Assert PREDICTION row lands in `predictions` table ---------
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, hypothesis, category, region, confidence, "
            "       target_id, analyst_id, derived_from, schema_uri "
            "FROM predictions WHERE id = $1",
            pred_id,
        )
    assert row is not None, (
        f"prediction row {pred_id} not in predictions table — "
        "OutputKind.PREDICTION dispatch broken"
    )
    assert row["category"] == "event_count_forecast"
    assert row["region"] == target_descriptor.identity.id
    assert row["target_id"] == target_descriptor.identity.id
    assert row["analyst_id"] == analyst_descriptor.identity.id
    assert "iglu:legba/prediction/jsonschema" in (row["schema_uri"] or "")

    # Provenance: derived_from should contain at least one seeded signal.
    derived = [UUID(str(u)) for u in (row["derived_from"] or [])]
    assert derived, "prediction has empty derived_from"
    seeded_set = set(seeded_ids)
    overlap = set(derived) & seeded_set
    assert overlap, (
        f"prediction derived_from did not intersect seeded signals: "
        f"derived={derived[:3]}... seeded[:3]={list(seeded_set)[:3]}"
    )


# ---------------------------------------------------------------------------
# Gate 10 — L-176 optimizer kind dispatches a Temporal workflow + writes a
# PROMPT_MODULE_CANDIDATE row.  Gated on LEGBA_TEST_TEMPORAL=1 because the
# default test run doesn't bring up `docker compose --profile temporal`.
# ---------------------------------------------------------------------------


def _build_optimizer_analyst_descriptor(
    *, analyzed_analyst_id: str,
) -> AnalystDescriptor:
    """L-176 optimizer descriptor — kind ``dspy_compile``, OPTIMIZER analyst.

    Carries an :class:`EvalBlock` with the ``optimizer.analyzed_analyst_id``
    pointer the kind's ``READ_SLICE`` adapter consults.  Per the L-176
    schema constraint (``AnalystDescriptor._kind_constraints``), the
    optimizer must declare an eval block — and ``dspy_compile`` is
    exempt from the ``prompt_module`` requirement.
    """
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id="optimizer_for_brazil_dapr",
            name="Optimizer for Brazil Energy (dapr-test)",
            schema_uri="legba/analyst/1.0.0",
            version=_placeholder_version(),
            kind=AnalystKind.OPTIMIZER,
            type_signature=TypeSignature(
                input_type="legba.runtime.TraceCritiqueRows",
                output_type="legba.runtime.PromptModuleCandidate",
            ),
            state=LifecycleState.ACTIVE,
            owner="phase5a_dapr_test",
        ),
        subscription=SubscriptionBlock(
            # Subscription has no targets/other_analysts — the optimizer's
            # input is the trace+critique join, surfaced via the kind's
            # READ_SLICE.  The substrate hint is direct_queries=True so the
            # runtime knows the kind reads via its own helper.
            substrate={"direct_queries": True},
        ),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="dspy_compile",
            # Wave B prereq: dspy_compile is exempt from prompt_module — the
            # optimizer COMPILES prompts, doesn't have a static one.
            prompt_module=None,
            llm={
                "primary": Property.StackRef(
                    raw="llm.primary.openai_compat",
                    expected_family="llm_provider",
                ).model_dump(),
                "max_tokens": 2048,
            },
            budget_tokens_per_day=200_000,
            timeout_seconds=3600,
        ),
        cadence=CadenceBlock(
            # Weekly cadence — optimizer is expensive; don't fire on every
            # cycle.  This fixture exercises a manual run via ActorProxy
            # so the schedule itself never fires.
            fallback_schedule="0 3 * * 0",  # 03:00 every Sunday
            cooldown_seconds=86400,
        ),
        outputs=[
            OutputBinding(
                kind="substrate_writer",
                config={"output_kind": "prompt_module_candidate"},
            ),
        ],
        eval=EvalBlock(
            optimizer={
                "analyzed_analyst_id": analyzed_analyst_id,
                "max_generations": 2,
                "auto_mode": "light",
                "min_traces_required": 1,
            },
        ),
    )


async def _seed_traces_and_critiques(
    pg_pool,
    *,
    analyzed_analyst_id: str,
    analyzed_analyst_version: str,
    n_rows: int = 5,
) -> list[UUID]:
    """Insert synthetic trace + critique rows for the optimizer to chew on.

    Returns the trace ``run_id`` list so the test can assert
    derived_from intersection on the resulting candidate row.
    """
    import hashlib
    seeded: list[UUID] = []
    async with pg_pool.acquire() as conn:
        for i in range(n_rows):
            run_id = uuid4()
            prompt_text = (
                f"Analyze Brazil energy signals from the last week — run {i}."
            )
            output_text = f"Finding {i}: Itaipu upgrade was the major event."
            receipt_hash = hashlib.sha256(f"r{i}".encode()).hexdigest()
            await conn.execute(
                """
                INSERT INTO analyst_traces (
                    run_id, analyst_id, analyst_version, cadence_trigger,
                    prompt_rendered, output_payload, status,
                    run_started_at, run_ended_at, receipt_hash,
                    prev_receipt_hash, schema_uri
                )
                VALUES (
                    $1, $2, $3, 'manual',
                    $4, $5, 'success',
                    NOW() - make_interval(hours => $6),
                    NOW() - make_interval(hours => $6, mins => 1),
                    $7, NULL,
                    'iglu:legba/analyst_trace/jsonschema/1-0-0'
                )
                """,
                run_id, analyzed_analyst_id, analyzed_analyst_version,
                prompt_text, output_text, i + 1, receipt_hash,
            )
            await conn.execute(
                """
                INSERT INTO analyst_critiques (
                    trace_id, judge_analyst_id, judge_analyst_version,
                    rubric_uri, scores, overall_score
                )
                VALUES (
                    $1, 'critic.brazil_dapr', 'v0',
                    'iglu:legba/rubric/jsonschema/1-0-0',
                    $2::jsonb, $3
                )
                """,
                run_id,
                '{"helpfulness": ' + str(0.5 + (i % 3) * 0.05) + '}',
                0.5 + (i % 3) * 0.05,
            )
            seeded.append(run_id)
    return seeded


@pytest.mark.skipif(
    os.environ.get("LEGBA_TEST_TEMPORAL") != "1",
    reason=(
        "L-176 optimizer gate requires Temporal — set LEGBA_TEST_TEMPORAL=1 "
        "and bring up `docker compose --profile temporal up -d` first"
    ),
)
@pytest.mark.asyncio
async def test_gate10_optimizer_dispatches_temporal_workflow(
    pg_pool,
    pg_store: PostgresStore,
    nats_store: NatsStore,
    descriptor_registry: DescriptorRegistry,
    stack_registry: StackRegistry,
    vault: CredentialVault,
    dapr_host_app,
    dapr_actor_session_prefix: str,
) -> None:
    """Register an optimizer analyst, seed trace+critique rows, dispatch via
    ActorProxy, assert the candidate row lands in analyst_outputs with
    ``kind='prompt_module_candidate'`` and Temporal workflow id stamped.

    Per L-176 brief — this is the spike-style end-to-end that confirms:

      * the optimizer kind registers + activates as a Dapr actor,
      * its run path dispatches a Temporal workflow,
      * the workflow's result lands as a PROMPT_MODULE_CANDIDATE row
        in ``analyst_outputs`` with the new OutputKind dispatch path,
      * derived_from carries the trace + critique UUIDs,
      * the ``temporal_workflow_id`` field is populated on the row
        (proves the workflow was actually dispatched + completed).
    """
    actor = "phase5a_dapr_test"
    analyzed_analyst_id = "india_energy_for_optimizer"
    analyzed_analyst_version = "v" + "0" * 16

    # ---- Register stack components + descriptors -----------------------
    for component in _build_stack_components():
        body = component.model_dump(mode="json", by_alias=True)
        with suppress(Exception):
            await stack_registry.register(body, actor)

    optimizer_descriptor = _build_optimizer_analyst_descriptor(
        analyzed_analyst_id=analyzed_analyst_id,
    )
    with suppress(Exception):
        await descriptor_registry.register(optimizer_descriptor, actor=actor)

    analyst_descriptor = await descriptor_registry.get_typed(
        optimizer_descriptor.identity.id, family=Family.ANALYST,
    )

    # ---- Seed trace + critique rows for the analyzed analyst ----------
    seeded_run_ids = await _seed_traces_and_critiques(
        pg_pool,
        analyzed_analyst_id=analyzed_analyst_id,
        analyzed_analyst_version=analyzed_analyst_version,
        n_rows=5,
    )
    assert len(seeded_run_ids) == 5

    # ---- Build dep wiring + ActorProxy --------------------------------
    from legba.data.analysts.optimizer import (
        OUTPUT_KIND as OPTIMIZER_OUTPUT_KIND,
        OptimizerDeps,
        READ_SLICE as optimizer_read_slice,
        run_method as optimizer_run_method,
    )
    from legba.runtime.dapr_workflow.gepa import build_default_client

    deps = StandardDeps(
        pg_pool=pg_pool,
        nats_publish=nats_store.publish_json,
        secrets_resolve=vault.resolve,
    )

    analyst_actor_id = (
        f"analyst::{dapr_actor_session_prefix}::"
        f"{analyst_descriptor.identity.id}::"
        f"{analyst_descriptor.identity.version[:8]}"
    )

    # Clear any leftover Dapr state for this actor_id.
    import asyncpg as _asyncpg
    _prod = await _asyncpg.connect(
        host="127.0.0.1", port=5432, user="legba", password="legba",
        database="legba",
    )
    try:
        await _prod.execute(
            "DELETE FROM dapr_state WHERE key LIKE '%' || $1 || '%'",
            analyst_actor_id,
        )
    finally:
        await _prod.close()

    # Use the in-process workflow client (build_default_client) — the
    # GEPA loop runs synchronously in this process, so the test
    # validates the full kind→write path without a workflow sidecar.
    kind_deps = OptimizerDeps(
        temporal_client=build_default_client(),
        max_generations=1,            # one mutation for test latency
        min_traces_required=1,
    )

    dapr_actors.register_analyst_deps(
        analyst_actor_id,
        dapr_actors._AnalystDeps(
            descriptor=analyst_descriptor,
            deps=deps,
            run_method=optimizer_run_method,
            kind_deps=kind_deps,
            output_kind=OPTIMIZER_OUTPUT_KIND,
            read_slice=optimizer_read_slice,
            budget=None,
        ),
    )

    opt_proxy = ActorProxy.create(
        "AnalystActor", ActorId(analyst_actor_id), AnalystActorInterface,
    )
    activated = await _await_actor_call(opt_proxy, "activate", None)
    assert activated.get("lifecycle") == "active", activated

    run_result = await _await_actor_call(
        opt_proxy, "run",
        {
            "trigger_kind": "method",
            "options": {
                "analyzed_analyst_id": analyzed_analyst_id,
                "analyzed_analyst_version": analyzed_analyst_version,
                "parent_prompt_module_path": "legba.prompts.inline_target.v1",
                "promotion_policy": "human_gated",
            },
        },
    )
    assert run_result.get("outcome") == "success", (
        f"optimizer run failed: {run_result}"
    )
    assert run_result.get("kind") == "prompt_module_candidate", (
        f"optimizer wrote unexpected kind: {run_result}"
    )
    output_id = UUID(run_result.get("output_id") or run_result["finding_id"])

    # ---- Assert PROMPT_MODULE_CANDIDATE row lands in `analyst_outputs` -
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, kind, data, analyst_id, derived_from, schema_uri "
            "FROM analyst_outputs WHERE id = $1",
            output_id,
        )
    assert row is not None, (
        f"prompt_module_candidate row {output_id} not in analyst_outputs"
    )
    assert row["kind"] == "prompt_module_candidate"
    assert "prompt_module_candidate" in (row["schema_uri"] or "")
    assert row["analyst_id"] == analyst_descriptor.identity.id

    # Data blob carries the candidate's load-bearing fields.
    data = row["data"]
    if isinstance(data, str):
        data = json.loads(data)
    assert data.get("analyst_id") == analyzed_analyst_id
    assert data.get("parent_prompt_module_path") == "legba.prompts.inline_target.v1"
    assert data.get("temporal_workflow_id"), (
        f"temporal_workflow_id missing from candidate row: {data!r}"
    )
    assert data.get("candidate_prompt_module_text")

    # Derived_from carries the trace UUIDs (critique UUIDs too, but the
    # trace set is the load-bearing subset for this assertion).
    derived = {UUID(str(u)) for u in (row["derived_from"] or [])}
    seeded_set = set(seeded_run_ids)
    overlap = derived & seeded_set
    assert overlap, (
        f"prompt_module_candidate derived_from did not intersect seeded "
        f"trace_ids: derived={list(derived)[:3]} seeded={list(seeded_set)[:3]}"
    )


# ---------------------------------------------------------------------------
# Gate 11 — L-175 critic kind grades a finding through daprd
# ---------------------------------------------------------------------------


def _build_analyst_with_rubric_descriptor(*, target_id: str) -> AnalystDescriptor:
    """Inline-target analyst that carries an ``eval.rubric`` block — the
    fixture finding is seeded under this analyst, and the critic resolves
    the rubric + allow_self_correlated flag from this descriptor at
    activation time.
    """
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id="india_energy_inline_for_critic_dapr",
            name="Brazil Energy Inline Analyst — critic-gradable (dapr-test)",
            schema_uri="legba/analyst/1.0.0",
            version=_placeholder_version(),
            kind=AnalystKind.INLINE_TARGET,
            type_signature=TypeSignature(
                input_type="legba.runtime.SignalList",
                output_type="legba.runtime.Finding",
            ),
            state=LifecycleState.ACTIVE,
            owner="phase5a_dapr_test",
        ),
        subscription=SubscriptionBlock(
            targets=SubscriptionTargets(
                predicate=f'target_id() == "{target_id}"',
                data_types=["signal"],
                time_window="24h",
            ),
        ),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="llm_single_turn",
            prompt_module="legba.prompts.inline_target.v1",
            llm={
                "primary": Property.StackRef(
                    raw="llm.primary.openai_compat",
                    expected_family="llm_provider",
                ).model_dump(),
                "max_tokens": 1024,
            },
            budget_tokens_per_day=200_000,
        ),
        cadence=CadenceBlock(
            fallback_schedule="*/10 * * * *",
            cooldown_seconds=300,
        ),
        outputs=[
            OutputBinding(
                kind="a2a_skill",
                config={
                    "skill_id": "intelligence.india_energy_for_critic_dapr",
                },
            ),
        ],
        # Rubric the critic grades against.  L-105 §3 — the rubric is the
        # contract between the analyzed analyst and the eval loop; without
        # it the critic raises MissingRubricError.
        eval=EvalBlock(
            rubric=(
                '{"dimensions": ['
                ' {"name": "evidence_quality", "weight": 0.4},'
                ' {"name": "actionability", "weight": 0.3},'
                ' {"name": "concision", "weight": 0.3}'
                "]}"
            ),
            allow_self_correlated=False,
        ),
    )


def _build_critic_analyst_descriptor() -> AnalystDescriptor:
    """L-175 critic descriptor — kind ``critic``, method.kind ``critic``."""
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id="critic_for_brazil_dapr",
            name="Critic for Brazil Energy (dapr-test)",
            schema_uri="legba/analyst/1.0.0",
            version=_placeholder_version(),
            kind=AnalystKind.CRITIC,
            type_signature=TypeSignature(
                input_type="legba.runtime.AnalystOutputRow",
                output_type="legba.runtime.Critique",
            ),
            state=LifecycleState.ACTIVE,
            owner="phase5a_dapr_test",
        ),
        subscription=SubscriptionBlock(
            # Critic doesn't pull from the substrate slice — it reads ONE
            # analyzed-output row via its own READ_SLICE adapter, keyed
            # off ``target_filter`` (the row UUID) or
            # ``options['analyzed_output_id']``.
            substrate={"direct_queries": True},
        ),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="critic",
            prompt_module="legba.prompts.critic.v1",
            llm={
                # Distinct from the analyzed analyst's stack ref so the
                # heterogeneity guard passes by construction (the analyzed
                # analyst uses ``llm.primary.openai_compat``).
                "primary": Property.StackRef(
                    raw="llm.anthropic.claude_sonnet_4_5",
                    expected_family="llm_provider",
                ).model_dump(),
                "max_tokens": 1536,
            },
            budget_tokens_per_day=50_000,
        ),
        cadence=CadenceBlock(
            # On-demand only — invoked via ActorProxy in this test.
            fallback_schedule="0 0 1 1 *",  # 1 Jan, effectively never
            cooldown_seconds=60,
        ),
        outputs=[
            # Critic primarily writes via the runtime's analyst-output
            # dispatcher (OUTPUT_KIND=CRITIQUE → analyst_outputs).  The
            # OutputBinding here is the FAN-OUT surface (publish the
            # critique on NATS too so downstream consumers — e.g. the
            # optimizer's read window — can stream them).
            OutputBinding(
                kind="nats_stream",
                config={"channel": "critiques"},
            ),
        ],
    )


class _CriticLLMHandler:
    """Canned-JSON LLM handler for the critic kind.

    Returns a strict-JSON critique with per-dimension scores, overall
    score, revision delta, and confidence — matching the schema the
    critic's ``_coerce_critique`` parser expects.
    """

    # Distinct from the analyzed analyst's ``openai`` subprovider so the
    # heterogeneity guard passes.
    subprovider = "anthropic"

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None,
        **kwargs,
    ):
        content = json.dumps({
            "scores": {
                "evidence_quality": 0.82,
                "actionability": 0.70,
                "concision": 0.91,
            },
            "overall_score": 0.81,
            "revision_delta": (
                "Add a one-sentence quantification of the upgrade's "
                "expected MW capacity uplift to strengthen actionability."
            ),
            "confidence": 0.88,
        })

        class _Usage:
            prompt_tokens = 420
            completion_tokens = 110
            reasoning_tokens = 0
            total_tokens = 530

        class _Response:
            tool_calls: list = []
            usage = _Usage()
            finish_reason = "stop"
            raw_response = None

        _Response.content = content
        return _Response()


@pytest.mark.asyncio
async def test_gate11_critic_grades_finding_through_daprd(
    pg_pool,
    pg_store: PostgresStore,
    nats_store: NatsStore,
    descriptor_registry: DescriptorRegistry,
    stack_registry: StackRegistry,
    vault: CredentialVault,
    dapr_host_app,
    dapr_actor_session_prefix: str,
) -> None:
    """Register a critic analyst, seed a finding under an analyst-with-rubric
    descriptor, invoke the critic via ActorProxy, assert a CritiquePayload
    lands in ``analyst_outputs`` with ``kind='critique'`` and lineage
    walks back to the analyzed finding's UUID.

    Pattern mirrors gate 8 (consult) + gate 9 (predictor): real daprd
    sidecar, ActorProxy invocation, substrate-side assertions.
    """
    actor = "phase5a_dapr_test"

    # ---- Register stack components + analyzed analyst + critic ----------
    for component in _build_stack_components():
        body = component.model_dump(mode="json", by_alias=True)
        with suppress(Exception):
            await stack_registry.register(body, actor)

    target = _build_target_descriptor()
    with suppress(Exception):
        await descriptor_registry.register(target, actor=actor)

    analyzed_descriptor = _build_analyst_with_rubric_descriptor(
        target_id=target.identity.id,
    )
    with suppress(Exception):
        await descriptor_registry.register(analyzed_descriptor, actor=actor)

    critic_descriptor = _build_critic_analyst_descriptor()
    with suppress(Exception):
        await descriptor_registry.register(critic_descriptor, actor=actor)

    analyzed_descriptor = await descriptor_registry.get_typed(
        analyzed_descriptor.identity.id, family=Family.ANALYST,
    )
    critic_descriptor = await descriptor_registry.get_typed(
        critic_descriptor.identity.id, family=Family.ANALYST,
    )

    # ---- Seed a finding under the analyzed analyst ---------------------
    # Direct INSERT — we don't need the full pipeline here, just a row
    # whose id the critic can grade.  Uses the FindingPayload schema_uri
    # the dispatcher writes (per provenance.kinds).
    from legba.data.provenance.kinds import spec_for_kind, OutputKind as _OK
    from uuid import uuid4 as _uuid4

    finding_id = _uuid4()
    finding_run_id = _uuid4()
    finding_schema_uri = spec_for_kind(_OK.FINDING).schema_uri
    finding_data = {
        "title": "Itaipu hydro plant upgrade complete",
        "body": (
            "Brazil's Itaipu hydro plant completed its third-generation "
            "turbine upgrade on 19 May 2026, raising regional capacity."
        ),
        "confidence": 0.78,
        "tags": ["itaipu", "hydro", "upgrade"],
        "evidence": ["EPE press release 2026-05-19"],
        "data": {"source": "epe_rss", "category": "infrastructure"},
    }
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                target_id, target_version, analyst_id, analyst_version,
                produced_at, derived_from, schema_uri, run_id
            ) VALUES (
                $1, 'finding', $2, $3, $4, NULL, $5::jsonb,
                $6, $7, $8, $9,
                NOW(), '{}'::uuid[], $10, $11
            )
            """,
            finding_id,
            finding_data["title"],
            finding_data["body"],
            finding_data["confidence"],
            json.dumps(finding_data),
            target.identity.id,
            target.identity.version,
            analyzed_descriptor.identity.id,
            analyzed_descriptor.identity.version,
            finding_schema_uri,
            finding_run_id,
        )

    # ---- Build dep wiring + ActorProxy --------------------------------
    from legba.data.analysts.critic import (
        OUTPUT_KIND as CRITIC_OUTPUT_KIND,
        CriticDeps,
        READ_SLICE as critic_read_slice,
        run_method as critic_run_method,
    )

    deps = StandardDeps(
        pg_pool=pg_pool,
        nats_publish=nats_store.publish_json,
        secrets_resolve=vault.resolve,
    )

    analyst_actor_id = (
        f"analyst::{dapr_actor_session_prefix}::"
        f"{critic_descriptor.identity.id}::"
        f"{critic_descriptor.identity.version[:8]}"
    )
    import asyncpg as _asyncpg
    _prod = await _asyncpg.connect(
        host="127.0.0.1", port=5432, user="legba", password="legba",
        database="legba",
    )
    try:
        await _prod.execute(
            "DELETE FROM dapr_state WHERE key LIKE '%' || $1 || '%'",
            analyst_actor_id,
        )
    finally:
        await _prod.close()

    kind_deps = CriticDeps(llm=_CriticLLMHandler())

    dapr_actors.register_analyst_deps(
        analyst_actor_id,
        dapr_actors._AnalystDeps(
            descriptor=critic_descriptor,
            deps=deps,
            run_method=critic_run_method,
            kind_deps=kind_deps,
            output_kind=CRITIC_OUTPUT_KIND,
            read_slice=critic_read_slice,
            budget=None,
        ),
    )

    critic_proxy = ActorProxy.create(
        "AnalystActor", ActorId(analyst_actor_id), AnalystActorInterface,
    )

    activated = await _await_actor_call(critic_proxy, "activate", None)
    assert activated.get("lifecycle") == "active", activated

    # Invoke with the analyzed-output id passed via target_filter (the
    # critic READ_SLICE accepts a UUID string here) AND via options
    # (the runtime's _resolve_critic_context resolves rubric +
    # analyzed_model + allow_self_correlated from the analyzed analyst's
    # descriptor body).
    run_result = await _await_actor_call(
        critic_proxy, "run",
        {
            "trigger_kind": "method",
            "target_filter": str(finding_id),
            "options": {
                "analyzed_analyst_id": analyzed_descriptor.identity.id,
                "analyzed_output_id": str(finding_id),
            },
        },
    )
    assert run_result.get("outcome") == "success", (
        f"critic run failed: {run_result}"
    )
    assert run_result.get("kind") == "critique", (
        f"critic wrote unexpected kind: {run_result}"
    )
    critique_id = UUID(run_result.get("output_id") or run_result["finding_id"])

    # ---- Assert CRITIQUE row lands in `analyst_outputs` ---------------
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, kind, data, analyst_id, derived_from, schema_uri "
            "FROM analyst_outputs WHERE id = $1",
            critique_id,
        )
    assert row is not None, (
        f"critique row {critique_id} not in analyst_outputs — "
        "OutputKind.CRITIQUE dispatch broken"
    )
    assert row["kind"] == "critique"
    assert "critique" in (row["schema_uri"] or "").lower()
    assert row["analyst_id"] == critic_descriptor.identity.id

    # Data blob carries the critique's load-bearing fields.
    data = row["data"]
    if isinstance(data, str):
        data = json.loads(data)
    # Locate the critique blob — _select_output_payload routes via the
    # CRITIQUE selector; the row's data column carries the CritiquePayload
    # dump (overall_score, scores dict, revision_delta, analyzed_*).
    nested = data.get("data") if isinstance(data, dict) else None
    blob = data
    for candidate in (nested, data):
        if isinstance(candidate, dict) and "overall_score" in candidate:
            blob = candidate
            break
    assert "overall_score" in blob, (
        f"overall_score missing from critique data: {data!r}"
    )
    assert 0.0 <= float(blob["overall_score"]) <= 1.0
    assert isinstance(blob.get("scores"), dict)
    assert blob.get("analyzed_analyst_id") == analyzed_descriptor.identity.id, (
        f"analyzed_analyst_id missing/mismatched on critique row: {blob!r}"
    )

    # Lineage: derived_from should include the analyzed finding UUID.
    derived = {UUID(str(u)) for u in (row["derived_from"] or [])}
    assert finding_id in derived, (
        f"critique derived_from did not include analyzed finding "
        f"{finding_id}: derived={derived}"
    )
