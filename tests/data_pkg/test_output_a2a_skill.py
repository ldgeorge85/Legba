# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for L-193 — `legba.data.outputs.a2a_skill`.

No substrate required: the registry + envelope helpers are pure-Python,
and the FastAPI route is exercised via `httpx.AsyncClient + ASGITransport`
against an in-memory app.

Covers:
  * Skill registration is auto-derived from descriptor outputs[*] of kind
    "a2a_skill", with config sourced from the descriptor's binding.
  * Auto-fill of input/response schemas when the binding omits them.
  * Envelope canonical-JSON sign / verify round-trip.
  * POST /a2a/skills/<id> with a valid signature returns 200 + signed
    response envelope; with a bad signature returns 401.
  * Schema validation of args returns 422 on shape mismatch.
  * Unknown signer (auth_required) returns 401.
  * Skill not found returns 404.
  * The response envelope verifies under the registry's verify-key.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.outputs.a2a_skill import (
    A2ASkillRegistration,
    A2ASkillRegistry,
    ENVELOPE_VERSION,
    HEADER_SIGNATURE,
    HEADER_SIGNER_DID,
    KIND_NAME,
    TrustedKeyDirectory,
    build_envelope,
    register_a2a_skill_route,
    sign_envelope,
    verify_envelope,
)
from legba.data.registry.signing import SigningIdentity


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


CALLER_DID = "did:legba:test-caller"
SERVER_DID = "did:legba:registry:test-server"


@pytest.fixture
def caller_keypair() -> tuple[SigningKey, bytes]:
    sk = SigningKey.generate()
    return sk, bytes(sk.verify_key)


@pytest.fixture
def server_identity() -> SigningIdentity:
    return SigningIdentity(signing_key=SigningKey.generate(), signer_did=SERVER_DID)


@pytest.fixture
def registry() -> A2ASkillRegistry:
    return A2ASkillRegistry()


@pytest.fixture
def trusted_keys(caller_keypair) -> TrustedKeyDirectory:
    _, vk_bytes = caller_keypair
    d = TrustedKeyDirectory()
    d.add(CALLER_DID, vk_bytes)
    return d


@pytest.fixture
def fake_outputs() -> list[dict[str, Any]]:
    return [
        {
            "id": "finding-1",
            "title": "Brazil energy outlook",
            "confidence": 0.84,
            "target_id": "brazil.energy",
            "produced_at": "2026-05-21T00:00:00Z",
        },
        {
            "id": "finding-2",
            "title": "Mexico tariff response",
            "confidence": 0.71,
            "target_id": "mexico.trade",
            "produced_at": "2026-05-21T00:01:00Z",
        },
    ]


@pytest.fixture
def fetch_latest(fake_outputs):
    captured: dict[str, Any] = {"calls": []}

    async def _fetch(*, analyst_ids, limit, target_filter):
        captured["calls"].append(
            {"analyst_ids": list(analyst_ids), "limit": limit, "target_filter": target_filter}
        )
        rows = list(fake_outputs)
        if target_filter is not None:
            rows = [r for r in rows if r["target_id"] == target_filter]
        return rows[: int(limit)]

    _fetch.captured = captured  # type: ignore[attr-defined]
    return _fetch


@pytest.fixture
def app(registry, server_identity, fetch_latest, trusted_keys) -> FastAPI:
    app = FastAPI()
    register_a2a_skill_route(
        app,
        registry=registry,
        identity=server_identity,
        fetch_latest_outputs=fetch_latest,
        trusted_keys=trusted_keys,
    )
    return app


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Envelope primitives
# ---------------------------------------------------------------------------


def test_envelope_sign_verify_roundtrip(caller_keypair):
    sk, _ = caller_keypair
    env = build_envelope(
        skill_id="intel.brazil",
        payload={"target_id": "brazil.energy", "limit": 3},
        signer_did=CALLER_DID,
    )
    sig = sign_envelope(env, sk)
    assert verify_envelope(env, sig, sk.verify_key) is True


def test_envelope_tampered_payload_fails(caller_keypair):
    sk, _ = caller_keypair
    env = build_envelope(
        skill_id="intel.brazil",
        payload={"target_id": "brazil.energy"},
        signer_did=CALLER_DID,
    )
    sig = sign_envelope(env, sk)
    # Tamper.
    env["payload"]["target_id"] = "mexico.trade"
    with pytest.raises(ValueError):
        verify_envelope(env, sig, sk.verify_key)


def test_envelope_bad_signature_b64_raises(caller_keypair):
    sk, _ = caller_keypair
    env = build_envelope(
        skill_id="intel.brazil", payload={}, signer_did=CALLER_DID,
    )
    with pytest.raises(ValueError):
        verify_envelope(env, "$$$not-base64$$$", sk.verify_key)


# ---------------------------------------------------------------------------
# Registry — register_from_descriptor
# ---------------------------------------------------------------------------


def test_register_from_descriptor_uses_config_block(registry):
    outputs = [
        {
            "kind": "a2a_skill",
            "config": {
                "skill_id": "intelligence.india_energy",
                "input_schema": {
                    "type": "object",
                    "properties": {"target_id": {"type": "string"}},
                    "required": ["target_id"],
                },
                "response_schema": {"type": "object"},
                "auth_required": True,
                "description": "Latest Brazil energy assessment.",
            },
        },
        {"kind": "nats_stream", "config": {"topic": "x"}},
    ]
    regs = registry.register_from_descriptor(
        analyst_id="analyst.india_energy",
        analyst_version="abcd1234abcd1234",
        descriptor_id="abcd1234abcd1234",
        outputs=outputs,
    )
    assert len(regs) == 1
    r = regs[0]
    assert r.skill_id == "intelligence.india_energy"
    assert r.analyst_id == "analyst.india_energy"
    assert r.input_schema["required"] == ["target_id"]
    assert registry.get("intelligence.india_energy") is r


def test_register_from_descriptor_auto_fills_schemas(registry):
    outputs = [
        {
            "kind": "a2a_skill",
            "config": {"skill_id": "intel.minimal"},
        },
    ]
    regs = registry.register_from_descriptor(
        analyst_id="analyst.minimal",
        analyst_version="11" * 8,
        descriptor_id="d1",
        outputs=outputs,
        type_signature={"input_type": "InModel", "output_type": "OutModel"},
    )
    assert len(regs) == 1
    r = regs[0]
    # Auto-derived schemas reference the analyst's type names.
    assert "InModel" in r.input_schema["description"]
    assert "OutModel" in r.response_schema["description"]


def test_register_from_descriptor_skips_no_skill_id(registry, caplog):
    outputs = [{"kind": "a2a_skill", "config": {}}]
    regs = registry.register_from_descriptor(
        analyst_id="x", analyst_version="11" * 8, descriptor_id="d", outputs=outputs,
    )
    assert regs == []
    assert registry.list_skills() == []


def test_unregister_by_analyst(registry):
    registry.register_from_descriptor(
        analyst_id="a1", analyst_version="11" * 8, descriptor_id="d",
        outputs=[
            {"kind": "a2a_skill", "config": {"skill_id": "s1"}},
            {"kind": "a2a_skill", "config": {"skill_id": "s2"}},
        ],
    )
    assert len(registry.list_skills()) == 2
    n = registry.unregister_by_analyst("a1")
    assert n == 2
    assert registry.list_skills() == []


def test_register_skill_id_collision_across_analysts(registry):
    registry.register(
        A2ASkillRegistration(
            skill_id="same",
            analyst_id="A",
            analyst_version="11" * 8,
            input_schema={"type": "object"},
            response_schema={"type": "object"},
        )
    )
    with pytest.raises(ValueError):
        registry.register(
            A2ASkillRegistration(
                skill_id="same",
                analyst_id="B",
                analyst_version="22" * 8,
                input_schema={"type": "object"},
                response_schema={"type": "object"},
            )
        )


# ---------------------------------------------------------------------------
# FastAPI route — list + GET
# ---------------------------------------------------------------------------


@pytest.fixture
def registered_skill(registry):
    registry.register(
        A2ASkillRegistration(
            skill_id="intelligence.india_energy",
            analyst_id="analyst.brazil",
            analyst_version="aa" * 8,
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": [],
            },
            response_schema={"type": "object"},
            auth_required=True,
        )
    )
    return "intelligence.india_energy"


@pytest.mark.asyncio
async def test_list_skills_endpoint(client, registered_skill, server_identity):
    resp = await client.get("/a2a/skills")
    assert resp.status_code == 200
    body = resp.json()
    assert body["signer_did"] == server_identity.signer_did
    assert any(s["skill_id"] == registered_skill for s in body["skills"])


@pytest.mark.asyncio
async def test_get_skill_returns_signed_envelope(
    client, registered_skill, server_identity, fake_outputs
):
    resp = await client.get(f"/a2a/skills/{registered_skill}")
    assert resp.status_code == 200
    body = resp.json()
    assert HEADER_SIGNATURE in {k.title(): None for k in resp.headers}.keys() or \
        HEADER_SIGNATURE.lower() in resp.headers
    env = body["envelope"]
    sig = body["signature"]
    # Signature verifies against the server's verify-key.
    assert verify_envelope(env, sig, server_identity.verify_key) is True
    assert env["skill_id"] == registered_skill
    assert env["signer_did"] == server_identity.signer_did
    assert len(env["payload"]["findings"]) == len(fake_outputs)


@pytest.mark.asyncio
async def test_get_skill_not_found(client):
    resp = await client.get("/a2a/skills/does.not.exist")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# FastAPI route — POST invoke (signed envelope)
# ---------------------------------------------------------------------------


def _post_body(sk: SigningKey, skill_id: str, args: dict[str, Any]) -> dict[str, Any]:
    env = build_envelope(skill_id=skill_id, payload=args, signer_did=CALLER_DID)
    sig = sign_envelope(env, sk)
    return {"envelope": env, "signature": sig}


@pytest.mark.asyncio
async def test_post_skill_good_signature_returns_signed_envelope(
    client, registered_skill, caller_keypair, server_identity, fake_outputs, fetch_latest,
):
    sk, _ = caller_keypair
    body = _post_body(sk, registered_skill, {"target_id": "brazil.energy", "limit": 1})
    resp = await client.post(f"/a2a/skills/{registered_skill}", json=body)
    assert resp.status_code == 200, resp.text

    out = resp.json()
    env = out["envelope"]
    sig = out["signature"]
    # Response envelope verifies under the server's verify-key.
    assert verify_envelope(env, sig, server_identity.verify_key) is True
    assert env["skill_id"] == registered_skill
    assert env["signer_did"] == server_identity.signer_did
    assert env["payload"]["analyst_id"] == "analyst.brazil"
    # The fake fetcher honored the target_id + limit.
    assert env["payload"]["findings"] == [fake_outputs[0]]
    # in_reply_to_nonce echoes the request envelope's nonce.
    assert env["payload"]["in_reply_to_nonce"] == body["envelope"]["nonce"]


@pytest.mark.asyncio
async def test_post_skill_bad_signature_returns_401(
    client, registered_skill, caller_keypair,
):
    sk, _ = caller_keypair
    body = _post_body(sk, registered_skill, {"target_id": "brazil.energy"})
    # Flip a byte in the signature so verification fails.
    bad_sig = list(body["signature"])
    bad_sig[0] = "A" if bad_sig[0] != "A" else "B"
    body["signature"] = "".join(bad_sig)
    resp = await client.post(f"/a2a/skills/{registered_skill}", json=body)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_post_skill_unknown_signer_returns_401(client, registered_skill):
    # Sign with a fresh key whose DID is NOT in the trusted directory.
    rogue = SigningKey.generate()
    env = build_envelope(
        skill_id=registered_skill,
        payload={"target_id": "brazil.energy"},
        signer_did="did:legba:rogue",
    )
    sig = sign_envelope(env, rogue)
    resp = await client.post(
        f"/a2a/skills/{registered_skill}",
        json={"envelope": env, "signature": sig},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_post_skill_schema_validation_failure(
    client, registry, caller_keypair, server_identity, fetch_latest, trusted_keys,
):
    # Register a skill that REQUIRES `target_id` as a string.
    registry.register(
        A2ASkillRegistration(
            skill_id="strict.skill",
            analyst_id="analyst.strict",
            analyst_version="cc" * 8,
            input_schema={
                "type": "object",
                "properties": {"target_id": {"type": "string"}},
                "required": ["target_id"],
            },
            response_schema={"type": "object"},
        )
    )
    sk, _ = caller_keypair
    # Wrong shape: missing required field.
    body = _post_body(sk, "strict.skill", {})
    resp = await client.post("/a2a/skills/strict.skill", json=body)
    assert resp.status_code == 422
    assert "missing required field" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_post_skill_envelope_skill_id_mismatch(
    client, registered_skill, caller_keypair,
):
    sk, _ = caller_keypair
    env = build_envelope(
        skill_id="some.other.skill",
        payload={},
        signer_did=CALLER_DID,
    )
    sig = sign_envelope(env, sk)
    resp = await client.post(
        f"/a2a/skills/{registered_skill}",
        json={"envelope": env, "signature": sig},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_post_skill_envelope_version_mismatch(
    client, registered_skill, caller_keypair,
):
    sk, _ = caller_keypair
    env = build_envelope(
        skill_id=registered_skill, payload={}, signer_did=CALLER_DID,
    )
    env["envelope_version"] = "999"
    sig = sign_envelope(env, sk)
    resp = await client.post(
        f"/a2a/skills/{registered_skill}",
        json={"envelope": env, "signature": sig},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_post_skill_unknown_skill_404(client, caller_keypair):
    sk, _ = caller_keypair
    body = _post_body(sk, "does.not.exist", {})
    resp = await client.post("/a2a/skills/does.not.exist", json=body)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_post_skill_invalid_json_400(client, registered_skill):
    resp = await client.post(
        f"/a2a/skills/{registered_skill}",
        content=b"this is not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_post_skill_missing_envelope_400(client, registered_skill):
    resp = await client.post(
        f"/a2a/skills/{registered_skill}",
        json={"signature": "abc"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# auth_required=False allows unsigned/unknown signers
# ---------------------------------------------------------------------------


@pytest.fixture
def public_app(registry, server_identity, fetch_latest):
    """App with empty trusted-key directory; only public skills succeed."""
    app = FastAPI()
    register_a2a_skill_route(
        app,
        registry=registry,
        identity=server_identity,
        fetch_latest_outputs=fetch_latest,
        trusted_keys=TrustedKeyDirectory(),  # empty
    )
    return app


@pytest_asyncio.fixture
async def public_client(public_app):
    async with AsyncClient(
        transport=ASGITransport(app=public_app), base_url="http://testserver",
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_post_public_skill_no_trust_required(
    public_client, registry, server_identity,
):
    registry.register(
        A2ASkillRegistration(
            skill_id="public.skill",
            analyst_id="analyst.public",
            analyst_version="bb" * 8,
            input_schema={"type": "object", "properties": {}, "required": []},
            response_schema={"type": "object"},
            auth_required=False,
        )
    )
    rogue = SigningKey.generate()
    env = build_envelope(
        skill_id="public.skill", payload={}, signer_did="did:legba:rando",
    )
    sig = sign_envelope(env, rogue)
    resp = await public_client.post(
        "/a2a/skills/public.skill", json={"envelope": env, "signature": sig},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Response is still server-signed even when caller is anonymous.
    assert verify_envelope(body["envelope"], body["signature"], server_identity.verify_key)


# ---------------------------------------------------------------------------
# TrustedKeyDirectory.from_env
# ---------------------------------------------------------------------------


def test_trusted_key_directory_from_env(monkeypatch):
    sk = SigningKey.generate()
    hexkey = bytes(sk.verify_key).hex()
    monkeypatch.setenv("LEGBA_A2A_TRUSTED_KEYS", f"did:legba:alpha={hexkey}")
    d = TrustedKeyDirectory.from_env()
    assert d.get("did:legba:alpha") is not None
    assert d.get("did:legba:absent") is None


# ---------------------------------------------------------------------------
# KIND_NAME constant
# ---------------------------------------------------------------------------


def test_kind_name_constant():
    assert KIND_NAME == "a2a_skill"
    assert ENVELOPE_VERSION == "1"
