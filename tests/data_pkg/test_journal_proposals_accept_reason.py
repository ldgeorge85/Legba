# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The accept-reason gap, closed (GLASS-3 item 3).

``journal_proposals.decision_reason`` has existed since migration 0048, and until
now only ``reject`` ever wrote it: the accept route set status/decided_by/
decided_at and returned a hardcoded ``decision_reason=None``, with no body model
to carry one. The decision trail was asymmetric by construction — every refusal
explained itself, and every APPLIED change, the half that actually mutates the
substrate, could not.

These run through the REAL FastAPI app over ASGI (the router mounted at its
production prefix, the real bearer dependency, the real atomic-claim SQL), not by
calling the handler — the existing ``tests/journal_w4`` lifecycle suite covers the
apply worker by direct call and explicitly does not stand the app up, so the
binding itself was never exercised. The accept path's body model, its optional-ness
and the column write are exactly the parts that only a real request can prove.

What is pinned:
  * an accept with a reason RECORDS it, and it survives to the list read;
  * an accept with NO body still works — the pre-existing contract, and the shape
    the shipped panel already posts;
  * empty / whitespace-only reasons store SQL NULL, never ``''``;
  * reject still REQUIRES its reason (the asymmetry is deliberate, not an
    oversight, so a change that "harmonized" the two would fail here);
  * the reason is written on the SAME atomic claim as the status flip, so a
    replayed accept neither re-applies nor overwrites the recorded reason.
"""
from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import MASTER_KEY_ENV, CredentialVault
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.journal_proposals_api import (
    build_journal_proposals_router,
)
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "66" * 32)

_BASE = "/api/v1/journal_proposals"

#: A `correction` whose diff routes to the real supersede path. It supersedes
#: nothing (no matching fact exists), which is a successful apply of zero rows —
#: exactly what this suite wants: the accept SUCCEEDS, so the reason lands on an
#: `accepted` row rather than on an `archived` apply-failure.
_DIFF = {
    "op": "supersede_fact",
    "subject": "GLASS-3 accept-reason probe",
    "predicate": "status",
    "value": "recorded",
}


@pytest_asyncio.fixture
async def api_app(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()

    identity = SigningIdentity(
        signing_key=SigningKey(b"glass3-accept-reason-route-seed-1"[:32]),
        signer_did="did:legba:registry:glass3-accept-reason-test",
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
    # The production prefix — `/api/v1`, NOT `/api/v1/v3`.
    app.include_router(build_journal_proposals_router(deps), prefix="/api/v1")

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


@pytest_asyncio.fixture
async def make_proposal(api_app):
    """Insert a pending proposal; delete only what we inserted on the way out."""
    _, pg_store = api_app
    made: list = []

    async def _make(kind: str = "correction", diff: dict | None = None) -> str:
        async with pg_store.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO journal_proposals
                    (proposal_kind, proposed_by_analyst_id, run_id, rationale,
                     diff, status)
                VALUES ($1, 'journal_assessor', $2, 'probe rationale',
                        $3::jsonb, 'pending')
                RETURNING id
                """,
                kind, uuid4(), json.dumps(diff if diff is not None else _DIFF),
            )
        made.append(row["id"])
        return str(row["id"])

    yield _make

    async with pg_store.pool.acquire() as conn:
        if made:
            await conn.execute(
                "DELETE FROM journal_proposals WHERE id = ANY($1::uuid[])", made,
            )


async def _row(pg_store, proposal_id: str) -> dict:
    async with pg_store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, decided_by, decision_reason, decided_at "
            "FROM journal_proposals WHERE id = $1::uuid",
            proposal_id,
        )
    return dict(row)


@pytest.mark.asyncio
async def test_accept_records_the_reason(client, api_app, make_proposal):
    """The gap, closed: an APPLIED change can now say why it was applied."""
    _, pg_store = api_app
    pid = await make_proposal()

    r = await client.post(
        f"{_BASE}/{pid}/accept",
        json={"decision_reason": "verified against the cited signal by hand"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["replayed"] is False
    assert body["decision_reason"] == "verified against the cited signal by hand"

    stored = await _row(pg_store, pid)
    assert stored["status"] == "accepted"
    assert stored["decision_reason"] == "verified against the cited signal by hand"
    assert stored["decided_at"] is not None


@pytest.mark.asyncio
async def test_the_reason_survives_to_the_list_read(client, make_proposal):
    """A decision trail nobody can read afterwards is not a trail. The ops view
    reads the LIST, so the reason has to be there and not only in the POST's
    response."""
    pid = await make_proposal()
    await client.post(
        f"{_BASE}/{pid}/accept", json={"decision_reason": "corroborated upstream"},
    )

    listing = await client.get(_BASE, params={"status": "accepted", "limit": 200})
    assert listing.status_code == 200
    rows = {p["id"]: p for p in listing.json()["proposals"]}
    assert rows[pid]["decision_reason"] == "corroborated upstream"
    assert rows[pid]["decided_by"]


@pytest.mark.asyncio
async def test_accept_with_no_body_still_works(client, api_app, make_proposal):
    """The pre-existing contract, and the shape the shipped Journal Gate panel
    already posts. Making the reason REQUIRED would have broken every caller."""
    _, pg_store = api_app
    pid = await make_proposal()

    r = await client.post(f"{_BASE}/{pid}/accept")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"
    assert r.json()["decision_reason"] is None
    assert (await _row(pg_store, pid))["decision_reason"] is None


@pytest.mark.asyncio
async def test_accept_with_empty_body_object_still_works(client, make_proposal):
    """`apiPost(path, {})` — literally what `lib/api.ts` sends today."""
    pid = await make_proposal()
    r = await client.post(f"{_BASE}/{pid}/accept", json={})
    assert r.status_code == 200, r.text
    assert r.json()["decision_reason"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
async def test_blank_reasons_store_null_not_empty_string(
    client, api_app, make_proposal, blank,
):
    """A column that can hold `''` acquires a third state that reads as "a reason
    was given" to every IS NOT NULL check downstream."""
    _, pg_store = api_app
    pid = await make_proposal()

    r = await client.post(f"{_BASE}/{pid}/accept", json={"decision_reason": blank})
    assert r.status_code == 200, r.text
    assert r.json()["decision_reason"] is None
    assert (await _row(pg_store, pid))["decision_reason"] is None


@pytest.mark.asyncio
async def test_reason_is_trimmed(client, api_app, make_proposal):
    _, pg_store = api_app
    pid = await make_proposal()
    await client.post(
        f"{_BASE}/{pid}/accept", json={"decision_reason": "  padded reason \n"},
    )
    assert (await _row(pg_store, pid))["decision_reason"] == "padded reason"


@pytest.mark.asyncio
async def test_an_overlong_reason_is_refused_not_silently_truncated(
    client, make_proposal,
):
    """2048 is the column's working bound. Pydantic refuses past it rather than
    storing a half-sentence that reads as the whole one."""
    pid = await make_proposal()
    r = await client.post(
        f"{_BASE}/{pid}/accept", json={"decision_reason": "x" * 2049},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_replayed_accept_returns_the_recorded_reason_and_reapplies_nothing(
    client, api_app, make_proposal,
):
    """§7.4 idempotency, now including the reason: the second call must not
    overwrite the first decision's reason with its own."""
    _, pg_store = api_app
    pid = await make_proposal()

    first = await client.post(
        f"{_BASE}/{pid}/accept", json={"decision_reason": "the original reason"},
    )
    assert first.json()["replayed"] is False

    second = await client.post(
        f"{_BASE}/{pid}/accept", json={"decision_reason": "a DIFFERENT reason"},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["replayed"] is True
    assert body["applied"] is None, "a replay must never re-apply"
    assert body["decision_reason"] == "the original reason"
    assert (await _row(pg_store, pid))["decision_reason"] == "the original reason"


@pytest.mark.asyncio
async def test_reject_still_requires_its_reason(client, make_proposal):
    """The asymmetry is deliberate — a refusal is only legible through its
    reason, an accept is already described by the diff it applied. A change that
    'harmonized' the two by making reject's optional would fail here."""
    pid = await make_proposal()
    assert (await client.post(f"{_BASE}/{pid}/reject", json={})).status_code == 422
    assert (
        await client.post(f"{_BASE}/{pid}/reject", json={"decision_reason": ""})
    ).status_code == 422
    assert (
        await client.post(f"{_BASE}/{pid}/reject", json={"decision_reason": "   "})
    ).status_code == 422

    ok = await client.post(
        f"{_BASE}/{pid}/reject", json={"decision_reason": "not corroborated"},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "rejected"
    assert ok.json()["decision_reason"] == "not corroborated"


@pytest.mark.asyncio
async def test_accept_of_a_missing_proposal_is_404(client):
    r = await client.post(f"{_BASE}/{uuid4()}/accept",
                          json={"decision_reason": "nope"})
    assert r.status_code == 404
