# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-node receipt enrichment across the whole lineage walk (P1-T4, PROVENANCE).

P0-T4 attached the analyst_traces receipt to the ROOT of the lineage walk only.
P1-T4 extends that to EVERY walk node that maps to an analyst run, so the P1-T5
UI can drill the DAG one hop at a time. These tests pin the same HONESTY
contract P0-T4 established, now per WALK node:

  * Every ANALYST-produced node carries a receipt whose ``chain_consistent`` is
    RE-COMPUTED (re-hash the trace, compare to the stored ``receipt_hash``) —
    True for an intact chain, and it FLIPS to False the instant a payload is
    mutated. Never a stored boolean.
  * SIGNAL / source-ingested nodes carry ``receipt=None`` honestly — there is
    no producing analyst run to re-hash, so nothing is fabricated.
  * The badge is EXACTLY ``"chain-consistent (single-node)"`` on every node; no
    produced field anywhere claims "signed", "tamper-proof", or Ed25519 for the
    per-row analyst_traces receipt.

The pure-logic block drives ``_attach_receipts_to_walk`` over a tiny in-memory
connection stub (no Postgres) so the per-node recompute + honesty are asserted
unconditionally. The integration block then exercises the real cross-table DB
join (signal → finding → situation) end-to-end via the router.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
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
    LineageNode,
    ReceiptChainNode,
    _attach_receipts_to_walk,
    build_lineage_router,
)


# ---------------------------------------------------------------------------
# Trace fixtures + an in-memory connection stub.
#
# ``_attach_receipts_to_walk`` issues exactly two query shapes per node:
#   1. ``... WHERE $1 = ANY(output_row_refs) ...``  (map node id → its trace)
#   2. ``... FROM audit_checkpoints WHERE analyst_id = $1 AND chain_head_hash
#      = $2 ...``                                    (covering checkpoint, if any)
# The stub answers both from in-memory dicts so no DB is needed.
# ---------------------------------------------------------------------------


def _make_trace(
    *,
    output_payload: dict,
    output_row_refs: list[UUID],
    analyst_id: str = "an_walk_test",
    prev_hash: str | None = ZERO_HASH,
) -> dict[str, Any]:
    """Build a trace dict shaped like an ``analyst_traces`` row as asyncpg
    returns it (output_payload is jsonb → a JSON *string*), with the stored
    ``receipt_hash`` honestly computed so a faithful re-hash reproduces it."""
    run_id = uuid4()
    analyst_version = "v1"
    in_refs = [uuid4()]
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
        output_row_refs=output_row_refs,
        output_payload=output_payload,
        run_ended_at=run_ended_at,
        prev_receipt_hash=prev_hash,
    )
    return {
        "run_id": run_id,
        "analyst_id": analyst_id,
        "analyst_version": analyst_version,
        "input_row_refs": in_refs,
        "prompt_module_hash": prompt_hash,
        "prompt_rendered": prompt_rendered,
        "output_row_refs": output_row_refs,
        "output_payload": json.dumps(output_payload),
        "run_ended_at": run_ended_at,
        "receipt_hash": stored_hash,
        "prev_receipt_hash": prev_hash,
    }


class _StubConn:
    """Minimal asyncpg.Connection stand-in for the walk-receipt query shapes."""

    def __init__(
        self,
        traces: list[dict[str, Any]],
        checkpoints: list[dict[str, Any]] | None = None,
    ) -> None:
        self._traces = traces
        self._checkpoints = checkpoints or []

    async def fetchrow(self, sql: str, *args: Any):
        if "output_row_refs" in sql:
            # _fetch_trace_for_node id-containment lookup: $1 = node id.
            node_id = args[0]
            hits = [
                t for t in self._traces if node_id in t["output_row_refs"]
            ]
            return hits[0] if hits else None
        if "audit_checkpoints" in sql:
            analyst_id, head = args
            for c in self._checkpoints:
                if (
                    c["analyst_id"] == analyst_id
                    and c["chain_head_hash"] == head
                ):
                    return c
            return None
        raise AssertionError(f"unexpected query: {sql!r}")


def _node(node_id: UUID, *, row_kind: str) -> LineageNode:
    """A bare LineageNode the way the walk emits one (receipt unset)."""
    return LineageNode(
        id=str(node_id),
        row_kind=row_kind,
        title="t",
        produced_at=datetime(2026, 6, 30, 11, 0, 0, tzinfo=timezone.utc),
        target_id=None,
        analyst_id=None,
        schema_uri="iglu:legba/finding/jsonschema/1-0-0",
        depth=1,
    )


# ---------------------------------------------------------------------------
# Pure-logic: per-node receipt enrichment (no DB).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_analyst_node_carries_recomputed_receipt():
    """A multi-node walk where two nodes are analyst-produced: each gets its
    own receipt with chain_consistent recomputed True for an intact chain."""
    finding_id = uuid4()
    situation_id = uuid4()
    signal_id = uuid4()

    finding_trace = _make_trace(
        output_payload={"summary": "finding"}, output_row_refs=[finding_id],
        analyst_id="an_finding",
    )
    situation_trace = _make_trace(
        output_payload={"summary": "situation"}, output_row_refs=[situation_id],
        analyst_id="an_situation",
    )
    conn = _StubConn([finding_trace, situation_trace])

    nodes = [
        _node(finding_id, row_kind="finding"),
        _node(situation_id, row_kind="situation"),
        _node(signal_id, row_kind="signal"),
    ]
    await _attach_receipts_to_walk(conn, nodes)  # type: ignore[arg-type]

    by_id = {n.id: n for n in nodes}

    # Both analyst nodes carry a recomputed-consistent receipt.
    for nid, trace in (
        (finding_id, finding_trace),
        (situation_id, situation_trace),
    ):
        rec = by_id[str(nid)].receipt
        assert isinstance(rec, ReceiptChainNode)
        assert rec.chain_consistent is True
        assert rec.receipt_hash == trace["receipt_hash"]
        assert rec.run_id == str(trace["run_id"])
        assert rec.badge == "chain-consistent (single-node)"
        assert rec.signer_did is None  # no covering checkpoint

    # The signal node — no producing analyst run — carries receipt=None.
    assert by_id[str(signal_id)].receipt is None


@pytest.mark.asyncio
async def test_mutated_node_payload_flips_only_that_node_to_false():
    """Mutating one node's recorded payload flips ITS chain_consistent to False
    while the sibling analyst node stays True — the recompute is per-node."""
    a_id = uuid4()
    b_id = uuid4()
    intact = _make_trace(
        output_payload={"summary": "intact"}, output_row_refs=[a_id],
        analyst_id="an_a",
    )
    tampered = _make_trace(
        output_payload={"summary": "original"}, output_row_refs=[b_id],
        analyst_id="an_b",
    )
    # Tamper b's stored payload in place, leaving its old receipt_hash —
    # a mutated row that must re-hash differently.
    tampered["output_payload"] = json.dumps({"summary": "WAR DECLARED"})

    conn = _StubConn([intact, tampered])
    nodes = [_node(a_id, row_kind="finding"), _node(b_id, row_kind="finding")]
    await _attach_receipts_to_walk(conn, nodes)  # type: ignore[arg-type]

    by_id = {n.id: n for n in nodes}
    assert by_id[str(a_id)].receipt.chain_consistent is True
    assert by_id[str(b_id)].receipt.chain_consistent is False
    # The tampered node still surfaces the stored hash verbatim (we report it,
    # we just don't vouch for it).
    assert by_id[str(b_id)].receipt.receipt_hash == tampered["receipt_hash"]


@pytest.mark.asyncio
async def test_signer_did_only_from_covering_checkpoint_per_node():
    """A node's signer_did is populated ONLY when a checkpoint signed that
    node's exact receipt_hash as the chain head — never borrowed."""
    covered_id = uuid4()
    uncovered_id = uuid4()
    covered = _make_trace(
        output_payload={"x": 1}, output_row_refs=[covered_id],
        analyst_id="an_cov",
    )
    uncovered = _make_trace(
        output_payload={"x": 2}, output_row_refs=[uncovered_id],
        analyst_id="an_unc",
    )
    checkpoints = [
        {
            "analyst_id": "an_cov",
            "chain_head_hash": covered["receipt_hash"],
            "signer_did": "did:key:z6MkDeploymentSigner",
        },
        # A checkpoint for an_unc but covering some OTHER head — must not apply.
        {
            "analyst_id": "an_unc",
            "chain_head_hash": "f" * 64,
            "signer_did": "did:key:z6MkSomeoneElse",
        },
    ]
    conn = _StubConn([covered, uncovered], checkpoints)
    nodes = [
        _node(covered_id, row_kind="finding"),
        _node(uncovered_id, row_kind="finding"),
    ]
    await _attach_receipts_to_walk(conn, nodes)  # type: ignore[arg-type]

    by_id = {n.id: n for n in nodes}
    assert by_id[str(covered_id)].receipt.signer_did == "did:key:z6MkDeploymentSigner"
    assert by_id[str(uncovered_id)].receipt.signer_did is None
    # Even with a covering checkpoint, the per-node badge stays honest.
    assert by_id[str(covered_id)].receipt.badge == "chain-consistent (single-node)"


@pytest.mark.asyncio
async def test_no_walk_node_field_claims_tamper_proof_or_ed25519():
    """No produced field anywhere in the enriched walk may claim
    tamper-proof / signed / Ed25519 for the per-row receipt."""
    a_id = uuid4()
    trace = _make_trace(
        output_payload={"x": 1}, output_row_refs=[a_id], analyst_id="an_z",
    )
    conn = _StubConn([trace])
    nodes = [_node(a_id, row_kind="finding")]
    await _attach_receipts_to_walk(conn, nodes)  # type: ignore[arg-type]

    blob = json.dumps(nodes[0].model_dump(), default=str).lower()
    assert "tamper-proof" not in blob
    assert "tamper proof" not in blob
    assert "ed25519" not in blob
    assert "signed" not in blob


# ---------------------------------------------------------------------------
# Integration — the real cross-table DB join over a multi-node lineage.
# Reuses the session-scoped ``migrated_pg`` fixture from data_pkg/conftest.py.
# ---------------------------------------------------------------------------


class _MinimalDescriptorRegistry:
    def __init__(self, pg_store: PostgresStore) -> None:
        self.pg = pg_store


@pytest_asyncio.fixture
async def walk_receipt_app(migrated_pg: PostgresConfig):
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
async def test_walk_carries_receipt_per_analyst_node(walk_receipt_app):
    """End-to-end: a signal → finding → situation chain where the finding and
    the situation were each produced by a recorded run. Walking upstream from
    the situation, EACH analyst walk node carries its own recomputed receipt;
    the signal node carries receipt=None; badge exact; chain_consistent flips
    to False when a node's stored trace payload is mutated."""
    client, pg_store = walk_receipt_app

    signal_id = uuid4()
    finding_id = uuid4()
    situation_id = uuid4()
    finding_run = uuid4()
    situation_run = uuid4()
    started = datetime(2026, 6, 30, 11, 0, 0, tzinfo=timezone.utc)
    ended = datetime(2026, 6, 30, 11, 0, 5, tzinfo=timezone.utc)

    async with pg_store.acquire() as conn:
        # Source signal (no analyst run).
        await conn.execute(
            """
            INSERT INTO signals
                (id, source_id, source_version, produced_by_kind, fetched_at,
                 modality, payload, content_hash, derived_from, schema_uri)
            VALUES ($1, 'rss_main', '', 'source', NOW(),
                    'text', $2::jsonb, '', '{}'::uuid[],
                    'iglu:legba/signal/jsonschema/3-0-0')
            """,
            signal_id, json.dumps({"title": "sig_root"}),
        )
        # finding ← signal, produced by finding_run.
        await conn.execute(
            """
            INSERT INTO analyst_outputs
                (id, kind, title, body, analyst_id, analyst_version,
                 produced_at, derived_from, schema_uri, run_id)
            VALUES ($1, 'finding', 'mid_finding', '', 'an_finding', 'v1',
                    NOW(), $2::uuid[],
                    'iglu:legba/finding/jsonschema/1-0-0', $3)
            """,
            finding_id, [signal_id], finding_run,
        )
        # situation ← finding, produced by situation_run.
        await conn.execute(
            """
            INSERT INTO situations
                (id, data, name, target_id, target_version, analyst_id,
                 analyst_version, produced_at, derived_from, schema_uri, run_id)
            VALUES ($1, '{}'::jsonb, 'leaf_situation', 'br_energy', 'tv',
                    'an_situation', 'v1', NOW(), $2::uuid[],
                    'iglu:legba/situation/jsonschema/2-0-0', $3)
            """,
            situation_id, [finding_id], situation_run,
        )

        chain = RuntimeReceiptChain(pg_store.pool)
        finding_hash, _ = await chain.record(
            run_id=finding_run, analyst_id="an_finding", analyst_version="v1",
            cadence_trigger="manual", target_id="br",
            input_row_refs=[signal_id], input_payload={"slice": "x"},
            prompt_module_hash="pmod-f", prompt_rendered="render",
            output_row_refs=[finding_id], output_payload={"summary": "intact"},
            run_started_at=started, run_ended_at=ended,
        )
        situation_hash, _ = await chain.record(
            run_id=situation_run, analyst_id="an_situation",
            analyst_version="v1", cadence_trigger="manual", target_id="br",
            input_row_refs=[finding_id], input_payload={"slice": "y"},
            prompt_module_hash="pmod-s", prompt_rendered="render",
            output_row_refs=[situation_id], output_payload={"frame": "intact"},
            run_started_at=started, run_ended_at=ended,
        )

    r = await client.get(
        f"/api/v1/lineage/situation/{situation_id}",
        params={"direction": "upstream", "depth": 3},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Root (the situation) carries its receipt (P0-T4 path, unchanged).
    assert body["root"]["receipt"] is not None
    assert body["root"]["receipt"]["receipt_hash"] == situation_hash
    assert body["root"]["receipt"]["chain_consistent"] is True
    assert body["root"]["receipt"]["badge"] == "chain-consistent (single-node)"

    by_id = {n["id"]: n for n in body["nodes"]}
    assert str(finding_id) in by_id
    assert str(signal_id) in by_id

    # The walk's finding node now carries its OWN recomputed receipt (P1-T4).
    finding_node = by_id[str(finding_id)]
    assert finding_node["receipt"] is not None
    assert finding_node["receipt"]["receipt_hash"] == finding_hash
    assert finding_node["receipt"]["chain_consistent"] is True
    assert finding_node["receipt"]["badge"] == "chain-consistent (single-node)"
    assert finding_node["receipt"]["signer_did"] is None

    # The signal node — source-ingested, no analyst run — honestly receipt=None.
    assert by_id[str(signal_id)]["receipt"] is None

    # No produced field anywhere claims a stronger guarantee than the chain has.
    blob = json.dumps(body).lower()
    assert "tamper-proof" not in blob
    assert "ed25519" not in blob
    assert "signed" not in blob

    # Mutate the finding's STORED trace payload in place (its receipt_hash kept)
    # → the finding walk node's chain_consistent must flip to False, while the
    # root situation stays True (independent per-node recompute).
    async with pg_store.acquire() as conn:
        await conn.execute(
            "UPDATE analyst_traces SET output_payload = $2::jsonb "
            "WHERE run_id = $1",
            finding_run, json.dumps({"summary": "MUTATED"}),
        )

    r2 = await client.get(
        f"/api/v1/lineage/situation/{situation_id}",
        params={"direction": "upstream", "depth": 3},
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    by_id2 = {n["id"]: n for n in body2["nodes"]}
    assert by_id2[str(finding_id)]["receipt"]["chain_consistent"] is False
    # The root situation (untouched) stays consistent.
    assert body2["root"]["receipt"]["chain_consistent"] is True
    # The stored hash is still surfaced verbatim on the tampered node.
    assert by_id2[str(finding_id)]["receipt"]["receipt_hash"] == finding_hash
