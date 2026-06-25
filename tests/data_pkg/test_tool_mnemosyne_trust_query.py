# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for L-211 ``mnemosyne_trust_query`` analyst tool.

Uses ``httpx.MockTransport`` so no Mnemosyne instance is required. The
test mock plays Mnemosyne's role at the A2A wire boundary:

  * Parses the JSON-RPC envelope produced by the tool.
  * Verifies the inbound signature using Mnemosyne's own algorithm
    (``f"{purpose}:{content_hash}:{nonce}:{timestamp}"``).
  * Returns a synthetic ``trust.query`` artifact, optionally signed
    with a known responder key so the artifact-verify path can be
    exercised.

Covers (per task brief):

  * happy path — mocked endpoint returns the shape, response collapses
    to ``{"weight", "hops"}``.
  * MN-3 Q13 ``chain_unavailable`` short-circuit → ``{"error": "chain_unavailable"}``.
  * network error (httpx.ConnectError) → ``{"error": "transport_error"}``.
  * signature mismatch (responder DID pinned, mock signs with a
    different key) → ``{"error": "signature_mismatch"}``.
  * JSON-RPC error in response → ``{"error": "rpc_error"}``.
  * missing peer — Mnemosyne returns unknown trust shape → zero-weight,
    hops=-1 (NOT an error per the contract).
  * inbound signature verifies under Mnemosyne's ``_verify_sender``
    algorithm — proves the tool's outbound envelope is bit-exact.
  * malformed args → ``MnemosyneTrustQueryError``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

import httpx
import pytest
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from legba.data.tools import MNEMOSYNE_TRUST_QUERY_TOOL_NAME
from legba.data.tools.mnemosyne_trust_query import (
    ALLOWED_SCOPES,
    DEFAULT_TIMEOUT_S,
    MnemosyneTrustQueryDeps,
    MnemosyneTrustQueryError,
    SIGN_PURPOSE,
    SKILL_ID,
    TOOL_NAME,
    _b64url_decode,
    _b64url_nopad,
    _canonical_json_bytes,
    call,
)
from legba.ui.agent_card import public_key_to_did


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signing_key() -> tuple[SigningKey, str]:
    """Generate an Ed25519 keypair + did:key string."""
    sk = SigningKey.generate()
    did = public_key_to_did(bytes(sk.verify_key))
    return sk, did


def _verify_inbound_envelope(
    meta: dict[str, Any], data: dict[str, Any]
) -> bool:
    """Mirror Mnemosyne's ``A2AServer._verify_sender`` exactly.

    Returns True iff the inbound envelope is a valid Ed25519 signature
    over ``f"{purpose}:{content_hash}:{nonce}:{timestamp}"``.
    """
    try:
        sig_b64 = meta["signature"]
        signer_did = meta["signer_did"]
        purpose = meta["purpose"]
        nonce = meta["nonce"]
        timestamp = int(meta["timestamp"])
        content_hash = meta["content_hash"]
    except (KeyError, ValueError, TypeError):
        return False

    # Timestamp freshness (5-minute window matches Mnemosyne).
    if abs(int(time.time()) - timestamp) > 300:
        return False

    # Recompute content hash against the actual data the client sent.
    canonical = _canonical_json_bytes(data)
    expected_hash = hashlib.sha256(canonical).hexdigest()
    if expected_hash != content_hash:
        return False

    message = f"{purpose}:{content_hash}:{nonce}:{timestamp}".encode("utf-8")
    public_key_bytes = _did_key_to_pub(signer_did)
    try:
        VerifyKey(public_key_bytes).verify(message, _b64url_decode(sig_b64))
        return True
    except BadSignatureError:
        return False


def _did_key_to_pub(did: str) -> bytes:
    # Reuse the legba ui decoder (Mnemosyne implements the same algorithm).
    from legba.ui.agent_card import did_to_public_key

    return did_to_public_key(did)


def _sign_artifact(
    payload: dict[str, Any], signing_key: SigningKey | None
) -> dict[str, Any]:
    """Mirror Mnemosyne's ``A2AServer._sign_artifact``."""
    content_hash = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    metadata: dict[str, Any] = {"content_hash": f"sha256:{content_hash}"}
    if signing_key is not None:
        signed = signing_key.sign(content_hash.encode("utf-8"))
        metadata["signature"] = _b64url_nopad(signed.signature)
        metadata["signer_did"] = public_key_to_did(bytes(signing_key.verify_key))
    return metadata


def _make_task_response(
    payload: dict[str, Any],
    *,
    req_id: str,
    state: str = "completed",
    responder_sk: SigningKey | None = None,
) -> dict[str, Any]:
    """Build a JSON-RPC `result` that wraps a Mnemosyne A2A task dict."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "task_id": "task-abc",
            "skill_id": "trust.query",
            "state": state,
            "artifacts": [
                {
                    "parts": [
                        {"type": "application/json", "data": payload},
                    ],
                    "metadata": _sign_artifact(payload, responder_sk),
                }
            ],
        },
    }


def _make_deps(
    *,
    transport: httpx.MockTransport,
    expected_responder_did: str | None = None,
) -> MnemosyneTrustQueryDeps:
    sk, did = _make_signing_key()
    client = httpx.AsyncClient(transport=transport, base_url="https://mnem.test")
    return MnemosyneTrustQueryDeps(
        base_url="https://mnem.test",
        signing_key=sk,
        signer_did=did,
        http_client=client,
        expected_responder_did=expected_responder_did,
    )


# ---------------------------------------------------------------------------
# Sanity / contract
# ---------------------------------------------------------------------------


def test_tool_name_and_skill_constants():
    assert TOOL_NAME == "mnemosyne_trust_query"
    assert MNEMOSYNE_TRUST_QUERY_TOOL_NAME == TOOL_NAME
    assert SKILL_ID == "trust.query"
    assert SIGN_PURPOSE == "a2a.trust.query"
    assert DEFAULT_TIMEOUT_S > 0
    assert "general" in ALLOWED_SCOPES


def test_canonical_json_is_stable():
    a = _canonical_json_bytes({"b": 1, "a": 2})
    b = _canonical_json_bytes({"a": 2, "b": 1})
    assert a == b
    assert b" " not in a  # tight separators


def test_b64url_roundtrip():
    raw = b"\x00\x01\xff\xfeA"
    encoded = _b64url_nopad(raw)
    assert not encoded.endswith("=")
    assert _b64url_decode(encoded) == raw


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_full_payload():
    """Mocked Mnemosyne returns the full TRUST_QUERY shape; tool reduces
    it to ``{"weight", "hops"}`` per MN-3 Q13.
    """
    responder_sk, responder_did = _make_signing_key()
    peer_did = "did:key:zPeer123abc"

    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured["body"] = body

        params = body["params"]
        meta = params["_meta"]
        data = params["data"]

        assert _verify_inbound_envelope(meta, data), (
            "Tool produced an envelope that fails Mnemosyne's verify algorithm"
        )

        payload = {
            "subject_did": peer_did,
            "trust_score": 0.4,
            "trust_level": "recognition",
            "trust_distance": {
                "distance": 2,
                "trust_weight": 0.73,
                "reachable": True,
                "path": ["did:key:zA", "did:key:zB"],  # MUST be stripped
            },
            "sybil_verified": True,
            "endorsement_count": 7,
            "evidence_summary": {"interaction_count": 12},
            "recommendation": "allow_with_caution",
            "assessment_timestamp": int(time.time()),
        }
        return httpx.Response(
            200,
            json=_make_task_response(
                payload, req_id=body["id"], responder_sk=responder_sk
            ),
        )

    deps = _make_deps(
        transport=httpx.MockTransport(handler),
        expected_responder_did=responder_did,
    )
    try:
        result = await call({"peer_did": peer_did, "scope": "general"}, deps)
    finally:
        await deps.http_client.aclose()

    assert result == {"weight": 0.73, "hops": 2}

    body = captured["body"]
    assert body["jsonrpc"] == "2.0"
    assert body["method"] == "tasks/send"
    assert body["params"]["skill_id"] == "trust.query"
    assert body["params"]["data"] == {
        "subject_did": peer_did,
        "scope": "general",
    }
    # No raw path leaks through the tool surface.
    assert "path" not in result
    assert "evidence_summary" not in result


@pytest.mark.asyncio
async def test_default_scope_is_general():
    responder_sk, responder_did = _make_signing_key()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["params"]["data"]["scope"] == "general"
        payload = {
            "subject_did": body["params"]["data"]["subject_did"],
            "trust_score": 0.0,
            "trust_level": "unknown",
            "trust_distance": {
                "distance": -1,
                "trust_weight": 0.0,
                "reachable": False,
                "path": [],
            },
        }
        return httpx.Response(
            200,
            json=_make_task_response(
                payload, req_id=body["id"], responder_sk=responder_sk
            ),
        )

    deps = _make_deps(
        transport=httpx.MockTransport(handler),
        expected_responder_did=responder_did,
    )
    try:
        # No scope key — should default to 'general'.
        result = await call({"peer_did": "did:key:zZeroPeer"}, deps)
    finally:
        await deps.http_client.aclose()

    assert result == {"weight": 0.0, "hops": -1}


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_unavailable_short_circuit():
    """P-007 Q5 chain_unavailable fallback shape."""
    responder_sk, responder_did = _make_signing_key()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        payload = {
            "subject_did": body["params"]["data"]["subject_did"],
            "trust_score": 0.0,
            "trust_level": "unknown",
            "chain_unavailable": True,
        }
        return httpx.Response(
            200,
            json=_make_task_response(
                payload, req_id=body["id"], responder_sk=responder_sk
            ),
        )

    deps = _make_deps(
        transport=httpx.MockTransport(handler),
        expected_responder_did=responder_did,
    )
    try:
        result = await call(
            {"peer_did": "did:key:zChainGone", "scope": "general"}, deps
        )
    finally:
        await deps.http_client.aclose()

    assert result == {"error": "chain_unavailable"}


@pytest.mark.asyncio
async def test_transport_error_returns_error_dict():
    """Network error must surface as a return value, not an exception."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    deps = _make_deps(transport=httpx.MockTransport(handler))
    try:
        result = await call(
            {"peer_did": "did:key:zUnreachable", "scope": "general"}, deps
        )
    finally:
        await deps.http_client.aclose()

    assert result == {"error": "transport_error"}


@pytest.mark.asyncio
async def test_signature_mismatch_when_responder_pinned():
    """Mock signs with key K1; deps pin DID derived from K2 → mismatch."""
    # Mnemosyne signs with this key:
    actual_sk, _ = _make_signing_key()
    # We pin a *different* DID:
    _, expected_other_did = _make_signing_key()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        payload = {
            "subject_did": body["params"]["data"]["subject_did"],
            "trust_score": 0.5,
            "trust_level": "recognition",
            "trust_distance": {
                "distance": 1, "trust_weight": 0.5,
                "reachable": True, "path": [],
            },
        }
        return httpx.Response(
            200,
            json=_make_task_response(
                payload, req_id=body["id"], responder_sk=actual_sk
            ),
        )

    deps = _make_deps(
        transport=httpx.MockTransport(handler),
        expected_responder_did=expected_other_did,
    )
    try:
        result = await call(
            {"peer_did": "did:key:zSomeone", "scope": "general"}, deps
        )
    finally:
        await deps.http_client.aclose()

    assert result == {"error": "signature_mismatch"}


@pytest.mark.asyncio
async def test_signature_tamper_detected_when_pinned():
    """Pinned DID matches but payload was changed post-signing."""
    responder_sk, responder_did = _make_signing_key()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        original = {
            "subject_did": body["params"]["data"]["subject_did"],
            "trust_score": 0.9,
            "trust_level": "deep_trust",
            "trust_distance": {
                "distance": 1, "trust_weight": 0.9,
                "reachable": True, "path": [],
            },
        }
        # Sign the ORIGINAL but ship a TAMPERED payload — the artifact's
        # content_hash will mismatch the body, and verification must fail.
        signed_meta = _sign_artifact(original, responder_sk)
        tampered = dict(original)
        tampered["trust_score"] = 0.1
        tampered["trust_distance"] = {
            "distance": 5, "trust_weight": 0.1,
            "reachable": True, "path": [],
        }
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "task_id": "tampered",
                    "skill_id": "trust.query",
                    "state": "completed",
                    "artifacts": [
                        {
                            "parts": [
                                {"type": "application/json", "data": tampered},
                            ],
                            "metadata": signed_meta,
                        }
                    ],
                },
            },
        )

    deps = _make_deps(
        transport=httpx.MockTransport(handler),
        expected_responder_did=responder_did,
    )
    try:
        result = await call(
            {"peer_did": "did:key:zTampered", "scope": "general"}, deps
        )
    finally:
        await deps.http_client.aclose()

    assert result == {"error": "signature_mismatch"}


@pytest.mark.asyncio
async def test_jsonrpc_error_response():
    """Mnemosyne returns a JSON-RPC error envelope."""

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32005, "message": "Verification failed"},
            },
        )

    deps = _make_deps(transport=httpx.MockTransport(handler))
    try:
        result = await call(
            {"peer_did": "did:key:zRejected", "scope": "general"}, deps
        )
    finally:
        await deps.http_client.aclose()

    assert result == {"error": "rpc_error"}


@pytest.mark.asyncio
async def test_upstream_500_returns_transport_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overloaded")

    deps = _make_deps(transport=httpx.MockTransport(handler))
    try:
        result = await call(
            {"peer_did": "did:key:zUnreachable", "scope": "general"}, deps
        )
    finally:
        await deps.http_client.aclose()

    assert result == {"error": "transport_error"}


@pytest.mark.asyncio
async def test_task_failed_state():
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "task_id": "x",
                    "skill_id": "trust.query",
                    "state": "failed",
                    "error": "internal error",
                    "artifacts": [],
                },
            },
        )

    deps = _make_deps(transport=httpx.MockTransport(handler))
    try:
        result = await call(
            {"peer_did": "did:key:zFailed", "scope": "general"}, deps
        )
    finally:
        await deps.http_client.aclose()

    assert result == {"error": "task_failed"}


@pytest.mark.asyncio
async def test_missing_peer_returns_zero_weight_not_error():
    """Per the docstring contract: unknown DIDs surface as zero-weight,
    NOT as an error code. (Mnemosyne returns trust_level=unknown with
    distance=-1; the tool maps that naturally.)
    """
    responder_sk, responder_did = _make_signing_key()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        payload = {
            "subject_did": body["params"]["data"]["subject_did"],
            "trust_score": 0.0,
            "trust_level": "unknown",
            "trust_distance": {
                "distance": -1,
                "trust_weight": 0.0,
                "reachable": False,
                "path": [],
            },
            "sybil_verified": False,
            "endorsement_count": 0,
            "recommendation": "unknown",
            "assessment_timestamp": int(time.time()),
        }
        return httpx.Response(
            200,
            json=_make_task_response(
                payload, req_id=body["id"], responder_sk=responder_sk
            ),
        )

    deps = _make_deps(
        transport=httpx.MockTransport(handler),
        expected_responder_did=responder_did,
    )
    try:
        result = await call(
            {"peer_did": "did:key:zMissingPeer", "scope": "general"}, deps
        )
    finally:
        await deps.http_client.aclose()

    assert result == {"weight": 0.0, "hops": -1}
    assert "error" not in result


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_args_raise():
    """Bad argument shape is a programmer error, not a runtime degradation."""
    deps = _make_deps(transport=httpx.MockTransport(
        lambda request: httpx.Response(500, text="unreachable")
    ))
    try:
        with pytest.raises(MnemosyneTrustQueryError):
            await call({"scope": "general"}, deps)  # missing peer_did
        with pytest.raises(MnemosyneTrustQueryError):
            await call({"peer_did": "not-a-did", "scope": "general"}, deps)
        with pytest.raises(MnemosyneTrustQueryError):
            await call({"peer_did": "did:key:zX", "scope": 123}, deps)  # bad type
        with pytest.raises(MnemosyneTrustQueryError):
            await call("nope", deps)  # type: ignore[arg-type]
    finally:
        await deps.http_client.aclose()


@pytest.mark.asyncio
async def test_deps_validation():
    """Missing or wrong-typed deps raise MnemosyneTrustQueryError."""
    with pytest.raises(MnemosyneTrustQueryError):
        await call({"peer_did": "did:key:zX"}, deps=object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_unknown_scope_does_not_block_but_warns(caplog):
    """Unknown scope is allowed through (Mnemosyne is the canonical
    authority) but a warning is emitted."""
    responder_sk, responder_did = _make_signing_key()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        # Note Mnemosyne would actually reject this; here we just verify
        # the tool forwards it.
        assert body["params"]["data"]["scope"] == "wildcard"
        payload = {
            "subject_did": body["params"]["data"]["subject_did"],
            "trust_score": 0.0,
            "trust_level": "unknown",
            "trust_distance": {
                "distance": -1, "trust_weight": 0.0,
                "reachable": False, "path": [],
            },
        }
        return httpx.Response(
            200,
            json=_make_task_response(
                payload, req_id=body["id"], responder_sk=responder_sk
            ),
        )

    deps = _make_deps(
        transport=httpx.MockTransport(handler),
        expected_responder_did=responder_did,
    )
    try:
        with caplog.at_level("WARNING"):
            await call(
                {"peer_did": "did:key:zScopeTest", "scope": "wildcard"},
                deps,
            )
    finally:
        await deps.http_client.aclose()

    assert any("scope" in rec.message for rec in caplog.records)
