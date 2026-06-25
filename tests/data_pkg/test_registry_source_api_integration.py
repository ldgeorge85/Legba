# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-05 integration tests — `source` + `action_pack` registry REST + lifecycle.

Runs against the live substrate via the `migrated_pg` fixture from
`conftest.py` (a fresh `legba_test_<uuid>` DB migrated through 0024). Builds a
real FastAPI app wired to a real `DescriptorRegistry` and hits the HTTP
surface added in P-05:

  * generic `/descriptors/source/*` + `/descriptors/action_pack/*` lifecycle
    (register → get → list → typed → update → transition → retire);
  * the projected UI read views `/sources`, `/sources/{id}`, `/action_packs`,
    `/action_packs/{id}`.

No mocks for the substrate boundary — same rule as the rest of the registry
suite. NATS is optional here (descriptor events are observability-only; the
registry's row state is correct regardless), so the app is wired without a
NATS store to keep the test hermetic.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.api import (
    API_TOKEN_ENV,
    RegistryAPIDeps,
    build_router,
)
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "33" * 32)


# ---------------------------------------------------------------------------
# Sample payloads
# ---------------------------------------------------------------------------


def _source_body(descriptor_id: str, *, state: str = "draft") -> dict[str, Any]:
    return {
        "identity": {
            "id": descriptor_id,
            "name": "Reuters World RSS",
            "kind": "rss",
            "schema_uri": "legba/source/1.0.0",
            "version": "0" * 16,
            "abstraction_level": "L1",
            "state": state,
            "owner": "lewis@local",
            "created": "2026-06-03T10:00:00+00:00",
        },
        "scope": {
            "owner_tenant": "default",
            "geo": ["BR", "US"],
            "languages": ["en", "pt-BR"],
            "tags": ["news", "geopolitics"],
        },
        "acquisition": "poll",
        "cadence": {"schedule": {"raw": "*/15 * * * *"}},
        "subscription_policy": "open",
        "output": {"delivery": "lossy"},
    }


def _action_pack_body(descriptor_id: str) -> dict[str, Any]:
    return {
        "identity": {
            "id": descriptor_id,
            "name": "Media Processing",
            "schema_uri": "legba/action_pack/1.0.0",
            "version": "0" * 16,
            "abstraction_level": "L1",
            "state": "draft",
            "owner": "lewis@local",
            "created": "2026-06-03T10:00:00+00:00",
        },
        "tools": [{"name": "process_media", "async_job": True}],
        "applies_to_tags": ["media"],
        "governor": {"max_invocations_per_hour": 50},
    }


def _fixed_identity() -> SigningIdentity:
    seed = b"P-05-source-api-integ-seed-deterministic"[:32]
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:p05-integ",
    )


# ---------------------------------------------------------------------------
# Wiring fixture (no NATS — descriptor events are observability-only)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api_app(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()

    identity = _fixed_identity()
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    vault = CredentialVault(pg_store)

    descriptor_registry = DescriptorRegistry(
        pg_store,
        vocabulary_cache=vocab,
        signing_identity=identity,
        audit_logger=audit,
        dead_letter=dlq,
    )
    await descriptor_registry.start()

    stack_registry = StackRegistry(pg_store, vault, audit=audit, dlq=dlq)

    deps = RegistryAPIDeps(
        descriptor_registry=descriptor_registry,
        stack_registry=stack_registry,
        vault=vault,
        dlq=dlq,
        audit_logger=audit,
        vocabulary_cache=vocab,
        nats_store=None,
    )

    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_router(deps), prefix="/api/v1/registry")

    yield app, deps, pg_store

    await descriptor_registry.stop()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(api_app):
    app, _, _ = api_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Source descriptor full round-trip via REST
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_descriptor_round_trip_via_http(client: AsyncClient):
    desc_id = f"source.rss.{uuid4().hex[:8]}"
    body = _source_body(desc_id)

    # REGISTER
    r = await client.post("/api/v1/registry/descriptors/source", json=body)
    assert r.status_code == 201, r.text
    row = r.json()
    v1 = row["version"]
    assert len(v1) == 64
    assert row["family"] == "source"
    assert row["kind"] == "rss"
    assert row["state"] == "draft"

    # GET (generic descriptor route)
    r = await client.get(f"/api/v1/registry/descriptors/source/{desc_id}")
    assert r.status_code == 200, r.text
    assert r.json()["version"] == v1

    # GET typed — re-parses into the SourceDescriptor model
    r = await client.get(f"/api/v1/registry/descriptors/source/{desc_id}/typed")
    assert r.status_code == 200, r.text
    typed = r.json()
    assert typed["identity"]["id"] == desc_id
    assert typed["acquisition"] == "poll"

    # LIST (generic) filtered to this id
    r = await client.get(
        "/api/v1/registry/descriptors",
        params={"family": "source", "descriptor_id": desc_id},
    )
    assert r.status_code == 200
    assert any(x["descriptor_id"] == desc_id for x in r.json())

    # PROJECTED LIST — /sources read view the UI consumes
    r = await client.get("/api/v1/registry/sources", params={"descriptor_id": desc_id})
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    proj = rows[0]
    assert proj["descriptor_id"] == desc_id
    assert proj["kind"] == "rss"
    assert proj["acquisition"] == "poll"
    assert proj["subscription_policy"] == "open"
    assert proj["owner_tenant"] == "default"
    assert proj["geo"] == ["BR", "US"]
    assert "news" in proj["tags"]
    assert proj["output_subject"] == f"source.{desc_id}.signals"
    assert proj["has_discovery"] is False

    # PROJECTED DETAIL — /sources/{id}
    r = await client.get(f"/api/v1/registry/sources/{desc_id}")
    assert r.status_code == 200, r.text
    assert r.json()["descriptor_id"] == desc_id
    assert r.json()["body"]["identity"]["id"] == desc_id

    # RETIRE
    r = await client.post(f"/api/v1/registry/descriptors/source/{desc_id}/retire")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "retired"

    # retired source no longer in head-only projected list
    r = await client.get("/api/v1/registry/sources", params={"descriptor_id": desc_id})
    assert all(x["state"] != "draft" for x in r.json())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_lifecycle_fsm_transitions_via_http(client: AsyncClient):
    desc_id = f"source.rss.{uuid4().hex[:8]}"
    r = await client.post(
        "/api/v1/registry/descriptors/source", json=_source_body(desc_id),
    )
    assert r.status_code == 201, r.text
    assert r.json()["state"] == "draft"

    # draft -> configured (legal)
    r = await client.post(
        f"/api/v1/registry/descriptors/source/{desc_id}/transition",
        json={"to_state": "configured"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "configured"

    # configured -> active (legal)
    r = await client.post(
        f"/api/v1/registry/descriptors/source/{desc_id}/transition",
        json={"to_state": "active"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "active"

    # active -> draft (ILLEGAL per ALLOWED_TRANSITIONS) -> 409
    r = await client.post(
        f"/api/v1/registry/descriptors/source/{desc_id}/transition",
        json={"to_state": "draft"},
    )
    assert r.status_code == 409, r.text

    # active -> paused (legal)
    r = await client.post(
        f"/api/v1/registry/descriptors/source/{desc_id}/transition",
        json={"to_state": "paused"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "paused"

    # transition to 'retired' is rejected here (use /retire) -> 400
    r = await client.post(
        f"/api/v1/registry/descriptors/source/{desc_id}/transition",
        json={"to_state": "retired"},
    )
    assert r.status_code == 400, r.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_action_pack_round_trip_via_http(client: AsyncClient):
    desc_id = f"media_processing_{uuid4().hex[:8]}"
    body = _action_pack_body(desc_id)

    # REGISTER
    r = await client.post("/api/v1/registry/descriptors/action_pack", json=body)
    assert r.status_code == 201, r.text
    row = r.json()
    v1 = row["version"]
    assert len(v1) == 64
    assert row["family"] == "action_pack"
    assert row["state"] == "draft"

    # GET (generic)
    r = await client.get(f"/api/v1/registry/descriptors/action_pack/{desc_id}")
    assert r.status_code == 200, r.text
    assert r.json()["version"] == v1

    # PROJECTED LIST — /action_packs
    r = await client.get(
        "/api/v1/registry/action_packs", params={"descriptor_id": desc_id},
    )
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    proj = rows[0]
    assert proj["descriptor_id"] == desc_id
    assert proj["tool_names"] == ["process_media"]
    assert proj["applies_to_tags"] == ["media"]
    assert proj["has_governor"] is True

    # PROJECTED DETAIL — /action_packs/{id}
    r = await client.get(f"/api/v1/registry/action_packs/{desc_id}")
    assert r.status_code == 200, r.text
    assert r.json()["descriptor_id"] == desc_id

    # RETIRE
    r = await client.post(
        f"/api/v1/registry/descriptors/action_pack/{desc_id}/retire",
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "retired"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_get_missing_returns_404(client: AsyncClient):
    r = await client.get("/api/v1/registry/sources/source.does.not_exist")
    assert r.status_code == 404, r.text
    r = await client.get("/api/v1/registry/action_packs/nope_missing")
    assert r.status_code == 404, r.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalid_source_body_routes_to_dlq(api_app, client: AsyncClient):
    """A bad source body fails validation; the registry routes it to the
    descriptor dead-letter table and the HTTP layer returns 422 with the
    dead-letter id (same contract as target/analyst)."""
    _, _, pg_store = api_app
    desc_id = f"source.rss.{uuid4().hex[:8]}"
    body = _source_body(desc_id)
    # active poll source with no cadence.schedule -> model_validator rejects.
    body["identity"]["state"] = "active"
    body["cadence"] = {}

    r = await client.post("/api/v1/registry/descriptors/source", json=body)
    # pydantic model-validator failure surfaces at parse time -> 422.
    assert r.status_code == 422, r.text
