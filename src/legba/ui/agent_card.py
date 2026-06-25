# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
A2A Agent Card — Legba instance identity and capability advertisement.

Publishes an Agent Card at /.well-known/agent.json per the A2A spec and
the shared Ed25519/DID specification. The card declares the instance's
identity (DID from Ed25519 keypair), capabilities, and exposed A2A skills.

The Agent Card is signed with the instance's Ed25519 key. Signature and
DID are returned in HTTP response headers per the spec.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from typing import Any

from nacl.signing import SigningKey, VerifyKey

from ..shared.crypto import load_signing_key

log = logging.getLogger(__name__)

# Multicodec prefix for Ed25519 public key
_ED25519_MULTICODEC = bytes([0xED, 0x01])

# Base58btc alphabet (Bitcoin)
_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58btc_encode(data: bytes) -> str:
    """Encode bytes to base58btc (Bitcoin alphabet)."""
    num = int.from_bytes(data, "big")
    result = []
    while num > 0:
        num, remainder = divmod(num, 58)
        result.append(_B58_ALPHABET[remainder:remainder + 1])
    # Preserve leading zero bytes
    for byte in data:
        if byte == 0:
            result.append(b"1")
        else:
            break
    return b"".join(reversed(result)).decode("ascii")


def _base58btc_decode(encoded: str) -> bytes:
    """Decode base58btc string to bytes."""
    num = 0
    for char in encoded.encode("ascii"):
        num = num * 58 + _B58_ALPHABET.index(char)
    # Count leading '1' characters (zero bytes)
    pad_size = 0
    for char in encoded:
        if char == "1":
            pad_size += 1
        else:
            break
    result = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    return b"\x00" * pad_size + result


def public_key_to_did(public_key_bytes: bytes) -> str:
    """Generate a did:key from raw Ed25519 public key bytes (32 bytes).

    Format: did:key:z<base58btc(0xED01 + public_key)>
    """
    if len(public_key_bytes) != 32:
        raise ValueError(f"Public key must be 32 bytes, got {len(public_key_bytes)}")
    prefixed = _ED25519_MULTICODEC + public_key_bytes
    encoded = _base58btc_encode(prefixed)
    return f"did:key:z{encoded}"


def did_to_public_key(did: str) -> bytes:
    """Extract 32-byte Ed25519 public key from a did:key string."""
    if not did.startswith("did:key:z"):
        raise ValueError(f"Expected did:key with 'z' multibase prefix, got: {did!r}")
    encoded = did[len("did:key:z"):]
    decoded = _base58btc_decode(encoded)
    if not decoded.startswith(_ED25519_MULTICODEC):
        raise ValueError("DID does not contain Ed25519 multicodec prefix (0xed01)")
    public_key_bytes = decoded[len(_ED25519_MULTICODEC):]
    if len(public_key_bytes) != 32:
        raise ValueError(
            f"Extracted public key is {len(public_key_bytes)} bytes, expected 32"
        )
    return public_key_bytes


def _canonical_json(data: dict) -> bytes:
    """Canonical JSON: sorted keys, no whitespace, UTF-8."""
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def sign_agent_card(
    card: dict, signing_key: SigningKey
) -> tuple[str, str]:
    """Sign an Agent Card body.

    Returns (signature_base64url, signer_did).

    Signature is computed over SHA-256 of canonical JSON of the card body,
    per the shared Ed25519/DID spec section 3.7.
    """
    canonical = _canonical_json(card)
    content_hash = hashlib.sha256(canonical).digest()
    # SigningKey.sign() returns a SignedMessage; .signature is the 64-byte sig
    signed = signing_key.sign(content_hash)
    signature = signed.signature  # 64 bytes
    sig_b64url = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    verify_key = signing_key.verify_key
    did = public_key_to_did(bytes(verify_key))
    return sig_b64url, did


def verify_agent_card_signature(
    card: dict, signature_b64url: str, signer_did: str
) -> bool:
    """Verify an Agent Card signature.

    Parameters
    ----------
    card : dict
        The Agent Card body.
    signature_b64url : str
        Base64url-encoded Ed25519 signature (no padding).
    signer_did : str
        The signer's DID (did:key:z...).

    Returns
    -------
    bool
        True if the signature is valid.
    """
    from nacl.exceptions import BadSignatureError

    canonical = _canonical_json(card)
    content_hash = hashlib.sha256(canonical).digest()
    public_key_bytes = did_to_public_key(signer_did)
    verify_key = VerifyKey(public_key_bytes)

    # Pad base64url if needed
    padded = signature_b64url + "=" * (-len(signature_b64url) % 4)
    signature = base64.urlsafe_b64decode(padded)

    try:
        verify_key.verify(content_hash, signature)
        return True
    except BadSignatureError:
        return False


def build_agent_card(
    instance_url: str,
    signing_key: SigningKey | None = None,
    domain_name: str = "geopolitical",
    version: str = "1.0.0",
) -> dict[str, Any]:
    """Build the Legba A2A Agent Card.

    Parameters
    ----------
    instance_url : str
        Base URL of this Legba instance (e.g. "https://legba.example.com").
    signing_key : SigningKey, optional
        The instance Ed25519 signing key. If provided, the DID is derived
        from it. If None, the DID is omitted.
    domain_name : str
        Active domain for this instance.
    version : str
        Instance version string.

    Returns
    -------
    dict
        A2A Agent Card JSON structure.
    """
    did = None
    if signing_key is not None:
        verify_key = signing_key.verify_key
        did = public_key_to_did(bytes(verify_key))

    a2a_url = f"{instance_url.rstrip('/')}/a2a"

    auth_block: dict[str, Any] = {"schemes": []}
    if did:
        auth_block["schemes"].append({
            "scheme": "did-ed25519",
            "did": did,
            "verificationMethod": f"{did}#{did.split(':')[-1]}",
        })

    card: dict[str, Any] = {
        "name": f"legba-{domain_name}",
        "description": (
            f"Autonomous intelligence analyst — {domain_name} domain. "
            "Provides analytical consultation, situation briefs, entity profiles, "
            "world assessments, and signal subscriptions."
        ),
        "url": a2a_url,
        "version": version,
        "documentationUrl": f"{instance_url.rstrip('/')}/docs",
        "capabilities": {
            "streaming": False,
            "pushNotifications": True,
            "stateTransitionHistory": True,
        },
        "authentication": auth_block,
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": [
            {
                "id": "analysis.consult",
                "name": "Analytical Consultation",
                "description": (
                    "Submit an analytical question. Returns structured analysis "
                    "with citations from the knowledge base."
                ),
                "tags": ["analysis", "consultation", "query"],
                "examples": [
                    "What is the current state of Iran-US relations?",
                    "Summarize the escalation timeline in the Gulf region.",
                ],
            },
            {
                "id": "analysis.situation_brief",
                "name": "Situation Brief",
                "description": (
                    "Get a situation brief by name or ID. Returns the brief's "
                    "thesis, evidence, competing hypotheses, and predictions."
                ),
                "tags": ["analysis", "situation", "brief", "report"],
            },
            {
                "id": "analysis.entity_profile",
                "name": "Entity Profile",
                "description": (
                    "Get the full dossier on an entity including profile data, "
                    "graph neighborhood, evidence chain, and known facts."
                ),
                "tags": ["data", "entity", "profile", "graph"],
            },
            {
                "id": "intelligence.world_assessment",
                "name": "World Assessment",
                "description": (
                    "Get the latest world assessment report produced by the "
                    "INTROSPECTION cycle."
                ),
                "tags": ["intelligence", "assessment", "report"],
            },
            {
                "id": "intelligence.subscribe",
                "name": "Signal Subscription",
                "description": (
                    "Subscribe to signals matching criteria. Push notifications "
                    "via A2A when significant changes occur."
                ),
                "tags": ["intelligence", "subscribe", "signals", "push"],
            },
            {
                "id": "data.graph_query",
                "name": "Graph Query",
                "description": (
                    "Execute a named graph operation on the entity relationship "
                    "graph. Returns structured results."
                ),
                "tags": ["data", "graph", "query", "entities"],
            },
        ],
        "extensions": {
            "pillarType": "legba",
            "domain": domain_name,
            "trustLevel": None,
            "groupMemberships": [],
        },
    }

    return card


def get_instance_signing_key() -> SigningKey | None:
    """Load the instance signing key from the configured path.

    Returns None if the key file doesn't exist (e.g. during development
    without crypto setup).
    """
    key_path = os.getenv(
        "LEGBA_SIGNING_KEY_PATH",
        "/shared/keys/signing.key",
    )
    try:
        return load_signing_key(key_path)
    except Exception:
        log.debug("Instance signing key not available at %s", key_path)
        return None


def get_instance_did() -> str | None:
    """Get the instance DID from the signing key, or None if unavailable."""
    sk = get_instance_signing_key()
    if sk is None:
        return None
    return public_key_to_did(bytes(sk.verify_key))
