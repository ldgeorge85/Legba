# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-1 — boot/wiring layer coverage (review §3.4).

Both live wiring bugs (the ``owner_tenant`` envelope stamp and the
``trigger_regs=0`` silent no-wire) shipped through 2300+ green tests because
NOTHING exercised the boot path end-to-end: descriptors in a REAL registry →
``bring_up_source_first_planes`` → subscription resolution → trigger
registrations → a published signal marking the right (analyst, target) pair
dirty. This module closes that hole with the REAL stack at every layer:

  * a fresh per-test migrated Postgres + the dev-rig NATS (no mocks),
  * real descriptors registered through :class:`DescriptorRegistry`,
  * the REAL registry HTTP API served by uvicorn on a localhost port —
    because ``_wire_targets_and_triggers`` lists families over real HTTP
    (``LEGBA_REGISTRY_API_URL``), an in-process ASGI transport would not
    exercise the production seam,
  * the production bring-up entry ``bring_up_source_first_planes`` with
    ``run_loops=False`` (planes + consumers + registrations, no loops).

The assertions are written so a regression where the wiring silently
produces ZERO trigger registrations (the exact live failure) turns this
test red:

  a. every subscribed (analyst, target) pair has a registration — counted
     pair-by-pair against the seeded descriptor topology, not ``>= 0``;
  b. each registration's coarse NATS subject filters equal the subject
     plan of its resolved source binding, and the trigger engine's durable
     consumer is REALLY bound to that union (asserted via consumer_info);
  c. a published matching signal marks the expected pairs dirty in the
     crash-safe ``trigger_state`` ledger (and a non-matching signal from
     the same source does not).

Isolation on the shared rig: unique descriptor ids per run, an injected
per-test job queue (unique stream/durable/subject space) and a unique
trigger-engine durable, so the bring-up can never touch the live
``LEGBA_JOBS`` topology or the live ``legba-trigger-engine`` consumer.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from contextlib import suppress
from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI
from nacl.signing import SigningKey

os.environ.setdefault("LEGBA_DATA_PG_HOST", "127.0.0.1")
os.environ.setdefault("LEGBA_DATA_PG_PORT", "5432")
os.environ.setdefault("LEGBA_DATA_PG_USER", "legba")
os.environ.setdefault("LEGBA_DATA_PG_PASSWORD", "legba")
os.environ.setdefault("LEGBA_DATA_NATS_URL", "nats://127.0.0.1:4222")

from legba.data.config import NatsConfig, PostgresConfig
from legba.data.migrate import apply_primary_migrations
from legba.data.nats import NatsStore, signal_subject_filter
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps, build_router
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache
from legba.data.schemas.analyst import (
    AnalystDescriptor,
    AnalystIdentity,
    CadenceBlock as AnalystCadenceBlock,
    MethodBlock,
    SubscriptionBlock,
    SubscriptionTargets,
    TypeSignature,
)
from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.properties import Cron
from legba.data.schemas.source import (
    CadenceBlock as SourceCadenceBlock,
    SourceDescriptor,
    SourceIdentity,
    SourceRef,
    SourceScope,
    Subscription,
)
from legba.data.schemas.target import (
    GeoScope,
    InlineAnalystBlock,
    TargetDescriptor,
    TargetIdentity,
)
from legba.data.sources._contract import Signal
from legba.runtime.deps import StandardDeps
from legba.runtime.jobs import JobQueue
from legba.runtime.registry_client import RegistryHTTPClient
from legba.runtime.source_actor import SourceCore, SourceDeps
from legba.runtime.source_first_runtime import bring_up_source_first_planes
from legba.runtime.subscription import target_consumer_name

ADMIN_DSN = "postgresql://legba:legba@127.0.0.1:5432/postgres"

# Registry app prerequisites (same fixed dev material as the registry API
# integration suite — placeholders, never real secrets).
_TEST_MASTER_KEY_HEX = (
    "0011223344556677889900112233445566778899001122334455667788990011"
)
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "22" * 32)


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Fixtures — fresh migrated DB, NATS, and the registry HTTP API over uvicorn
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_store():
    if not _port_open("127.0.0.1", 5432):
        pytest.skip("dev-rig Postgres not reachable on 127.0.0.1:5432")
    db_name = f"legba_bootwire_test_{uuid4().hex[:10]}"
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()

    cfg = PostgresConfig(
        host="127.0.0.1", port=5432, user="legba", password="legba",
        database=db_name,
    )
    applied = await apply_primary_migrations(cfg)
    assert applied, "expected migrations to apply"

    store = PostgresStore(cfg)
    await store.connect()
    try:
        yield store
    finally:
        await store.close()
        conn = await asyncpg.connect(ADMIN_DSN)
        try:
            await conn.execute(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname='{db_name}' AND pid <> pg_backend_pid()"
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            await conn.close()


@pytest_asyncio.fixture
async def nats_store():
    if not _port_open("127.0.0.1", 4222):
        pytest.skip("dev-rig NATS not reachable on 127.0.0.1:4222")
    store = NatsStore(NatsConfig.from_env())
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def registry(pg_store: PostgresStore, nats_store: NatsStore):
    """The full descriptor registry over the per-test DB (real validation)."""
    identity = SigningIdentity(
        signing_key=SigningKey(b"C-1-boot-wiring-test-seed-32byte"[:32]),
        signer_did="did:legba:registry:c1-boot-wiring",
    )
    reg = DescriptorRegistry(
        pg_store,
        nats_store=nats_store,
        vocabulary_cache=VocabularyCache(pg_store),
        signing_identity=identity,
        audit_logger=AuditLogger(identity=identity),
        dead_letter=DescriptorDeadLetter(pg_store),
    )
    await reg.start()
    try:
        yield reg
    finally:
        await reg.stop()


@pytest_asyncio.fixture
async def registry_http(
    pg_store: PostgresStore,
    nats_store: NatsStore,
    registry: DescriptorRegistry,
    monkeypatch: pytest.MonkeyPatch,
):
    """Serve the REAL registry API over uvicorn on a localhost port.

    Yields ``(base_url, RegistryHTTPClient)`` with ``LEGBA_REGISTRY_API_URL``
    pointed at it (dev-mode auth: token popped; the root conftest restores
    the env at test boundary).
    """
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    monkeypatch.setenv("LEGBA_DEV_MODE", "1")

    deps = RegistryAPIDeps(
        descriptor_registry=registry,
        stack_registry=StackRegistry(
            pg_store, CredentialVault(pg_store),
        ),
        vault=CredentialVault(pg_store),
        dlq=DescriptorDeadLetter(pg_store),
        audit_logger=AuditLogger(),
        vocabulary_cache=VocabularyCache(pg_store),
        nats_store=nats_store,
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_router(deps), prefix="/api/v1/registry")

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port,
            log_level="warning", log_config=None, access_log=False,
        )
    )
    task = asyncio.create_task(server.serve(), name="c1-registry-api")
    for _ in range(60):  # ~6s
        if server.started:
            break
        await asyncio.sleep(0.1)
    else:
        raise RuntimeError("registry API uvicorn did not start within 6s")

    base_url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("LEGBA_REGISTRY_API_URL", base_url)
    client = RegistryHTTPClient(base_url=base_url, token=None)
    try:
        yield base_url, client
    finally:
        await client.close()
        server.should_exit = True
        with suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=5)


# ---------------------------------------------------------------------------
# Descriptor builders (typed models — the registry re-validates on register)
# ---------------------------------------------------------------------------


def _source(sid: str, *, tags: list[str]) -> SourceDescriptor:
    return SourceDescriptor(
        identity=SourceIdentity(
            id=sid, name=sid, kind="rss",
            schema_uri="legba/source/1.0.0", version="0" * 16,
            state=LifecycleState.ACTIVE, owner="c1-boot-wiring",
            created=datetime.now(tz=timezone.utc),
        ),
        scope=SourceScope(owner_tenant="default", geo=["BR"], tags=tags),
        acquisition="poll",
        cadence=SourceCadenceBlock(schedule=Cron.of("*/30 * * * *")),
        subscription_policy="open",
    )


def _target(
    tid: str, *, source_id: str, scope_tag: str, analyst_ref: str,
) -> TargetDescriptor:
    return TargetDescriptor(
        identity=TargetIdentity(
            id=tid, name=tid,
            schema_uri="legba/target/2.0.0", version="0" * 16,
            state=LifecycleState.ACTIVE, owner="c1-boot-wiring",
            created=datetime.now(tz=timezone.utc),
        ),
        scope=GeoScope(
            domain="geo", geo=["BR"], languages=["pt"], tags=[scope_tag],
        ),
        sources=[
            SourceRef(
                source_id=source_id,
                subscription=Subscription(tags=["news"]),
            )
        ],
        # Binding path 1 (target → analyst): inline block naming a standalone
        # analyst via analyst_ref.
        analyst=InlineAnalystBlock(use="inline_target", analyst_ref=analyst_ref),
    )


def _analyst(aid: str, *, selector_predicate: str | None) -> AnalystDescriptor:
    """An LLM analyst; ``selector_predicate`` set → binding path 2
    (analyst → targets selector). ``None`` → no subscription.targets block
    (binds only if some target names it via analyst_ref)."""
    sub = SubscriptionBlock(
        targets=(
            SubscriptionTargets(predicate=selector_predicate)
            if selector_predicate is not None
            else None
        ),
    )
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id=aid, name=aid,
            schema_uri="legba/analyst/2.0.0", version="0" * 16,
            kind="inline_target",
            type_signature=TypeSignature(
                input_type="legba.x.In", output_type="legba.x.Out",
            ),
            state=LifecycleState.ACTIVE, owner="c1-boot-wiring",
        ),
        subscription=sub,
        method=MethodBlock(
            kind="llm_planner", prompt_module="legba.prompts.inline_target.v1",
        ),
        cadence=AnalystCadenceBlock(fallback_schedule="*/5 * * * *"),
    )


def _composition_analyst(aid: str, *, selector_predicate: str) -> AnalystDescriptor:
    """H4 — the shape of ``country_composition`` / ``region_composition``:
    ``subscription.targets`` selector-bound like path 2 above, but
    ``data_types=["finding"]`` — it reads OTHER analysts' heads
    (``meta_findings_synthesizer``), never a raw signal off the target's own
    ``sources:``. Otherwise identical to :func:`_analyst`'s selector shape so
    the ONLY variable between the two in the test below is ``data_types``.
    """
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id=aid, name=aid,
            schema_uri="legba/analyst/2.0.0", version="0" * 16,
            kind="meta_findings_synthesizer",
            type_signature=TypeSignature(
                input_type="legba.x.In", output_type="legba.x.Out",
            ),
            state=LifecycleState.ACTIVE, owner="c1-boot-wiring",
        ),
        subscription=SubscriptionBlock(
            targets=SubscriptionTargets(
                predicate=selector_predicate, data_types=["finding"],
            ),
        ),
        method=MethodBlock(
            kind="llm_planner",
            prompt_module="legba.prompts.meta_findings_synthesizer.v1",
        ),
        cadence=AnalystCadenceBlock(fallback_schedule="30 11,23 * * *"),
    )


# ---------------------------------------------------------------------------
# The boot-wiring test
# ---------------------------------------------------------------------------


async def test_boot_wiring_registers_triggers_and_marks_dirty(
    pg_store: PostgresStore,
    nats_store: NatsStore,
    registry: DescriptorRegistry,
    registry_http,
):
    run = uuid4().hex[:8]
    source_id = f"source.bootwire.{run}"  # SourceId allows dotted ids
    target_id = f"target_bootwire_{run}"  # TargetId is [a-z][a-z0-9_]*
    analyst_ref_id = f"analyst_bootwire_ref_{run}"
    analyst_sel_id = f"analyst_bootwire_sel_{run}"
    scope_tag = f"bootwire_{run}"
    tenant = "default"
    _base_url, registry_client = registry_http

    # ---- Seed real descriptors through the real registry ----------------
    await registry.register(_source(source_id, tags=["news"]), actor="c1")
    await registry.register(
        _analyst(analyst_ref_id, selector_predicate=None), actor="c1",
    )
    await registry.register(
        _analyst(analyst_sel_id, selector_predicate=f'has_tag("{scope_tag}")'),
        actor="c1",
    )
    await registry.register(
        _target(
            target_id, source_id=source_id, scope_tag=scope_tag,
            analyst_ref=analyst_ref_id,
        ),
        actor="c1",
    )

    # ---- Run the REAL boot wiring (no loops) -----------------------------
    # Isolated job plane + trigger durable so the shared rig's live
    # LEGBA_JOBS topology / legba-trigger-engine consumer are never touched.
    job_queue = JobQueue(
        nats_store,
        stream=f"LEGBA_JOBS_BOOTWIRE_{run.upper()}",
        durable=f"legba-job-workers-bootwire-{run}",
        subject_prefix=f"jobs_bootwire_{run}",
        max_age_seconds=600,
    )
    trigger_durable = f"legba-trigger-bootwire-{run}"

    handles = await bring_up_source_first_planes(
        pg_store=pg_store,
        nats_store=nats_store,
        standard_deps=object(),  # held by the source-deps resolver, not called
        registry_client=registry_client,
        run_loops=False,
        job_queue=job_queue,
        trigger_durable=trigger_durable,
    )
    engine = handles.trigger_engine
    try:
        # ---- (a) trigger registrations > 0 for EVERY subscribed pair ----
        # The exact live failure was trigger_regs=0 with everything green.
        expected_pairs = {
            (analyst_ref_id, target_id),  # path 1: target.analyst.analyst_ref
            (analyst_sel_id, target_id),  # path 2: subscription.targets selector
        }
        assert target_id in handles.registered_targets, (
            f"wiring did not register the seeded target: "
            f"registered={handles.registered_targets!r}"
        )
        regs = {
            (r.analyst_id, r.target_id): r
            for r in engine._regs
            if r.target_id == target_id
        }
        assert handles.trigger_registrations >= len(expected_pairs)
        for pair in expected_pairs:
            assert pair in regs, (
                f"no trigger registration for subscribed pair {pair}; "
                f"got {sorted(regs)} (trigger_regs="
                f"{handles.trigger_registrations})"
            )

        # ---- (b) registration subjects match the subscription binding ----
        expected_filter = signal_subject_filter(
            tenant=tenant, source_id=source_id,
            modality=None, event_class=None,
        )
        for pair in expected_pairs:
            reg = regs[pair]
            assert reg.subject_filters == [expected_filter], (
                f"{pair} bound the wrong subjects: {reg.subject_filters!r} "
                f"!= [{expected_filter!r}]"
            )
            assert [b.source_id for b in reg.bindings] == [source_id]
            assert all(b.owner_tenant == tenant for b in reg.bindings)
        # ...and the durable consumer is REALLY bound onto that union.
        info = await nats_store.js.consumer_info(engine._stream, trigger_durable)
        bound = list(
            getattr(info.config, "filter_subjects", None)
            or [getattr(info.config, "filter_subject", None)]
        )
        assert bound == [expected_filter], (
            f"trigger consumer bound {bound!r}, expected [{expected_filter!r}]"
        )

        # ---- (c) a published signal marks the pairs dirty ----------------
        await engine.bind()
        matching = Signal(
            source_id=source_id, owner_tenant=tenant, modality="text",
            tags=["news"], payload={"severity": "low"},
        )
        await handles.subscription_engine.publish_signal(signal=matching)
        # Non-matching control: same source (delivered on the coarse
        # subject) but the structured re-check must drop it.
        noise = Signal(
            source_id=source_id, owner_tenant=tenant, modality="text",
            tags=["sports"],
        )
        await handles.subscription_engine.publish_signal(signal=noise)

        for _ in range(10):
            await engine.drain_once()
            if engine.delivered >= 2:
                break
        assert engine.delivered >= 2, "both signals should be delivered"
        # The matching signal matched BOTH registrations; the noise neither.
        assert engine.matched == len(expected_pairs)
        # LLM analysts never fire per-signal (accumulation floored to 2).
        assert engine.fired == 0

        for analyst_id, tid in expected_pairs:
            acc = await handles.trigger_state.get(analyst_id, tid)
            assert acc.pending_count == 1, (
                f"({analyst_id}, {tid}) should be dirty with exactly the "
                f"matching signal counted, got pending={acc.pending_count}"
            )
            assert acc.first_dirty_at is not None
            assert acc.last_fired_at is None
            assert str(matching.signal_id) in acc.seen_signal_ids

        dirty = set(
            (a, t) for a, t, _tenant in await handles.trigger_state.list_dirty()
        )
        assert expected_pairs <= dirty
    finally:
        await handles.stop()
        # Rig hygiene: drop the per-test NATS artifacts.
        with suppress(Exception):
            await nats_store.js.delete_consumer(engine._stream, trigger_durable)
        with suppress(Exception):
            await nats_store.js.delete_consumer(
                engine._stream, target_consumer_name(target_id),
            )
        with suppress(Exception):
            await nats_store.js.delete_stream(job_queue.stream)


async def test_h4_finding_only_composition_registers_no_coalescing_trigger(
    pg_store: PostgresStore,
    nats_store: NatsStore,
    registry: DescriptorRegistry,
    registry_http,
):
    """H4 — THE SCHEDULING RACE, end to end through the REAL wiring pass.

    ``country_composition`` / ``region_composition`` read OTHER ANALYSTS'
    heads (``data_types=["finding"]``), never a raw signal off the target's
    own ``sources:``. Before this fix, ``_wire_targets_and_triggers`` handed
    them a coalescing trigger anyway, bound to the SAME raw-signal bindings
    every unit on the target also watches — so a composition woke reactively
    on wire volume with no bearing on whether its own units had run, and (via
    the shared per-(analyst, target) cadence cooldown) could suppress its own
    correctly-ordered scheduled tick. This seeds a real
    ``meta_findings_synthesizer``-shaped analyst beside an ordinary selector-
    bound unit on the SAME target and proves, through the production
    ``bring_up_source_first_planes`` pass (not a hand-called helper), that
    only the unit gets a coalescing-trigger registration.
    """
    run = uuid4().hex[:8]
    source_id = f"source.h4race.{run}"
    target_id = f"target_h4race_{run}"
    unit_id = f"analyst_h4race_unit_{run}"
    composition_id = f"analyst_h4race_composition_{run}"
    scope_tag = f"h4race_{run}"
    _base_url, registry_client = registry_http

    await registry.register(_source(source_id, tags=["news"]), actor="c1")
    await registry.register(
        _analyst(unit_id, selector_predicate=f'has_tag("{scope_tag}")'),
        actor="c1",
    )
    await registry.register(
        _composition_analyst(
            composition_id, selector_predicate=f'has_tag("{scope_tag}")',
        ),
        actor="c1",
    )
    await registry.register(
        _target(
            target_id, source_id=source_id, scope_tag=scope_tag,
            analyst_ref=unit_id,  # path-1 binding stays on the unit only
        ),
        actor="c1",
    )

    job_queue = JobQueue(
        nats_store,
        stream=f"LEGBA_JOBS_H4RACE_{run.upper()}",
        durable=f"legba-job-workers-h4race-{run}",
        subject_prefix=f"jobs_h4race_{run}",
        max_age_seconds=600,
    )
    trigger_durable = f"legba-trigger-h4race-{run}"

    handles = await bring_up_source_first_planes(
        pg_store=pg_store,
        nats_store=nats_store,
        standard_deps=object(),
        registry_client=registry_client,
        run_loops=False,
        job_queue=job_queue,
        trigger_durable=trigger_durable,
    )
    engine = handles.trigger_engine
    try:
        assert target_id in handles.registered_targets
        regs = {
            (r.analyst_id, r.target_id): r
            for r in engine._regs
            if r.target_id == target_id
        }
        assert (unit_id, target_id) in regs, (
            "the signal-consuming unit must still get its coalescing trigger "
            f"— got {sorted(regs)}"
        )
        assert (composition_id, target_id) not in regs, (
            "a finding-only composition must NOT get a coalescing trigger — "
            "it has no legitimate signal-driven wake, and one would race it "
            f"against its own units; got {sorted(regs)}"
        )

        # And a real published signal only ever marks the UNIT dirty — never
        # the composition, which the reactive path can no longer see at all.
        await engine.bind()
        await handles.subscription_engine.publish_signal(
            signal=Signal(
                source_id=source_id, owner_tenant="default", modality="text",
                tags=["news"], payload={"severity": "low"},
            )
        )
        for _ in range(10):
            await engine.drain_once()
            if engine.delivered >= 1:
                break
        assert engine.delivered >= 1
        assert engine.matched == 1, (
            "exactly one registration (the unit's) should have matched — "
            "the composition was never registered to match anything"
        )
        unit_acc = await handles.trigger_state.get(unit_id, target_id)
        assert unit_acc.pending_count == 1
        comp_acc = await handles.trigger_state.get(composition_id, target_id)
        assert comp_acc.pending_count == 0, (
            "the composition's dirty-state must stay at zero — nothing ever "
            "reaches it reactively"
        )
    finally:
        await handles.stop()
        with suppress(Exception):
            await nats_store.js.delete_consumer(engine._stream, trigger_durable)
        with suppress(Exception):
            await nats_store.js.delete_consumer(
                engine._stream, target_consumer_name(target_id),
            )
        with suppress(Exception):
            await nats_store.js.delete_stream(job_queue.stream)


async def test_published_envelope_carries_source_owner_tenant(
    pg_store: PostgresStore,
):
    """C-1 / 761be14: the PUBLISHED envelope must carry the source's scope
    tenant, not the Signal model default ('default').

    The end-to-end boot test above exercises subscription resolution + trigger
    registration, but it publishes a hand-built Signal directly through
    ``subscription_engine.publish_signal`` with tenant 'default' — it never runs
    ``SourceCore._process_one`` and uses the *exact regressed value*, so a
    dropped tenant stamp in ``_process_one`` would be invisible to it. This test
    closes that hole on the real SourceCore publish path:

      * a SourceDescriptor whose scope tenant is NON-default ('shared'),
      * a raw Signal left at its model-default ``owner_tenant`` ('default'),
      * driven through the production ``_process_one`` → ``_publish`` path via
        ``make_emit_callback`` (a recording ``nats_publish`` captures the wire
        envelope — the real ``model_dump_json`` the trigger plane consumes),

    then asserts the published envelope's ``owner_tenant`` is the source's scope
    tenant. If ``_process_one`` stopped stamping ``enriched.owner_tenant`` the
    envelope would carry the model default 'default' and reactive triggering
    would silently never fire — exactly the 761be14 regression.
    """
    from datetime import datetime

    from legba.data.nats import signal_subject
    from legba.data.schemas.source import SourcePipeline

    tenant = "shared"  # NON-default scope tenant — the discriminating value
    assert tenant != Signal.model_fields["owner_tenant"].default, (
        "fixture must use a tenant distinct from the Signal model default, "
        "else a dropped stamp is indistinguishable from a correct one"
    )
    run = uuid4().hex[:8]
    source_id = f"source.bootwire_envelope.{run}"
    sd = SourceDescriptor(
        identity=SourceIdentity(
            id=source_id, name=source_id, kind="generic_webhook",
            schema_uri="legba/source/3.0.0", version="0" * 16,
            state=LifecycleState.ACTIVE, owner="c1-boot-wiring",
            created=datetime.now(tz=timezone.utc),
        ),
        scope=SourceScope(owner_tenant=tenant, geo=["BR"], tags=["news"]),
        acquisition="push",
        pipeline=SourcePipeline(media="reference"),
    )

    published: list[dict] = []

    async def _recording_publish(subject: str, payload: bytes) -> None:
        published.append(
            {"subject": subject, "env": json.loads(payload.decode("utf-8"))}
        )

    deps = StandardDeps(pg_pool=pg_store.pool, nats_publish=_recording_publish)
    core = SourceCore(f"source::{source_id}::env", SourceDeps(descriptor=sd, deps=deps))

    # A raw signal left at the Signal model default tenant ('default'): the
    # ONLY thing that can move it to 'shared' on the wire is the _process_one
    # stamp under test. (Source-owned raw rows carry no canonical_signal_id.)
    raw = Signal(
        source_id=source_id, modality="text", tags=["news"],
        payload={"title": "Brazil energy policy shift", "severity": "low"},
        canonical_url=f"https://example.test/{run}",
    )
    assert raw.owner_tenant == "default", "precondition: raw left at model default"

    emit = core.make_emit_callback()
    await emit(raw)

    assert len(published) == 1, f"expected exactly one fan-out, got {published!r}"
    record = published[0]
    env = record["env"]
    assert env["source_id"] == source_id
    assert env["owner_tenant"] == tenant, (
        f"published envelope carries owner_tenant={env['owner_tenant']!r}, "
        f"expected the source scope tenant {tenant!r} — _process_one dropped "
        f"the tenant stamp (761be14-class regression: reactive triggering "
        f"would silently never fire)"
    )
    # The subject token must agree (the trigger plane subject-filters on it).
    assert record["subject"] == signal_subject(
        tenant=tenant, source_id=source_id, modality="text", event_class="raw",
    )

    # The DB row is stamped from a write param (always correct); the regression
    # was envelope-only, so assert the row agrees too (full G4 parity).
    async with pg_store.pool.acquire() as conn:
        row_tenant = await conn.fetchval(
            "SELECT owner_tenant FROM signals WHERE source_id=$1", source_id,
        )
    assert row_tenant == tenant
