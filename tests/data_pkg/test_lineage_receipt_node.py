# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Receipt-chain node enrichment for the lineage walk (P0-T4, PROVENANCE).

These are pure-logic tests (no DB) over ``_receipt_node_from_trace`` — the
function the lineage router calls to attach the analyst_traces receipt chain
to a finding's root node. They pin the HONESTY contract that the glass-tower
oracle exists to enforce:

  * ``chain_consistent`` is RE-COMPUTED (re-hash the trace, compare to the
    stored ``receipt_hash``) — True for an intact row, and it FLIPS to False
    the instant the payload is mutated. It never trusts a stored boolean.
  * The badge is EXACTLY ``"chain-consistent (single-node)"`` — the chain is a
    SHA-256 hash-chain, not an Ed25519 signature, so no field may claim
    "signed", "tamper-proof", or Ed25519 for the per-row receipt.
  * ``signer_did`` is present ONLY when an ``audit_checkpoints`` row whose
    ``chain_head_hash`` equals this trace's ``receipt_hash`` actually covers
    it; otherwise it is honestly ``None``.

The integration suite (``test_lineage_api.py``) exercises the real DB join
path; here we isolate the recompute + honesty so the contract is asserted even
without Postgres.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.provenance._core import ZERO_HASH, compute_receipt_hash
from legba.data.provenance.receipts import RuntimeReceiptChain
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.lineage_api import (
    _RECEIPT_BADGE,
    ReceiptChainNode,
    _receipt_node_from_trace,
    build_lineage_router,
)


# ---------------------------------------------------------------------------
# Trace fixtures — shaped exactly like an ``analyst_traces`` row as asyncpg
# returns it (output_payload is jsonb → JSON *string*).
# ---------------------------------------------------------------------------


def _make_trace(*, output_payload: dict, prev_hash: str | None = ZERO_HASH):
    """Build a trace dict + its honestly-computed stored receipt_hash.

    Mirrors ``RuntimeReceiptChain.record``: the receipt is computed over the
    *parsed* payload object, then the column stores the jsonb *string*. So the
    stored hash here is the ground truth a faithful re-hash must reproduce.
    """
    run_id = uuid4()
    analyst_id = "an_receipt_test"
    analyst_version = "v1"
    in_refs = [uuid4(), uuid4()]
    out_refs = [uuid4()]
    prompt_hash = "pmod-abc"
    prompt_rendered = "render the world"
    run_ended_at = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)

    stored_hash = compute_receipt_hash(
        run_id=run_id,
        analyst_id=analyst_id,
        analyst_version=analyst_version,
        input_row_refs=in_refs,
        prompt_module_hash=prompt_hash,
        prompt_rendered=prompt_rendered,
        output_row_refs=out_refs,
        output_payload=output_payload,
        run_ended_at=run_ended_at,
        prev_receipt_hash=prev_hash,
    )

    # asyncpg returns jsonb columns as JSON strings — mimic that exactly so the
    # helper's parse-then-rehash path is what gets exercised.
    trace = {
        "run_id": run_id,
        "analyst_id": analyst_id,
        "analyst_version": analyst_version,
        "input_row_refs": in_refs,
        "prompt_module_hash": prompt_hash,
        "prompt_rendered": prompt_rendered,
        "output_row_refs": out_refs,
        "output_payload": json.dumps(output_payload),
        "run_ended_at": run_ended_at,
        "receipt_hash": stored_hash,
        "prev_receipt_hash": prev_hash,
    }
    return trace, stored_hash


# ---------------------------------------------------------------------------
# Recompute + chain_consistent
# ---------------------------------------------------------------------------


def test_intact_chain_recomputes_and_is_consistent():
    """An untouched trace re-hashes to the stored receipt_hash → consistent."""
    trace, stored = _make_trace(output_payload={"summary": "all quiet"})

    node = _receipt_node_from_trace(trace)

    assert isinstance(node, ReceiptChainNode)
    assert node.receipt_hash == stored
    assert node.chain_consistent is True
    assert node.prev_receipt_hash == ZERO_HASH
    assert node.run_id == str(trace["run_id"])


def test_mutated_payload_flips_chain_consistent_to_false():
    """Mutating the recorded payload (without re-deriving the hash, i.e. a
    tampered row) must flip chain_consistent to False — the whole point."""
    trace, _stored = _make_trace(output_payload={"summary": "all quiet"})

    # Tamper: rewrite the stored payload but leave the old receipt_hash in
    # place (an attacker/bug mutating the row in situ).
    trace["output_payload"] = json.dumps({"summary": "WAR DECLARED"})

    node = _receipt_node_from_trace(trace)

    assert node.chain_consistent is False
    # The stored hash is still surfaced verbatim — we report what's there, we
    # just don't VOUCH for it.
    assert node.receipt_hash == trace["receipt_hash"]


def test_null_output_payload_rehashes_against_empty_object():
    """The writer stores a NULL/absent payload as ``{}`` (json.dumps(x or {})),
    so a NULL column must re-hash against ``{}`` and stay consistent."""
    trace, stored = _make_trace(output_payload={})
    # Now drop the column to NULL as the DB would for an absent payload.
    trace["output_payload"] = None

    node = _receipt_node_from_trace(trace)

    assert node.receipt_hash == stored
    assert node.chain_consistent is True


# ---------------------------------------------------------------------------
# Honest badge + no false guarantees
# ---------------------------------------------------------------------------


def test_badge_is_exactly_chain_consistent_single_node():
    trace, _ = _make_trace(output_payload={"summary": "x"})
    node = _receipt_node_from_trace(trace)

    assert node.badge == "chain-consistent (single-node)"
    assert _RECEIPT_BADGE == "chain-consistent (single-node)"


def test_no_field_claims_tamper_proof_or_ed25519():
    """No produced field anywhere may claim tamper-proof / signed / Ed25519
    for the per-row analyst_traces receipt."""
    trace, _ = _make_trace(output_payload={"summary": "x"})
    node = _receipt_node_from_trace(trace)

    blob = json.dumps(node.model_dump(), default=str).lower()
    assert "tamper-proof" not in blob
    assert "tamper proof" not in blob
    assert "ed25519" not in blob
    # "signed" must not appear — the badge is the only descriptive string and
    # it deliberately avoids that word.
    assert "signed" not in blob


# ---------------------------------------------------------------------------
# signer_did honesty — only when a checkpoint truly covers the row
# ---------------------------------------------------------------------------


def test_signer_did_none_without_checkpoint():
    trace, _ = _make_trace(output_payload={"summary": "x"})
    node = _receipt_node_from_trace(trace)
    assert node.signer_did is None


def test_signer_did_present_when_checkpoint_covers_the_head():
    """A checkpoint whose chain_head_hash == this receipt_hash covers the row,
    so its signer_did is surfaced."""
    trace, stored = _make_trace(output_payload={"summary": "x"})
    checkpoint = {
        "chain_head_hash": stored,
        "signer_did": "did:key:z6MkExampleDeploymentSigner",
    }

    node = _receipt_node_from_trace(trace, covering_checkpoint=checkpoint)

    assert node.signer_did == "did:key:z6MkExampleDeploymentSigner"
    # Even WITH a real Ed25519 checkpoint, the per-row badge stays honest —
    # the checkpoint signs the head, not the individual receipt.
    assert node.badge == "chain-consistent (single-node)"


def test_signer_did_omitted_when_checkpoint_head_mismatches():
    """A checkpoint for a DIFFERENT head does not cover this row — signer_did
    must stay None rather than borrow an unrelated signer."""
    trace, _stored = _make_trace(output_payload={"summary": "x"})
    checkpoint = {
        "chain_head_hash": "f" * 64,  # some other chain head
        "signer_did": "did:key:z6MkSomeoneElse",
    }

    node = _receipt_node_from_trace(trace, covering_checkpoint=checkpoint)

    assert node.signer_did is None


# ---------------------------------------------------------------------------
# Integration — the real router join (finding → analyst_traces → checkpoint).
# Reuses the session-scoped ``migrated_pg`` fixture from data_pkg/conftest.py.
# ---------------------------------------------------------------------------


class _MinimalDescriptorRegistry:
    def __init__(self, pg_store: PostgresStore) -> None:
        self.pg = pg_store


@pytest_asyncio.fixture
async def receipt_lineage_app(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)
    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()
    deps = RegistryAPIDeps(
        descriptor_registry=_MinimalDescriptorRegistry(pg_store),  # type: ignore[arg-type]
        stack_registry=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        dlq=None,  # type: ignore[arg-type]
        audit_logger=None,  # type: ignore[arg-type]
        vocabulary_cache=None,  # type: ignore[arg-type]
        nats_store=None,
        conversion_registry=None,
    )
    app = FastAPI()
    app.include_router(build_lineage_router(deps), prefix="/api/v1")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        yield client, pg_store
    await pg_store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_root_finding_carries_recomputed_receipt(receipt_lineage_app):
    """End-to-end: a finding produced by a recorded run carries its receipt
    on the lineage root, with chain_consistent recomputed True and the honest
    single-node badge; signer_did is None until a checkpoint covers the head,
    then surfaces."""
    client, pg_store = receipt_lineage_app

    run_id = uuid4()
    finding_id = uuid4()
    analyst_id = "an_receipt_int"
    run_started = datetime(2026, 6, 30, 11, 0, 0, tzinfo=timezone.utc)
    run_ended = datetime(2026, 6, 30, 11, 0, 5, tzinfo=timezone.utc)

    async with pg_store.acquire() as conn:
        # The finding row, carrying its producing run_id.
        await conn.execute(
            """
            INSERT INTO analyst_outputs
                (id, kind, title, body, analyst_id, analyst_version,
                 produced_at, derived_from, schema_uri, run_id)
            VALUES ($1, 'finding', 'receipt_finding', '', $2, 'v1',
                    NOW(), '{}'::uuid[],
                    'iglu:legba/finding/jsonschema/1-0-0', $3)
            """,
            finding_id, analyst_id, run_id,
        )

        # Record the analyst_trace via the real receipt-chain writer so the
        # stored receipt_hash is the ground truth (output_row_refs includes
        # the finding; run_id matches).
        chain = RuntimeReceiptChain(pg_store.pool)
        receipt_hash, prev_hash = await chain.record(
            run_id=run_id,
            analyst_id=analyst_id,
            analyst_version="v1",
            cadence_trigger="manual",
            target_id="br",
            input_row_refs=[],
            input_payload={"slice": "x"},
            prompt_module_hash="pmod-int",
            prompt_rendered="render",
            output_row_refs=[finding_id],
            output_payload={"summary": "intact"},
            run_started_at=run_started,
            run_ended_at=run_ended,
        )

    r = await client.get(
        f"/api/v1/lineage/finding/{finding_id}",
        params={"direction": "upstream", "depth": 3},
    )
    assert r.status_code == 200, r.text
    receipt = r.json()["root"]["receipt"]
    assert receipt is not None
    assert receipt["run_id"] == str(run_id)
    assert receipt["receipt_hash"] == receipt_hash
    assert receipt["prev_receipt_hash"] == prev_hash
    assert receipt["chain_consistent"] is True
    assert receipt["badge"] == "chain-consistent (single-node)"
    # No checkpoint yet → no signer.
    assert receipt["signer_did"] is None

    # Now write an audit_checkpoint covering THIS receipt as the chain head.
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO audit_checkpoints
                (analyst_id, chain_head_hash, trace_count,
                 checkpointed_at, signature, signer_did)
            VALUES ($1, $2, 1, NOW(), '\\x00'::bytea, $3)
            """,
            analyst_id, receipt_hash, "did:key:z6MkDeploymentSigner",
        )

    r2 = await client.get(
        f"/api/v1/lineage/finding/{finding_id}",
        params={"direction": "upstream", "depth": 3},
    )
    assert r2.status_code == 200, r2.text
    receipt2 = r2.json()["root"]["receipt"]
    assert receipt2["signer_did"] == "did:key:z6MkDeploymentSigner"
    # The badge stays honest even with a covering checkpoint.
    assert receipt2["badge"] == "chain-consistent (single-node)"
