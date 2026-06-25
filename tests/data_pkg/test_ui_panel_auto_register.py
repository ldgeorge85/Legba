# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-192 wire-up: `DescriptorRegistry` auto-registers `ui_panel` outputs.

These tests pin down the contract the L-204 frontend relies on: when an
operator registers (or updates) a descriptor whose ``outputs`` block
includes a ``ui_panel`` entry, a matching row materializes in
``ui_panel_registrations`` synchronously, inside the same DB transaction.
On ``retire`` the registry's panel rows soft-delete.

Real Postgres via the shared ``migrated_pg`` fixture; no mocks. The
descriptor registry's NATS dependency is bypassed via the
``registry_no_nats`` fixture style.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from nacl.signing import SigningKey

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.descriptor import DescriptorRegistry, Family
from legba.data.registry.signing import SigningIdentity
from legba.data.schemas import (
    AbstractionLevel,
    AnalystDescriptor,
    AnalystIdentity,
    AnalystKind,
    CadenceBlock,
    GeoScope,
    LifecycleState,
    MethodBlock,
    OutputBinding,
    SourceRef,
    SubscriptionBlock,
    TargetDescriptor,
    TargetIdentity,
    TypeSignature,
)


_FIXED_CREATED = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_identity() -> SigningIdentity:
    seed = b"L-192-auto-register-test-seed-1!"[:32]
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:l192-test",
    )


@pytest_asyncio.fixture
async def pg_store(migrated_pg: PostgresConfig):
    store = PostgresStore(migrated_pg)
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


@pytest_asyncio.fixture
async def registry(pg_store: PostgresStore):
    reg = DescriptorRegistry(pg_store, signing_identity=_fixed_identity())
    await reg.start()
    try:
        yield reg
    finally:
        await reg.stop()


def _panel_output(*, panel: str, slot: str, mode: str = "personal",
                  title: str | None = None) -> OutputBinding:
    config = {
        "panel": f"panels.{panel}",
        "mode": mode,
        "layout_slot": slot,
        "binding": {"target_id": "{self.id}"},
        "data_query": {"kind": "rest", "path": f"/api/v3/x/{panel}"},
    }
    if title is not None:
        config["title"] = title
    return OutputBinding(kind="ui_panel", config=config)


def _draft_target(*, descriptor_id: str, panels: list[OutputBinding],
                  state: LifecycleState = LifecycleState.ACTIVE) -> TargetDescriptor:
    identity = TargetIdentity(
        id=descriptor_id,
        name=f"Test target {descriptor_id}",
        schema_uri="legba/target/2.0.0",
        version="0" * 16,
        abstraction_level=AbstractionLevel.L1,
        state=state,
        owner="lewis@local",
        created=_FIXED_CREATED,
    )
    # Source-first pivot: targets reference SHARED sources via SourceRef
    # (explicit id or selector) — they no longer own inline SourceBindings.
    sources: list[SourceRef] = []
    if state == LifecycleState.ACTIVE:
        sources.append(SourceRef(source_id="source.rss.main"))
    return TargetDescriptor(
        identity=identity,
        scope=GeoScope(
            geo=["BR"],
            languages=["pt-BR"],
            entity_classes=["organization", "country"],
            relationship_types=["LocatedIn"],
            time_horizon_days=90,
        ),
        sources=sources,
        outputs=panels,
    )


def _draft_analyst(*, descriptor_id: str,
                   panels: list[OutputBinding]) -> AnalystDescriptor:
    identity = AnalystIdentity(
        id=descriptor_id,
        name=f"Analyst {descriptor_id}",
        schema_uri="legba/analyst/2.0.0",
        version="0" * 16,
        kind=AnalystKind.INLINE_TARGET,
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
        outputs=panels,
    )


async def _panel_rows(pg_store: PostgresStore, descriptor_id: str) -> list[dict]:
    async with pg_store.acquire() as conn:
        rows = await conn.fetch(
            "SELECT panel_id, descriptor_id, descriptor_version, "
            "descriptor_family, layout_slot, mode, retired "
            "FROM ui_panel_registrations "
            "WHERE descriptor_id = $1 "
            "ORDER BY created_at",
            descriptor_id,
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# register() — target with one ui_panel output → 1 row appears
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_target_with_ui_panel_materializes_row(
    registry: DescriptorRegistry,
    pg_store: PostgresStore,
):
    desc_id = f"t_panel_{uuid4().hex[:8]}"
    desc = _draft_target(
        descriptor_id=desc_id,
        panels=[_panel_output(
            panel="target_overview",
            slot=f"dashboard.test.{desc_id}",
            title="Test overview",
        )],
    )
    head = await registry.register(desc, actor="lewis@local")

    rows = await _panel_rows(pg_store, desc_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["panel_id"] == "target_overview"
    assert row["descriptor_family"] == "target"
    assert row["descriptor_version"] == head.version
    assert row["layout_slot"] == f"dashboard.test.{desc_id}"
    assert row["mode"] == "personal"
    assert row["retired"] is False


# ---------------------------------------------------------------------------
# register() — analyst with multiple ui_panel outputs → N rows
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_analyst_with_multiple_panels(
    registry: DescriptorRegistry,
    pg_store: PostgresStore,
):
    desc_id = f"a_panel_{uuid4().hex[:8]}"
    desc = _draft_analyst(
        descriptor_id=desc_id,
        panels=[
            _panel_output(panel="analyst_runs",
                          slot=f"dashboard.test.runs.{desc_id}"),
            _panel_output(panel="analyst_findings",
                          slot=f"dashboard.test.findings.{desc_id}",
                          mode="above_ai"),
        ],
    )
    head = await registry.register(desc, actor="lewis@local")

    rows = await _panel_rows(pg_store, desc_id)
    assert len(rows) == 2
    panel_ids = {r["panel_id"] for r in rows}
    assert panel_ids == {"analyst_runs", "analyst_findings"}
    for r in rows:
        assert r["descriptor_family"] == "analyst"
        assert r["descriptor_version"] == head.version
        assert r["retired"] is False


# ---------------------------------------------------------------------------
# register() — descriptor with no ui_panel outputs → no rows (no-op)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_without_ui_panel_outputs_is_noop(
    registry: DescriptorRegistry,
    pg_store: PostgresStore,
):
    desc_id = f"t_nopanel_{uuid4().hex[:8]}"
    desc = _draft_target(descriptor_id=desc_id, panels=[])
    await registry.register(desc, actor="lewis@local")

    rows = await _panel_rows(pg_store, desc_id)
    assert rows == []


# ---------------------------------------------------------------------------
# retire() — soft-deletes the panel rows owned by the descriptor
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retire_descriptor_retires_ui_panels(
    registry: DescriptorRegistry,
    pg_store: PostgresStore,
):
    desc_id = f"t_retire_{uuid4().hex[:8]}"
    desc = _draft_target(
        descriptor_id=desc_id,
        panels=[_panel_output(
            panel="target_overview",
            slot=f"dashboard.test.retire.{desc_id}",
        )],
    )
    await registry.register(desc, actor="lewis@local")

    rows_before = await _panel_rows(pg_store, desc_id)
    assert len(rows_before) == 1
    assert rows_before[0]["retired"] is False

    await registry.retire(desc_id, actor="lewis@local", family=Family.TARGET)

    rows_after = await _panel_rows(pg_store, desc_id)
    assert len(rows_after) == 1
    assert rows_after[0]["retired"] is True


# ---------------------------------------------------------------------------
# update() — prior-version panels retire, new-version panels appear
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_rotates_panel_rows_to_new_version(
    registry: DescriptorRegistry,
    pg_store: PostgresStore,
):
    desc_id = f"t_update_{uuid4().hex[:8]}"
    slot = f"dashboard.test.update.{desc_id}"
    desc_v1 = _draft_target(
        descriptor_id=desc_id,
        panels=[_panel_output(panel="target_overview", slot=slot,
                              title="v1 title")],
    )
    head_v1 = await registry.register(desc_v1, actor="lewis@local")

    desc_v2 = _draft_target(
        descriptor_id=desc_id,
        panels=[_panel_output(panel="target_overview", slot=slot,
                              title="v2 title")],
    )
    head_v2 = await registry.update(desc_id, desc_v2, actor="lewis@local")
    assert head_v2.version != head_v1.version

    rows = await _panel_rows(pg_store, desc_id)
    by_version = {r["descriptor_version"]: r for r in rows}
    # There should be exactly one active row pointing at the new version.
    active = [r for r in rows if not r["retired"]]
    assert len(active) == 1
    assert active[0]["descriptor_version"] == head_v2.version
    # The v1 row (if still present) should be retired.
    if head_v1.version in by_version:
        assert by_version[head_v1.version]["retired"] is True
