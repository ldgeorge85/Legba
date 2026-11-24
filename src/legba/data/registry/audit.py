# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ed25519-signed audit-log writer.

Shared by both the descriptor registry (L-110) and the stack registry
(L-111). Persists every mutation to `descriptor_audit_log` (migration 0009)
with a signature over canonical-JSON.

The actor / role split mirrors `design/legba_observability.md` §8.

`AuditLogger.record(...)` is fire-and-forget from the caller's perspective
in the sense that it raises on failure — registries surface that as an
`AuditChainError`. The caller wraps the audit-log write + the mutation in
a single transaction so an audit-write failure aborts the mutation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from ...shared.registry_principal import role_for_actor
from ..provenance import canonical_json
from .audit_chain import chain_insert_values
from .errors import AuditChainError
from .events import audit_payload
from .signing import SigningIdentity, load_default_identity, sign_audit_payload

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditEntry:
    """Row shape returned to callers (immutable view)."""

    id: UUID
    occurred_at: datetime
    actor_id: str
    actor_role: str
    namespace: str
    descriptor_id: str
    action: str
    from_version: str | None
    to_version: str | None
    change_summary: dict[str, Any]
    signer_did: str
    #: Migration 0248 — the row's chain position and hash (None while the log
    #: still holds unchained rows, i.e. before the backfill).
    chain_seq: int | None = None
    row_hash: str | None = None


def signed_payload_for_row(
    row: Any, *, actor_id: str | None = None,
) -> dict[str, Any]:
    """Rebuild the exact dict :meth:`AuditLogger.record` signed for a stored row.

    The signature covers ``actor_id`` (alongside occurred_at, actor_role,
    namespace, descriptor_id, action, from/to_version and change_summary); it
    does NOT cover ``id`` or ``signer_did`` — the hash CHAIN does (migration
    0248, ``audit_chain.row_hash_for``: id, signer_did, the signature and the
    previous row's hash). ``occurred_at`` is re-rendered as UTC isoformat, which is what
    ``record`` signed (it stamps ``datetime.now(tz=timezone.utc)``) and what the
    ``GET /audit`` verifier (``api._audit_row_out``) rebuilds.

    ``actor_id`` overrides the stored actor — the signed re-stamp in
    ``scripts/scrub_audit_actor_tokens.py`` signs the same row under its new,
    non-secret actor label.
    """
    summary = row["change_summary"]
    if isinstance(summary, str):
        import json
        try:
            summary = json.loads(summary)
        except Exception:
            summary = {}
    return audit_payload(
        action=row["action"],
        family=row["namespace"],
        descriptor_id=row["descriptor_id"],
        actor_id=row["actor_id"] if actor_id is None else actor_id,
        actor_role=row["actor_role"],
        from_version=row["from_version"],
        to_version=row["to_version"],
        change_summary=summary or {},
        occurred_at=row["occurred_at"].astimezone(timezone.utc).isoformat(),
    )


class AuditLogger:
    """Writes `descriptor_audit_log` rows with Ed25519 signatures.

    Construct once per process; share the instance across registries.
    """

    def __init__(
        self,
        identity: SigningIdentity | None = None,
    ):
        self._identity = identity or load_default_identity()

    @property
    def identity(self) -> SigningIdentity:
        return self._identity

    @property
    def signer_did(self) -> str:
        return self._identity.signer_did

    async def record(
        self,
        conn: asyncpg.Connection,
        *,
        actor_id: str,
        namespace: str,
        descriptor_id: str,
        action: str,
        actor_role: str | None = None,
        from_version: str | None = None,
        to_version: str | None = None,
        change_summary: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> AuditEntry:
        """Write one audit-log row using the given connection.

        Caller passes an existing `asyncpg.Connection` so the audit write
        participates in the surrounding transaction (so it rolls back with
        the mutation on failure).

        ``actor_role`` omitted (W11-F62) follows the principal the ``actor_id``
        label names (``service:…`` -> ``service``, else ``operator``).
        """
        if actor_role is None:
            actor_role = role_for_actor(actor_id)
        now = occurred_at or datetime.now(tz=timezone.utc)
        payload = audit_payload(
            action=action,
            family=namespace,
            descriptor_id=descriptor_id,
            actor_id=actor_id,
            actor_role=actor_role,
            from_version=from_version,
            to_version=to_version,
            change_summary=change_summary,
            occurred_at=now.isoformat(),
        )
        try:
            signature = sign_audit_payload(self._identity, payload)
        except AuditChainError:
            raise

        row_id = uuid4()
        summary_json = canonical_json(change_summary or {}).decode("utf-8")
        # Migration 0248 — the row joins the hash chain in THIS transaction,
        # under the chain lock (a savepoint when the caller already holds a
        # transaction, so the lock lasts until the caller's commit).
        stored = {
            "id": row_id, "occurred_at": now, "actor_id": actor_id,
            "actor_role": actor_role, "namespace": namespace,
            "descriptor_id": descriptor_id, "action": action,
            "from_version": from_version, "to_version": to_version,
            # The dict exactly as the jsonb column will hand it back.
            "change_summary": json.loads(summary_json),
            "signed_payload": signature, "signer_did": self._identity.signer_did,
        }
        async with conn.transaction():
            chain = await chain_insert_values(conn, stored)
            if chain is None:
                logger.warning(
                    "audit_chain.unchained_insert id=%s — unchained rows remain; "
                    "run scripts/audit_chain.py backfill --apply", row_id,
                )
            seq, prev, row_hash = chain if chain is not None else (None, None, None)
            # `signed_payload` per migration 0009 is BYTEA — store the raw
            # signature (consumers can re-verify with the same identity).
            await conn.execute(
                """
                INSERT INTO descriptor_audit_log
                    (id, occurred_at, actor_id, actor_role, namespace,
                     descriptor_id, action, from_version, to_version,
                     change_summary, signed_payload, signer_did,
                     chain_seq, prev_hash, row_hash)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12,
                        $13, $14, $15)
                """,
                row_id,
                now,
                actor_id,
                actor_role,
                namespace,
                descriptor_id,
                action,
                from_version,
                to_version,
                summary_json,
                signature,
                self._identity.signer_did,
                seq,
                prev,
                row_hash,
            )
        logger.debug(
            "audit recorded ns=%s id=%s action=%s actor=%s",
            namespace, descriptor_id, action, actor_id,
        )
        return AuditEntry(
            id=row_id,
            occurred_at=now,
            actor_id=actor_id,
            actor_role=actor_role,
            namespace=namespace,
            descriptor_id=descriptor_id,
            action=action,
            from_version=from_version,
            to_version=to_version,
            change_summary=change_summary or {},
            signer_did=self._identity.signer_did,
            chain_seq=seq,
            row_hash=row_hash,
        )

    async def fetch_entries(
        self,
        conn: asyncpg.Connection,
        *,
        descriptor_id: str | None = None,
        namespace: str | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        """Query audit entries — for inspection + tests."""
        where: list[str] = []
        params: list[Any] = []
        if descriptor_id is not None:
            params.append(descriptor_id)
            where.append(f"descriptor_id = ${len(params)}")
        if namespace is not None:
            params.append(namespace)
            where.append(f"namespace = ${len(params)}")
        sql = (
            "SELECT id, occurred_at, actor_id, actor_role, namespace, "
            "descriptor_id, action, from_version, to_version, "
            "change_summary, signer_did "
            "FROM descriptor_audit_log"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        params.append(limit)
        sql += f" ORDER BY occurred_at DESC LIMIT ${len(params)}"
        rows = await conn.fetch(sql, *params)
        out: list[AuditEntry] = []
        for r in rows:
            summary = r["change_summary"]
            if isinstance(summary, str):
                # asyncpg returns jsonb as str if no codec; parse defensively.
                import json
                try:
                    summary = json.loads(summary)
                except Exception:
                    summary = {}
            out.append(
                AuditEntry(
                    id=r["id"],
                    occurred_at=r["occurred_at"],
                    actor_id=r["actor_id"],
                    actor_role=r["actor_role"],
                    namespace=r["namespace"],
                    descriptor_id=r["descriptor_id"],
                    action=r["action"],
                    from_version=r["from_version"],
                    to_version=r["to_version"],
                    change_summary=summary or {},
                    signer_did=r["signer_did"],
                )
            )
        return out
