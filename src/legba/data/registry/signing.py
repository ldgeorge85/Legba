# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ed25519 signing helpers for the descriptor audit log.

Per `design/legba_observability.md` §8 every audit-log row carries an
Ed25519 signature over the canonical-JSON of the payload. The signing key
defaults to the per-process key resolved from `LEGBA_REGISTRY_SIGNING_KEY`
(hex-encoded private key) or `LEGBA_REGISTRY_SIGNING_KEY_FILE` (path to a
32-byte file). For test runs we fall back to an ephemeral in-memory key —
the test fixtures pin this so signatures verify in CI.

`signer_did` is the DID-style identifier of the signing identity. For the
registry process this defaults to `did:legba:registry:<host>` and is
override-able via `LEGBA_REGISTRY_SIGNER_DID`.
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nacl.signing import SigningKey, VerifyKey
from nacl.exceptions import BadSignatureError

from ..provenance import canonical_json
from .errors import AuditChainError

logger = logging.getLogger(__name__)


@dataclass
class SigningIdentity:
    """Bundles the SigningKey + the DID-style identifier used as `signer_did`.

    `verify_key` is exposed so tests / verifiers can re-verify chained
    audit rows.
    """

    signing_key: SigningKey
    signer_did: str

    @property
    def verify_key(self) -> VerifyKey:
        return self.signing_key.verify_key


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------


def _hostname_fragment() -> str:
    try:
        return socket.gethostname().split(".")[0]
    except Exception:
        return "unknown"


def default_signer_did() -> str:
    return os.getenv(
        "LEGBA_REGISTRY_SIGNER_DID",
        f"did:legba:registry:{_hostname_fragment()}",
    )


def _load_key_from_env() -> SigningKey | None:
    """Resolve a key from `LEGBA_REGISTRY_SIGNING_KEY` (hex) or
    `LEGBA_REGISTRY_SIGNING_KEY_FILE` (32 raw bytes). Returns None when
    neither is configured."""
    hex_key = os.getenv("LEGBA_REGISTRY_SIGNING_KEY")
    if hex_key:
        try:
            return SigningKey(bytes.fromhex(hex_key))
        except Exception as exc:
            raise AuditChainError(
                f"LEGBA_REGISTRY_SIGNING_KEY invalid: {exc}"
            ) from exc

    key_path = os.getenv("LEGBA_REGISTRY_SIGNING_KEY_FILE")
    if key_path:
        p = Path(key_path)
        if not p.exists():
            raise AuditChainError(
                f"LEGBA_REGISTRY_SIGNING_KEY_FILE not found: {key_path}"
            )
        raw = p.read_bytes()
        if len(raw) != 32:
            raise AuditChainError(
                f"LEGBA_REGISTRY_SIGNING_KEY_FILE must be 32 bytes, got {len(raw)}"
            )
        return SigningKey(raw)
    return None


def load_default_identity() -> SigningIdentity:
    """Return the process-wide signing identity.

    Resolution order:
      1. `LEGBA_REGISTRY_SIGNING_KEY` (hex)
      2. `LEGBA_REGISTRY_SIGNING_KEY_FILE` (path to 32-byte file)
      3. Ephemeral in-memory key (logged at WARNING — tests rely on this)
    """
    key = _load_key_from_env()
    if key is None:
        logger.warning(
            "no LEGBA_REGISTRY_SIGNING_KEY[_FILE] set; using ephemeral signing key "
            "(audit signatures will not verify across process restarts)"
        )
        key = SigningKey.generate()
    return SigningIdentity(signing_key=key, signer_did=default_signer_did())


# ---------------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------------


def sign_audit_payload(
    identity: SigningIdentity,
    payload: dict[str, Any],
) -> bytes:
    """Sign the canonical-JSON serialisation of `payload`.

    Returns the raw 64-byte signature.  Reuses the project's
    `canonical_json` (sort_keys, no whitespace, UTF-8) for consistency with
    receipt-hash payloads.
    """
    try:
        body = canonical_json(payload)
        signed = identity.signing_key.sign(body)
        return signed.signature
    except Exception as exc:
        raise AuditChainError(f"failed to sign audit payload: {exc}") from exc


def verify_audit_payload(
    verify_key: VerifyKey,
    payload: dict[str, Any],
    signature: bytes,
) -> bool:
    """Verify a signature against a payload. Raises AuditChainError on
    failure (so tests fail loudly), returns True on success."""
    try:
        verify_key.verify(canonical_json(payload), signature)
        return True
    except BadSignatureError as exc:
        raise AuditChainError(f"bad audit signature: {exc}") from exc
