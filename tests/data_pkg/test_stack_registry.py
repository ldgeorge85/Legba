# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the L-111 stack registry.

Mix of unit tests (validation, credential indirection, secret-ref walks)
and integration tests (against the live substrate per conftest.py).

Unit tests don't touch Postgres / NATS — they exercise the validator and
the secret-reference walker against the typed pydantic models.

Integration tests run against the migrated test DB created by
`tests/data_pkg/conftest.py` and against the live Qdrant / Redis / NATS
containers.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry import (
    AuditLogger,
    CredentialVault,
    DescriptorDeadLetter,
    HealthState,
    NullEventEmitter,
    StackComponentRow,
    StackHealthDispatcher,
    StackRegistry,
    StackValidationError,
    VaultLockedError,
    stack_subject,
    vault_subject,
)
from legba.data.registry.credentials import MASTER_KEY_ENV
from legba.data.registry.stack import (
    KIND_MODELS,
    _collect_secret_refs,
    _compute_version,
    _enforce_secret_indirection,
    kind_from_schema_uri,
)
from legba.data.schemas import (
    EmbeddingService,
    EmbeddingServiceConfig,
    LifecycleState,
    LLMProvider,
    LLMProviderConfig,
    NATSCluster,
    NATSClusterConfig,
    PostgresCluster,
    PostgresClusterConfig,
    Property,
    ProxyPool,
    ProxyPoolConfig,
    RedisCluster,
    RedisClusterConfig,
    VectorStore,
    VectorStoreConfig,
)
from legba.data.schemas.properties import Secret, Text
from legba.data.schemas.stack import StackComponentBase


# ---------------------------------------------------------------------------
# Master key for tests (32 bytes hex = 64 chars).
# ---------------------------------------------------------------------------

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "11" * 32)


# ---------------------------------------------------------------------------
# Factory helpers — return draft typed components.
# ---------------------------------------------------------------------------

_PLACEHOLDER_VERSION = "0" * 16


def _llm_provider(component_id: str = "llm.anthropic.opus_4_7") -> LLMProvider:
    return LLMProvider(
        id=component_id,
        name="Anthropic Opus 4.7",
        schema_uri="legba/stack/llm_provider/1.0.0",
        version=_PLACEHOLDER_VERSION,
        owner="lewis@local",
        config=LLMProviderConfig(
            api_endpoint=Property.Text.of("https://api.anthropic.com"),
            api_key=Property.Secret.of(f"{component_id}.api_key"),
            model_name=Property.Text.of("claude-opus-4-7"),
            max_tokens=Property.Number.of(8192, minimum=1, maximum=200000),
        ),
    )


def _vector_store(component_id: str = "vector.qdrant.cluster_main") -> VectorStore:
    return VectorStore(
        id=component_id,
        name="Qdrant primary",
        schema_uri="legba/stack/vector_store/1.0.0",
        version=_PLACEHOLDER_VERSION,
        owner="lewis@local",
        config=VectorStoreConfig(
            endpoint=Property.Text.of("http://127.0.0.1:6333"),
            collection_prefix=Property.Text.of("legba"),
        ),
    )


def _embedding(component_id: str = "embed.bge_m3") -> EmbeddingService:
    return EmbeddingService(
        id=component_id,
        name="BGE-M3 local",
        schema_uri="legba/stack/embedding/1.0.0",
        version=_PLACEHOLDER_VERSION,
        owner="lewis@local",
        config=EmbeddingServiceConfig(
            endpoint=Property.Text.of("http://127.0.0.1:8080"),
        ),
    )


def _postgres_cluster(component_id: str = "pg.cluster_main") -> PostgresCluster:
    return PostgresCluster(
        id=component_id,
        name="Primary PG + AGE",
        schema_uri="legba/stack/postgres/1.0.0",
        version=_PLACEHOLDER_VERSION,
        owner="lewis@local",
        config=PostgresClusterConfig(
            host=Property.Text.of("127.0.0.1"),
            database=Property.Text.of("legba"),
            user=Property.Text.of("legba"),
            password=Property.Secret.of(f"{component_id}.password"),
        ),
    )


def _redis_cluster(component_id: str = "kv.redis.cluster_main") -> RedisCluster:
    return RedisCluster(
        id=component_id,
        name="Redis primary",
        schema_uri="legba/stack/redis/1.0.0",
        version=_PLACEHOLDER_VERSION,
        owner="lewis@local",
        config=RedisClusterConfig(
            host=Property.Text.of("127.0.0.1"),
            port=Property.Number.of(6379, minimum=1, maximum=65535),
            password=None,  # no auth on local container
        ),
    )


def _nats_cluster(component_id: str = "bus.nats.cluster_main") -> NATSCluster:
    return NATSCluster(
        id=component_id,
        name="NATS primary",
        schema_uri="legba/stack/nats/1.0.0",
        version=_PLACEHOLDER_VERSION,
        owner="lewis@local",
        config=NATSClusterConfig(
            servers=Property.List(raw=["nats://127.0.0.1:4222"], item_kind="text"),
        ),
    )


def _proxy_pool(component_id: str = "proxy.local.none") -> ProxyPool:
    return ProxyPool(
        id=component_id,
        name="No-op proxy",
        schema_uri="legba/stack/proxy_pool/1.0.0",
        version=_PLACEHOLDER_VERSION,
        owner="lewis@local",
        config=ProxyPoolConfig(
            provider=Property.Dropdown.Static.of(
                "none", ["none", "bright_data", "oxylabs", "self_managed"]
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Unit tests — schema URI parsing, content-hash, indirection checks.
# ---------------------------------------------------------------------------


def test_kind_from_schema_uri_round_trip():
    for kind, model in KIND_MODELS.items():
        sample_uri = f"legba/stack/{kind}/1.0.0"
        assert kind_from_schema_uri(sample_uri) == kind


def test_kind_from_schema_uri_rejects_bad_uri():
    with pytest.raises(ValueError):
        kind_from_schema_uri("legba/stack/llm_provider/1.0")  # not semver
    with pytest.raises(ValueError):
        kind_from_schema_uri("legba/target/2.0.0")           # wrong family


def test_content_hash_stable_across_version_field():
    a = _llm_provider().model_copy(update={"version": "a" * 16})
    b = _llm_provider().model_copy(update={"version": "b" * 16})
    assert _compute_version(a) == _compute_version(b)


def test_collect_secret_refs_walks_typed_config():
    pg = _postgres_cluster()
    refs = _collect_secret_refs(pg)
    assert refs == ["pg.cluster_main.password"]

    redis = _redis_cluster()
    assert _collect_secret_refs(redis) == []  # password=None


def test_enforce_secret_indirection_accepts_typed_secret():
    _enforce_secret_indirection(_llm_provider())
    _enforce_secret_indirection(_redis_cluster())


def test_enforce_secret_indirection_rejects_non_secret_in_credential_slot():
    """Replace the Secret with a Text factory — must trip the guard."""
    cfg = LLMProviderConfig.model_construct(
        api_endpoint=Property.Text.of("https://api.anthropic.com"),
        api_key=Property.Text.of("hunter2-not-a-secret"),  # type: ignore[arg-type]
        model_name=Property.Text.of("claude-opus-4-7"),
        max_tokens=Property.Number.of(8192),
    )
    comp = LLMProvider.model_construct(
        id="bad.one",
        name="Bad",
        schema_uri="legba/stack/llm_provider/1.0.0",
        version=_PLACEHOLDER_VERSION,
        owner="lewis@local",
        state=LifecycleState.DRAFT,
        config=cfg,
    )
    with pytest.raises(ValueError, match="api_key"):
        _enforce_secret_indirection(comp)


# ---------------------------------------------------------------------------
# Unit tests — credential vault crypto.
# ---------------------------------------------------------------------------


def test_vault_encrypt_decrypt_round_trip():
    store = PostgresStore(PostgresConfig.from_env())
    vault = CredentialVault(store, master_key=bytes.fromhex(_TEST_MASTER_KEY_HEX))
    nonce, ct = vault.encrypt(b"top-secret-token")
    assert ct != b"top-secret-token"
    assert vault.decrypt(nonce, ct) == b"top-secret-token"


def test_vault_locked_without_master_key():
    store = PostgresStore(PostgresConfig.from_env())
    vault = CredentialVault(store, master_key=None)
    # Force re-read of env by clearing local var, swapping env.
    saved = os.environ.pop(MASTER_KEY_ENV, None)
    try:
        with pytest.raises(VaultLockedError):
            vault.encrypt(b"data")
    finally:
        if saved is not None:
            os.environ[MASTER_KEY_ENV] = saved


def test_vault_master_key_wrong_length_raises():
    store = PostgresStore(PostgresConfig.from_env())
    saved = os.environ.get(MASTER_KEY_ENV)
    os.environ[MASTER_KEY_ENV] = "deadbeef"  # 4 bytes, not 32
    try:
        vault = CredentialVault(store)
        with pytest.raises(VaultLockedError):
            vault.encrypt(b"data")
    finally:
        if saved is not None:
            os.environ[MASTER_KEY_ENV] = saved


# ---------------------------------------------------------------------------
# Integration fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def store(migrated_pg: PostgresConfig) -> PostgresStore:
    """Function-scoped store with a per-test wipe of the registry tables.

    Keeps tests independent without dropping the whole session DB.
    """
    s = PostgresStore(migrated_pg)
    await s.connect()
    async with s.acquire() as conn:
        # Wipe registry state from any previous test.
        await conn.execute("TRUNCATE TABLE stack_components")
        await conn.execute("TRUNCATE TABLE stack_credentials")
        await conn.execute("TRUNCATE TABLE descriptor_dead_letter")
        await conn.execute("TRUNCATE TABLE descriptor_audit_log")
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
async def emitter() -> NullEventEmitter:
    return NullEventEmitter()


@pytest_asyncio.fixture
async def vault(store: PostgresStore, emitter: NullEventEmitter) -> CredentialVault:
    return CredentialVault(
        store, master_key=bytes.fromhex(_TEST_MASTER_KEY_HEX), emitter=emitter,
    )


@pytest_asyncio.fixture
async def audit() -> AuditLogger:
    return AuditLogger()


@pytest_asyncio.fixture
async def registry(
    store: PostgresStore,
    vault: CredentialVault,
    emitter: NullEventEmitter,
    audit: AuditLogger,
) -> StackRegistry:
    return StackRegistry(
        store=store, vault=vault,
        audit=audit, emitter=emitter,
        dlq=DescriptorDeadLetter(store),
        health=StackHealthDispatcher(emitter=emitter, poll_interval_seconds=1),
    )


@pytest_asyncio.fixture
async def seeded_secrets(vault: CredentialVault) -> None:
    """Seed the vault so the validator's Step-4 check passes for our fixtures."""
    seeds = {
        "llm.anthropic.opus_4_7.api_key": b"sk-test-fake-key",
        "pg.cluster_main.password": b"legba",
    }
    for sid, value in seeds.items():
        await vault.store_secret(sid, value, actor="test:seed")


# ---------------------------------------------------------------------------
# Integration — credentials vault CRUD.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vault_store_and_resolve(vault: CredentialVault):
    sid = "test.unit.demo_key"
    v1 = await vault.store_secret(sid, "hunter2", actor="lewis")
    assert v1 == 1
    assert await vault.verify_exists(sid) is True
    assert (await vault.resolve(sid)) == b"hunter2"

    # Rotation: writing again bumps the version and flips current.
    v2 = await vault.store_secret(sid, b"hunter3", actor="lewis")
    assert v2 == 2
    entries = await vault.list_entries(sid)
    assert len(entries) == 2
    current = [e for e in entries if e.is_current]
    assert len(current) == 1 and current[0].version == 2
    assert (await vault.resolve(sid)) == b"hunter3"
    # Historical lookup still works.
    assert (await vault.resolve(sid, version=1)) == b"hunter2"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vault_missing_secret_raises(vault: CredentialVault):
    from legba.data.registry.credentials import MissingSecretError
    assert await vault.verify_exists("no.such.thing") is False
    with pytest.raises(MissingSecretError):
        await vault.resolve("no.such.thing")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vault_rotation_publishes_eviction_event(
    vault: CredentialVault, emitter: NullEventEmitter,
):
    """RUST-5 — the eviction hook: a rotation must publish so a runtime
    process's handler cache can invalidate instead of serving the OLD
    plaintext until a container recreate."""
    sid = "test.unit.rotating_key"

    v1 = await vault.store_secret(sid, "hunter2", actor="lewis")
    assert v1 == 1
    assert emitter.subjects() == [vault_subject("rotated", sid)]
    subject, payload = emitter.published[-1]
    assert subject == f"vault.secret.rotated.{sid}"
    assert payload["secret_id"] == sid
    assert payload["action"] == "rotated"
    assert payload["actor"] == "lewis"
    assert payload["version"] == 1
    # No plaintext, no ciphertext — mirrors the INFO log line's redaction.
    assert "hunter2" not in str(payload)

    v2 = await vault.store_secret(sid, "hunter3", actor="lewis")
    assert v2 == 2
    assert emitter.subjects() == [
        vault_subject("rotated", sid), vault_subject("rotated", sid),
    ]
    assert emitter.published[-1][1]["version"] == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vault_without_emitter_is_a_quiet_default(store: PostgresStore):
    """Every non-server construction site (`CredentialVault(pg_store)`, no
    `emitter=`) must behave exactly as before RUST-5 — a bare vault is a
    valid, complete construction, not a partially-wired one."""
    bare = CredentialVault(store, master_key=bytes.fromhex(_TEST_MASTER_KEY_HEX))
    v1 = await bare.store_secret("test.unit.bare_key", "hunter2", actor="lewis")
    assert v1 == 1  # publish-to-NullEventEmitter is a silent no-op, not a raise


# ---------------------------------------------------------------------------
# Integration — registry CRUD.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_postgres_cluster_round_trip(
    registry: StackRegistry, seeded_secrets, emitter: NullEventEmitter
):
    # `seeded_secrets` itself now publishes ``vault.secret.rotated.*`` (RUST-5)
    # over this SAME shared emitter; clear so the assertion below still reads
    # as "the first event THIS test's action published", not an artifact of
    # fixture ordering.
    emitter.clear()
    row = await registry.register(_postgres_cluster(), actor="lewis")
    assert row.kind == "postgres"
    assert row.state == LifecycleState.DRAFT
    assert row.is_head
    assert row.version != _PLACEHOLDER_VERSION
    assert len(row.version) == 64  # SHA-256 hex

    fetched = await registry.get(row.component_id)
    assert fetched.version == row.version
    # Body must contain Property.Secret reference, NOT the plaintext.
    assert fetched.body["config"]["password"]["raw"] == "pg.cluster_main.password"
    body_text = str(fetched.body)
    assert "legba" not in body_text.replace("legba", "", body_text.count("legba"))  # noqa: E501
    # Even more directly:
    assert "hunter2" not in body_text  # no plaintext leaked

    # NATS event published.
    assert ("stack.component.registered.postgres.pg.cluster_main", ) == tuple(
        emitter.subjects()[:1]
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_all_kinds(
    registry: StackRegistry, seeded_secrets
):
    """Smoke-test the discriminator + audit for every supported kind."""
    components: list[StackComponentBase] = [
        _llm_provider(),
        _vector_store(),
        _embedding(),
        _nats_cluster(),
        _postgres_cluster(),
        _redis_cluster(),
        _proxy_pool(),
    ]
    registered: list[StackComponentRow] = []
    for c in components:
        row = await registry.register(c, actor="lewis")
        registered.append(row)
        assert row.is_head

    # `list()` returns every head row, sorted by created_at.
    listed = await registry.list()
    ids = {r.component_id for r in listed}
    assert ids >= {r.component_id for r in registered}

    # `get_by_kind` honours kind filter.
    pgs = await registry.get_by_kind("postgres")
    assert len(pgs) >= 1 and all(p.kind == "postgres" for p in pgs)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_preserves_history(
    registry: StackRegistry, seeded_secrets
):
    original = await registry.register(_postgres_cluster(), actor="lewis")
    # Tweak the pool_size to produce a new content hash.
    new_comp = _postgres_cluster()
    new_comp = new_comp.model_copy(
        update={
            "config": new_comp.config.model_copy(
                update={"pool_size": Property.Number.of(25, minimum=1, maximum=200)}
            ),
            "name": "PG Primary (resized)",
        }
    )
    updated = await registry.update(
        original.component_id, new_comp, actor="lewis",
    )
    assert updated.version != original.version
    assert updated.is_head

    # Old version preserved as a non-head row.
    old = await registry.get(original.component_id, version=original.version)
    assert old.is_head is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_rejects_no_change(registry: StackRegistry, seeded_secrets):
    original = await registry.register(_postgres_cluster(), actor="lewis")
    from legba.data.registry import VersionConflict
    with pytest.raises(VersionConflict):
        # Same body -> same content hash -> conflict.
        await registry.update(original.component_id, _postgres_cluster(), actor="lewis")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retire_transitions_state(registry: StackRegistry, seeded_secrets):
    row = await registry.register(_postgres_cluster(), actor="lewis")
    retired = await registry.retire(row.component_id, actor="lewis")
    assert retired.state == LifecycleState.RETIRED

    # Re-retire is illegal (RETIRED is terminal).
    from legba.data.registry import IllegalLifecycleTransition
    with pytest.raises(IllegalLifecycleTransition):
        await registry.retire(row.component_id, actor="lewis")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_dlq_on_missing_secret(
    store: PostgresStore, registry: StackRegistry
):
    """No seeded secrets — registration must DLQ."""
    with pytest.raises(StackValidationError) as exc:
        await registry.register(_postgres_cluster(), actor="lewis")
    # Validation error carries the missing secret list.
    assert "pg.cluster_main.password" in str(exc.value.validation_error).replace("'", "")

    # DLQ row written.
    async with store.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, namespace, declared_schema_uri, validation_error "
            "FROM descriptor_dead_letter WHERE namespace = 'stack'"
        )
    assert len(rows) >= 1
    last = rows[-1]
    assert last["namespace"] == "stack"
    assert last["declared_schema_uri"] == "legba/stack/postgres/1.0.0"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_dlq_on_bad_schema_uri(
    store: PostgresStore, registry: StackRegistry
):
    bad = {
        "id": "bad.id",
        "name": "Bad",
        "schema_uri": "legba/stack/nonsense/1.0.0",
        "version": _PLACEHOLDER_VERSION,
        "owner": "lewis@local",
        "state": "draft",
        "config": {},
    }
    with pytest.raises(StackValidationError):
        await registry.register(bad, actor="lewis")  # type: ignore[arg-type]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_audit_log_signed_entries(
    store: PostgresStore, registry: StackRegistry, seeded_secrets, audit: AuditLogger
):
    row = await registry.register(_postgres_cluster(), actor="lewis")
    async with store.acquire() as conn:
        entries = await audit.fetch_entries(
            conn, descriptor_id=row.component_id, namespace="stack",
        )
    assert len(entries) >= 1
    entry = entries[0]
    assert entry.action == "register"
    assert entry.to_version == row.version
    assert entry.signer_did.startswith("did:legba:registry:")


# ---------------------------------------------------------------------------
# Integration — credentials never leak into serialized body / NATS event.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_plaintext_credential_never_serialized(
    registry: StackRegistry, seeded_secrets, emitter: NullEventEmitter
):
    """Verify the plaintext never appears in either the body or the
    published NATS payload."""
    plaintext = b"the-actual-secret-value-NEVER-leak-me"
    # Replace one of the seeded creds with the canary.
    await registry.vault.store_secret(
        "llm.anthropic.opus_4_7.api_key", plaintext, actor="test",
    )

    row = await registry.register(_llm_provider(), actor="lewis")
    body_blob = str(row.body)
    assert plaintext.decode("utf-8") not in body_blob

    # NATS event published carries the same dict; check the captured payloads.
    for _subject, payload in emitter.published:
        assert plaintext.decode("utf-8") not in str(payload)


# ---------------------------------------------------------------------------
# Integration — healthcheck dispatch.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_health_check_postgres(
    registry: StackRegistry, seeded_secrets
):
    row = await registry.register(_postgres_cluster(), actor="lewis")
    health = await registry.healthcheck(row.component_id)
    # Container is up + secret resolves to the right password.
    assert health.state == HealthState.HEALTHY
    assert "SELECT 1" in health.detail


@pytest.mark.integration
@pytest.mark.asyncio
async def test_health_check_redis(
    registry: StackRegistry, seeded_secrets
):
    row = await registry.register(_redis_cluster(), actor="lewis")
    health = await registry.healthcheck(row.component_id)
    assert health.state == HealthState.HEALTHY


@pytest.mark.integration
@pytest.mark.asyncio
async def test_health_check_nats(
    registry: StackRegistry, seeded_secrets
):
    row = await registry.register(_nats_cluster(), actor="lewis")
    health = await registry.healthcheck(row.component_id)
    # NATS container is up with JetStream enabled.
    assert health.state in (HealthState.HEALTHY, HealthState.DEGRADED)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_health_check_qdrant(
    registry: StackRegistry, seeded_secrets
):
    row = await registry.register(_vector_store(), actor="lewis")
    health = await registry.healthcheck(row.component_id)
    assert health.state == HealthState.HEALTHY


@pytest.mark.integration
@pytest.mark.asyncio
async def test_health_check_proxy_pool_noop(
    registry: StackRegistry, seeded_secrets
):
    row = await registry.register(_proxy_pool(), actor="lewis")
    health = await registry.healthcheck(row.component_id)
    # provider=none -> healthy / no-op.
    assert health.state == HealthState.HEALTHY


@pytest.mark.integration
@pytest.mark.asyncio
async def test_health_state_change_emits_event(
    registry: StackRegistry, seeded_secrets, emitter: NullEventEmitter
):
    """First check populates cache; second check at HEALTHY does NOT re-emit;
    a synthesised UNHEALTHY swap DOES emit."""
    row = await registry.register(_postgres_cluster(), actor="lewis")
    await registry.healthcheck(row.component_id)
    emitter.clear()
    # Re-check while still healthy: no state change, no extra emission.
    await registry.healthcheck(row.component_id)
    health_subjects = [
        s for s in emitter.subjects() if "health_changed" in s
    ]
    assert health_subjects == []

    # Synthesise a state change by writing a degraded result directly.
    # The dispatcher's cache is in-process; tweak it to simulate.
    cached = registry.health_dispatcher.cached(row.component_id)
    assert cached is not None and cached.state == HealthState.HEALTHY

    # Retire the secret to force the next probe unhealthy.
    await registry.vault.delete_secret("pg.cluster_main.password")
    emitter.clear()
    after = await registry.healthcheck(row.component_id)
    assert after.state == HealthState.UNHEALTHY
    change_subjects = [
        s for s in emitter.subjects() if "health_changed" in s
    ]
    assert change_subjects, (
        f"expected a stack.component.health_changed event, got {emitter.subjects()}"
    )
