# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for :mod:`legba.clients.mnemosyne_a2a`.

Covers L-210:

  * Envelope construction — shape mirrors the inbound a2a_skill wire
    form (``{envelope: {envelope_version, skill_id, nonce, issued_at,
    sender_did, recipient_did, signer_did, payload}, signature}``);
    signature is base64url Ed25519 over the canonical-JSON of the inner
    envelope and verifies with the sender's verify-key.
  * Happy-path invoke — request envelope is signed by the client, the
    mocked transport returns a response envelope signed by the
    recipient, the client verifies + returns the inner payload.
  * trust_query convenience — maps Mnemosyne's TRUST_QUERY output onto
    the L-210 ``{score, rationale, hop_count, ...}`` surface.
  * Signature mismatch on the response → :class:`A2ASignatureError`.
  * Signer DID not in trusted_keys → :class:`A2ASignatureError`.
  * Transport failure (connection refused) → :class:`A2ATransportError`.
  * 5xx upstream → :class:`A2ATransportError`.
  * 4xx upstream → :class:`A2ARemoteError` with detail.
  * ``from_env`` — happy path with required env vars; missing required
    var raises ValueError.
  * Live test gated on ``LEGBA_TEST_MNEMOSYNE=1`` that POSTs against
    the real Mnemosyne deployment URL from ``MNEMOSYNE_A2A_URL``.
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from nacl.signing import SigningKey, VerifyKey

from legba.clients.mnemosyne_a2a import (
    DEFAULT_TIMEOUT_S,
    ENV_BASE_URL,
    ENV_RECIPIENT_DID,
    ENV_TIMEOUT_S,
    A2ARemoteError,
    A2ASignatureError,
    A2ATransportError,
    MnemosyneA2AClient,
    _b64url_decode,
    _map_trust_query_payload,
)
from legba.data.outputs.a2a_skill import (
    ENVELOPE_VERSION,
    TrustedKeyDirectory,
    build_envelope,
    sign_envelope,
)
from legba.data.provenance import canonical_json
from legba.data.registry.signing import SigningIdentity


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


SENDER_DID = "did:legba:registry:test-host"
RECIPIENT_DID = "did:key:zMnemosyneTestRecipient"


def _make_identity() -> SigningIdentity:
    """Build a deterministic-per-test SigningIdentity."""
    return SigningIdentity(signing_key=SigningKey.generate(), signer_did=SENDER_DID)


def _make_recipient_signing() -> tuple[SigningKey, VerifyKey, TrustedKeyDirectory]:
    """Make a recipient keypair + populated TrustedKeyDirectory.

    Returns the recipient's signing key (used to sign response
    envelopes in the mock transport) plus a directory the client can
    use to verify those responses.
    """
    sk = SigningKey.generate()
    vk = sk.verify_key
    directory = TrustedKeyDirectory()
    directory.add(RECIPIENT_DID, vk)
    return sk, vk, directory


def _signed_response_envelope(
    *,
    skill_id: str,
    payload: dict[str, Any],
    recipient_signing_key: SigningKey,
    signer_did: str = RECIPIENT_DID,
) -> dict[str, Any]:
    """Build the ``{envelope, signature}`` body the mock transport
    returns. Uses the inbound a2a_skill helpers so the test exercises
    real cross-direction interop (server signs, client verifies)."""
    envelope = build_envelope(
        skill_id=skill_id, payload=payload, signer_did=signer_did,
    )
    signature = sign_envelope(envelope, recipient_signing_key)
    return {"envelope": envelope, "signature": signature}


# ---------------------------------------------------------------------------
# Envelope shape tests
# ---------------------------------------------------------------------------


def test_build_envelope_shape_and_signature_verifies() -> None:
    """The constructed envelope has the documented shape and is
    signed by the client's identity."""
    identity = _make_identity()
    client = MnemosyneA2AClient(
        base_url="https://mnemosyne.test",
        sender_did=identity.signer_did,
        identity=identity,
        recipient_did=RECIPIENT_DID,
    )

    wire = client.build_envelope(
        skill_id="trust.query",
        args={"subject_did": "did:key:zPeer", "scope": "general"},
    )

    assert set(wire.keys()) == {"envelope", "signature"}
    env = wire["envelope"]
    assert env["envelope_version"] == ENVELOPE_VERSION
    assert env["skill_id"] == "trust.query"
    assert env["sender_did"] == identity.signer_did
    assert env["recipient_did"] == RECIPIENT_DID
    assert env["signer_did"] == identity.signer_did  # alias for sender_did
    assert env["payload"] == {"subject_did": "did:key:zPeer", "scope": "general"}
    # nonce + issued_at are present and non-empty
    assert isinstance(env["nonce"], str) and env["nonce"]
    assert isinstance(env["issued_at"], str) and env["issued_at"]

    # Signature verifies with the sender's verify-key over the canonical
    # JSON of the inner envelope.
    sig = _b64url_decode(wire["signature"])
    identity.verify_key.verify(canonical_json(env), sig)


def test_build_envelope_distinct_nonces() -> None:
    """Each call generates a fresh nonce so replays are detectable."""
    identity = _make_identity()
    client = MnemosyneA2AClient(
        base_url="https://mnemosyne.test",
        sender_did=identity.signer_did,
        identity=identity,
        recipient_did=RECIPIENT_DID,
    )
    a = client.build_envelope(skill_id="x.y", args={})
    b = client.build_envelope(skill_id="x.y", args={})
    assert a["envelope"]["nonce"] != b["envelope"]["nonce"]


# ---------------------------------------------------------------------------
# Happy-path invoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_happy_path_signed_response() -> None:
    """End-to-end: client signs request, transport returns signed
    response envelope, client verifies + returns payload."""
    recipient_sk, _vk, trusted = _make_recipient_signing()
    identity = _make_identity()

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode("utf-8"))

        # Verify the inbound envelope is signed by the sender.
        env = captured["body"]["envelope"]
        sig = _b64url_decode(captured["body"]["signature"])
        identity.verify_key.verify(canonical_json(env), sig)

        # Build a signed response.
        response_body = _signed_response_envelope(
            skill_id="trust.query",
            payload={
                "subject_did": "did:key:zPeer",
                "trust_score": 0.82,
                "trust_level": "recognition",
                "trust_distance": {"distance": 2, "trust_weight": 0.65, "reachable": True},
                "recommendation": "allow_with_caution",
                "endorsement_count": 3,
                "sybil_verified": True,
            },
            recipient_signing_key=recipient_sk,
        )
        return httpx.Response(200, json=response_body)

    transport = httpx.MockTransport(handler)
    client = MnemosyneA2AClient(
        base_url="https://mnemosyne.test",
        sender_did=identity.signer_did,
        identity=identity,
        recipient_did=RECIPIENT_DID,
        trusted_keys=trusted,
    )

    real_async_client = httpx.AsyncClient

    def _patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("transport", transport)
        return real_async_client(*args, **kwargs)

    with patch.object(httpx, "AsyncClient", _patched_async_client):
        payload = await client.invoke(
            "trust.query",
            {"subject_did": "did:key:zPeer", "scope": "general"},
        )

    assert captured["url"].endswith("/a2a/skills/trust.query")
    assert payload["trust_score"] == 0.82
    assert payload["trust_level"] == "recognition"


@pytest.mark.asyncio
async def test_trust_query_maps_to_l210_shape() -> None:
    """The trust_query wrapper projects onto {score, rationale, hop_count, ...}."""
    recipient_sk, _vk, trusted = _make_recipient_signing()
    identity = _make_identity()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["envelope"]["payload"] == {
            "subject_did": "did:key:zPeer",
            "scope": "data_access",
        }
        response_body = _signed_response_envelope(
            skill_id="trust.query",
            payload={
                "subject_did": "did:key:zPeer",
                "trust_score": 0.42,
                "trust_level": "familiarity",
                "trust_distance": {"distance": 3, "trust_weight": 0.41, "reachable": True},
                "recommendation": "allow",
                "endorsement_count": 1,
                "sybil_verified": False,
            },
            recipient_signing_key=recipient_sk,
        )
        return httpx.Response(200, json=response_body)

    transport = httpx.MockTransport(handler)
    client = MnemosyneA2AClient(
        base_url="https://mnemosyne.test",
        sender_did=identity.signer_did,
        identity=identity,
        recipient_did=RECIPIENT_DID,
        trusted_keys=trusted,
    )

    real_async_client = httpx.AsyncClient

    def _patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("transport", transport)
        return real_async_client(*args, **kwargs)

    with patch.object(httpx, "AsyncClient", _patched_async_client):
        result = await client.trust_query("did:key:zPeer", scope="data_access")

    assert result["score"] == 0.42
    assert result["hop_count"] == 3
    assert result["trust_level"] == "familiarity"
    assert result["recommendation"] == "allow"
    assert result["subject_did"] == "did:key:zPeer"
    assert "score=0.420" in result["rationale"]
    assert "trust_level=familiarity" in result["rationale"]
    assert "recommendation=allow" in result["rationale"]
    # raw payload is carried through
    assert result["raw"]["sybil_verified"] is False


# ---------------------------------------------------------------------------
# trust_query payload mapping — unit tests on the pure mapping function
# ---------------------------------------------------------------------------


def test_map_trust_query_payload_fallback_to_trust_weight() -> None:
    """When trust_score is missing, fall back to trust_distance.trust_weight."""
    out = _map_trust_query_payload(
        {
            "trust_distance": {"distance": 4, "trust_weight": 0.31},
        },
        target_did="did:key:zPeer",
    )
    assert out["score"] == pytest.approx(0.31)
    assert out["hop_count"] == 4


def test_map_trust_query_payload_unreachable() -> None:
    """Unknown DID → hop_count -1, score 0.0, rationale flag."""
    out = _map_trust_query_payload(
        {"trust_level": "unknown", "trust_score": 0.0},
        target_did="did:key:zUnknown",
    )
    assert out["score"] == 0.0
    assert out["hop_count"] == -1
    assert out["trust_level"] == "unknown"


def test_map_trust_query_payload_empty_returns_unavailable_rationale() -> None:
    """No structured signal → 'trust evidence unavailable'."""
    out = _map_trust_query_payload({}, target_did="did:key:zPeer")
    assert "trust evidence unavailable" in out["rationale"] or "score=0.000" in out["rationale"]
    assert out["score"] == 0.0
    assert out["hop_count"] == -1


# ---------------------------------------------------------------------------
# Signature failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_signature_mismatch_raises() -> None:
    """Wrong signing key on response → A2ASignatureError."""
    _good_sk, _vk, trusted = _make_recipient_signing()  # trusted has _good's vk
    rogue_sk = SigningKey.generate()
    identity = _make_identity()

    def handler(request: httpx.Request) -> httpx.Response:
        response_body = _signed_response_envelope(
            skill_id="trust.query",
            payload={"trust_score": 0.5},
            recipient_signing_key=rogue_sk,  # signed by NOT the trusted key
        )
        return httpx.Response(200, json=response_body)

    transport = httpx.MockTransport(handler)
    client = MnemosyneA2AClient(
        base_url="https://mnemosyne.test",
        sender_did=identity.signer_did,
        identity=identity,
        recipient_did=RECIPIENT_DID,
        trusted_keys=trusted,
    )

    real_async_client = httpx.AsyncClient

    def _patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("transport", transport)
        return real_async_client(*args, **kwargs)

    with patch.object(httpx, "AsyncClient", _patched_async_client):
        with pytest.raises(A2ASignatureError):
            await client.invoke("trust.query", {"subject_did": "did:key:zPeer"})


@pytest.mark.asyncio
async def test_signer_did_not_in_trusted_keys_raises() -> None:
    """Response signed by an unknown DID → A2ASignatureError."""
    recipient_sk, _vk, _trusted_with_recipient = _make_recipient_signing()
    # Use an empty TrustedKeyDirectory — but non-None — so verification is enforced.
    trusted = TrustedKeyDirectory()
    identity = _make_identity()

    def handler(request: httpx.Request) -> httpx.Response:
        response_body = _signed_response_envelope(
            skill_id="trust.query",
            payload={"trust_score": 0.5},
            recipient_signing_key=recipient_sk,
            signer_did="did:key:zUnknownSigner",
        )
        return httpx.Response(200, json=response_body)

    transport = httpx.MockTransport(handler)
    client = MnemosyneA2AClient(
        base_url="https://mnemosyne.test",
        sender_did=identity.signer_did,
        identity=identity,
        recipient_did=RECIPIENT_DID,
        trusted_keys=trusted,
    )

    real_async_client = httpx.AsyncClient

    def _patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("transport", transport)
        return real_async_client(*args, **kwargs)

    with patch.object(httpx, "AsyncClient", _patched_async_client):
        with pytest.raises(A2ASignatureError, match="not in trusted_keys"):
            await client.invoke("trust.query", {"subject_did": "did:key:zPeer"})


@pytest.mark.asyncio
async def test_dev_mode_accepts_unsigned_response(caplog: Any) -> None:
    """trusted_keys=None → warn-and-accept (dev mode)."""
    recipient_sk, _vk, _trusted = _make_recipient_signing()
    identity = _make_identity()

    def handler(request: httpx.Request) -> httpx.Response:
        response_body = _signed_response_envelope(
            skill_id="trust.query",
            payload={"trust_score": 0.7},
            recipient_signing_key=recipient_sk,
            signer_did="did:key:zUnknownDevSigner",
        )
        return httpx.Response(200, json=response_body)

    transport = httpx.MockTransport(handler)
    client = MnemosyneA2AClient(
        base_url="https://mnemosyne.test",
        sender_did=identity.signer_did,
        identity=identity,
        recipient_did=RECIPIENT_DID,
        trusted_keys=None,
    )

    real_async_client = httpx.AsyncClient

    def _patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("transport", transport)
        return real_async_client(*args, **kwargs)

    with patch.object(httpx, "AsyncClient", _patched_async_client):
        with caplog.at_level("WARNING"):
            payload = await client.invoke(
                "trust.query", {"subject_did": "did:key:zPeer"},
            )

    assert payload["trust_score"] == 0.7
    assert any(
        "without signature verification" in rec.message for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Transport + remote failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_error_raises_a2a_transport_error() -> None:
    """ConnectError → A2ATransportError."""
    identity = _make_identity()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(handler)
    client = MnemosyneA2AClient(
        base_url="https://mnemosyne.test",
        sender_did=identity.signer_did,
        identity=identity,
        recipient_did=RECIPIENT_DID,
    )

    real_async_client = httpx.AsyncClient

    def _patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("transport", transport)
        return real_async_client(*args, **kwargs)

    with patch.object(httpx, "AsyncClient", _patched_async_client):
        with pytest.raises(A2ATransportError, match="transport error"):
            await client.invoke("trust.query", {"subject_did": "did:key:zPeer"})


@pytest.mark.asyncio
async def test_5xx_response_raises_a2a_transport_error() -> None:
    """5xx upstream → A2ATransportError (retries are reasonable)."""
    identity = _make_identity()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    transport = httpx.MockTransport(handler)
    client = MnemosyneA2AClient(
        base_url="https://mnemosyne.test",
        sender_did=identity.signer_did,
        identity=identity,
        recipient_did=RECIPIENT_DID,
    )

    real_async_client = httpx.AsyncClient

    def _patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("transport", transport)
        return real_async_client(*args, **kwargs)

    with patch.object(httpx, "AsyncClient", _patched_async_client):
        with pytest.raises(A2ATransportError, match="upstream 503"):
            await client.invoke("trust.query", {"subject_did": "did:key:zPeer"})


@pytest.mark.asyncio
async def test_4xx_response_raises_a2a_remote_error_with_detail() -> None:
    """4xx → A2ARemoteError carrying status + detail."""
    identity = _make_identity()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"detail": "args failed input_schema validation: missing 'subject_did'"},
        )

    transport = httpx.MockTransport(handler)
    client = MnemosyneA2AClient(
        base_url="https://mnemosyne.test",
        sender_did=identity.signer_did,
        identity=identity,
        recipient_did=RECIPIENT_DID,
    )

    real_async_client = httpx.AsyncClient

    def _patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("transport", transport)
        return real_async_client(*args, **kwargs)

    with patch.object(httpx, "AsyncClient", _patched_async_client):
        with pytest.raises(A2ARemoteError) as excinfo:
            await client.invoke("trust.query", {})

    assert excinfo.value.status_code == 422
    assert "missing 'subject_did'" in str(excinfo.value.detail)


@pytest.mark.asyncio
async def test_4xx_response_without_json_body() -> None:
    """4xx with non-JSON body still surfaces as A2ARemoteError."""
    identity = _make_identity()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="not authorized")

    transport = httpx.MockTransport(handler)
    client = MnemosyneA2AClient(
        base_url="https://mnemosyne.test",
        sender_did=identity.signer_did,
        identity=identity,
        recipient_did=RECIPIENT_DID,
    )

    real_async_client = httpx.AsyncClient

    def _patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("transport", transport)
        return real_async_client(*args, **kwargs)

    with patch.object(httpx, "AsyncClient", _patched_async_client):
        with pytest.raises(A2ARemoteError) as excinfo:
            await client.invoke("trust.query", {})

    assert excinfo.value.status_code == 401
    assert "not authorized" in str(excinfo.value.detail)


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------


def test_from_env_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """from_env resolves required vars + sets defaults."""
    monkeypatch.setenv(ENV_BASE_URL, "https://mnemosyne.example")
    monkeypatch.setenv(ENV_RECIPIENT_DID, "did:key:zMn")
    # Force ephemeral identity (no signing key env).
    monkeypatch.delenv("LEGBA_REGISTRY_SIGNING_KEY", raising=False)
    monkeypatch.delenv("LEGBA_REGISTRY_SIGNING_KEY_FILE", raising=False)
    monkeypatch.delenv("LEGBA_A2A_TRUSTED_KEYS", raising=False)
    monkeypatch.delenv(ENV_TIMEOUT_S, raising=False)

    client = MnemosyneA2AClient.from_env()
    assert client.base_url == "https://mnemosyne.example"
    assert client.recipient_did == "did:key:zMn"
    assert client.timeout_seconds == DEFAULT_TIMEOUT_S
    assert client.sender_did.startswith("did:legba:registry:")


def test_from_env_custom_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_BASE_URL, "https://mnemosyne.example")
    monkeypatch.setenv(ENV_RECIPIENT_DID, "did:key:zMn")
    monkeypatch.setenv(ENV_TIMEOUT_S, "5.5")
    monkeypatch.delenv("LEGBA_REGISTRY_SIGNING_KEY", raising=False)
    monkeypatch.delenv("LEGBA_REGISTRY_SIGNING_KEY_FILE", raising=False)
    monkeypatch.delenv("LEGBA_A2A_TRUSTED_KEYS", raising=False)

    client = MnemosyneA2AClient.from_env()
    assert client.timeout_seconds == 5.5


def test_from_env_missing_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_BASE_URL, raising=False)
    monkeypatch.setenv(ENV_RECIPIENT_DID, "did:key:zMn")
    with pytest.raises(ValueError, match=ENV_BASE_URL):
        MnemosyneA2AClient.from_env()


def test_from_env_missing_recipient_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_BASE_URL, "https://mnemosyne.example")
    monkeypatch.delenv(ENV_RECIPIENT_DID, raising=False)
    with pytest.raises(ValueError, match=ENV_RECIPIENT_DID):
        MnemosyneA2AClient.from_env()


def test_from_env_bad_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_BASE_URL, "https://mnemosyne.example")
    monkeypatch.setenv(ENV_RECIPIENT_DID, "did:key:zMn")
    monkeypatch.setenv(ENV_TIMEOUT_S, "not-a-number")
    with pytest.raises(ValueError, match=ENV_TIMEOUT_S):
        MnemosyneA2AClient.from_env()


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_constructor_rejects_empty_base_url() -> None:
    identity = _make_identity()
    with pytest.raises(ValueError, match="base_url"):
        MnemosyneA2AClient(
            base_url="",
            sender_did=identity.signer_did,
            identity=identity,
            recipient_did=RECIPIENT_DID,
        )


def test_constructor_rejects_missing_recipient() -> None:
    identity = _make_identity()
    with pytest.raises(ValueError, match="recipient_did"):
        MnemosyneA2AClient(
            base_url="https://m.test",
            sender_did=identity.signer_did,
            identity=identity,
            recipient_did="",
        )


@pytest.mark.asyncio
async def test_invoke_rejects_empty_skill_id() -> None:
    identity = _make_identity()
    client = MnemosyneA2AClient(
        base_url="https://mnemosyne.test",
        sender_did=identity.signer_did,
        identity=identity,
        recipient_did=RECIPIENT_DID,
    )
    with pytest.raises(ValueError, match="skill_id"):
        await client.invoke("", {})


# ---------------------------------------------------------------------------
# Live test (gated)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.getenv("LEGBA_TEST_MNEMOSYNE") != "1",
    reason="set LEGBA_TEST_MNEMOSYNE=1 to hit the real Mnemosyne deployment",
)
@pytest.mark.asyncio
async def test_live_mnemosyne_trust_query() -> None:
    """Hit the actual Mnemosyne A2A surface.

    Requires ``MNEMOSYNE_A2A_URL`` + ``MNEMOSYNE_RECIPIENT_DID`` and
    that the deployment trusts this Legba's ``LEGBA_REGISTRY_SIGNING_KEY``
    DID. When ``LEGBA_TEST_MNEMOSYNE_SUBJECT_DID`` is set, that DID is
    queried; otherwise the recipient DID is queried (self-query is a
    valid degenerate test).
    """
    client = MnemosyneA2AClient.from_env()
    subject = os.getenv("LEGBA_TEST_MNEMOSYNE_SUBJECT_DID", client.recipient_did)
    result = await client.trust_query(subject, scope="general")
    assert "score" in result
    assert "hop_count" in result
    assert "rationale" in result
