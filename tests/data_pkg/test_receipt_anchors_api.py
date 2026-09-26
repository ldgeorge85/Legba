# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/system/receipt-anchors`` — the H13 external-timestamp ledger read.

The route lists the (day, calendar) ``receipt_anchors`` rows the
``receipt_anchor`` handler writes: Merkle root, leaf count, calendar, status
('submitted'/'pending') and the stored proof bytes. Tests pin:

  * the real binding path (the router mounted on a FastAPI app over a migrated
    test DB, like the staleness-debt precedent);
  * the honest empty state — an absent/unreadable ledger is ``[]`` at HTTP 200,
    never a 500;
  * the slim-image import guard — the module ships in the registry image, so it
    must import with the analyst-runtime dependency stack poisoned.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.receipt_anchors_api import (
    RECEIPT_ANCHORS_ROUTE_VERSION,
    build_receipt_anchors_router,
)
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "66" * 32)

_ROUTE = "/api/v1/v3/system/receipt-anchors"


@pytest_asyncio.fixture
async def api_app(migrated_pg: PostgresConfig, clean_tables):
    os.environ.pop(API_TOKEN_ENV, None)
    await clean_tables("receipt_anchors")

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()
    identity = SigningIdentity(
        signing_key=SigningKey(b"h13-receipt-anchors-route-seed1!"[:32]),
        signer_did="did:legba:registry:h13-receipt-anchors-test",
    )
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
    deps = RegistryAPIDeps(
        descriptor_registry=descriptor_registry,
        stack_registry=StackRegistry(pg_store, vault, audit=audit, dlq=dlq),
        vault=vault,
        dlq=dlq,
        audit_logger=audit,
        vocabulary_cache=vocab,
        nats_store=None,
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_receipt_anchors_router(deps), prefix="/api/v1/v3")
    yield app, pg_store
    await descriptor_registry.stop()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(api_app):
    app, _ = api_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_ledger_reads_honest_empty(client):
    r = await client.get(_ROUTE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == RECEIPT_ANCHORS_ROUTE_VERSION
    assert body["anchors"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_route_lists_seeded_anchor_rows(client, api_app):
    _, pg_store = api_app
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO receipt_anchors
                (day, root_hash, leaf_count, calendar, proof, status,
                 submitted_at)
            VALUES
                ('2026-09-24', 'aa' || repeat('11', 31), 42,
                 'https://a.pool.opentimestamps.org/digest',
                 '\\x00ff'::bytea, 'submitted', now()),
                ('2026-09-24', 'aa' || repeat('11', 31), 42,
                 'https://b.pool.opentimestamps.org/digest',
                 NULL, 'pending', NULL)
            """
        )
    r = await client.get(_ROUTE)
    assert r.status_code == 200, r.text
    anchors = r.json()["anchors"]
    assert len(anchors) == 2
    by_cal = {a["calendar"].split("//")[1].split(".")[0]: a for a in anchors}
    a_row = by_cal["a"]
    b_row = by_cal["b"]
    assert a_row["status"] == "submitted"
    assert a_row["proof"] == "00ff"
    assert a_row["submitted_at"] is not None
    assert a_row["leaf_count"] == 42
    assert a_row["day"] == "2026-09-24"
    assert b_row["status"] == "pending"
    assert b_row["proof"] is None


# ---------------------------------------------------------------------------
# The slim-image import guard (the graph_triggers_api precedent)
# ---------------------------------------------------------------------------

_SRC = str(pathlib.Path(__file__).resolve().parents[2] / "src")


def test_receipt_anchors_route_imports_without_the_runtime_stack() -> None:
    """The route must import in the slim image: no analyst/runtime modules in
    the import graph, and the router factory is present."""
    probe = textwrap.dedent(
        """
        import sys
        # Exactly what the slim image does to these: not present.
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None

        import legba.data.registry.receipt_anchors_api as api

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts")
               or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime package reachable from the route: %r" % leaked
        assert api.build_receipt_anchors_router is not None
        assert api.RECEIPT_ANCHORS_ROUTE_VERSION == "2026-09/h13"
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=120,
        # Keep the parent's PYTHONPATH tail: in the test container the deps
        # live outside src/, reachable ONLY through it (2026-09-22 fix).
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0, (
        f"receipt_anchors_api must import without the analyst runtime stack.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
