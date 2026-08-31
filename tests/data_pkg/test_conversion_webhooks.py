# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for L-112 conversion-webhook framework.

Three sections:
  * Unit tests — pure-Python pieces (URI helpers, impl resolution, path
    finding via a stubbed registry, ConversionError construction).
  * Integration tests — live Postgres + NATS via the conftest fixtures.
    Exercise register_webhook / retire_webhook / find_path persistence,
    ConversionExecutor end-to-end, archive writing, DLQ routing, and the
    register_raw / get_typed integration on DescriptorRegistry.

Pattern follows tests/data_pkg/test_registry_descriptor_*.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from nacl.signing import SigningKey

from legba.data.config import NatsConfig, PostgresConfig
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.registry.conversion import (
    CONVERSION_DLQ_PREFIX,
    CONVERSION_TOPIC_PREFIX,
    ConversionError,
    ConversionExecutor,
    ConversionWebhookRegistry,
    WebhookNotFound,
    WebhookValidationError,
    conversion_dlq_subject,
    conversion_subject,
    family_of_uri,
    resolve_impl,
    version_of_uri,
)
from legba.data.registry.descriptor import (
    DescriptorRegistry,
    Family,
)
from legba.data.registry.errors import DescriptorValidationError
from legba.data.registry.signing import SigningIdentity
from legba.data.schemas import (
    AbstractionLevel,
    AnalystDescriptor,
    AnalystIdentity,
    AnalystKind,
    CadenceBlock,
    ConversionWebhook,
    LifecycleState,
    MethodBlock,
    SourceBinding,
    SubscriptionBlock,
    TargetDescriptor,
    TargetIdentity,
    TargetScope,
    TypeSignature,
)


# ===========================================================================
# Unit tests — no live containers
# ===========================================================================


# ---- URI helpers ----------------------------------------------------------


def test_family_of_uri_extracts_prefix():
    assert family_of_uri("legba/target/2.0.0") == "legba/target"
    assert family_of_uri("legba/analyst/3.1.2") == "legba/analyst"
    assert family_of_uri("legba/stack/llm_provider/1.0.0") == "legba/stack/llm_provider"


def test_version_of_uri_parses_semver():
    assert version_of_uri("legba/target/2.0.0") == (2, 0, 0)
    assert version_of_uri("legba/analyst/3.1.7") == (3, 1, 7)


def test_version_of_uri_rejects_malformed():
    with pytest.raises(ValueError):
        version_of_uri("legba/target/2.0")
    with pytest.raises(ValueError):
        version_of_uri("legba/target/notasemver")


# ---- NATS subject naming --------------------------------------------------


def test_conversion_subject_registered_pattern():
    sub = conversion_subject(
        "registered", "legba/target",
        from_uri="legba/target/1.0.0",
        to_uri="legba/target/2.0.0",
    )
    assert sub == f"{CONVERSION_TOPIC_PREFIX}.registered.legba/target.legba_target_1.0.0-legba_target_2.0.0"


def test_conversion_subject_retired_pattern():
    sub = conversion_subject(
        "retired", "legba/target",
        webhook_id="abc-123",
    )
    assert sub == f"{CONVERSION_TOPIC_PREFIX}.retired.legba/target.abc-123"


def test_conversion_subject_executed_pattern():
    sub = conversion_subject(
        "executed", "legba/target",
        descriptor_id="br_energy",
    )
    assert sub == f"{CONVERSION_TOPIC_PREFIX}.executed.legba/target.br_energy"


def test_conversion_subject_rejects_unknown_action():
    with pytest.raises(ValueError):
        conversion_subject("bogus", "legba/target", from_uri="x", to_uri="y")


def test_conversion_dlq_subject_with_and_without_id():
    assert conversion_dlq_subject("legba/target", "x") == (
        f"{CONVERSION_DLQ_PREFIX}.legba/target.x"
    )
    assert conversion_dlq_subject("legba/target", None) == (
        f"{CONVERSION_DLQ_PREFIX}.legba/target.__unknown__"
    )


# ---- impl resolution ------------------------------------------------------


def test_resolve_impl_resolves_valid_dotted_path():
    func = resolve_impl("legba.data.conversions.target_v1_to_v2:convert")
    assert callable(func)
    out = func({"identity": {"schema_uri": "legba/target/1.0.0"}})
    assert out["identity"]["schema_uri"] == "legba/target/2.0.0"


def test_resolve_impl_rejects_missing_colon():
    with pytest.raises(WebhookValidationError):
        resolve_impl("legba.data.conversions.target_v1_to_v2.convert")


def test_resolve_impl_reports_unimportable_module():
    with pytest.raises(WebhookValidationError):
        resolve_impl("legba.no.such.module:convert")


def test_resolve_impl_reports_missing_attribute():
    with pytest.raises(WebhookValidationError):
        resolve_impl("legba.data.conversions.target_v1_to_v2:not_a_function")


# ---- ConversionError shape ------------------------------------------------


def test_conversion_error_to_context_round_trip():
    cause = ValueError("inner")
    err = ConversionError(
        "outer",
        family="legba/target",
        from_uri="legba/target/1.0.0",
        to_uri="legba/target/2.0.0",
        descriptor_id="br_energy",
        path=["legba/target/1.0.0", "legba/target/2.0.0"],
        failed_at_step=0,
        error_kind="webhook_raise",
        original_body={"key": "value"},
        cause=cause,
    )
    ctx = err.to_context()
    assert ctx["kind"] == "conversion"
    assert ctx["family"] == "legba/target"
    assert ctx["descriptor_id"] == "br_energy"
    assert ctx["path_attempted"] == ["legba/target/1.0.0", "legba/target/2.0.0"]
    assert ctx["failed_at_step"] == 0
    assert ctx["error_kind"] == "webhook_raise"
    assert "ValueError" in ctx["cause"]


# ---- Example webhook callable shapes --------------------------------------


def test_example_target_v1_to_v2_drops_legacy_owner_email():
    from legba.data.conversions.target_v1_to_v2 import convert
    out = convert({
        "identity": {"schema_uri": "legba/target/1.0.0", "id": "x"},
        "scope": {"region_codes": ["BR"], "lang": "pt-BR"},
        "legacy_owner_email": "drop_me@example.com",
    })
    assert out["identity"]["schema_uri"] == "legba/target/2.0.0"
    assert "legacy_owner_email" not in out
    # Field renames applied.
    assert out["scope"]["geo"] == ["BR"]
    assert out["scope"]["languages"] == ["pt-BR"]
    assert "region_codes" not in out["scope"]
    assert "lang" not in out["scope"]


def test_example_target_v2_to_v3_renames_horizon():
    from legba.data.conversions.target_v2_to_v3 import convert
    out = convert({
        "identity": {"schema_uri": "legba/target/2.0.0"},
        "scope": {"time_horizon_days": 90},
        "deprecated_metadata_blob": {"foo": "bar"},
    })
    assert out["identity"]["schema_uri"] == "legba/target/3.0.0"
    assert out["scope"]["horizon_days"] == 90
    assert "time_horizon_days" not in out["scope"]
    assert "deprecated_metadata_blob" not in out


# ===========================================================================
# Integration tests — live Postgres + NATS
# ===========================================================================


def _fixed_identity() -> SigningIdentity:
    seed = b"L-112-integ-test-seed-deterministic1"[:32]
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:l112",
    )


@pytest_asyncio.fixture
async def pg_store(migrated_pg: PostgresConfig) -> PostgresStore:
    store = PostgresStore(migrated_pg)
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def nats_store() -> NatsStore:
    store = NatsStore(NatsConfig.from_env())
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def webhook_registry(pg_store: PostgresStore) -> ConversionWebhookRegistry:
    identity = _fixed_identity()
    return ConversionWebhookRegistry(pg_store, signing_identity=identity)


@pytest_asyncio.fixture
async def webhook_registry_with_nats(
    pg_store: PostgresStore,
    nats_store: NatsStore,
) -> ConversionWebhookRegistry:
    identity = _fixed_identity()
    return ConversionWebhookRegistry(
        pg_store, nats_store=nats_store, signing_identity=identity
    )


@pytest_asyncio.fixture
async def executor(
    pg_store: PostgresStore,
    webhook_registry: ConversionWebhookRegistry,
) -> ConversionExecutor:
    return ConversionExecutor(webhook_registry, pg_store)


@pytest_asyncio.fixture
async def executor_with_nats(
    pg_store: PostgresStore,
    nats_store: NatsStore,
    webhook_registry_with_nats: ConversionWebhookRegistry,
) -> ConversionExecutor:
    return ConversionExecutor(
        webhook_registry_with_nats, pg_store, nats_store=nats_store
    )


# ---------------------------------------------------------------------------
# NATS subscription helper (mirrors L-110 pattern)
# ---------------------------------------------------------------------------


class _SubjectCollector:
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
# Webhook CRUD
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_webhook_persists_row_and_returns_active(
    webhook_registry: ConversionWebhookRegistry,
):
    # NOT legba/target/1.0.0 -> 2.0.0 on purpose: that pair is the CANONICAL
    # v1->v2 webhook `_ensure_v1_to_v2_webhook` registers (tolerantly) for
    # the DescriptorRegistry integration tests below (register_raw /
    # get_typed auto-upgrade), and the test DB is per-session, not per-test
    # (see test_list_webhooks_filters above). Under a shuffled order where
    # one of those runs first, register_webhook here would hit "active
    # conversion webhook already exists" instead of exercising a fresh
    # registration — a real 08-27 shuffled failure, root-caused and fixed
    # here by giving this test its own private version range, same as every
    # other integration test in this file.
    w = ConversionWebhook(
        from_uri="legba/target/6.0.0",
        to_uri="legba/target/7.0.0",
        impl="legba.data.conversions.target_v1_to_v2:convert",
    )
    row = await webhook_registry.register_webhook(w, actor="lewis@local")
    assert row.from_uri == "legba/target/6.0.0"
    assert row.to_uri == "legba/target/7.0.0"
    assert row.is_active is True
    assert row.family == "legba/target"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_webhook_rejects_cross_family(
    webhook_registry: ConversionWebhookRegistry,
):
    # Pydantic catches first (model_validator); register_webhook also
    # double-checks. Either layer rejecting is acceptable; pydantic comes
    # first so we go through it.
    with pytest.raises(ValueError):
        ConversionWebhook(
            from_uri="legba/target/1.0.0",
            to_uri="legba/analyst/2.0.0",
            impl="legba.data.conversions.target_v1_to_v2:convert",
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_webhook_rejects_backward_versions(
    webhook_registry: ConversionWebhookRegistry,
):
    w = ConversionWebhook(
        from_uri="legba/target/3.0.0",
        to_uri="legba/target/2.0.0",
        impl="legba.data.conversions.target_v1_to_v2:convert",
    )
    with pytest.raises(WebhookValidationError) as exc:
        await webhook_registry.register_webhook(w, actor="lewis@local")
    assert "forward-only" in str(exc.value)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_webhook_rejects_unimportable_impl(
    webhook_registry: ConversionWebhookRegistry,
):
    w = ConversionWebhook(
        from_uri="legba/target/1.0.0",
        to_uri="legba/target/2.0.0",
        impl="legba.no.such.module:convert",
    )
    with pytest.raises(WebhookValidationError):
        await webhook_registry.register_webhook(w, actor="lewis@local")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_webhook_duplicate_active_rejected(
    webhook_registry: ConversionWebhookRegistry,
):
    w = ConversionWebhook(
        from_uri="legba/target/4.0.0",
        to_uri="legba/target/5.0.0",
        impl="legba.data.conversions.target_v1_to_v2:convert",
    )
    await webhook_registry.register_webhook(w, actor="lewis@local")
    with pytest.raises(WebhookValidationError) as exc:
        await webhook_registry.register_webhook(w, actor="lewis@local")
    assert "active conversion webhook already exists" in str(exc.value)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_webhooks_filters(
    webhook_registry: ConversionWebhookRegistry,
):
    # Register a couple in non-overlapping versions to keep this test
    # isolated from sibling tests (test DB is per-session, not per-test).
    w1 = ConversionWebhook(
        from_uri="legba/target/10.0.0",
        to_uri="legba/target/11.0.0",
        impl="legba.data.conversions.target_v1_to_v2:convert",
    )
    w2 = ConversionWebhook(
        from_uri="legba/target/11.0.0",
        to_uri="legba/target/12.0.0",
        impl="legba.data.conversions.target_v2_to_v3:convert",
    )
    r1 = await webhook_registry.register_webhook(w1, actor="lewis@local")
    r2 = await webhook_registry.register_webhook(w2, actor="lewis@local")

    by_from = await webhook_registry.list_webhooks(from_uri="legba/target/10.0.0")
    assert {r.id for r in by_from} == {r1.id}

    by_family = await webhook_registry.list_webhooks(family="legba/target")
    ids = {r.id for r in by_family}
    assert r1.id in ids and r2.id in ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retire_webhook_soft_retires_and_skips_in_find_path(
    webhook_registry: ConversionWebhookRegistry,
):
    w = ConversionWebhook(
        from_uri="legba/target/20.0.0",
        to_uri="legba/target/21.0.0",
        impl="legba.data.conversions.target_v1_to_v2:convert",
    )
    row = await webhook_registry.register_webhook(w, actor="lewis@local")
    # Path exists.
    path = await webhook_registry.find_path(
        "legba/target", "legba/target/20.0.0", "legba/target/21.0.0"
    )
    assert path == [row.id]

    # Retire.
    retired = await webhook_registry.retire_webhook(
        row.id, actor="lewis@local", reason="superseded"
    )
    assert retired.is_active is False
    assert retired.retired_at is not None

    # Active-only path search now finds nothing.
    path2 = await webhook_registry.find_path(
        "legba/target", "legba/target/20.0.0", "legba/target/21.0.0"
    )
    assert path2 is None

    # Include_retired listing still shows the row.
    listing = await webhook_registry.list_webhooks(
        from_uri="legba/target/20.0.0", include_retired=True
    )
    assert any(r.id == row.id for r in listing)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retire_unknown_webhook_raises(
    webhook_registry: ConversionWebhookRegistry,
):
    with pytest.raises(WebhookNotFound):
        await webhook_registry.retire_webhook(uuid4(), actor="lewis@local")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retire_webhook_idempotent(
    webhook_registry: ConversionWebhookRegistry,
):
    w = ConversionWebhook(
        from_uri="legba/target/22.0.0",
        to_uri="legba/target/23.0.0",
        impl="legba.data.conversions.target_v1_to_v2:convert",
    )
    row = await webhook_registry.register_webhook(w, actor="lewis@local")
    once = await webhook_registry.retire_webhook(row.id, actor="lewis@local")
    twice = await webhook_registry.retire_webhook(row.id, actor="lewis@local")
    assert once.id == twice.id
    assert twice.retired_at is not None


# ---------------------------------------------------------------------------
# Path finding
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_path_single_step(
    webhook_registry: ConversionWebhookRegistry,
):
    w = ConversionWebhook(
        from_uri="legba/target/30.0.0",
        to_uri="legba/target/31.0.0",
        impl="legba.data.conversions.target_v1_to_v2:convert",
    )
    row = await webhook_registry.register_webhook(w, actor="lewis@local")
    path = await webhook_registry.find_path(
        "legba/target", "legba/target/30.0.0", "legba/target/31.0.0"
    )
    assert path == [row.id]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_path_multi_step(
    webhook_registry: ConversionWebhookRegistry,
):
    w1 = ConversionWebhook(
        from_uri="legba/target/40.0.0",
        to_uri="legba/target/41.0.0",
        impl="legba.data.conversions.target_v1_to_v2:convert",
    )
    w2 = ConversionWebhook(
        from_uri="legba/target/41.0.0",
        to_uri="legba/target/42.0.0",
        impl="legba.data.conversions.target_v2_to_v3:convert",
    )
    r1 = await webhook_registry.register_webhook(w1, actor="lewis@local")
    r2 = await webhook_registry.register_webhook(w2, actor="lewis@local")
    path = await webhook_registry.find_path(
        "legba/target", "legba/target/40.0.0", "legba/target/42.0.0"
    )
    assert path == [r1.id, r2.id]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_path_picks_shortest(
    webhook_registry: ConversionWebhookRegistry,
):
    # Register 3-hop chain AND a 1-hop shortcut between the same endpoints.
    w_hop1 = ConversionWebhook(
        from_uri="legba/target/50.0.0",
        to_uri="legba/target/51.0.0",
        impl="legba.data.conversions.target_v1_to_v2:convert",
    )
    w_hop2 = ConversionWebhook(
        from_uri="legba/target/51.0.0",
        to_uri="legba/target/52.0.0",
        impl="legba.data.conversions.target_v2_to_v3:convert",
    )
    w_hop3 = ConversionWebhook(
        from_uri="legba/target/52.0.0",
        to_uri="legba/target/53.0.0",
        impl="legba.data.conversions.target_v1_to_v2:convert",
    )
    w_shortcut = ConversionWebhook(
        from_uri="legba/target/50.0.0",
        to_uri="legba/target/53.0.0",
        impl="legba.data.conversions.target_v1_to_v2:convert",
    )
    await webhook_registry.register_webhook(w_hop1, actor="op")
    await webhook_registry.register_webhook(w_hop2, actor="op")
    await webhook_registry.register_webhook(w_hop3, actor="op")
    short_row = await webhook_registry.register_webhook(w_shortcut, actor="op")

    path = await webhook_registry.find_path(
        "legba/target", "legba/target/50.0.0", "legba/target/53.0.0"
    )
    # Shortest path is the direct shortcut.
    assert path == [short_row.id]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_path_returns_none_when_unreachable(
    webhook_registry: ConversionWebhookRegistry,
):
    path = await webhook_registry.find_path(
        "legba/target", "legba/target/60.0.0", "legba/target/61.0.0"
    )
    assert path is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_path_same_uri_is_empty(
    webhook_registry: ConversionWebhookRegistry,
):
    # No conversion needed when from == to.
    path = await webhook_registry.find_path(
        "legba/target", "legba/target/70.0.0", "legba/target/70.0.0"
    )
    assert path == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_path_rejects_cross_family(
    webhook_registry: ConversionWebhookRegistry,
):
    path = await webhook_registry.find_path(
        "legba/target",
        "legba/target/80.0.0",
        "legba/analyst/80.0.0",
    )
    assert path is None


# ---------------------------------------------------------------------------
# ConversionExecutor — single + multi step
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_executor_single_step_returns_upgraded_body(
    webhook_registry: ConversionWebhookRegistry,
    executor: ConversionExecutor,
):
    w = ConversionWebhook(
        from_uri="legba/target/100.0.0",
        to_uri="legba/target/101.0.0",
        impl="legba.data.conversions.target_v1_to_v2:convert",
    )
    await webhook_registry.register_webhook(w, actor="op")

    body = {
        "identity": {"schema_uri": "legba/target/100.0.0", "id": "x"},
        "scope": {"region_codes": ["BR"], "lang": "pt-BR"},
        "legacy_owner_email": "drop@example.com",
    }
    converted = await executor.convert(
        body,
        family="legba/target",
        from_uri="legba/target/100.0.0",
        to_uri="legba/target/101.0.0",
        descriptor_id="x",
        actor="op",
    )
    # Sample webhook hard-codes to_uri = legba/target/2.0.0 — that's just
    # the example. The executor doesn't enforce the to_uri inside the body
    # matches the path's to_uri (different concerns: schema rewrite is the
    # webhook's job; the framework's job is to walk the graph).
    assert converted.path_uri_chain == [
        "legba/target/100.0.0", "legba/target/101.0.0",
    ]
    assert len(converted.path_webhook_ids) == 1
    assert "legacy_owner_email" not in converted.body
    assert converted.body["scope"]["geo"] == ["BR"]
    assert len(converted.archived_legacy_fields) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_executor_multi_step_walks_full_chain(
    webhook_registry: ConversionWebhookRegistry,
    executor: ConversionExecutor,
):
    await webhook_registry.register_webhook(
        ConversionWebhook(
            from_uri="legba/target/110.0.0",
            to_uri="legba/target/111.0.0",
            impl="legba.data.conversions.target_v1_to_v2:convert",
        ),
        actor="op",
    )
    await webhook_registry.register_webhook(
        ConversionWebhook(
            from_uri="legba/target/111.0.0",
            to_uri="legba/target/112.0.0",
            impl="legba.data.conversions.target_v2_to_v3:convert",
        ),
        actor="op",
    )

    body = {
        "identity": {"schema_uri": "legba/target/110.0.0"},
        "scope": {
            "region_codes": ["BR"], "lang": "pt-BR",
            "time_horizon_days": 90,
        },
        "legacy_owner_email": "x@y",
        "deprecated_metadata_blob": {"a": 1},
    }
    converted = await executor.convert(
        body,
        family="legba/target",
        from_uri="legba/target/110.0.0",
        to_uri="legba/target/112.0.0",
        descriptor_id="multi",
        actor="op",
    )
    assert converted.path_uri_chain == [
        "legba/target/110.0.0", "legba/target/111.0.0", "legba/target/112.0.0"
    ]
    # Two webhooks executed → two archive entries.
    assert len(converted.archived_legacy_fields) == 2
    # Final body has the v3 horizon name, no legacy keys.
    assert converted.body["scope"]["horizon_days"] == 90
    assert "time_horizon_days" not in converted.body["scope"]
    assert "legacy_owner_email" not in converted.body
    assert "deprecated_metadata_blob" not in converted.body


@pytest.mark.integration
@pytest.mark.asyncio
async def test_executor_async_webhook_supported(
    webhook_registry: ConversionWebhookRegistry,
    executor: ConversionExecutor,
):
    await webhook_registry.register_webhook(
        ConversionWebhook(
            from_uri="legba/analyst/120.0.0",
            to_uri="legba/analyst/121.0.0",
            impl="legba.data.conversions.analyst_v1_to_v2:convert_async",
        ),
        actor="op",
    )
    converted = await executor.convert(
        {
            "identity": {"schema_uri": "legba/analyst/120.0.0"},
            "method": {"kind": "llm_planner", "timeout": 60},
            "legacy_prompt_template": "old text",
        },
        family="legba/analyst",
        from_uri="legba/analyst/120.0.0",
        to_uri="legba/analyst/121.0.0",
        descriptor_id="a1",
    )
    assert converted.body["method"]["timeout_seconds"] == 60
    assert "legacy_prompt_template" not in converted.body


@pytest.mark.integration
@pytest.mark.asyncio
async def test_executor_noop_when_from_equals_to(
    executor: ConversionExecutor,
):
    converted = await executor.convert(
        {"identity": {"schema_uri": "legba/target/130.0.0"}},
        family="legba/target",
        from_uri="legba/target/130.0.0",
        to_uri="legba/target/130.0.0",
    )
    assert converted.path_webhook_ids == []
    assert converted.path_uri_chain == ["legba/target/130.0.0"]
    assert converted.archived_legacy_fields == []


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_executor_missing_path_routes_to_dlq(
    pg_store: PostgresStore,
    executor: ConversionExecutor,
):
    body = {"identity": {"schema_uri": "legba/target/140.0.0"}, "extra": 1}
    with pytest.raises(ConversionError) as exc:
        await executor.convert(
            body,
            family="legba/target",
            from_uri="legba/target/140.0.0",
            to_uri="legba/target/141.0.0",
            descriptor_id="no_path",
            actor="op",
        )
    assert exc.value.error_kind == "no_path"
    assert "no conversion webhook" in str(exc.value)

    # DLQ row written.
    async with pg_store.acquire() as conn:
        dlq_rows = await conn.fetch(
            "SELECT validation_error FROM descriptor_dead_letter "
            "WHERE namespace = 'target' AND declared_schema_uri = 'legba/target/140.0.0'",
        )
    assert len(dlq_rows) >= 1
    err = dlq_rows[-1]["validation_error"]
    if isinstance(err, str):
        err = json.loads(err)
    assert err["kind"] == "conversion"
    assert err["error_kind"] == "no_path"

    # conversion_executions row.
    async with pg_store.acquire() as conn:
        runs = await conn.fetch(
            "SELECT success, error_kind FROM conversion_executions "
            "WHERE descriptor_id = 'no_path'",
        )
    assert any(r["success"] is False and r["error_kind"] == "no_path" for r in runs)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_executor_webhook_raise_routes_to_dlq(
    pg_store: PostgresStore,
    webhook_registry: ConversionWebhookRegistry,
    executor: ConversionExecutor,
):
    # Register a webhook pointing at a callable that raises.
    await webhook_registry.register_webhook(
        ConversionWebhook(
            from_uri="legba/target/150.0.0",
            to_uri="legba/target/151.0.0",
            # The example doesn't raise — we monkey-patch by referencing
            # a known-bad function inside an existing module instead.
            impl="legba.data.registry.conversion:_unsafe_test_raise",
        ),
        actor="op",
    )
    body = {"identity": {"schema_uri": "legba/target/150.0.0"}}
    with pytest.raises(ConversionError) as exc:
        await executor.convert(
            body,
            family="legba/target",
            from_uri="legba/target/150.0.0",
            to_uri="legba/target/151.0.0",
            descriptor_id="raises",
            actor="op",
        )
    assert exc.value.error_kind == "webhook_raise"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_executor_non_dict_return_routes_to_dlq(
    pg_store: PostgresStore,
    webhook_registry: ConversionWebhookRegistry,
    executor: ConversionExecutor,
):
    await webhook_registry.register_webhook(
        ConversionWebhook(
            from_uri="legba/target/160.0.0",
            to_uri="legba/target/161.0.0",
            impl="legba.data.registry.conversion:_unsafe_test_non_dict",
        ),
        actor="op",
    )
    with pytest.raises(ConversionError) as exc:
        await executor.convert(
            {"identity": {"schema_uri": "legba/target/160.0.0"}},
            family="legba/target",
            from_uri="legba/target/160.0.0",
            to_uri="legba/target/161.0.0",
            descriptor_id="non_dict",
            actor="op",
        )
    assert exc.value.error_kind == "post_validate"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_executor_legacy_fields_archive_persists(
    pg_store: PostgresStore,
    webhook_registry: ConversionWebhookRegistry,
    executor: ConversionExecutor,
):
    await webhook_registry.register_webhook(
        ConversionWebhook(
            from_uri="legba/target/170.0.0",
            to_uri="legba/target/171.0.0",
            impl="legba.data.conversions.target_v1_to_v2:convert",
        ),
        actor="op",
    )
    body = {
        "identity": {"schema_uri": "legba/target/170.0.0"},
        "scope": {"region_codes": ["BR"], "lang": "pt"},
        "legacy_owner_email": "drop@x.com",
    }
    await executor.convert(
        body,
        family="legba/target",
        from_uri="legba/target/170.0.0",
        to_uri="legba/target/171.0.0",
        descriptor_id="legacy_demo",
        actor="op",
    )
    async with pg_store.acquire() as conn:
        rows = await conn.fetch(
            "SELECT legacy_fields FROM descriptor_conversion_archives "
            "WHERE descriptor_id = 'legacy_demo'",
        )
    assert len(rows) == 1
    legacy = rows[0]["legacy_fields"]
    if isinstance(legacy, str):
        legacy = json.loads(legacy)
    assert legacy.get("legacy_owner_email") == "drop@x.com"


# ---------------------------------------------------------------------------
# Audit log writes on register_webhook + retire_webhook + execute
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_audit_log_entries_for_register_retire_convert(
    pg_store: PostgresStore,
    webhook_registry: ConversionWebhookRegistry,
    executor: ConversionExecutor,
):
    w = ConversionWebhook(
        from_uri="legba/target/180.0.0",
        to_uri="legba/target/181.0.0",
        impl="legba.data.conversions.target_v1_to_v2:convert",
    )
    row = await webhook_registry.register_webhook(w, actor="lewis@local")
    await executor.convert(
        {"identity": {"schema_uri": "legba/target/180.0.0"}},
        family="legba/target",
        from_uri="legba/target/180.0.0",
        to_uri="legba/target/181.0.0",
        descriptor_id="audited",
        actor="lewis@local",
    )
    await webhook_registry.retire_webhook(row.id, actor="lewis@local", reason="done")

    async with pg_store.acquire() as conn:
        # Webhook lifecycle entries (namespace prefix legba/target → 'target').
        reg_audit = await conn.fetch(
            "SELECT action, from_version, to_version FROM descriptor_audit_log "
            "WHERE descriptor_id = $1 ORDER BY occurred_at",
            str(row.id),
        )
        convert_audit = await conn.fetch(
            "SELECT action, from_version, to_version FROM descriptor_audit_log "
            "WHERE descriptor_id = 'audited' AND action = 'convert'",
        )
    actions = [r["action"] for r in reg_audit]
    assert "register_webhook" in actions
    assert "retire_webhook" in actions
    assert len(convert_audit) == 1
    assert convert_audit[0]["from_version"] == "legba/target/180.0.0"
    assert convert_audit[0]["to_version"] == "legba/target/181.0.0"


# ---------------------------------------------------------------------------
# NATS events
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nats_event_fires_on_register_webhook(
    webhook_registry_with_nats: ConversionWebhookRegistry,
    nats_store: NatsStore,
):
    pattern = f"{CONVERSION_TOPIC_PREFIX}.registered.>"
    async with _SubjectCollector(nats_store, pattern) as col:
        await webhook_registry_with_nats.register_webhook(
            ConversionWebhook(
                from_uri="legba/target/200.0.0",
                to_uri="legba/target/201.0.0",
                impl="legba.data.conversions.target_v1_to_v2:convert",
            ),
            actor="op",
        )
        await col.wait_for(n=1, timeout=5.0)
    assert any("registered" in m[0] for m in col.messages)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nats_event_fires_on_executor_success(
    webhook_registry_with_nats: ConversionWebhookRegistry,
    executor_with_nats: ConversionExecutor,
    nats_store: NatsStore,
):
    await webhook_registry_with_nats.register_webhook(
        ConversionWebhook(
            from_uri="legba/target/210.0.0",
            to_uri="legba/target/211.0.0",
            impl="legba.data.conversions.target_v1_to_v2:convert",
        ),
        actor="op",
    )
    pattern = f"{CONVERSION_TOPIC_PREFIX}.executed.>"
    async with _SubjectCollector(nats_store, pattern) as col:
        await executor_with_nats.convert(
            {"identity": {"schema_uri": "legba/target/210.0.0"}},
            family="legba/target",
            from_uri="legba/target/210.0.0",
            to_uri="legba/target/211.0.0",
            descriptor_id="nats_exec",
            actor="op",
        )
        await col.wait_for(n=1, timeout=5.0)
    assert any("nats_exec" in m[0] for m in col.messages)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nats_event_fires_on_executor_failure(
    executor_with_nats: ConversionExecutor,
    nats_store: NatsStore,
):
    pattern = f"{CONVERSION_DLQ_PREFIX}.>"
    async with _SubjectCollector(nats_store, pattern) as col:
        with pytest.raises(ConversionError):
            await executor_with_nats.convert(
                {"identity": {"schema_uri": "legba/target/220.0.0"}},
                family="legba/target",
                from_uri="legba/target/220.0.0",
                to_uri="legba/target/221.0.0",
                descriptor_id="nats_fail",
                actor="op",
            )
        await col.wait_for(n=1, timeout=5.0)
    assert any("nats_fail" in m[0] for m in col.messages)


# ---------------------------------------------------------------------------
# DescriptorRegistry integration — register_raw + get_typed(auto_upgrade)
# ---------------------------------------------------------------------------


_FIXED_CREATED = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)


def _v2_target_body(descriptor_id: str) -> dict[str, Any]:
    """Body for a v2 (current) target descriptor — matches the vendored
    L-101 pydantic shape and registers cleanly."""
    return {
        "identity": {
            "id": descriptor_id,
            "name": "Demo target",
            "schema_uri": "legba/target/2.0.0",
            "version": "0" * 16,
            "abstraction_level": "L1",
            "state": "draft",
            "owner": "lewis@local",
            "created": _FIXED_CREATED.isoformat(),
            "inherits": [],
        },
        "scope": {
            # Source-first pivot: TargetScope is a discriminated union; the
            # geopolitical founding case carries domain="geo".
            "domain": "geo",
            "geo": ["BR"],
            "languages": ["pt-BR"],
            "entity_classes": ["organization", "country"],
            "relationship_types": ["LocatedIn"],
            "time_horizon_days": 90,
        },
        "sources": [],
    }


def _v1_style_target_body(descriptor_id: str) -> dict[str, Any]:
    """Body that mimics a 'v1' shape with the v1→v2 conversion fields
    (`region_codes`, `lang`, plus a `legacy_owner_email` to be dropped)."""
    return {
        "identity": {
            "id": descriptor_id,
            "name": "Demo old target",
            "schema_uri": "legba/target/1.0.0",
            "version": "0" * 16,
            "abstraction_level": "L1",
            "state": "draft",
            "owner": "lewis@local",
            "created": _FIXED_CREATED.isoformat(),
            "inherits": [],
        },
        "scope": {
            # domain survives the v1->v2 conversion (the impl only rewrites
            # region_codes/lang); post-pivot scope is a discriminated union.
            "domain": "geo",
            "region_codes": ["BR"],
            "lang": "pt-BR",
            "entity_classes": ["organization", "country"],
            "relationship_types": ["LocatedIn"],
            "time_horizon_days": 90,
        },
        "sources": [],
        "legacy_owner_email": "drop@example.com",
    }


@pytest_asyncio.fixture
async def wired_registry(
    pg_store: PostgresStore,
    webhook_registry: ConversionWebhookRegistry,
    executor: ConversionExecutor,
) -> DescriptorRegistry:
    identity = _fixed_identity()
    reg = DescriptorRegistry(
        pg_store,
        signing_identity=identity,
        webhook_registry=webhook_registry,
        conversion_executor=executor,
        current_schema_uris={"target": "legba/target/2.0.0"},
    )
    await reg.start()
    yield reg
    await reg.stop()


async def _ensure_v1_to_v2_webhook(
    webhook_registry: ConversionWebhookRegistry,
) -> None:
    """Helper: register the canonical v1→v2 target webhook, tolerant of
    sibling tests already having registered it (shared session DB)."""
    try:
        await webhook_registry.register_webhook(
            ConversionWebhook(
                from_uri="legba/target/1.0.0",
                to_uri="legba/target/2.0.0",
                impl="legba.data.conversions.target_v1_to_v2:convert",
            ),
            actor="op",
        )
    except WebhookValidationError:
        # Already registered by an earlier test in this session.
        pass


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_raw_upgrades_v1_through_to_current(
    pg_store: PostgresStore,
    webhook_registry: ConversionWebhookRegistry,
    wired_registry: DescriptorRegistry,
):
    desc_id = f"raw_upgrade_{uuid4().hex[:8]}"
    await _ensure_v1_to_v2_webhook(webhook_registry)
    body = _v1_style_target_body(desc_id)
    row = await wired_registry.register_raw(
        body, actor="op", family=Family.TARGET
    )
    # Stored row is at the current schema_uri.
    assert row.schema_uri == "legba/target/2.0.0"
    assert row.body["scope"]["geo"] == ["BR"]
    assert row.body["scope"]["languages"] == ["pt-BR"]
    # legacy_owner_email was dropped + archived.
    assert "legacy_owner_email" not in row.body
    async with pg_store.acquire() as conn:
        archive = await conn.fetchrow(
            "SELECT legacy_fields FROM descriptor_conversion_archives "
            "WHERE descriptor_id = $1 ORDER BY archived_at DESC LIMIT 1",
            desc_id,
        )
    assert archive is not None
    lf = archive["legacy_fields"]
    if isinstance(lf, str):
        lf = json.loads(lf)
    assert lf.get("legacy_owner_email") == "drop@example.com"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_raw_passthrough_when_already_current(
    wired_registry: DescriptorRegistry,
):
    desc_id = f"raw_current_{uuid4().hex[:8]}"
    body = _v2_target_body(desc_id)
    row = await wired_registry.register_raw(
        body, actor="op", family=Family.TARGET
    )
    assert row.schema_uri == "legba/target/2.0.0"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_raw_no_path_dlqs(
    pg_store: PostgresStore,
    wired_registry: DescriptorRegistry,
):
    # No webhook registered for legba/target/99.0.0 → legba/target/2.0.0.
    desc_id = f"raw_nopath_{uuid4().hex[:8]}"
    body = _v1_style_target_body(desc_id)
    body["identity"]["schema_uri"] = "legba/target/99.0.0"
    with pytest.raises(ConversionError) as exc:
        await wired_registry.register_raw(
            body, actor="op", family=Family.TARGET
        )
    assert exc.value.error_kind == "no_path"
    # DLQ row recorded.
    async with pg_store.acquire() as conn:
        dlq = await conn.fetch(
            "SELECT validation_error FROM descriptor_dead_letter "
            "WHERE namespace = 'target' AND declared_schema_uri = 'legba/target/99.0.0'",
        )
    assert len(dlq) >= 1
    err = dlq[-1]["validation_error"]
    if isinstance(err, str):
        err = json.loads(err)
    assert "no conversion webhook" in err.get("message", "")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_typed_auto_upgrade_on_read(
    pg_store: PostgresStore,
    webhook_registry: ConversionWebhookRegistry,
    wired_registry: DescriptorRegistry,
):
    # 1. Manually insert an OLDER row into target_descriptors. We use the
    #    raw store rather than the public API so the row holds a non-
    #    current schema_uri without any conversion mediation (pretending
    #    it landed pre-bump).
    desc_id = f"upgrade_read_{uuid4().hex[:8]}"
    legacy_body = _v1_style_target_body(desc_id)
    async with pg_store.transaction() as conn:
        await conn.execute(
            """
            INSERT INTO target_descriptors
                (descriptor_id, version, schema_uri, is_head,
                 abstraction_level, state, owner, name, body,
                 inherits, created_at)
            VALUES ($1, $2, $3, true, $4, $5, $6, $7, $8::jsonb, $9, NOW())
            """,
            desc_id,
            "f" * 64,
            "legba/target/1.0.0",
            "L1",
            "draft",
            "lewis@local",
            "Demo old target",
            json.dumps(legacy_body),
            [],
        )

    # 2. Register the conversion webhook (tolerant of prior tests).
    await _ensure_v1_to_v2_webhook(webhook_registry)

    # 3. get_typed with auto_upgrade=True returns a v2-parsed model.
    typed = await wired_registry.get_typed(
        desc_id, family=Family.TARGET, auto_upgrade=True
    )
    assert isinstance(typed, TargetDescriptor)
    assert typed.identity.schema_uri == "legba/target/2.0.0"
    assert typed.scope.geo == ["BR"]
    assert typed.scope.languages == ["pt-BR"]

    # 4. Stored row is unchanged — auto_upgrade is transparent, not
    #    persistent.
    async with pg_store.acquire() as conn:
        stored = await conn.fetchrow(
            "SELECT schema_uri FROM target_descriptors WHERE descriptor_id = $1",
            desc_id,
        )
    assert stored["schema_uri"] == "legba/target/1.0.0"


# ---------------------------------------------------------------------------
# Test helpers exported on the conversion module (referenced by impl strings)
# ---------------------------------------------------------------------------


# Patched onto legba.data.registry.conversion at import time so the impl
# resolver picks them up for the negative-path tests.
def _unsafe_test_raise(body: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
    raise RuntimeError("intentional webhook failure for test")


def _unsafe_test_non_dict(body: dict[str, Any]):  # pragma: no cover
    return ["not", "a", "dict"]


import legba.data.registry.conversion as _conversion_module
_conversion_module._unsafe_test_raise = _unsafe_test_raise
_conversion_module._unsafe_test_non_dict = _unsafe_test_non_dict
