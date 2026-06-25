# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the P-8 source-credibility CRUD endpoints.

Runs against the live substrate via the `migrated_pg` fixture from
`tests/data_pkg/conftest.py`. Builds a real FastAPI app, wires it to a
real `DescriptorRegistry` (only the `pg` pool is exercised) and asserts
both the HTTP responses and the table side-effects.

No mocks for substrate boundaries.
"""

from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.config import NatsConfig, PostgresConfig
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.source_credibility_api import (
    build_source_credibility_router,
)
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache


_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "33" * 32)


def _fixed_identity() -> SigningIdentity:
    seed = b"p8-source-credibility-test-seedXX"[:32]
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:p8-cred-test",
    )


@pytest_asyncio.fixture
async def api_app(migrated_pg: PostgresConfig):
    """Spin up the source_credibility router against the migrated test DB.

    We construct a `RegistryAPIDeps` bundle the same way the canonical
    `server.py` does, except we only mount the source_credibility router
    (the rest of the API isn't needed for these tests).
    """
    os.environ.pop(API_TOKEN_ENV, None)

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()

    nats_store = NatsStore(NatsConfig.from_env())
    await nats_store.connect()

    identity = _fixed_identity()
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    vault = CredentialVault(pg_store)

    descriptor_registry = DescriptorRegistry(
        pg_store,
        nats_store=nats_store,
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
        nats_store=nats_store,
        conversion_registry=None,
    )

    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_source_credibility_router(deps), prefix="/api/v1")

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unique_host(prefix: str) -> str:
    return f"{prefix}.{uuid4().hex[:8]}.example.com"


async def _direct_count(pg_store: PostgresStore, host: str) -> int:
    async with pg_store.acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM source_credibility WHERE source_host = $1",
            host,
        )
    return int(n)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_returns_seed_rows(client: AsyncClient):
    """Migration 0014 seeds the canonical baseline — list returns them."""
    r = await client.get("/api/v1/source_credibility")
    assert r.status_code == 200
    rows = r.json()
    hosts = {row["host"] for row in rows}
    # Migration 0014 seeds at least 12 canonical rows.
    assert "reuters.com" in hosts
    assert "bbc.com" in hosts
    assert "infowars.com" in hosts


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_with_host_filter(client: AsyncClient):
    """`?host=substring` does an ILIKE substring match."""
    r = await client.get("/api/v1/source_credibility", params={"host": "bbc"})
    assert r.status_code == 200
    rows = r.json()
    hosts = [row["host"] for row in rows]
    assert all("bbc" in h for h in hosts)
    # The two seeded bbc rows are present.
    assert "bbc.com" in hosts
    assert "bbc.co.uk" in hosts


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_single_host_happy_path(client: AsyncClient):
    r = await client.get("/api/v1/source_credibility/reuters.com")
    assert r.status_code == 200
    body = r.json()
    assert body["host"] == "reuters.com"
    assert 0.0 <= body["score"] <= 1.0
    assert body["score"] >= 0.85
    assert body["scored_by"] == "system.seed"
    assert body["score_rationale"]
    assert body["scored_at"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_single_host_404(client: AsyncClient):
    r = await client.get(
        f"/api/v1/source_credibility/{_unique_host('never-seen')}",
    )
    assert r.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_inserts_new_host(api_app, client: AsyncClient):
    _, _, pg_store = api_app
    host = _unique_host("insert")
    assert await _direct_count(pg_store, host) == 0

    r = await client.put(
        f"/api/v1/source_credibility/{host}",
        json={
            "score": 0.55,
            "score_rationale": "Operator-added mid-tier source.",
            "scored_by": "operator:lewis",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["host"] == host
    assert body["score"] == 0.55
    assert body["scored_by"] == "operator:lewis"
    assert body["score_rationale"] == "Operator-added mid-tier source."

    # Persisted.
    assert await _direct_count(pg_store, host) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_updates_existing_host(api_app, client: AsyncClient):
    _, _, pg_store = api_app
    host = _unique_host("update")
    # Insert via direct SQL so the test doesn't depend on the PUT path.
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO source_credibility
                (source_host, score, score_rationale, scored_by, last_updated)
            VALUES ($1, 0.20, 'initial', 'system.seed', NOW())
            """,
            host,
        )

    r = await client.put(
        f"/api/v1/source_credibility/{host}",
        json={
            "score": 0.80,
            "score_rationale": "Operator reviewed; raised after fact-check audit.",
            "scored_by": "operator:lewis",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["score"] == 0.80
    assert body["scored_by"] == "operator:lewis"
    # No duplicate row.
    assert await _direct_count(pg_store, host) == 1

    # GET picks up the update.
    r = await client.get(f"/api/v1/source_credibility/{host}")
    assert r.status_code == 200
    assert r.json()["score"] == 0.80


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_rejects_score_out_of_range(client: AsyncClient):
    host = _unique_host("range")
    r = await client.put(
        f"/api/v1/source_credibility/{host}",
        json={"score": 1.5, "scored_by": "operator:lewis"},
    )
    assert r.status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_existing_row(api_app, client: AsyncClient):
    _, _, pg_store = api_app
    host = _unique_host("delete")
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO source_credibility
                (source_host, score, score_rationale, scored_by, last_updated)
            VALUES ($1, 0.5, 'transient', 'test', NOW())
            """,
            host,
        )

    r = await client.delete(f"/api/v1/source_credibility/{host}")
    assert r.status_code == 200
    assert r.json() == {"host": host, "removed": True}
    assert await _direct_count(pg_store, host) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_404(client: AsyncClient):
    r = await client.delete(
        f"/api/v1/source_credibility/{_unique_host('absent')}",
    )
    assert r.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_upload_three_rows_happy_path(api_app, client: AsyncClient):
    _, _, pg_store = api_app
    h1 = _unique_host("bulk1")
    h2 = _unique_host("bulk2")
    h3 = _unique_host("bulk3")

    csv_body = (
        "host,score,score_rationale,scored_by\n"
        f"{h1},0.90,Top tier wire,operator:lewis\n"
        f"{h2},0.55,Mid tier regional,operator:lewis\n"
        f"{h3},0.10,Known disinfo node,operator:lewis\n"
    )
    r = await client.post(
        "/api/v1/source_credibility/bulk",
        content=csv_body,
        headers={"Content-Type": "text/csv"},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["inserted"] == 3
    assert out["updated"] == 0
    assert out["errors"] == []

    # All three persisted with the right scores.
    async with pg_store.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source_host, score, scored_by FROM source_credibility "
            "WHERE source_host = ANY($1::text[])",
            [h1, h2, h3],
        )
    persisted = {r["source_host"]: (r["score"], r["scored_by"]) for r in rows}
    assert persisted == {
        h1: (0.90, "operator:lewis"),
        h2: (0.55, "operator:lewis"),
        h3: (0.10, "operator:lewis"),
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_upload_reports_per_row_errors(api_app, client: AsyncClient):
    """One bad row (score outside [0.0, 1.0]) should not fail the whole upload."""
    _, _, pg_store = api_app
    h1 = _unique_host("bulkok1")
    h2 = _unique_host("bulkbad")
    h3 = _unique_host("bulkok2")

    csv_body = (
        "host,score,score_rationale,scored_by\n"
        f"{h1},0.85,good,operator:lewis\n"
        f"{h2},1.55,bad score,operator:lewis\n"
        f"{h3},0.42,also good,operator:lewis\n"
    )
    r = await client.post(
        "/api/v1/source_credibility/bulk",
        content=csv_body,
        headers={"Content-Type": "text/csv"},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["inserted"] == 2
    assert out["updated"] == 0
    assert len(out["errors"]) == 1
    err = out["errors"][0]
    assert err["host"] == h2
    assert "outside [0.0, 1.0]" in err["error"]

    # Only the two good rows landed.
    assert await _direct_count(pg_store, h1) == 1
    assert await _direct_count(pg_store, h2) == 0
    assert await _direct_count(pg_store, h3) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_upload_rejects_non_csv_content_type(client: AsyncClient):
    r = await client.post(
        "/api/v1/source_credibility/bulk",
        content=b'{"host": "x"}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 415


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_upload_updates_existing_row(api_app, client: AsyncClient):
    """CSV row whose host already exists is an UPDATE, not an INSERT."""
    _, _, pg_store = api_app
    host = _unique_host("bulkupd")
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO source_credibility
                (source_host, score, score_rationale, scored_by, last_updated)
            VALUES ($1, 0.1, 'initial', 'system.seed', NOW())
            """,
            host,
        )

    csv_body = (
        "host,score,score_rationale,scored_by\n"
        f"{host},0.7,raised by audit,operator:lewis\n"
    )
    r = await client.post(
        "/api/v1/source_credibility/bulk",
        content=csv_body,
        headers={"Content-Type": "text/csv"},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["inserted"] == 0
    assert out["updated"] == 1
    assert out["errors"] == []

    # Verify the score got bumped.
    async with pg_store.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT score, scored_by FROM source_credibility WHERE source_host = $1",
            host,
        )
    assert row["score"] == 0.7
    assert row["scored_by"] == "operator:lewis"
