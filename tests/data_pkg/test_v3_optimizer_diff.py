# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for ``GET /api/v1/v3/optimizer/candidates/{id}/diff``.

Covers the prompt-module diff route added to
:mod:`legba.data.registry.v3_api`. The route builds the current-vs-candidate
diff ENTIRELY from substrate (the persisted candidate row's
``parent_prompt_module_text`` snapshot + an optional live promoted-prompt
override) — it must NEVER import dspy or the prompt package.

The substrate is real (``migrated_pg`` fixture from
``tests/data_pkg/conftest.py``); no mocks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.config import NatsConfig, PostgresConfig
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps, build_router
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.v3_api import build_v3_router
from legba.data.registry.vocabulary_cache import VocabularyCache

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "44" * 32)


def _fixed_identity() -> SigningIdentity:
    seed = b"v3-optimizer-diff-test-seedXXXXX"[:32]
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:v3-diff-test",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api_app(migrated_pg: PostgresConfig):
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
    pg_store: PostgresStore,
    *,
    analyst_id: str,
    parent_text: str,
    candidate_text: str,
    gepa_generation: int = 1,
    promotion_gate: str = "human_gated",
    eval_score: float = 0.71,
    eval_score_delta: float = 0.06,
) -> UUID:
    """Persist a ``prompt_module_candidate`` row, return its id."""
    payload: dict[str, Any] = {
        "kind_marker": "prompt_module_candidate",
        "analyst_id": analyst_id,
        "analyst_version": "deadbeef" * 8,
        "parent_prompt_module_path": "legba.prompts.india.v1",
        "candidate_prompt_module_text": candidate_text,
        "parent_prompt_module_text": parent_text,
        "training_set_size": 42,
        "eval_score": eval_score,
        "eval_score_delta": eval_score_delta,
        "gepa_generation": gepa_generation,
        "promotion_gate": promotion_gate,
        "temporal_workflow_id": "wf_test_diff",
        "temporal_run_id": "run_test_diff",
        "data": {"method": "naive_best_of_n"},
    }
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_diff_returns_snapshot_current_vs_candidate(api_app, client: AsyncClient):
    _, _, pg_store = api_app
    analyst_id = f"india_{uuid4().hex[:8]}"
    parent_text = "You are the India analyst.\nLine A.\nLine B.\n"
    candidate_text = "You are the India analyst.\nLine A.\nLine B (revised).\nLine C.\n"

    cid = await _insert_candidate(
        pg_store,
        analyst_id=analyst_id,
        parent_text=parent_text,
        candidate_text=candidate_text,
    )

    r = await client.get(f"/api/v1/v3/optimizer/candidates/{cid}/diff")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["candidate_id"] == str(cid)
    assert body["analyst_id"] == analyst_id
    # No promoted row exists → current_text is the parent snapshot.
    assert body["current_text"] == parent_text
    assert body["candidate_text"] == candidate_text
    assert body["current_module_path"] == "legba.prompts.india.v1"
    assert body["candidate_module_path"] == "legba.prompts.india.v1.gepa_gen_1"
    assert body["eval_score"] == pytest.approx(0.71)
    assert body["eval_score_delta"] == pytest.approx(0.06)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_diff_uses_live_promoted_prompt_as_current(api_app, client: AsyncClient):
    _, _, pg_store = api_app
    analyst_id = f"india_{uuid4().hex[:8]}"

    # A previously-promoted candidate is the analyst's live prompt today.
    await _insert_candidate(
        pg_store,
        analyst_id=analyst_id,
        parent_text="original baseline",
        candidate_text="PROMOTED live prompt text",
        gepa_generation=1,
        promotion_gate="promoted",
    )
    # The candidate under review carries a stale snapshot of its own parent.
    cid = await _insert_candidate(
        pg_store,
        analyst_id=analyst_id,
        parent_text="stale parent snapshot",
        candidate_text="the proposed new prompt",
        gepa_generation=2,
    )

    r = await client.get(f"/api/v1/v3/optimizer/candidates/{cid}/diff")
    assert r.status_code == 200, r.text
    body = r.json()
    # current_text should be the LIVE promoted prompt, not the stale snapshot.
    assert body["current_text"] == "PROMOTED live prompt text"
    assert body["candidate_text"] == "the proposed new prompt"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_diff_degrades_when_snapshot_missing(api_app, client: AsyncClient):
    """Rows written before the snapshot field existed → empty current_text."""
    _, _, pg_store = api_app
    analyst_id = f"india_{uuid4().hex[:8]}"
    # Insert a candidate WITHOUT parent_prompt_module_text (old-row shape).
    payload = {
        "kind_marker": "prompt_module_candidate",
        "analyst_id": analyst_id,
        "analyst_version": "deadbeef" * 8,
        "parent_prompt_module_path": "legba.prompts.india.v1",
        "candidate_prompt_module_text": "candidate body only",
        "training_set_size": 10,
        "eval_score": 0.5,
        "eval_score_delta": 0.0,
        "gepa_generation": 1,
        "promotion_gate": "human_gated",
        "data": {},
    }
    row_id = uuid4()
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                target_id, target_version, analyst_id, analyst_version,
                produced_at, derived_from, schema_uri, run_id
            ) VALUES (
                $1, 'prompt_module_candidate', 'old row', '', 0.5, NULL, $2::jsonb,
                NULL, NULL, 'optimizer.analyst', 'aaaa' || repeat('0', 60),
                NOW(), '{}'::uuid[],
                'iglu:legba/prompt_module_candidate/jsonschema/1-0-0', $3
            )
            """,
            row_id, json.dumps(payload), uuid4(),
        )

    r = await client.get(f"/api/v1/v3/optimizer/candidates/{row_id}/diff")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current_text"] == ""
    assert body["candidate_text"] == "candidate body only"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_diff_404_for_unknown_candidate(api_app, client: AsyncClient):
    r = await client.get(f"/api/v1/v3/optimizer/candidates/{uuid4()}/diff")
    assert r.status_code == 404
    # A non-UUID id is also a clean 404, never a 500.
    r = await client.get("/api/v1/v3/optimizer/candidates/not-a-uuid/diff")
    assert r.status_code == 404


def test_diff_route_does_not_import_dspy():
    """The registry process must never import dspy to serve the diff route.

    Guards the L-176 invariant: dspy lives only in the opt-in GEPA worker. The
    diff route reads the parent snapshot from substrate, so importing the v3
    API module (and exercising its route table) must not drag dspy in.

    RUN IN A SUBPROCESS, deliberately. Asserting on the CURRENT process's
    ``sys.modules`` measured the whole session's import history, not this
    module's imports: any earlier test that touched ``legba.prompts`` left
    dspy resident and failed this one, while in file order it passed. That
    made it an ORDER-DEPENDENT test — worthless as a guard (in a dspy-free
    image it can never fail; in a dspy-bearing one it fails for reasons that
    have nothing to do with the route) and an entry on the nightly's
    known-failure allowlist. A fresh interpreter measures exactly the claim:
    importing this module, and nothing else, must not pull dspy in.
    """
    probe = (
        "import sys\n"
        "import legba.data.registry.v3_api  # noqa: F401\n"
        "sys.exit(3 if 'dspy' in sys.modules else 0)\n"
    )
    # Hand the child THIS interpreter's resolved sys.path so it imports the
    # same tree under test — inheriting PYTHONPATH alone is not enough when
    # the parent's path came from anywhere else (editable install, conftest
    # insert, `-p` plugin bootstrap).
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert proc.returncode != 3, (
        "dspy was imported by the registry v3 API module — the diff route must "
        "stay dspy-free (snapshot-based), per the litellm/dspy production ban."
    )
    assert proc.returncode == 0, (
        "the dspy-freedom probe could not even import "
        f"legba.data.registry.v3_api (rc={proc.returncode}):\n{proc.stderr}"
    )
