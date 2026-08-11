# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the L-110 descriptor registry.

Runs against the live legba-* containers via the `migrated_pg` fixture from
conftest.py (separate test database per session, migrations applied).

These exercise the full round-trip: register -> query -> update -> retire,
vocabulary-validator end-to-end (DLQ routing on unknown values), audit-log
verification on every mutation, and NATS event firing.

Per the L-001 / L-110 brief: no mocks for substrate boundaries. NATS events
are published over core NATS (observability signals, not durable work
items); the SubjectCollector subscribes and drains the messages into a
list, then tests assert what fired.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from nacl.signing import SigningKey

from legba.data.config import NatsConfig, PostgresConfig
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.registry.descriptor import (
    DescriptorPredicate,
    DescriptorRegistry,
    Family,
)
from legba.data.registry.errors import (
    DescriptorNotFound,
    DescriptorValidationError,
    IllegalLifecycleTransition,
    VersionConflict,
)
from legba.data.registry.events import (
    DEAD_LETTER_TOPIC_PREFIX,
    DESCRIPTOR_TOPIC_PREFIX,
)
from legba.data.registry.signing import SigningIdentity, verify_audit_payload
from legba.data.schemas import (
    AbstractionLevel,
    AnalystDescriptor,
    AnalystIdentity,
    AnalystKind,
    CadenceBlock,
    GeoScope,
    InlineAnalystBlock,
    LifecycleState,
    MethodBlock,
    SourceRef,
    SubscriptionBlock,
    TargetDescriptor,
    TargetIdentity,
    TypeSignature,
)


# ---------------------------------------------------------------------------
# Test-scoped fixtures
# ---------------------------------------------------------------------------


_FIXED_CREATED = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)


def _draft_target(
    *,
    descriptor_id: str = "br_energy",
    entity_classes: tuple[str, ...] = ("organization", "country"),
    relationship_types: tuple[str, ...] = ("LocatedIn",),
    name: str = "Brazil Energy",
    state: LifecycleState = LifecycleState.DRAFT,
    analyst: "InlineAnalystBlock | None" = None,
) -> TargetDescriptor:
    """Build a syntactically-valid target descriptor for the test suite.

    Vocabulary terms default to seeded values so the descriptor registers
    cleanly. Override `entity_classes` to force a DLQ event. `analyst` is
    the X-1 inline-analyst-block dead-config case (defect C) — None by
    default (every other test in this file exercises the clean path).
    """
    identity = TargetIdentity(
        id=descriptor_id,
        name=name,
        schema_uri="legba/target/2.0.0",
        # Placeholder — registry overwrites with the real content hash.
        version="0" * 16,
        abstraction_level=AbstractionLevel.L1,
        state=state,
        owner="lewis@local",
        created=_FIXED_CREATED,
    )
    # Source-first pivot: targets reference shared sources via SourceRef
    # (explicit id OR selector) rather than owning inline SourceBindings.
    sources: list[SourceRef] = []
    if state == LifecycleState.ACTIVE:
        sources.append(SourceRef(source_id="rss_main"))
    return TargetDescriptor(
        identity=identity,
        # Pivot: TargetScope is a discriminated union by ``domain``. The
        # geopolitical/OSINT founding case is GeoScope.
        scope=GeoScope(
            geo=["BR"],
            languages=["pt-BR"],
            entity_classes=list(entity_classes),
            relationship_types=list(relationship_types),
            time_horizon_days=90,
        ),
        sources=sources,
        analyst=analyst,
    )


def _draft_analyst(
    *,
    descriptor_id: str = "critic_v2",
    kind: AnalystKind = AnalystKind.INLINE_TARGET,
) -> AnalystDescriptor:
    identity = AnalystIdentity(
        id=descriptor_id,
        name=f"Analyst {descriptor_id}",
        schema_uri="legba/analyst/2.0.0",
        version="0" * 16,
        kind=kind,
        type_signature=TypeSignature(
            input_type="legba.x.In",
            output_type="legba.x.Out",
        ),
        owner="lewis@local",
    )
    return AnalystDescriptor(
        identity=identity,
        subscription=SubscriptionBlock(),
        method=MethodBlock(kind="llm_planner", prompt_module="legba.prompts.x"),
        cadence=CadenceBlock(),
    )


def _fixed_identity() -> SigningIdentity:
    seed = b"L-110-integ-test-seed-deterministic1"[:32]
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:integ",
    )


@pytest_asyncio.fixture
async def pg_store(migrated_pg: PostgresConfig) -> PostgresStore:
    store = PostgresStore(migrated_pg)
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def nats_store() -> NatsStore:
    """Real NATS connection. Each test composes its own subscription so we
    don't fan out into a shared inbox.
    """
    store = NatsStore(NatsConfig.from_env())
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def registry(pg_store: PostgresStore, nats_store: NatsStore) -> DescriptorRegistry:
    # TEST_DEBT_RECON.md Bucket I: DescriptorRegistry.start() calls
    # _sync_analyst_kind_registry(), which REPLACES (not unions)
    # ANALYST_KIND_REGISTRY's process-wide extension set from this fresh
    # session DB's (empty) vocabulary_entries.analyst_kind rows — wiping the
    # in-code registrations legba.data.analysts does at import time
    # (journal_assessor / entity_researcher / signal_salience) for the
    # REMAINDER of the pytest process, poisoning any later test/file that
    # assumes those kinds validate. Snapshot + restore around this fixture
    # (the shared setup/teardown point every test in this file goes through)
    # so the singleton never leaks past a single test, mirroring the
    # snapshot/restore discipline test_analyst_kind_registry_seeded_from_vocab_on_start
    # already uses locally for its OWN extra mutation.
    from legba.data.schemas import ANALYST_KIND_REGISTRY

    snapshot = ANALYST_KIND_REGISTRY.extension_values()
    identity = _fixed_identity()
    reg = DescriptorRegistry(
        pg_store,
        nats_store=nats_store,
        signing_identity=identity,
    )
    await reg.start()
    try:
        yield reg
    finally:
        await reg.stop()
        ANALYST_KIND_REGISTRY.replace_extensions(snapshot)


@pytest_asyncio.fixture
async def registry_no_nats(pg_store: PostgresStore) -> DescriptorRegistry:
    """Variant without NATS — for tests that don't care about events and
    want to avoid the subscription teardown noise.

    Same ANALYST_KIND_REGISTRY snapshot/restore as `registry` above (Bucket I)
    — .start() replaces the process-wide extension set from this session DB's
    empty vocabulary_entries.analyst_kind rows.
    """
    from legba.data.schemas import ANALYST_KIND_REGISTRY

    snapshot = ANALYST_KIND_REGISTRY.extension_values()
    identity = _fixed_identity()
    reg = DescriptorRegistry(pg_store, signing_identity=identity)
    await reg.start()
    try:
        yield reg
    finally:
        await reg.stop()
        ANALYST_KIND_REGISTRY.replace_extensions(snapshot)


# ---------------------------------------------------------------------------
# NATS subscription helper
# ---------------------------------------------------------------------------


class _SubjectCollector:
    """Subscribes to a NATS subject pattern and collects messages into a list.

    Use as:
        async with _SubjectCollector(nats_store, "descriptor.>") as collector:
            ...do mutations...
            await collector.wait_for(n=3, timeout=5.0)
            assert len(collector.messages) == 3
    """

    def __init__(self, nats_store: NatsStore, pattern: str):
        self._nats = nats_store
        self._pattern = pattern
        self._sub = None
        self.messages: list[tuple[str, dict[str, Any]]] = []
        self._event = asyncio.Event()

    async def __aenter__(self) -> "_SubjectCollector":
        async def _cb(msg):
            try:
                payload = json.loads(msg.data.decode())
            except Exception:
                payload = {"raw": msg.data}
            self.messages.append((msg.subject, payload))
            self._event.set()
        self._sub = await self._nats.nc.subscribe(self._pattern, cb=_cb)
        # Give NATS a moment to register the interest.
        await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._sub is not None:
            await self._sub.unsubscribe()

    async def wait_for(self, *, n: int, timeout: float = 5.0) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while len(self.messages) < n:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return


# ---------------------------------------------------------------------------
# Registry startup / vocabulary cache
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registry_start_loads_vocabulary_cache(registry_no_nats: DescriptorRegistry):
    """`start()` populates the vocabulary cache from the seeded rows."""
    cache = registry_no_nats.vocabulary
    ec = cache.values("entity_class")
    rt = cache.values("relationship_type")
    # The seed migration inserts 9 entity_classes + 14 relationship_types.
    assert "organization" in ec
    assert "country" in ec
    assert "LocatedIn" in rt
    # Alias roundtrip.
    assert cache.resolve_alias("relationship_type", "INVOLVED_IN") == "InvolvedIn"
    assert cache.contains("relationship_type", "INVOLVED_IN") is True


# ---------------------------------------------------------------------------
# Target descriptor full round trip
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_target_register_returns_row_with_content_hash(
    registry_no_nats: DescriptorRegistry,
):
    desc = _draft_target(descriptor_id=f"t_register_{uuid4().hex[:8]}")
    row = await registry_no_nats.register(desc, actor="lewis@local")
    assert row.descriptor_id == desc.identity.id
    assert row.is_head is True
    assert len(row.version) == 64  # SHA-256 hex
    assert row.schema_uri == "legba/target/2.0.0"
    assert row.state == "draft"
    assert row.abstraction_level == "L1"
    assert row.body["scope"]["entity_classes"] == ["organization", "country"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_target_register_with_inline_analyst_block_warns(
    registry_no_nats: DescriptorRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defect C — a target body carrying an inline ``analyst`` block is dead
    config (the runtime never builds a running analyst from it; only
    ``analyst_ref`` is ever consulted, and this block doesn't set one). The
    registry does NOT refuse the write (X-1 refuse-vs-degrade: registry rows
    outlive code) but it MUST warn loudly — in the log and in the returned
    row — so this doesn't silently sit inert forever the way the live
    ``target_situation_iran_war`` did from registration onward.
    """
    desc = _draft_target(
        descriptor_id=f"t_inline_analyst_{uuid4().hex[:8]}",
        analyst=InlineAnalystBlock(
            use="inline_target",
            cadence={"fallback_schedule": "*/15 * * * *"},
            method={"kind": "llm_planner"},
        ),
    )
    with caplog.at_level("WARNING", logger="legba.data.registry.descriptor"):
        row = await registry_no_nats.register(desc, actor="lewis@local")

    assert len(row.warnings) == 1
    assert desc.identity.id in row.warnings[0]
    assert "inline `analyst`" in row.warnings[0]
    assert "subscription.targets" in row.warnings[0]

    assert any(
        "inert_inline_analyst_block" in rec.getMessage()
        and desc.identity.id in rec.getMessage()
        for rec in caplog.records
    ), "expected a loud WARNING log line naming the inert inline analyst block"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_target_register_without_inline_analyst_block_is_silent(
    registry_no_nats: DescriptorRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defect C counterpart — a clean target body (no inline ``analyst``
    block) registers with zero warnings, silently, exactly as before."""
    desc = _draft_target(descriptor_id=f"t_no_inline_analyst_{uuid4().hex[:8]}")
    assert desc.analyst is None

    with caplog.at_level("WARNING", logger="legba.data.registry.descriptor"):
        row = await registry_no_nats.register(desc, actor="lewis@local")

    assert row.warnings == []
    assert not any(
        "inert_inline_analyst_block" in rec.getMessage() for rec in caplog.records
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_target_register_double_register_conflict(
    registry_no_nats: DescriptorRegistry,
):
    desc = _draft_target(descriptor_id=f"t_dup_{uuid4().hex[:8]}")
    await registry_no_nats.register(desc, actor="lewis@local")
    with pytest.raises(VersionConflict):
        await registry_no_nats.register(desc, actor="lewis@local")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_target_update_preserves_history(
    registry_no_nats: DescriptorRegistry,
):
    desc_id = f"t_upd_{uuid4().hex[:8]}"
    initial = _draft_target(descriptor_id=desc_id)
    head_v1 = await registry_no_nats.register(initial, actor="lewis@local")

    # Mutate the descriptor body — add a relationship type, then update.
    mutated = _draft_target(
        descriptor_id=desc_id,
        relationship_types=("LocatedIn", "PartOf"),
    )
    head_v2 = await registry_no_nats.update(desc_id, mutated, actor="lewis@local")
    assert head_v2.version != head_v1.version

    # History has both, newest first.
    history = await registry_no_nats.query_history(desc_id, family=Family.TARGET)
    assert [r.version for r in history][0] == head_v2.version
    assert head_v1.version in [r.version for r in history]
    heads = [r for r in history if r.is_head]
    assert len(heads) == 1 and heads[0].version == head_v2.version


@pytest.mark.integration
@pytest.mark.asyncio
async def test_target_update_idempotent_on_identical_body(
    registry_no_nats: DescriptorRegistry,
):
    desc_id = f"t_noop_{uuid4().hex[:8]}"
    desc = _draft_target(descriptor_id=desc_id)
    await registry_no_nats.register(desc, actor="lewis@local")
    again = await registry_no_nats.update(desc_id, _draft_target(descriptor_id=desc_id), actor="lewis@local")
    history = await registry_no_nats.query_history(desc_id, family=Family.TARGET)
    # Only one row exists — second update was a no-op.
    assert len(history) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_target_retire_state_transition(
    registry_no_nats: DescriptorRegistry,
):
    desc_id = f"t_ret_{uuid4().hex[:8]}"
    await registry_no_nats.register(
        _draft_target(descriptor_id=desc_id), actor="lewis@local"
    )
    retired = await registry_no_nats.retire(
        desc_id, actor="lewis@local", family=Family.TARGET, reason="end of life"
    )
    assert retired.state == "retired"

    # Retired is terminal — can't transition out.
    with pytest.raises(IllegalLifecycleTransition):
        await registry_no_nats.retire(
            desc_id, actor="lewis@local", family=Family.TARGET
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_target_promote_and_rollback(
    registry_no_nats: DescriptorRegistry,
):
    desc_id = f"t_prom_{uuid4().hex[:8]}"
    v1 = await registry_no_nats.register(
        _draft_target(descriptor_id=desc_id), actor="lewis@local"
    )
    v2 = await registry_no_nats.update(
        desc_id,
        _draft_target(descriptor_id=desc_id, relationship_types=("LocatedIn", "PartOf")),
        actor="lewis@local",
    )
    # Rollback to v1: rollback shifts head back.
    rolled = await registry_no_nats.rollback(
        desc_id, v1.version, actor="lewis@local", family=Family.TARGET, reason="ramp-back"
    )
    assert rolled.version == v1.version
    assert rolled.is_head is True

    # Promote v2 back forward.
    promoted = await registry_no_nats.promote(
        desc_id, v2.version, actor="lewis@local", family=Family.TARGET
    )
    assert promoted.version == v2.version
    assert promoted.is_head is True

    # Promoting the current head is a conflict.
    with pytest.raises(VersionConflict):
        await registry_no_nats.promote(
            desc_id, v2.version, actor="lewis@local", family=Family.TARGET
        )

    # Promoting a non-existent version is a conflict.
    with pytest.raises(VersionConflict):
        await registry_no_nats.promote(
            desc_id, "f" * 64, actor="lewis@local", family=Family.TARGET
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transition_round_trip_is_idempotent(
    registry_no_nats: DescriptorRegistry,
):
    """active → paused → active must NOT 500 on the second transition.

    State is part of content_hash, so the active→active round-trip recomputes
    the ORIGINAL active version, which is still present as a prior row. The fix
    SHIFTs the head pointer to that existing version instead of re-INSERTing the
    (descriptor_id, version) PK (which raised UniqueViolationError → HTTP 500
    live). Head ends ACTIVE on the original version; no duplicate row.
    """
    reg = registry_no_nats
    desc_id = f"t_rt_{uuid4().hex[:8]}"
    active_v = await reg.register(
        _draft_target(descriptor_id=desc_id, state=LifecycleState.ACTIVE),
        actor="lewis@local",
    )
    assert active_v.state == "active"

    def _restamp(row_state: LifecycleState) -> TargetDescriptor:
        # Mirror api.transition_descriptor: re-stamp identity.state then update.
        # The body/sources differ between active+paused only by the state stamp,
        # so the active hash is reproducible on the way back.
        return _draft_target(descriptor_id=desc_id, state=row_state)

    # active → paused (mints/inserts a NEW paused version, head shifts).
    paused = await reg.update(
        desc_id, _restamp(LifecycleState.PAUSED), actor="lewis@local",
    )
    assert paused.state == "paused"
    assert paused.version != active_v.version

    # paused → active: recomputes the ORIGINAL active hash, which already
    # exists. Pre-fix this raised UniqueViolationError (asyncpg) → 500.
    re_active = await reg.update(
        desc_id, _restamp(LifecycleState.ACTIVE), actor="lewis@local",
    )
    assert re_active.state == "active"
    # Head is back on the ORIGINAL active version — no new row minted.
    assert re_active.version == active_v.version
    assert re_active.is_head is True

    # History holds exactly the two distinct versions (active + paused); the
    # round-trip did NOT insert a third (duplicate) active row.
    history = await reg.query_history(desc_id, family=Family.TARGET)
    versions = {r.version for r in history}
    assert versions == {active_v.version, paused.version}
    heads = [r for r in history if r.is_head]
    assert len(heads) == 1 and heads[0].version == active_v.version


@pytest.mark.integration
@pytest.mark.asyncio
async def test_target_get_typed_returns_pydantic_model(
    registry_no_nats: DescriptorRegistry,
):
    desc_id = f"t_typed_{uuid4().hex[:8]}"
    await registry_no_nats.register(_draft_target(descriptor_id=desc_id), actor="lewis@local")
    typed = await registry_no_nats.get_typed(desc_id, family=Family.TARGET)
    assert isinstance(typed, TargetDescriptor)
    assert typed.identity.id == desc_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_target_list_with_predicate_filters(
    registry_no_nats: DescriptorRegistry,
):
    a = f"t_list_a_{uuid4().hex[:8]}"
    b = f"t_list_b_{uuid4().hex[:8]}"
    await registry_no_nats.register(_draft_target(descriptor_id=a), actor="lewis@local")
    await registry_no_nats.register(_draft_target(descriptor_id=b), actor="lewis@local")

    rows = await registry_no_nats.list(
        DescriptorPredicate(family=Family.TARGET, descriptor_id=a)
    )
    assert {r.descriptor_id for r in rows} == {a}

    rows = await registry_no_nats.list(
        DescriptorPredicate(family=Family.TARGET, state="draft", owner="lewis@local")
    )
    assert a in {r.descriptor_id for r in rows}
    assert b in {r.descriptor_id for r in rows}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_target_get_missing_raises(registry_no_nats: DescriptorRegistry):
    with pytest.raises(DescriptorNotFound):
        await registry_no_nats.get("never_registered_xyz", family=Family.TARGET)


# ---------------------------------------------------------------------------
# Vocabulary DLQ routing
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_with_unknown_entity_class_routes_to_dlq(
    registry_no_nats: DescriptorRegistry,
    pg_store: PostgresStore,
):
    bad = _draft_target(
        descriptor_id=f"t_dlq_{uuid4().hex[:8]}",
        entity_classes=("organization", "definitely_not_a_real_entity_class"),
    )

    with pytest.raises(DescriptorValidationError) as exc:
        await registry_no_nats.register(bad, actor="lewis@local")
    assert exc.value.dead_letter_id is not None
    assert "definitely_not_a_real_entity_class" in str(exc.value)

    # Confirm the DLQ row exists with the right context.
    async with pg_store.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT actor, namespace, declared_schema_uri, validation_error "
            "FROM descriptor_dead_letter WHERE id = $1",
            exc.value.dead_letter_id,
        )
    assert row is not None
    assert row["actor"] == "lewis@local"
    assert row["namespace"] == "target"
    assert row["declared_schema_uri"] == "legba/target/2.0.0"
    err = row["validation_error"]
    if isinstance(err, str):
        err = json.loads(err)
    assert err["kind"] == "vocabulary"
    assert "definitely_not_a_real_entity_class" in err["unknown_values"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_with_unknown_relationship_type_routes_to_dlq(
    registry_no_nats: DescriptorRegistry,
    pg_store: PostgresStore,
):
    bad = _draft_target(
        descriptor_id=f"t_dlq_rt_{uuid4().hex[:8]}",
        relationship_types=("LocatedIn", "FakedRelationship"),
    )
    with pytest.raises(DescriptorValidationError) as exc:
        await registry_no_nats.register(bad, actor="lewis@local")
    assert exc.value.dead_letter_id is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vocabulary_alias_resolution_via_cache_helper(
    registry_no_nats: DescriptorRegistry,
):
    """Alias resolution lives in the cache; the schema-level pydantic
    pattern (^[A-Z][A-Za-z0-9]*$ for relationship_type) rejects underscore
    forms before the registry validator runs, so the alias path is
    primarily an ingest-time concern for legacy DATA. We still exercise the
    cache's alias-aware lookup here so the seeded aliases are wired."""
    cache = registry_no_nats.vocabulary
    assert cache.resolve_alias("relationship_type", "INVOLVED_IN") == "InvolvedIn"
    assert cache.resolve_alias("relationship_type", "TRACKED_BY") == "InvolvedIn"
    assert cache.resolve_alias("relationship_type", "PART_OF") == "PartOf"
    # `contains()` resolves the alias before the membership check.
    assert cache.contains("relationship_type", "INVOLVED_IN") is True


# ---------------------------------------------------------------------------
# Analyst descriptor round trip
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_analyst_register_update_retire(
    registry_no_nats: DescriptorRegistry,
):
    desc_id = f"a_{uuid4().hex[:8]}"
    initial = _draft_analyst(descriptor_id=desc_id)
    head = await registry_no_nats.register(initial, actor="lewis@local")
    assert head.kind == "inline_target"
    assert head.type_signature == {
        "input_type": "legba.x.In",
        "output_type": "legba.x.Out",
        "deps_type": "legba.runtime.deps.StandardDeps",
    }

    # Mutate method.timeout to produce a new content hash.
    new = _draft_analyst(descriptor_id=desc_id)
    new = new.model_copy(
        update={
            "method": MethodBlock(
                kind="llm_planner",
                prompt_module="legba.prompts.x_v2",
                timeout_seconds=240,
            )
        }
    )
    updated = await registry_no_nats.update(desc_id, new, actor="lewis@local")
    assert updated.version != head.version

    retired = await registry_no_nats.retire(
        desc_id, actor="lewis@local", family=Family.ANALYST
    )
    assert retired.state == "retired"


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_mutation_writes_audit_row_with_valid_signature(
    registry_no_nats: DescriptorRegistry,
    pg_store: PostgresStore,
):
    desc_id = f"t_audit_{uuid4().hex[:8]}"
    initial = _draft_target(descriptor_id=desc_id)
    head_v1 = await registry_no_nats.register(initial, actor="lewis@local")
    head_v2 = await registry_no_nats.update(
        desc_id,
        _draft_target(descriptor_id=desc_id, relationship_types=("LocatedIn", "PartOf")),
        actor="lewis@local",
    )
    await registry_no_nats.rollback(
        desc_id, head_v1.version, actor="lewis@local", family=Family.TARGET
    )
    await registry_no_nats.promote(
        desc_id, head_v2.version, actor="lewis@local", family=Family.TARGET
    )
    await registry_no_nats.retire(
        desc_id, actor="lewis@local", family=Family.TARGET, reason="done"
    )

    # 5 mutations → 5 audit rows.
    async with pg_store.acquire() as conn:
        rows = await conn.fetch(
            "SELECT occurred_at, actor_id, actor_role, namespace, descriptor_id, "
            "action, from_version, to_version, change_summary, signed_payload, "
            "signer_did "
            "FROM descriptor_audit_log WHERE descriptor_id = $1 "
            "ORDER BY occurred_at",
            desc_id,
        )
    actions = [r["action"] for r in rows]
    assert actions == ["register", "update", "rollback", "promote", "retire"]
    # Each row carries a 64-byte Ed25519 signature.
    for r in rows:
        assert isinstance(r["signed_payload"], (bytes, bytearray))
        assert len(r["signed_payload"]) == 64
        assert r["signer_did"] == "did:legba:registry:integ"

    # Verify the signatures using the deterministic test key.
    identity = _fixed_identity()
    from legba.data.registry.events import audit_payload

    for r in rows:
        summary = r["change_summary"]
        if isinstance(summary, str):
            summary = json.loads(summary)
        payload = audit_payload(
            action=r["action"],
            family=r["namespace"],
            descriptor_id=r["descriptor_id"],
            actor_id=r["actor_id"],
            actor_role=r["actor_role"],
            from_version=r["from_version"],
            to_version=r["to_version"],
            change_summary=summary,
            occurred_at=r["occurred_at"].astimezone(timezone.utc).isoformat(),
        )
        assert verify_audit_payload(identity.verify_key, payload, r["signed_payload"]) is True


# ---------------------------------------------------------------------------
# NATS event firing
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_fires_nats_event(
    registry: DescriptorRegistry,
    nats_store: NatsStore,
):
    desc_id = f"t_evt_{uuid4().hex[:8]}"
    async with _SubjectCollector(nats_store, f"{DESCRIPTOR_TOPIC_PREFIX}.registered.target.{desc_id}") as col:
        await registry.register(_draft_target(descriptor_id=desc_id), actor="lewis@local")
        await col.wait_for(n=1, timeout=5.0)
    assert len(col.messages) == 1
    subject, payload = col.messages[0]
    assert subject == f"descriptor.registered.target.{desc_id}"
    assert payload["descriptor_id"] == desc_id
    assert payload["actor"] == "lewis@local"
    assert payload["schema_uri"] == "legba/target/2.0.0"
    assert "version" in payload


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dlq_event_fires_on_validation_failure(
    registry: DescriptorRegistry,
    nats_store: NatsStore,
):
    desc_id = f"t_dlqev_{uuid4().hex[:8]}"
    async with _SubjectCollector(
        nats_store, f"{DEAD_LETTER_TOPIC_PREFIX}.target.{desc_id}"
    ) as col:
        bad = _draft_target(
            descriptor_id=desc_id,
            entity_classes=("organization", "totally_not_real"),
        )
        with pytest.raises(DescriptorValidationError):
            await registry.register(bad, actor="lewis@local")
        await col.wait_for(n=1, timeout=5.0)
    assert len(col.messages) == 1
    subject, payload = col.messages[0]
    assert subject == f"legba.dlq.descriptor.target.{desc_id}"
    assert payload["error_kind"] == "vocabulary"
    assert payload["dead_letter_id"] is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_lifecycle_emits_event_per_action(
    registry: DescriptorRegistry,
    nats_store: NatsStore,
):
    desc_id = f"t_full_{uuid4().hex[:8]}"
    async with _SubjectCollector(nats_store, f"descriptor.*.target.{desc_id}") as col:
        v1 = await registry.register(_draft_target(descriptor_id=desc_id), actor="op")
        v2 = await registry.update(
            desc_id,
            _draft_target(descriptor_id=desc_id, relationship_types=("LocatedIn", "PartOf")),
            actor="op",
        )
        await registry.rollback(desc_id, v1.version, actor="op", family=Family.TARGET)
        await registry.promote(desc_id, v2.version, actor="op", family=Family.TARGET)
        await registry.retire(desc_id, actor="op", family=Family.TARGET)
        await col.wait_for(n=5, timeout=10.0)

    subjects = [m[0] for m in col.messages]
    assert any(s.startswith("descriptor.registered.") for s in subjects)
    assert any(s.startswith("descriptor.updated.") for s in subjects)
    assert any(s.startswith("descriptor.rolled_back.") for s in subjects)
    assert any(s.startswith("descriptor.promoted.") for s in subjects)
    assert any(s.startswith("descriptor.retired.") for s in subjects)


# ---------------------------------------------------------------------------
# Vocabulary cache live refresh via NATS
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vocabulary_cache_reload_after_runtime_insert(
    registry: DescriptorRegistry,
    pg_store: PostgresStore,
    nats_store: NatsStore,
):
    """Insert a new vocabulary row, publish vocabulary.updated, the cache
    refreshes and the new value validates."""
    new_value = f"runtime_added_class_{uuid4().hex[:6]}"
    async with pg_store.acquire() as conn:
        await conn.execute(
            "INSERT INTO vocabulary_entries (family, value) VALUES ($1, $2)",
            "entity_class",
            new_value,
        )
    # Notify the cache (the registry subscribed in start()).
    await nats_store.nc.publish(
        "vocabulary.updated.entity_class", b"{}"
    )
    # Give the subscription handler a tick.
    await asyncio.sleep(0.3)

    desc = _draft_target(
        descriptor_id=f"t_voc_{uuid4().hex[:8]}",
        entity_classes=("organization", new_value),
    )
    row = await registry.register(desc, actor="lewis@local")
    assert row.is_head is True


# ---------------------------------------------------------------------------
# Descriptor ID mismatch on update
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_with_mismatched_id_raises(
    registry_no_nats: DescriptorRegistry,
):
    desc_id = f"t_mm_{uuid4().hex[:8]}"
    await registry_no_nats.register(
        _draft_target(descriptor_id=desc_id), actor="lewis@local"
    )
    other = _draft_target(descriptor_id="different_id")
    with pytest.raises(DescriptorValidationError):
        await registry_no_nats.update(desc_id, other, actor="lewis@local")


# ---------------------------------------------------------------------------
# Audit / DLQ list helpers
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_audit_log_for_returns_history(
    registry_no_nats: DescriptorRegistry,
):
    desc_id = f"t_auditq_{uuid4().hex[:8]}"
    await registry_no_nats.register(_draft_target(descriptor_id=desc_id), actor="lewis@local")
    await registry_no_nats.retire(desc_id, actor="lewis@local", family=Family.TARGET)
    entries = await registry_no_nats.audit_log_for(desc_id, family=Family.TARGET)
    actions = [e["action"] for e in entries]
    # newest first per the SQL ORDER BY
    assert "retire" in actions
    assert "register" in actions


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dead_letter_for_lists_open_entries(
    registry_no_nats: DescriptorRegistry,
):
    desc_id = f"t_dlqlist_{uuid4().hex[:8]}"
    bad = _draft_target(
        descriptor_id=desc_id,
        entity_classes=("organization", f"ufo_{uuid4().hex[:6]}"),
    )
    with pytest.raises(DescriptorValidationError):
        await registry_no_nats.register(bad, actor="lewis@local")
    entries = await registry_no_nats.dead_letter_for(family=Family.TARGET)
    assert any(e["namespace"] == "target" for e in entries)


# ---------------------------------------------------------------------------
# L-244: public `pg` accessor
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registry_exposes_public_pg_accessor(
    registry_no_nats: DescriptorRegistry, pg_store: PostgresStore,
):
    """`registry.pg` is the same pool object that was passed in at construction
    time. Callers can issue raw SQL through it without reaching into
    `_pg`."""
    assert registry_no_nats.pg is pg_store
    async with registry_no_nats.pg.acquire() as conn:
        rows = await conn.fetch(
            "SELECT COUNT(*) AS n FROM vocabulary_entries WHERE family = $1",
            "entity_class",
        )
        # Seed migration inserts the 9 entity_classes; count matches the
        # seed unless a previous test added more — assert >= 9.
        assert rows[0]["n"] >= 9


# ---------------------------------------------------------------------------
# L-241: AnalystKind open-extension end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_analyst_kind_registry_seeded_from_vocab_on_start(
    registry_no_nats: DescriptorRegistry,
    pg_store: PostgresStore,
):
    """Inserting a `vocabulary_entries` row with `family='analyst_kind'`
    and calling `sync_analyst_kinds()` makes the kind valid for the
    typed schema."""
    from legba.data.schemas import (
        ANALYST_KIND_REGISTRY, is_known_analyst_kind,
    )

    extension_value = f"runtime_kind_{uuid4().hex[:6]}"
    # Snapshot to restore at teardown so we don't poison other tests.
    snapshot = ANALYST_KIND_REGISTRY.extension_values()
    try:
        async with pg_store.acquire() as conn:
            await conn.execute(
                "INSERT INTO vocabulary_entries (family, value) VALUES ($1, $2)",
                "analyst_kind",
                extension_value,
            )
        # The registry's seed-mirror runs on start(); pull again to pick up
        # the new row.
        await registry_no_nats.sync_analyst_kinds()
        assert is_known_analyst_kind(extension_value)

        # Build an analyst descriptor with the extension kind directly.
        from legba.data.schemas import (
            AnalystDescriptor, AnalystIdentity, MethodBlock,
            SubscriptionBlock, CadenceBlock, TypeSignature,
        )
        ext_id = f"a_ext_{uuid4().hex[:8]}"
        ext_descriptor = AnalystDescriptor(
            identity=AnalystIdentity(
                id=ext_id,
                name="Extension-kind analyst",
                schema_uri="legba/analyst/2.0.0",
                version="0" * 16,
                kind=extension_value,
                type_signature=TypeSignature(
                    input_type="legba.x.In", output_type="legba.x.Out",
                ),
                owner="lewis@local",
            ),
            subscription=SubscriptionBlock(),
            method=MethodBlock(
                kind="llm_planner", prompt_module="legba.prompts.x",
            ),
            cadence=CadenceBlock(),
        )
        row = await registry_no_nats.register(ext_descriptor, actor="lewis@local")
        assert row.is_head is True
        assert row.kind == extension_value

        # The body in the row reflects the extension kind too.
        assert row.body["identity"]["kind"] == extension_value
        # And re-fetching via get_typed round-trips through the registry.
        typed = await registry_no_nats.get_typed(ext_id, family=Family.ANALYST)
        assert typed.identity.kind == extension_value
    finally:
        ANALYST_KIND_REGISTRY.replace_extensions(snapshot)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_analyst_kind_extension_survives_via_nats_refresh(
    registry: DescriptorRegistry,
    pg_store: PostgresStore,
    nats_store: NatsStore,
):
    """Inserting a `vocabulary_entries` row for `analyst_kind` and emitting
    a `vocabulary.updated.analyst_kind` NATS message refreshes both the
    vocab cache AND the analyst-kind registry."""
    from legba.data.schemas import (
        ANALYST_KIND_REGISTRY, is_known_analyst_kind,
    )

    extension_value = f"nats_kind_{uuid4().hex[:6]}"
    snapshot = ANALYST_KIND_REGISTRY.extension_values()
    try:
        async with pg_store.acquire() as conn:
            await conn.execute(
                "INSERT INTO vocabulary_entries (family, value) VALUES ($1, $2)",
                "analyst_kind",
                extension_value,
            )
        await nats_store.nc.publish(
            "vocabulary.updated.analyst_kind", b"{}"
        )
        # Let the subscription handler tick.
        for _ in range(20):
            await asyncio.sleep(0.1)
            if is_known_analyst_kind(extension_value):
                break
        assert is_known_analyst_kind(extension_value), (
            "expected NATS refresh to seed analyst_kind into the registry"
        )
    finally:
        ANALYST_KIND_REGISTRY.replace_extensions(snapshot)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_consult_on_demand_now_registrable(
    registry_no_nats: DescriptorRegistry,
):
    """L-241: `consult_on_demand` is one of the 9 built-ins; an analyst
    descriptor with that kind registers cleanly."""
    from legba.data.schemas import AnalystKind

    desc_id = f"a_cod_{uuid4().hex[:8]}"
    desc = _draft_analyst(descriptor_id=desc_id, kind=AnalystKind.CONSULT_ON_DEMAND)
    row = await registry_no_nats.register(desc, actor="lewis@local")
    assert row.kind == "consult_on_demand"


# ---------------------------------------------------------------------------
# K-3 — descriptor string references are resolved at registration
# ---------------------------------------------------------------------------


def _analyst_with_prompt_module(
    *,
    descriptor_id: str,
    prompt_module: str,
    state: LifecycleState = LifecycleState.DRAFT,
) -> AnalystDescriptor:
    """Same shape as `_draft_analyst`, with the prompt reference under test."""
    identity = AnalystIdentity(
        id=descriptor_id,
        name=f"Analyst {descriptor_id}",
        schema_uri="legba/analyst/2.0.0",
        version="0" * 16,
        kind=AnalystKind.INLINE_TARGET,
        state=state,
        type_signature=TypeSignature(
            input_type="legba.x.In",
            output_type="legba.x.Out",
        ),
        owner="lewis@local",
    )
    return AnalystDescriptor(
        identity=identity,
        subscription=SubscriptionBlock(),
        method=MethodBlock(kind="llm_planner", prompt_module=prompt_module),
        cadence=CadenceBlock(),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_active_analyst_naming_a_dead_prompt_module_is_refused(
    registry_no_nats: DescriptorRegistry,
):
    """K-3 — the registry now resolves what a descriptor names.

    Nothing checked these strings before: pydantic asserts `prompt_module` is
    a non-empty string and never resolves it, so a descriptor naming a renamed
    module registered cleanly, went active, and surfaced later as an analyst
    reasoning with the wrong prompt. This drives the real `register()` path,
    not the resolver in isolation.
    """
    desc_id = f"a_deadref_{uuid4().hex[:8]}"
    bad = _analyst_with_prompt_module(
        descriptor_id=desc_id,
        prompt_module="legba.prompts.renamed_away_by_a_moves_wave.v1",
        state=LifecycleState.ACTIVE,
    )
    with pytest.raises(DescriptorValidationError) as exc_info:
        await registry_no_nats.register(bad, actor="lewis@local")

    assert "renamed_away_by_a_moves_wave" in str(exc_info.value)
    assert exc_info.value.dead_letter_id is not None
    unresolved = exc_info.value.validation_error["unresolved"]
    assert unresolved[0]["field"] == "method.prompt_module"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_active_analyst_naming_a_live_prompt_module_registers(
    registry_no_nats: DescriptorRegistry,
):
    """The gate must not reject a descriptor whose reference is real."""
    desc_id = f"a_liveref_{uuid4().hex[:8]}"
    good = _analyst_with_prompt_module(
        descriptor_id=desc_id,
        prompt_module="legba.prompts.inline_target.v1",
        state=LifecycleState.ACTIVE,
    )
    row = await registry_no_nats.register(good, actor="lewis@local")
    assert row.descriptor_id == desc_id
    assert row.state == "active"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dead_reference_on_a_non_binding_state_warns_but_stores(
    registry_no_nats: DescriptorRegistry,
):
    """A draft may name something that is not there yet.

    Fatal-in-every-state would block the act of parking or retiring a
    descriptor whose module has already been deleted — so the check is fatal
    only where the runtime will actually bind and follow the reference.
    """
    desc_id = f"a_draftref_{uuid4().hex[:8]}"
    draft = _analyst_with_prompt_module(
        descriptor_id=desc_id,
        prompt_module="legba.prompts.not_written_yet.v1",
        state=LifecycleState.DRAFT,
    )
    row = await registry_no_nats.register(draft, actor="lewis@local")
    assert row.descriptor_id == desc_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_activating_a_descriptor_with_a_dead_reference_is_refused(
    registry_no_nats: DescriptorRegistry,
):
    """The lifecycle transition is the moment the reference starts mattering.

    `POST /transition` re-stamps `identity.state` and goes through
    `registry.update()`, so the same check that lets a draft through must stop
    it at the activation boundary.
    """
    desc_id = f"a_activate_{uuid4().hex[:8]}"

    def _at(state: LifecycleState) -> AnalystDescriptor:
        return _analyst_with_prompt_module(
            descriptor_id=desc_id,
            prompt_module="legba.prompts.not_written_yet.v1",
            state=state,
        )

    # draft -> configured is legal and both are non-binding, so the dead
    # reference rides along as a warning.
    await registry_no_nats.register(_at(LifecycleState.DRAFT), actor="lewis@local")
    await registry_no_nats.update(
        desc_id, _at(LifecycleState.CONFIGURED), actor="lewis@local",
    )

    # configured -> active is where the runtime starts following the string.
    with pytest.raises(DescriptorValidationError) as exc_info:
        await registry_no_nats.update(
            desc_id, _at(LifecycleState.ACTIVE), actor="lewis@local",
        )
    assert "not_written_yet" in str(exc_info.value)
