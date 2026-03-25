"""
Cryptographic utilities for Legba.

Ed25519 signing and verification for supervisor ↔ agent challenge-response
and self-modification accountability.

Adapted from AXIS shared/crypto/signing.py.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from nacl.signing import SigningKey, VerifyKey
from nacl.encoding import HexEncoder
from nacl.exceptions import BadSignatureError


class SigningError(Exception):
    pass


class VerificationError(Exception):
    pass


def hash_payload(payload: dict) -> str:
    """SHA-256 hash of a payload using canonical JSON (sorted keys, no whitespace)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def generate_keypair(private_path: str | Path, public_path: str | Path) -> None:
    """Generate a new Ed25519 keypair and save to files."""
    private_path = Path(private_path)
    public_path = Path(public_path)

    signing_key = SigningKey.generate()
    verify_key = signing_key.verify_key

    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(bytes(signing_key))
    private_path.chmod(0o600)

    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_bytes(bytes(verify_key))


def load_signing_key(path: str | Path) -> SigningKey:
    path = Path(path)
    if not path.exists():
        raise SigningError(f"Signing key not found: {path}")
    return SigningKey(path.read_bytes())


def load_verify_key(path: str | Path) -> VerifyKey:
    path = Path(path)
    if not path.exists():
        raise VerificationError(f"Verify key not found: {path}")
    return VerifyKey(path.read_bytes())


def sign_message(signing_key: SigningKey, message: str) -> str:
    """Sign a message string. Returns hex-encoded signature."""
    signed = signing_key.sign(message.encode("utf-8"), encoder=HexEncoder)
    return signed.signature.decode("utf-8")


def verify_message(verify_key: VerifyKey, signature: str, message: str) -> bool:
    """Verify a signature against a message. Raises VerificationError on failure."""
    try:
        verify_key.verify(message.encode("utf-8"), bytes.fromhex(signature))
        return True
    except BadSignatureError:
        raise VerificationError("Invalid signature")


def sign_challenge_response(signing_key: SigningKey, nonce: str, cycle_number: int) -> str:
    """Sign a challenge-response for the heartbeat protocol."""
    message = f"{nonce}:{cycle_number}"
    return sign_message(signing_key, message)


def verify_challenge_response(
    verify_key: VerifyKey, signature: str, nonce: str, cycle_number: int
) -> bool:
    """Verify a challenge-response signature."""
    message = f"{nonce}:{cycle_number}"
    return verify_message(verify_key, signature, message)
