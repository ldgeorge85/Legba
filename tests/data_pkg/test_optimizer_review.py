# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the P-11 optimizer-candidate review mutation.

Covers the ``POST /api/v1/v3/optimizer/candidates/{id}/review`` endpoint
added in :mod:`legba.data.registry.v3_api`.

Promotion path
--------------

The test:

  1. Registers a real analyst descriptor through the registry
     (India analyst, ``method.prompt_module = "legba.prompts.india.v1"``).
  2. Inserts a fake candidate row into ``analyst_outputs`` with
     ``kind='prompt_module_candidate'`` and ``analyst_id`` set to that
     descriptor.
  3. Calls the review endpoint with ``action='promote'``.
  4. Asserts:
       * a new descriptor head was minted (new content-hash);
       * the new descriptor body's ``method.prompt_module`` flipped to the
         derived candidate path (``legba.prompts.india.v1.gepa_gen_1``);
       * a signed audit-log row landed against the analyst (action='update');
       * the candidate row's ``data->promotion_gate`` is now ``'promoted'``.

Reject path
-----------

  1. Same fixture set-up.
  2. Calls the review endpoint with ``action='reject'``.
  3. Asserts:
       * no new descriptor version (the head is unchanged);
       * one audit row exists tagged ``action='optimizer_reject'`` for the
         analyst, carrying the reviewer's note in ``change_summary``;
       * the candidate row's ``data->promotion_gate`` is ``'rejected'``.

The substrate is real (``migrated_pg`` fixture from
``tests/data_pkg/conftest.py``); no mocks.
"""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.config import NatsConfig, PostgresConfig
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.registry.api import (
    API_TOKEN_ENV,
    RegistryAPIDeps,
    build_router,
)
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry, Family
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.v3_api import build_v3_router
from legba.data.registry.vocabulary_cache import VocabularyCache


_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "44" * 32)


def _fixed_identity() -> SigningIdentity:
    seed = b"p11-optimizer-review-test-seedXX"[:32]
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:p11-review-test",
    )


def _india_analyst_body(descriptor_id: str) -> dict[str, Any]:
    return {
        "identity": {
            "id": descriptor_id,
            "name": f"India analyst {descriptor_id}",
            "schema_uri": "legba/analyst/2.0.0",
            "version": "0" * 16,
            "kind": "inline_target",
            "type_signature": {
                "input_type": "legba.x.IndiaIn",
                "output_type": "legba.x.IndiaOut",
            },
            "owner": "lewis@local",
        },
        "subscription": {},
        "method": {
            "kind": "llm_planner",
            "prompt_module": "legba.prompts.india.v1",
        },
        "cadence": {},
    }


def _candidate_payload(*, analyst_id: str, gepa_generation: int = 1) -> dict[str, Any]:
    """Minimal stored shape that `_apply_optimizer_review` reads."""
    return {
        "kind_marker": "prompt_module_candidate",
        "analyst_id": analyst_id,
        "analyst_version": "deadbeef" * 8,
        "parent_prompt_module_path": "legba.prompts.india.v1",
        "candidate_prompt_module_text": (
            "You are the India energy analyst. Be terse, cite priors."
        ),
        "training_set_size": 42,
        "eval_score": 0.71,
        "eval_score_delta": 0.06,
        "gepa_generation": gepa_generation,
        "promotion_gate": "human_gated",
        "temporal_workflow_id": "wf_test_optimizer_review",
        "temporal_run_id": "run_test_optimizer_review",
        "data": {
            "diagnostics": {"baseline_score": 0.65, "method": "dspy_gepa"},
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api_app(migrated_pg: PostgresConfig):
    """Build a full registry + v3 app against the migrated test DB."""
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
    app.include_router(build_router(deps), prefix="/api/v1/registry")
    app.include_router(build_v3_router(deps), prefix="/api/v1/v3")

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


async def _insert_candidate(
    pg_store: PostgresStore, *, analyst_id: str, gepa_generation: int = 1,
) -> UUID:
    """Persist a `prompt_module_candidate` row, return its id.

    Bypasses the runtime's `write_analyst_output` because that path is
    only available when an actor is wired; the substrate behavior the
    review endpoint cares about is purely a row presence + JSONB shape.
    """
    payload = _candidate_payload(
        analyst_id=analyst_id, gepa_generation=gepa_generation,
    )
    row_id = uuid4()
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                target_id, target_version, analyst_id, analyst_version,
                produced_at, derived_from, schema_uri, run_id
            ) VALUES (
                $1, 'prompt_module_candidate', $2, '', 0.71, NULL, $3::jsonb,
                NULL, NULL, 'optimizer.analyst', 'aaaa' || repeat('0', 60),
                NOW(), '{}'::uuid[],
                'iglu:legba/prompt_module_candidate/jsonschema/1-0-0',
                $4
            )
            """,
            row_id,
            f"Optimizer candidate for {analyst_id}",
            json.dumps(payload),
            uuid4(),
        )
    return row_id


async def _audit_rows(
    pg_store: PostgresStore, descriptor_id: str,
) -> list[dict[str, Any]]:
    async with pg_store.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, action, actor_id, change_summary, from_version,
                   to_version, namespace
              FROM descriptor_audit_log
             WHERE descriptor_id = $1
             ORDER BY occurred_at ASC
            """,
            descriptor_id,
        )
    out = []
    for r in rows:
        summary = r["change_summary"]
        if isinstance(summary, str):
            summary = json.loads(summary)
        out.append(
            {
                "id": str(r["id"]),
                "action": r["action"],
                "actor_id": r["actor_id"],
                "change_summary": summary,
                "from_version": r["from_version"],
                "to_version": r["to_version"],
                "namespace": r["namespace"],
            }
        )
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_promote_flips_prompt_module_and_writes_audit(
    api_app, client: AsyncClient,
):
    _, _, pg_store = api_app
    analyst_id = f"india_analyst_{uuid4().hex[:8]}"

    # 1. Register the parent analyst descriptor.
    r = await client.post(
        "/api/v1/registry/descriptors/analyst",
        json=_india_analyst_body(analyst_id),
    )
    assert r.status_code == 201, r.text
    parent = r.json()
    parent_version = parent["version"]
    assert parent["body"]["method"]["prompt_module"] == "legba.prompts.india.v1"

    # 2. Insert a candidate pointed at this analyst.
    cand_id = await _insert_candidate(
        pg_store, analyst_id=analyst_id, gepa_generation=1,
    )

    # 3. Promote.
    r = await client.post(
        f"/api/v1/v3/optimizer/candidates/{cand_id}/review",
        json={
            "action": "promote",
            "reviewer": "operator:lewis",
            "note": "evals look clean; promoting",
        },
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["action"] == "promote"
    assert out["analyst_id"] == analyst_id
    assert out["promotion_gate"] == "promoted"
    new_version = out["new_descriptor_version"]
    assert new_version is not None
    assert new_version != parent_version

    # 4a. New descriptor's prompt_module is the derived path.
    r = await client.get(f"/api/v1/registry/descriptors/analyst/{analyst_id}")
    assert r.status_code == 200
    head = r.json()
    assert head["version"] == new_version
    assert head["is_head"] is True
    assert (
        head["body"]["method"]["prompt_module"]
        == "legba.prompts.india.v1.gepa_gen_1"
    )

    # 4b. History shows both versions.
    r = await client.get(
        f"/api/v1/registry/descriptors/analyst/{analyst_id}/history",
    )
    assert r.status_code == 200
    versions = [h["version"] for h in r.json()]
    assert parent_version in versions
    assert new_version in versions

    # 4c. Audit row landed for the update.
    rows = await _audit_rows(pg_store, analyst_id)
    actions = [r["action"] for r in rows]
    assert "register" in actions
    assert "update" in actions
    update_row = next(r for r in rows if r["action"] == "update")
    assert update_row["from_version"] == parent_version
    assert update_row["to_version"] == new_version
    assert update_row["actor_id"] == "operator:lewis"
    assert update_row["namespace"] == "analyst"

    # 4d. Candidate row's promotion_gate is now 'promoted' with
    #     the new descriptor version recorded inline.
    async with pg_store.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT data FROM analyst_outputs WHERE id = $1", cand_id,
        )
    data = row["data"]
    if isinstance(data, str):
        data = json.loads(data)
    assert data["promotion_gate"] == "promoted"
    assert data["reviewed_by"] == "operator:lewis"
    assert data["promoted_to_descriptor_version"] == new_version
    assert (
        data["promoted_prompt_module_path"]
        == "legba.prompts.india.v1.gepa_gen_1"
    )
    assert data["review_note"] == "evals look clean; promoting"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_writes_audit_and_does_not_touch_descriptor(
    api_app, client: AsyncClient,
):
    _, _, pg_store = api_app
    analyst_id = f"india_analyst_{uuid4().hex[:8]}"

    # 1. Register the parent.
    r = await client.post(
        "/api/v1/registry/descriptors/analyst",
        json=_india_analyst_body(analyst_id),
    )
    assert r.status_code == 201
    parent_version = r.json()["version"]

    cand_id = await _insert_candidate(pg_store, analyst_id=analyst_id)

    # 2. Reject.
    r = await client.post(
        f"/api/v1/v3/optimizer/candidates/{cand_id}/review",
        json={
            "action": "reject",
            "reviewer": "operator:lewis",
            "note": "regression on holdout; not promoting",
        },
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["action"] == "reject"
    assert out["new_descriptor_version"] is None
    assert out["promotion_gate"] == "rejected"

    # 3. Descriptor head is unchanged.
    r = await client.get(f"/api/v1/registry/descriptors/analyst/{analyst_id}")
    assert r.status_code == 200
    head = r.json()
    assert head["version"] == parent_version
    assert head["body"]["method"]["prompt_module"] == "legba.prompts.india.v1"

    # 4. Audit row tagged 'optimizer_reject' for this analyst.
    rows = await _audit_rows(pg_store, analyst_id)
    actions = [r["action"] for r in rows]
    assert actions.count("optimizer_reject") == 1
    assert "update" not in actions
    reject_row = next(r for r in rows if r["action"] == "optimizer_reject")
    assert reject_row["actor_id"] == "operator:lewis"
    assert (
        reject_row["change_summary"]["reason"]
        == "regression on holdout; not promoting"
    )
    assert reject_row["change_summary"]["candidate_id"] == str(cand_id)
    assert reject_row["change_summary"]["prior_gate"] == "human_gated"
    assert reject_row["namespace"] == "analyst"

    # 5. Candidate row's promotion_gate is now 'rejected'.
    async with pg_store.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT data FROM analyst_outputs WHERE id = $1", cand_id,
        )
    data = row["data"]
    if isinstance(data, str):
        data = json.loads(data)
    assert data["promotion_gate"] == "rejected"
    assert data["reviewed_by"] == "operator:lewis"
    assert data["review_note"] == "regression on holdout; not promoting"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_review_404_when_candidate_missing(client: AsyncClient):
    r = await client.post(
        f"/api/v1/v3/optimizer/candidates/{uuid4()}/review",
        json={"action": "promote", "reviewer": "operator:lewis"},
    )
    assert r.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_review_409_when_candidate_already_decided(
    api_app, client: AsyncClient,
):
    _, _, pg_store = api_app
    analyst_id = f"india_analyst_{uuid4().hex[:8]}"
    r = await client.post(
        "/api/v1/registry/descriptors/analyst",
        json=_india_analyst_body(analyst_id),
    )
    assert r.status_code == 201

    cand_id = await _insert_candidate(pg_store, analyst_id=analyst_id)

    # Decide once.
    r = await client.post(
        f"/api/v1/v3/optimizer/candidates/{cand_id}/review",
        json={"action": "reject", "reviewer": "operator:lewis"},
    )
    assert r.status_code == 200

    # Decide a second time → 409.
    r = await client.post(
        f"/api/v1/v3/optimizer/candidates/{cand_id}/review",
        json={"action": "promote", "reviewer": "operator:lewis"},
    )
    assert r.status_code == 409


@pytest.mark.integration
@pytest.mark.asyncio
async def test_review_400_when_row_kind_mismatch(api_app, client: AsyncClient):
    """A row exists at that id but is the wrong kind → 400."""
    _, _, pg_store = api_app
    bogus_id = uuid4()
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                target_id, target_version, analyst_id, analyst_version,
                produced_at, derived_from, schema_uri
            ) VALUES (
                $1, 'finding', 'bogus', '', 0.5, NULL, '{}'::jsonb,
                NULL, NULL, 'a.b', 'aaaa' || repeat('0', 60),
                NOW(), '{}'::uuid[],
                'iglu:legba/finding/jsonschema/1-0-0'
            )
            """,
            bogus_id,
        )
    r = await client.post(
        f"/api/v1/v3/optimizer/candidates/{bogus_id}/review",
        json={"action": "promote", "reviewer": "operator:lewis"},
    )
    assert r.status_code == 400
