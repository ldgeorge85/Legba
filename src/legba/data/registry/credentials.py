# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Encrypted credentials vault.

Per topology §9.9 + §2.5: descriptors carry `Property.Secret("foo.bar.api_key")`
references; the actual credential lives elsewhere and is resolved at use time.

Phase 1 implementation: a Postgres-backed vault (`stack_credentials` table,
migration 0011) with at-rest XSalsa20-Poly1305 encryption (libsodium
SecretBox via PyNaCl) keyed by a 32-byte master key supplied via
`LEGBA_DATA_MASTER_KEY` (32 raw bytes hex-encoded → 64 hex chars).

Key contracts:

  * `CredentialResolverProtocol` — the indirection point a future external
    vault (HashiCorp Vault, AWS Secrets Manager, etc.) implements. The
    registry never depends on `CredentialVault` directly, only on this
    protocol.

  * `CredentialVault.verify_exists(secret_id)` — registry calls this to check
    a Property.Secret reference at register-time. It MUST NOT return the
    plaintext; that's reserved for actual handler use sites (Phase 2).

  * `CredentialVault.resolve(secret_id)` — Phase-2 handler entry point.
    Returns plaintext bytes. Logged at INFO with the secret_id (NOT the
    plaintext) for audit.

  * Credential rotation: `store(secret_id, plaintext)` writes a new version
    row, flips the previous version's `is_current=false`. Old versions are
    preserved so audit replays of old descriptor versions can resolve.

  * Rotation eviction: every `store_secret` call (first-store AND rotation)
    publishes `vault.secret.rotated.<secret_id>` (see `vault_events.py`) via
    an injected `RegistryEventEmitter`, so a runtime process's cached LLM
    handlers (which resolved-and-cached the OLD plaintext at build time) get
    invalidated instead of serving stale credentials until a recreate — see
    `legba.runtime.nats_informer.NatsVaultRotationInformer`. Defaults to
    `NullEventEmitter` (no-op) so a bare `CredentialVault(store)` — the shape
    every non-server construction site uses — is unaffected.

Credentials are NEVER serialized into the descriptor payload sent to NATS
or stored in `stack_components.body`. The registry's `register()` walks the
body for Secret references and verifies them via `verify_exists`; if a
plaintext value ever appears in the body the validator rejects it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import asyncpg
from nacl.exceptions import CryptoError
from nacl.secret import SecretBox
from nacl.utils import random as nacl_random

from ..config import PostgresConfig
from ..postgres import PostgresStore
from .emitter import NullEventEmitter, RegistryEventEmitter
from .vault_events import vault_event_payload, vault_subject

logger = logging.getLogger(__name__)

MASTER_KEY_ENV = "LEGBA_DATA_MASTER_KEY"
MASTER_KEY_BYTES = 32


class MissingSecretError(KeyError):
    """Raised when a referenced `Property.Secret` id has no current entry in
    the vault. Surfaced as a descriptor-validation error at register-time."""

    def __init__(self, secret_id: str):
        super().__init__(f"vault missing secret {secret_id!r}")
        self.secret_id = secret_id


class VaultLockedError(RuntimeError):
    """Raised when the vault is asked to encrypt / decrypt but no master key
    has been configured (`LEGBA_DATA_MASTER_KEY` unset or invalid)."""


@runtime_checkable
class CredentialResolverProtocol(Protocol):
    """The indirection surface the registry depends on.

    Future external-vault adapters (`VaultBackedResolver`, `AwsSecretsResolver`)
    implement this and are swapped in at registry construction time. Method
    signatures match the in-tree `CredentialVault` below.
    """

    async def verify_exists(self, secret_id: str) -> bool: ...
    async def resolve(self, secret_id: str) -> bytes: ...


# ---------------------------------------------------------------------------
# Master-key handling
# ---------------------------------------------------------------------------


def _load_master_key() -> bytes | None:
    """Read and decode the master key. Returns None if not configured."""
    raw = os.getenv(MASTER_KEY_ENV, "").strip()
    if not raw:
        return None
    try:
        decoded = bytes.fromhex(raw)
    except ValueError as exc:
        raise VaultLockedError(
            f"{MASTER_KEY_ENV} must be hex-encoded ({MASTER_KEY_BYTES * 2} chars); "
            f"got {len(raw)} chars: {exc}"
        ) from exc
    if len(decoded) != MASTER_KEY_BYTES:
        raise VaultLockedError(
            f"{MASTER_KEY_ENV} decodes to {len(decoded)} bytes; need {MASTER_KEY_BYTES}"
        )
    return decoded


# ---------------------------------------------------------------------------
# Vault
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CredentialEntry:
    secret_id: str
    version: int
    is_current: bool
    created_at: Any
    created_by: str
    notes: str | None = None


class CredentialVault:
    """In-tree Postgres-backed vault. Conforms to `CredentialResolverProtocol`.

    Construct with either an existing `PostgresStore` (preferred — shares
    the connection pool with the registry) or a `PostgresConfig`. The vault
    table is created by migration 0011.

    `master_key` may be passed in for tests; otherwise read from
    `LEGBA_DATA_MASTER_KEY` per the bootstrap convention (config.py).

    `emitter` is the same `RegistryEventEmitter` protocol `StackRegistry`
    depends on (`NATSEventEmitter` in production, `NullEventEmitter` in
    tests / every construction site that doesn't pass one). Only the
    registry API server's construction wires a real emitter today — the
    runtime process's own `CredentialVault(pg_store)` instances (read-time
    resolution) have nothing to publish and stay on the `NullEventEmitter`
    default.
    """

    def __init__(
        self,
        store: PostgresStore,
        *,
        master_key: bytes | None = None,
        emitter: RegistryEventEmitter | None = None,
    ):
        self._store = store
        # Defer key resolution until first use so unit tests that don't touch
        # the vault don't need the env var.
        self._master_key = master_key
        self._box: SecretBox | None = None
        self._emitter = emitter or NullEventEmitter()

    @classmethod
    def from_env(cls, pg: PostgresConfig | None = None) -> "CredentialVault":
        store = PostgresStore(pg or PostgresConfig.from_env())
        return cls(store)

    @property
    def store(self) -> PostgresStore:
        return self._store

    def _ensure_box(self) -> SecretBox:
        if self._box is not None:
            return self._box
        key = self._master_key or _load_master_key()
        if key is None:
            raise VaultLockedError(
                f"vault has no master key; set {MASTER_KEY_ENV} (32 bytes hex)"
            )
        self._box = SecretBox(key)
        return self._box

    # ------------------------------------------------------------------
    # Encryption primitives (exposed for tests + tooling).
    # ------------------------------------------------------------------

    def encrypt(self, plaintext: bytes) -> tuple[bytes, bytes]:
        """Encrypt `plaintext`; return `(nonce, ciphertext)`."""
        box = self._ensure_box()
        nonce = nacl_random(SecretBox.NONCE_SIZE)
        ciphertext = box.encrypt(plaintext, nonce).ciphertext
        return nonce, ciphertext

    def decrypt(self, nonce: bytes, ciphertext: bytes) -> bytes:
        """Decrypt the (nonce, ciphertext) pair."""
        box = self._ensure_box()
        # SecretBox.decrypt expects nonce + ciphertext concatenated, or
        # nonce passed separately:
        try:
            return box.decrypt(ciphertext, nonce)
        except CryptoError as exc:
            raise VaultLockedError(f"decrypt failed: {exc}") from exc

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def store_secret(
        self,
        secret_id: str,
        plaintext: bytes | str,
        *,
        actor: str,
        notes: str | None = None,
    ) -> int:
        """Write `plaintext` for `secret_id`. Returns the new version number.

        If the secret already exists, this is a rotation: a fresh version row
        is inserted and the previous current row is flipped off.
        """
        if not secret_id or " " in secret_id:
            raise ValueError("secret_id must be a non-empty dotted identifier")
        if isinstance(plaintext, str):
            plaintext = plaintext.encode("utf-8")
        nonce, ciphertext = self.encrypt(plaintext)

        async with self._store.transaction() as conn:
            # Determine the next version number atomically.
            row = await conn.fetchrow(
                "SELECT COALESCE(MAX(version), 0) AS v "
                "FROM stack_credentials WHERE secret_id = $1",
                secret_id,
            )
            next_version = int(row["v"]) + 1

            # Flip prior current row.
            await conn.execute(
                "UPDATE stack_credentials SET is_current = FALSE "
                "WHERE secret_id = $1 AND is_current",
                secret_id,
            )

            await conn.execute(
                """
                INSERT INTO stack_credentials
                    (secret_id, version, is_current, nonce, ciphertext,
                     created_by, notes)
                VALUES ($1, $2, TRUE, $3, $4, $5, $6)
                """,
                secret_id,
                next_version,
                nonce,
                ciphertext,
                actor,
                notes,
            )
        logger.info(
            "stored credential secret_id=%s version=%d actor=%s",
            secret_id,
            next_version,
            actor,
        )
        # Eviction hook: publish AFTER the write commits, so a subscriber that
        # reacts by re-resolving never race-reads the pre-rotation row. This
        # covers first-store too (next_version == 1) — nothing has a handler
        # built off a not-yet-existing secret, so that sweep is a no-op; the
        # alternative (branching on rotation-vs-create) buys nothing and adds
        # a code path nobody would exercise differently. No try/except here —
        # same convention as `StackRegistry._emit`'s call sites: the emitter
        # itself (`NATSEventEmitter`/`NullEventEmitter`) already swallows and
        # logs publish failures, so the Postgres write above stays the source
        # of truth regardless of NATS reachability.
        await self._emitter.publish(
            vault_subject("rotated", secret_id),
            vault_event_payload(
                action="rotated",
                secret_id=secret_id,
                actor=actor,
                version=next_version,
            ),
        )
        return next_version

    async def verify_exists(self, secret_id: str) -> bool:
        """Return True iff a current row for `secret_id` exists in the vault."""
        async with self._store.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM stack_credentials "
                "WHERE secret_id = $1 AND is_current",
                secret_id,
            )
        return row is not None

    async def resolve(self, secret_id: str, version: int | None = None) -> bytes:
        """Resolve a credential to plaintext bytes.

        `version=None` resolves the current version; otherwise the named
        historical version (for audit-replay against old descriptor versions).
        """
        async with self._store.acquire() as conn:
            if version is None:
                row = await conn.fetchrow(
                    "SELECT nonce, ciphertext "
                    "FROM stack_credentials "
                    "WHERE secret_id = $1 AND is_current",
                    secret_id,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT nonce, ciphertext FROM stack_credentials "
                    "WHERE secret_id = $1 AND version = $2",
                    secret_id,
                    version,
                )
        if row is None:
            raise MissingSecretError(secret_id)
        plaintext = self.decrypt(bytes(row["nonce"]), bytes(row["ciphertext"]))
        # IMPORTANT: log the lookup but NEVER the plaintext.
        logger.debug("resolved credential secret_id=%s", secret_id)
        return plaintext

    async def list_entries(self, secret_id: str | None = None) -> list[CredentialEntry]:
        """Return entry metadata (no plaintext, no ciphertext) for inspection."""
        async with self._store.acquire() as conn:
            if secret_id is None:
                rows = await conn.fetch(
                    "SELECT secret_id, version, is_current, created_at, "
                    "created_by, notes FROM stack_credentials "
                    "ORDER BY secret_id, version DESC"
                )
            else:
                rows = await conn.fetch(
                    "SELECT secret_id, version, is_current, created_at, "
                    "created_by, notes FROM stack_credentials "
                    "WHERE secret_id = $1 ORDER BY version DESC",
                    secret_id,
                )
        return [
            CredentialEntry(
                secret_id=r["secret_id"],
                version=r["version"],
                is_current=r["is_current"],
                created_at=r["created_at"],
                created_by=r["created_by"],
                notes=r["notes"],
            )
            for r in rows
        ]

    async def delete_secret(self, secret_id: str) -> int:
        """Hard-delete all rows for a secret_id. Returns row count removed.

        Use sparingly — preferred path is rotation (`store_secret`). Hard
        delete breaks audit replay; left as an admin-only operation.
        """
        async with self._store.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM stack_credentials WHERE secret_id = $1",
                secret_id,
            )
        # asyncpg returns the tag string 'DELETE <n>'; parse it.
        try:
            n = int(result.split()[1])
        except (IndexError, ValueError):
            n = 0
        logger.warning("hard-deleted credential secret_id=%s rows=%d", secret_id, n)
        return n
