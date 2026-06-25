# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-204 — `/api/v1/registry/ui_panels` route tests.

The L-204 frontend calls this route at boot to populate its reactive
panel registry. The contract:

  * `GET /api/v1/registry/ui_panels?mode=<mode>` → list of active rows.
  * `?include_retired=true` returns soft-deleted rows too.
  * Mode aliases (`above-ai`, `cis_fellowship`) normalize via the
    registry's `_normalize_mode`; unknown modes return 400.
  * `/ui_panels/by_slot/<layout_slot>` returns rows holding that slot.

These tests reuse the integration test app from
`test_registry_api_integration.py` patterns (real Postgres, real
DescriptorRegistry, real NATS) — the route reads the
`ui_panel_registrations` table via `UIPanelRegistry` so a real DB is
required.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data.config import PostgresConfig
from legba.data.outputs.ui_panel import register_from_descriptor
from legba.data.registry.api import (
    API_TOKEN_ENV,
    RegistryAPIDeps,
    build_router,
)
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.emitter import NATSEventEmitter
from legba.data.registry.signing import SigningIdentity, load_default_identity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.config import NatsConfig


_TEST_MASTER_KEY_HEX = (
    "0011223344556677889900112233445566778899001122334455667788990011"
)
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "22" * 32)


# ---------------------------------------------------------------------------
# App + client fixtures (mirror test_registry_api_integration.py shape, but
# only build the pieces the ui_panels route needs).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api_app(migrated_pg: PostgresConfig):
    """A FastAPI app wired to the real registry deps. Returns the app + the
    deps bundle + raw pg_store so tests can seed `ui_panel_registrations`
    directly via `register_from_descriptor`."""
    os.environ.pop(API_TOKEN_ENV, None)  # dev mode — any bearer accepted

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()

    nats_store = NatsStore(NatsConfig.from_env())
    await nats_store.connect()

    identity = load_default_identity()
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    vault = CredentialVault(pg_store)
    emitter = NATSEventEmitter(nats_store)

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
        emitter=emitter,
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
    app.include_router(build_router(deps), prefix="/api/v1/registry")

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


def _outputs_for_target(target_id: str) -> list[dict[str, Any]]:
    return [
        {"kind": "ui_panel", "config": {
            "panel": "panels.target_overview",
            "binding": {"target_id": target_id},
            "mode": "personal",
            "layout_slot": f"dashboard.{target_id}.overview",
            "title": f"Target — {target_id}",
            "data_query": {"kind": "rest", "path": f"/api/v3/targets/{target_id}"},
        }},
        {"kind": "ui_panel", "config": {
            "panel": "panels.target_findings",
            "binding": {"target_id": target_id},
            "mode": "personal",
            "layout_slot": f"dashboard.{target_id}.findings",
            "title": f"Findings — {target_id}",
        }},
    ]


def _outputs_for_cis_target(target_id: str) -> list[dict[str, Any]]:
    return [
        {"kind": "ui_panel", "config": {
            "panel": "panels.target_overview",
            "binding": {"target_id": target_id},
            "mode": "cis_fellowship",   # alias; should normalize to "cis"
            "layout_slot": f"dashboard.{target_id}.cis_overview",
        }},
    ]


# ---------------------------------------------------------------------------
# /ui_panels GET — happy path
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_ui_panels_returns_active_rows_for_mode(api_app, client):
    """Two personal-mode panels register; the route returns both with
    payload fields matching the dataclass."""
    _, _, pg_store = api_app
    target = f"r_{uuid4().hex[:8]}"

    async with pg_store.acquire() as conn:
        rows = await register_from_descriptor(
            conn,
            descriptor_id=target,
            descriptor_version="v" + "a" * 15,
            descriptor_family="target",
            outputs=_outputs_for_target(target),
        )
    assert len(rows) == 2

    r = await client.get("/api/v1/registry/ui_panels", params={"mode": "personal"})
    assert r.status_code == 200, r.text
    payload = r.json()
    by_slot = {p["layout_slot"]: p for p in payload if p["descriptor_id"] == target}
    assert f"dashboard.{target}.overview" in by_slot
    assert f"dashboard.{target}.findings" in by_slot

    overview = by_slot[f"dashboard.{target}.overview"]
    assert overview["panel_id"] == "target_overview"
    assert overview["mode"] == "personal"
    assert overview["binding"] == {"target_id": target}
    assert overview["data_query"] == {
        "kind": "rest", "path": f"/api/v3/targets/{target}",
    }
    assert overview["retired"] is False
    assert overview["descriptor_family"] == "target"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_ui_panels_filters_by_mode(api_app, client):
    """Personal-mode and CIS-mode panels coexist; the route filters."""
    _, _, pg_store = api_app
    personal_target = f"p_{uuid4().hex[:8]}"
    cis_target = f"c_{uuid4().hex[:8]}"

    async with pg_store.acquire() as conn:
        await register_from_descriptor(
            conn,
            descriptor_id=personal_target,
            descriptor_version="v" + "1" * 15,
            descriptor_family="target",
            outputs=_outputs_for_target(personal_target),
        )
        await register_from_descriptor(
            conn,
            descriptor_id=cis_target,
            descriptor_version="v" + "2" * 15,
            descriptor_family="target",
            outputs=_outputs_for_cis_target(cis_target),
        )

    r_personal = await client.get(
        "/api/v1/registry/ui_panels", params={"mode": "personal"},
    )
    assert r_personal.status_code == 200
    personal_ids = {p["descriptor_id"] for p in r_personal.json()}
    assert personal_target in personal_ids
    assert cis_target not in personal_ids

    r_cis = await client.get(
        "/api/v1/registry/ui_panels", params={"mode": "cis"},
    )
    assert r_cis.status_code == 200
    cis_ids = {p["descriptor_id"] for p in r_cis.json()}
    assert cis_target in cis_ids
    assert personal_target not in cis_ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_ui_panels_normalizes_mode_aliases(api_app, client):
    """Querying with `?mode=above-ai` normalizes to `above_ai` (no rows,
    but a 200 not a 400)."""
    r = await client.get(
        "/api/v1/registry/ui_panels", params={"mode": "above-ai"},
    )
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_ui_panels_rejects_unknown_mode(api_app, client):
    r = await client.get(
        "/api/v1/registry/ui_panels", params={"mode": "operator"},
    )
    assert r.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_ui_panels_include_retired(api_app, client):
    """`include_retired=true` returns soft-deleted rows."""
    _, _, pg_store = api_app
    target = f"r_{uuid4().hex[:8]}"

    async with pg_store.acquire() as conn:
        rows = await register_from_descriptor(
            conn,
            descriptor_id=target,
            descriptor_version="v" + "9" * 15,
            descriptor_family="target",
            outputs=_outputs_for_target(target),
        )
        # Soft-delete one of the two.
        await conn.execute(
            "UPDATE ui_panel_registrations SET retired = TRUE, retired_at = NOW() "
            "WHERE id = $1",
            rows[0].id,
        )

    # Default: only the un-retired one.
    r_active = await client.get(
        "/api/v1/registry/ui_panels", params={"mode": "personal"},
    )
    active_ids = {p["id"] for p in r_active.json() if p["descriptor_id"] == target}
    assert str(rows[0].id) not in active_ids
    assert str(rows[1].id) in active_ids

    # With include_retired: both.
    r_all = await client.get(
        "/api/v1/registry/ui_panels",
        params={"mode": "personal", "include_retired": "true"},
    )
    all_ids = {p["id"] for p in r_all.json() if p["descriptor_id"] == target}
    assert str(rows[0].id) in all_ids
    assert str(rows[1].id) in all_ids


# ---------------------------------------------------------------------------
# /ui_panels/by_slot
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_ui_panels_by_slot(api_app, client):
    _, _, pg_store = api_app
    target = f"r_{uuid4().hex[:8]}"
    slot = f"dashboard.{target}.overview"

    async with pg_store.acquire() as conn:
        await register_from_descriptor(
            conn,
            descriptor_id=target,
            descriptor_version="v" + "3" * 15,
            descriptor_family="target",
            outputs=_outputs_for_target(target),
        )

    r = await client.get(f"/api/v1/registry/ui_panels/by_slot/{slot}")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["layout_slot"] == slot
    assert rows[0]["descriptor_id"] == target


# ---------------------------------------------------------------------------
# Schema round-trip — sanity check the TS interface
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ui_panel_row_out_field_set_matches_ts_interface(api_app, client):
    """The TS `PanelRegistration` interface (legba-ui-v3/src/types.ts)
    expects exactly these fields. Drift breaks the L-204 frontend
    silently, so we lock it down here."""
    _, _, pg_store = api_app
    target = f"r_{uuid4().hex[:8]}"

    async with pg_store.acquire() as conn:
        await register_from_descriptor(
            conn,
            descriptor_id=target,
            descriptor_version="v" + "5" * 15,
            descriptor_family="target",
            outputs=_outputs_for_target(target),
        )

    r = await client.get("/api/v1/registry/ui_panels", params={"mode": "personal"})
    assert r.status_code == 200
    rows = [p for p in r.json() if p["descriptor_id"] == target]
    assert rows, "no rows returned for the seeded target"
    expected_fields = {
        "id",
        "panel_id",
        "descriptor_id",
        "descriptor_version",
        "descriptor_family",
        "analyst_id",
        "title",
        "mode",
        "layout_slot",
        "data_query",
        "binding",
        "retired",
        "created_at",
        "retired_at",
    }
    actual_fields = set(rows[0].keys())
    assert actual_fields == expected_fields, (
        f"shape drift: TS expects {sorted(expected_fields)}, route returned "
        f"{sorted(actual_fields)}"
    )
