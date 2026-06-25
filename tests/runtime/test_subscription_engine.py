# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-08 fan-out + subscription engine — integration tests (live substrate).

Runs against the dev rig (legba_pivot_test schema, real Postgres + NATS). No
mocks. Covers the three acceptance criteria:

  1. A target with one EXPLICIT + one SELECTOR SourceRef receives ONLY matching
     signals — verified against real ``signals`` rows via the structured SQL
     filter (GIN/btree) + Starlark residual.
  2. A ``grant`` source REFUSES an ungranted target (and admits it once a
     wiring_descriptor grant exists).
  3. Per-target consumer lag (``num_pending``) is observable.

Each test uses its own fresh `legba_sub_test_<uuid>` DB (migrated 0001→0024) so
runs are isolated and repeatable.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

os.environ.setdefault("LEGBA_DATA_PG_HOST", "127.0.0.1")
os.environ.setdefault("LEGBA_DATA_PG_PORT", "5432")
os.environ.setdefault("LEGBA_DATA_PG_USER", "legba")
os.environ.setdefault("LEGBA_DATA_PG_PASSWORD", "legba")
os.environ.setdefault("LEGBA_DATA_NATS_URL", "nats://127.0.0.1:4222")

from legba.data.config import NatsConfig, PostgresConfig
from legba.data.migrate import apply_primary_migrations
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.registry.descriptor import DescriptorRegistry, Family
from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.properties import Cron
from legba.data.schemas.source import (
    CadenceBlock,
    SourceDescriptor,
    SourceIdentity,
    SourceRef,
    SourceScope,
    SourceSelector,
    Subscription,
)
from legba.data.sources._contract import Signal
from legba.runtime.subscription import (
    SubscriptionEngine,
    SubscriptionPolicyError,
    resolve_source_refs,
    write_grant,
)

ADMIN_DSN = "postgresql://legba:legba@127.0.0.1:5432/postgres"


# ---------------------------------------------------------------------------
# Fixtures — fresh migrated DB + connected stores
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_store():
    db_name = f"legba_sub_test_{uuid4().hex[:10]}"
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()

    cfg = PostgresConfig(
        host="127.0.0.1", port=5432, user="legba", password="legba", database=db_name
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
    store = NatsStore(NatsConfig.from_env())
    try:
        await store.connect()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"NATS not reachable: {exc}")
    try:
        yield store
    finally:
        await store.close()


@pytest_asyncio.fixture
async def registry(pg_store):
    reg = DescriptorRegistry(pg_store)
    await reg.start()
    try:
        yield reg
    finally:
        await reg.stop()


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _source(
    sid: str,
    *,
    kind: str = "rss",
    tenant: str = "default",
    geo: list[str] | None = None,
    languages: list[str] | None = None,
    tags: list[str] | None = None,
    policy: str = "open",
    allowed_targets: list[str] | None = None,
    allowed_tenants: list[str] | None = None,
) -> SourceDescriptor:
    return SourceDescriptor(
        identity=SourceIdentity(
            id=sid, name=sid, kind=kind,
            schema_uri="legba/source/1.0.0", version="0" * 16,
            state=LifecycleState.ACTIVE, owner="operator",
            created=datetime.now(tz=timezone.utc),
        ),
        scope=SourceScope(
            owner_tenant=tenant,
            geo=geo or [],
            languages=languages or [],
            tags=tags or [],
        ),
        acquisition="poll",
        cadence=CadenceBlock(schedule=Cron.of("*/30 * * * *")),
        subscription_policy=policy,
        allowed_targets=allowed_targets or [],
        allowed_tenants=allowed_tenants or [],
    )


async def _insert_signal(
    pg: PostgresStore,
    *,
    source_id: str,
    tenant: str = "default",
    modality: str = "text",
    geo: list[str] | None = None,
    tags: list[str] | None = None,
    entity_classes: list[str] | None = None,
    language: str | None = None,
    produced_by_kind: str = "source",
    canonical_signal_id=None,
) -> str:
    """Insert a real signals row; return its id."""
    sig = Signal(
        source_id=source_id, owner_tenant=tenant, modality=modality,
        geo=geo or [], tags=tags or [], entity_classes=entity_classes or [],
        language=language, produced_by_kind=produced_by_kind,
    )
    async with pg.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO signals
                (id, source_id, owner_tenant, modality, geo, tags,
                 entity_classes, language, produced_by_kind, canonical_signal_id)
            VALUES ($1,$2,$3,$4,$5::text[],$6::text[],$7::text[],$8,$9,$10)
            """,
            sig.signal_id, source_id, tenant, modality,
            geo or [], tags or [], entity_classes or [], language,
            produced_by_kind, canonical_signal_id,
        )
    return str(sig.signal_id)


# ---------------------------------------------------------------------------
# Acceptance 1 — explicit + selector SourceRef receives only matching signals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_plus_selector_receives_only_matching(registry, pg_store):
    # Three sources: reuters (explicit target), an energy feed (selector match),
    # and a sports feed (selector should NOT match).
    await registry.register(_source(
        "source.reuters.world", tenant="default", geo=["BR"], languages=["pt"],
        tags=["news"],
    ), actor="op")
    await registry.register(_source(
        "source.energy.feed", tenant="default", geo=["BR"], tags=["energy", "news"],
    ), actor="op")
    await registry.register(_source(
        "source.sports.feed", tenant="default", geo=["BR"], tags=["sports"],
    ), actor="op")

    # Signals: matching + non-matching per source.
    s_reuters_match = await _insert_signal(
        pg_store, source_id="source.reuters.world", geo=["BR"], tags=["news"],
        entity_classes=["organization"], language="pt",
    )
    s_reuters_wronggeo = await _insert_signal(
        pg_store, source_id="source.reuters.world", geo=["US"], tags=["news"],
        language="pt",
    )
    s_energy_match = await _insert_signal(
        pg_store, source_id="source.energy.feed", geo=["BR"], tags=["energy"],
        entity_classes=["organization"],
    )
    s_energy_noresidual = await _insert_signal(
        pg_store, source_id="source.energy.feed", geo=["BR"], tags=["energy"],
        entity_classes=["person"],  # fails the mentions('organization') residual
    )
    s_sports = await _insert_signal(
        pg_store, source_id="source.sports.feed", geo=["BR"], tags=["sports"],
    )

    # Target: explicit reuters (geo=BR) + selector tags⊇{energy} with a residual.
    refs = [
        SourceRef(
            source_id="source.reuters.world",
            subscription=Subscription(geo=["BR"], canonical_only=True),
        ),
        SourceRef(
            source_selector=SourceSelector(tags=["energy"]),
            subscription=Subscription(
                tags=["energy"], predicate="mentions('organization')",
                canonical_only=True,
            ),
        ),
    ]

    engine = SubscriptionEngine(pg_store)
    sub = await engine.register_target(
        target_id="india_energy", target_tenant="default", source_refs=refs,
    )

    # Selector resolved to the energy feed (open), NOT sports.
    assert set(sub.source_ids) == {"source.reuters.world", "source.energy.feed"}

    rows = await engine.read_target_slice(sub)
    got = {str(r["id"]) for r in rows}

    # Only the matching signals are delivered.
    assert s_reuters_match in got
    assert s_energy_match in got
    # Wrong-geo reuters, residual-failing energy, and the sports feed are NOT.
    assert s_reuters_wronggeo not in got
    assert s_energy_noresidual not in got
    assert s_sports not in got


@pytest.mark.asyncio
async def test_selector_skips_locked_sources(registry, pg_store):
    # A selector NEVER auto-wires a non-open source (PIVOT §4.4.1 / §4.7).
    await registry.register(_source(
        "source.open.energy", tags=["energy"], policy="open",
    ), actor="op")
    await registry.register(_source(
        "source.locked.energy", tags=["energy"], policy="allowlist",
    ), actor="op")

    refs = [SourceRef(
        source_selector=SourceSelector(tags=["energy"]),
        subscription=Subscription(),
    )]
    bindings = await resolve_source_refs(
        pg_store, target_id="t1", target_tenant="default", source_refs=refs,
    )
    ids = {b.source_id for b in bindings}
    assert ids == {"source.open.energy"}


@pytest.mark.asyncio
async def test_canonical_only_filters_aliases(registry, pg_store):
    await registry.register(_source("source.dup.feed", tags=["news"]), actor="op")
    canonical = await _insert_signal(pg_store, source_id="source.dup.feed", tags=["news"])
    # An alias points at the canonical.
    alias = await _insert_signal(
        pg_store, source_id="source.dup.feed", tags=["news"],
        canonical_signal_id=canonical,
    )

    refs = [SourceRef(
        source_id="source.dup.feed",
        subscription=Subscription(tags=["news"], canonical_only=True),
    )]
    engine = SubscriptionEngine(pg_store)
    sub = await engine.register_target(
        target_id="t_canon", target_tenant="default", source_refs=refs,
    )
    got = {str(r["id"]) for r in await engine.read_target_slice(sub)}
    assert canonical in got
    assert alias not in got  # alias suppressed by canonical_only

    # canonical_only=False delivers both.
    refs2 = [SourceRef(
        source_id="source.dup.feed",
        subscription=Subscription(tags=["news"], canonical_only=False),
    )]
    sub2 = await engine.register_target(
        target_id="t_all", target_tenant="default", source_refs=refs2,
    )
    got2 = {str(r["id"]) for r in await engine.read_target_slice(sub2)}
    assert canonical in got2 and alias in got2


# ---------------------------------------------------------------------------
# Acceptance 2 — grant source refuses an ungranted target
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_source_refuses_then_admits(registry, pg_store):
    await registry.register(_source(
        "source.facial.rec", tags=["biometric"], policy="grant",
    ), actor="op")

    refs = [SourceRef(
        source_id="source.facial.rec", subscription=Subscription(),
    )]
    engine = SubscriptionEngine(pg_store)

    # No grant yet → refused.
    with pytest.raises(SubscriptionPolicyError) as exc:
        await engine.register_target(
            target_id="nosy_target", target_tenant="default", source_refs=refs,
        )
    assert exc.value.policy == "grant"
    assert exc.value.source_id == "source.facial.rec"

    # Record a grant → admitted.
    await write_grant(
        pg_store, source_id="source.facial.rec", target_id="nosy_target",
        owner="op", reason="approved",
    )
    sub = await engine.register_target(
        target_id="nosy_target", target_tenant="default", source_refs=refs,
    )
    assert sub.source_ids == ["source.facial.rec"]


@pytest.mark.asyncio
async def test_allowlist_and_cross_tenant(registry, pg_store):
    await registry.register(_source(
        "source.allow.feed", tags=["x"], policy="allowlist",
        allowed_targets=["good_target"],
    ), actor="op")
    refs = [SourceRef(source_id="source.allow.feed", subscription=Subscription())]
    engine = SubscriptionEngine(pg_store)

    with pytest.raises(SubscriptionPolicyError):
        await engine.register_target(
            target_id="bad_target", target_tenant="default", source_refs=refs,
        )
    sub = await engine.register_target(
        target_id="good_target", target_tenant="default", source_refs=refs,
    )
    assert sub.source_ids == ["source.allow.feed"]

    # Cross-tenant to an OPEN, non-shared source is refused.
    await registry.register(_source(
        "source.tenant_a.feed", tenant="tenant_a", tags=["y"], policy="open",
    ), actor="op")
    refs_x = [SourceRef(source_id="source.tenant_a.feed", subscription=Subscription())]
    with pytest.raises(SubscriptionPolicyError):
        await engine.register_target(
            target_id="t_in_b", target_tenant="tenant_b", source_refs=refs_x,
        )

    # A `shared` source crosses tenants freely.
    await registry.register(_source(
        "source.shared.feed", tenant="shared", tags=["z"], policy="open",
    ), actor="op")
    refs_shared = [SourceRef(source_id="source.shared.feed", subscription=Subscription())]
    sub_shared = await engine.register_target(
        target_id="t_in_b2", target_tenant="tenant_b", source_refs=refs_shared,
    )
    assert sub_shared.source_ids == ["source.shared.feed"]


# ---------------------------------------------------------------------------
# Acceptance 3 — per-target consumer lag is observable (live NATS)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_target_consumer_lag_observable(registry, pg_store, nats_store):
    await registry.register(_source(
        "source.lag.feed", tenant="default", tags=["news"], policy="open",
    ), actor="op")

    engine = SubscriptionEngine(pg_store, nats=nats_store)
    # Use a unique consumer per run by deriving a unique target id.
    target_id = f"lagwatch_{uuid4().hex[:8]}"
    refs = [SourceRef(
        source_id="source.lag.feed",
        subscription=Subscription(tags=["news"]),
    )]
    sub = await engine.register_target(
        target_id=target_id, target_tenant="default", source_refs=refs,
    )
    assert sub.subject_filters  # at least one coarse filter bound

    try:
        # Baseline lag.
        lag0 = await engine.consumer_lag(target_id)
        base = lag0["num_pending"]

        # Publish 3 matching signals onto the coarse subject.
        for _ in range(3):
            sig = Signal(
                source_id="source.lag.feed", owner_tenant="default",
                modality="text", tags=["news"], geo=["BR"],
            )
            await engine.publish_signal(signal=sig)

        # Lag is observable + reflects the published, undelivered messages.
        lag1 = await engine.consumer_lag(target_id)
        assert set(lag1.keys()) >= {
            "num_pending", "num_ack_pending", "num_redelivered",
            "delivered_stream_seq", "ack_floor_stream_seq",
        }
        assert lag1["num_pending"] >= base + 3

        growth = await engine.stream_growth()
        assert growth["messages"] >= 3
        assert growth["last_seq"] >= growth["first_seq"]
    finally:
        # Clean up the per-run durable consumer so the shared dev stream
        # doesn't accumulate orphans.
        try:
            await nats_store.js.delete_consumer(
                "legba_signals", sub.consumer_name
            )
        except Exception:
            pass


@pytest.mark.asyncio
async def test_coarse_subject_filters_are_coarse(registry, pg_store):
    # The subject filter set encodes ONLY tenant/source/modality — never the
    # arbitrary structured predicate (geo/tags/residual). Two subscriptions
    # with different geo/tags but same source+modality produce the SAME filter.
    await registry.register(_source("source.coarse.feed", tags=["a", "b"]), actor="op")
    engine = SubscriptionEngine(pg_store)

    sub1 = await engine.register_target(
        target_id="c1", target_tenant="default",
        source_refs=[SourceRef(
            source_id="source.coarse.feed",
            subscription=Subscription(geo=["BR"], tags=["a"]),
        )],
    )
    sub2 = await engine.register_target(
        target_id="c2", target_tenant="default",
        source_refs=[SourceRef(
            source_id="source.coarse.feed",
            subscription=Subscription(geo=["US"], tags=["b"], predicate="mentions('person')"),
        )],
    )
    assert sub1.subject_filters == sub2.subject_filters
    # And the coarse filter carries a wildcard modality + event-class.
    assert all(f.endswith(".*.*") for f in sub1.subject_filters)
