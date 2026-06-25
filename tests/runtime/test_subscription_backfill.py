# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-12 backfill / catch-up for late-joining subscriptions — live-substrate tests.

Runs against the dev rig (real Postgres + NATS, no mocks). Covers the acceptance
criteria:

  1. A target registered AFTER signals exist backfills its MATCHING slice
     (predicate-filtered over the persistent pool — the W2 structured SQL
     ``WHERE`` + Starlark residual), and ONLY the matching slice. No re-pull
     from the source: every backfilled row comes from ``signals``.
  2. No gap / no duplicate at the backfill→forward handoff: the catch-up covers
     stream seq ``<= boundary``, the forward consumer (anchored at
     ``boundary + 1`` via ``DeliverPolicy.BY_START_SEQUENCE``) covers
     ``> boundary``, and a signal published AFTER the boundary is delivered by
     the forward stream exactly once and never re-replayed by backfill.

Each test uses its own fresh ``legba_bf_test_<uuid>`` DB (migrated 0001→0024).
"""

from __future__ import annotations

import json
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
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.properties import Cron
from legba.data.schemas.source import (
    CadenceBlock,
    SourceDescriptor,
    SourceIdentity,
    SourceRef,
    SourceScope,
    Subscription,
)
from legba.data.sources._contract import Signal
from legba.runtime.subscription import (
    Backfiller,
    SubscriptionEngine,
    capture_cursor,
)

ADMIN_DSN = "postgresql://legba:legba@127.0.0.1:5432/postgres"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_store():
    db_name = f"legba_bf_test_{uuid4().hex[:10]}"
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


def _source(sid: str, *, tenant: str = "default", tags=None, geo=None) -> SourceDescriptor:
    return SourceDescriptor(
        identity=SourceIdentity(
            id=sid, name=sid, kind="rss",
            schema_uri="legba/source/1.0.0", version="0" * 16,
            state=LifecycleState.ACTIVE, owner="operator",
            created=datetime.now(tz=timezone.utc),
        ),
        scope=SourceScope(
            owner_tenant=tenant, geo=geo or [], languages=[], tags=tags or [],
        ),
        acquisition="poll",
        cadence=CadenceBlock(schedule=Cron.of("*/30 * * * *")),
        subscription_policy="open",
    )


async def _insert_signal(
    pg: PostgresStore,
    *,
    source_id: str,
    tenant: str = "default",
    modality: str = "text",
    geo=None,
    tags=None,
    entity_classes=None,
    language=None,
    fetched_at: datetime | None = None,
    canonical_signal_id=None,
) -> str:
    """Insert a real signals row; return its id. ``fetched_at`` lets a test
    place a row deterministically before/after the captured watermark."""
    sig = Signal(
        source_id=source_id, owner_tenant=tenant, modality=modality,
        geo=geo or [], tags=tags or [], entity_classes=entity_classes or [],
        language=language,
    )
    async with pg.acquire() as conn:
        if fetched_at is None:
            await conn.execute(
                """
                INSERT INTO signals
                    (id, source_id, owner_tenant, modality, geo, tags,
                     entity_classes, language, canonical_signal_id)
                VALUES ($1,$2,$3,$4,$5::text[],$6::text[],$7::text[],$8,$9)
                """,
                sig.signal_id, source_id, tenant, modality,
                geo or [], tags or [], entity_classes or [], language,
                canonical_signal_id,
            )
        else:
            await conn.execute(
                """
                INSERT INTO signals
                    (id, source_id, owner_tenant, modality, geo, tags,
                     entity_classes, language, canonical_signal_id, fetched_at)
                VALUES ($1,$2,$3,$4,$5::text[],$6::text[],$7::text[],$8,$9,$10)
                """,
                sig.signal_id, source_id, tenant, modality,
                geo or [], tags or [], entity_classes or [], language,
                canonical_signal_id, fetched_at,
            )
    return str(sig.signal_id)


# ---------------------------------------------------------------------------
# Acceptance 1 — late join backfills the predicate-filtered slice (pool only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_late_join_backfills_matching_slice_only(registry, pg_store):
    """A target registered AFTER signals exist gets exactly its matching
    historical slice — from the persistent pool, predicate-filtered, no re-pull.
    Pure-pool path (no NATS): backfill still runs end-to-end."""
    await registry.register(_source("source.bf.feed", tags=["energy", "news"]), actor="op")

    match_a = await _insert_signal(
        pg_store, source_id="source.bf.feed", tags=["energy"],
        entity_classes=["organization"],
    )
    match_b = await _insert_signal(
        pg_store, source_id="source.bf.feed", tags=["energy"],
        entity_classes=["organization"],
    )
    # Fails the residual (no 'organization' entity class).
    no_residual = await _insert_signal(
        pg_store, source_id="source.bf.feed", tags=["energy"],
        entity_classes=["person"],
    )
    # Fails the structured filter (wrong tag).
    wrong_tag = await _insert_signal(
        pg_store, source_id="source.bf.feed", tags=["sports"],
    )

    engine = SubscriptionEngine(pg_store)  # no NATS → pure pool catch-up
    refs = [SourceRef(
        source_id="source.bf.feed",
        subscription=Subscription(tags=["energy"], predicate="mentions('organization')"),
    )]

    delivered: list[dict] = []

    async def sink(row):
        delivered.append(row)

    sub, result = await engine.register_target_with_catch_up(
        target_id="bf_target", target_tenant="default", source_refs=refs, sink=sink,
    )

    got = {str(r["id"]) for r in delivered}
    assert got == {match_a, match_b}, f"expected matching slice, got {got}"
    assert no_residual not in got
    assert wrong_tag not in got
    assert result.delivered == 2
    assert set(result.delivered_ids) == {match_a, match_b}
    # No NATS → boundary is informational only, no forward consumer.
    assert result.cursor.stream_present is False
    assert result.forward_consumer is None


@pytest.mark.asyncio
async def test_backfill_replays_chronologically(registry, pg_store):
    """Catch-up replays oldest-first (matching the monotonic forward stream)."""
    await registry.register(_source("source.bf.chrono", tags=["x"]), actor="op")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Insert out of chronological order; backfill must sort ascending.
    third = await _insert_signal(
        pg_store, source_id="source.bf.chrono", tags=["x"],
        fetched_at=base.replace(hour=3),
    )
    first = await _insert_signal(
        pg_store, source_id="source.bf.chrono", tags=["x"],
        fetched_at=base.replace(hour=1),
    )
    second = await _insert_signal(
        pg_store, source_id="source.bf.chrono", tags=["x"],
        fetched_at=base.replace(hour=2),
    )

    engine = SubscriptionEngine(pg_store)
    sub = await engine.register_target(
        target_id="chrono", target_tenant="default",
        source_refs=[SourceRef(
            source_id="source.bf.chrono", subscription=Subscription(tags=["x"]),
        )],
    )
    order: list[str] = []

    async def sink(row):
        order.append(str(row["id"]))

    cursor = await capture_cursor(engine)
    n, ids = await Backfiller(engine).backfill(sub, cursor, sink)
    assert n == 3
    assert order == [first, second, third]


# ---------------------------------------------------------------------------
# Acceptance 2 — seamless backfill→forward handoff (live NATS, no gap/no dup)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_gap_no_dup_at_handoff(registry, pg_store, nats_store):
    """Full seam on live NATS: catch-up covers the historical slice, the forward
    consumer resumes at boundary+1. A signal published AFTER registration is
    delivered ONCE by the forward stream and never re-replayed by backfill;
    historical signals are delivered ONCE by backfill and never re-delivered by
    the forward consumer. No gap, no dup."""
    sid = "source.bf.handoff"
    await registry.register(_source(sid, tags=["news"], geo=["BR"]), actor="op")

    engine = SubscriptionEngine(pg_store, nats=nats_store)
    await engine.ensure_signal_stream()

    target_id = f"bf_handoff_{uuid4().hex[:8]}"
    refs = [SourceRef(source_id=sid, subscription=Subscription(tags=["news"]))]

    # --- BEFORE registration: 2 historical signals exist in the pool AND on the
    # stream (the late-join scenario — pool write then publish, P-06 order).
    hist_ids: list[str] = []
    for _ in range(2):
        sig = Signal(source_id=sid, owner_tenant="default", modality="text",
                     tags=["news"], geo=["BR"])
        async with pg_store.acquire() as conn:
            await conn.execute(
                "INSERT INTO signals (id, source_id, owner_tenant, modality, geo, tags) "
                "VALUES ($1,$2,$3,$4,$5::text[],$6::text[])",
                sig.signal_id, sid, "default", "text", ["BR"], ["news"],
            )
        await engine.publish_signal(signal=sig)
        hist_ids.append(str(sig.signal_id))

    backfilled: list[str] = []

    async def sink(row):
        backfilled.append(str(row["id"]))

    try:
        # --- Late join: capture boundary, backfill pool, bind forward@boundary+1.
        sub, result = await engine.register_target_with_catch_up(
            target_id=target_id, target_tenant="default", source_refs=refs, sink=sink,
        )

        # Backfill delivered exactly the 2 historical rows, once each.
        assert sorted(backfilled) == sorted(hist_ids)
        assert result.delivered == 2
        assert result.cursor.stream_present is True
        assert result.cursor.boundary_seq >= 2
        assert result.forward_consumer == sub.consumer_name

        # Cursor contract made explicit: the forward consumer is anchored at
        # boundary+1 (BY_START_SEQUENCE) — the seam between catch-up and live.
        from nats.js.api import DeliverPolicy
        info = await nats_store.js.consumer_info("legba_signals", sub.consumer_name)
        assert info.config.deliver_policy == DeliverPolicy.BY_START_SEQUENCE
        assert info.config.opt_start_seq == result.cursor.boundary_seq + 1

        # --- AFTER registration: publish a NEW signal (also persisted).
        fwd_sig = Signal(source_id=sid, owner_tenant="default", modality="text",
                         tags=["news"], geo=["BR"])
        async with pg_store.acquire() as conn:
            await conn.execute(
                "INSERT INTO signals (id, source_id, owner_tenant, modality, geo, tags) "
                "VALUES ($1,$2,$3,$4,$5::text[],$6::text[])",
                fwd_sig.signal_id, sid, "default", "text", ["BR"], ["news"],
            )
        ack = await nats_store.js.publish(
            await _subject_for(engine, fwd_sig),
            fwd_sig.model_dump_json().encode("utf-8"),
        )
        # The new signal's stream seq is strictly past the captured boundary.
        assert ack.seq > result.cursor.boundary_seq

        # --- The forward consumer delivers ONLY messages > boundary: i.e. the
        # one new signal, NOT the 2 historical ones the backfill already covered.
        msgs = await _fetch_all(nats_store, sub.consumer_name, expect=1)
        fwd_ids = []
        for m in msgs:
            body = json.loads(m.data.decode("utf-8"))
            fwd_ids.append(str(body.get("signal_id") or body.get("id")))
            await m.ack()

        # No gap: the forward signal arrived. No dup: the 2 historical ids did
        # NOT come through the forward consumer.
        assert fwd_ids == [str(fwd_sig.signal_id)], f"forward delivered {fwd_ids}"
        for hid in hist_ids:
            assert hid not in fwd_ids

        # Union of backfill + forward == every matching signal, each exactly once.
        union = backfilled + fwd_ids
        assert sorted(union) == sorted(hist_ids + [str(fwd_sig.signal_id)])
        assert len(union) == len(set(union)), "duplicate at the handoff"
    finally:
        try:
            await nats_store.js.delete_consumer("legba_signals", sub.consumer_name)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _subject_for(engine, sig):
    from legba.data.nats import signal_subject
    return signal_subject(
        tenant=sig.owner_tenant, source_id=sig.source_id,
        modality=sig.modality, event_class="raw",
    )


async def _fetch_all(nats_store, durable, *, expect):
    """Pull up to ``expect`` messages from a durable consumer (bind by name)."""
    psub = await nats_store.js.pull_subscribe_bind(durable=durable, stream="legba_signals")
    out = []
    try:
        out = await psub.fetch(expect, timeout=5)
    except Exception:
        pass
    return out
