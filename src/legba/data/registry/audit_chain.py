# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The administrative audit log as a hash chain (migration 0248; audit A-06/A-07).

Every ``descriptor_audit_log`` row is Ed25519-signed (``audit.AuditLogger``);
this module chains them, on the receipt chain's conventions
(``provenance.receipts``: canonical JSON, SHA-256, ``ZERO_HASH`` genesis):

    row_hash = sha256(canonical_json({
        "v": 1, "chain_seq": n, "id": <uuid>, "prev_hash": <previous row_hash>,
        "content": <the exact dict the row's signature covers>,
        "signature": <signed_payload hex>, "signer_did": <signer_did>,
    }))

* :func:`chain_insert_values` — called by ``AuditLogger.record`` inside the
  insert's transaction, under :data:`CHAIN_LOCK_KEY`: the next ``chain_seq``,
  ``prev_hash`` and ``row_hash``, or ``None`` while unchained rows remain
  (before the backfill: the backfill then chains them in order, so the chain
  never has a hole in the middle).
* :func:`backfill` — chains every unchained row in ``(occurred_at, id)``
  order after the current head, never touching signed content; idempotent.
* :func:`verify_audit_chain` — walks the chain and reports the FIRST break
  (a gap or reorder in ``chain_seq``, a ``prev_hash`` that is not the previous
  row's hash, a ``row_hash`` that does not recompute) plus per-row signature
  status: ``verified``, ``signature not verified`` (kept and chained as it is,
  never dropped or re-signed — two June rows written under an earlier key),
  or ``redacted``.
* :func:`redact_row` — the ONLY way content changes after chaining: append a
  signed, chained ``redact`` event naming the row and its row_hash, then
  replace the named fields. The original row_hash stays in the chain; the
  verifier checks the row's links but not its (tombstoned) content.
* :func:`chain_head` — the ``(chain_seq, row_hash)`` the daily
  ``receipt_anchor`` folds in, so an outside witness covers this history.

Threat model (docs/ANALYSIS.md, "Audit-log chain"): a holder of the database
and the signing key can still rewrite or truncate the whole chain and
recompute every hash; they cannot do so without the result disagreeing with
a head already anchored outside (OpenTimestamps), and anything short of a full
recompute — an edited, deleted, inserted or reordered row — breaks the chain
at that row.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable
from uuid import UUID

from ..provenance import canonical_json
from ..provenance._core import ZERO_HASH

#: Chain material revision — bump if the hashed shape ever changes.
CHAIN_VERSION = 1

#: ``pg_advisory_xact_lock`` key serialising chain appends (the ASCII of
#: "LAUD", so it is recognisable in ``pg_locks``).
CHAIN_LOCK_KEY = 0x4C_41_55_44

REDACT_ACTION = "redact"

SIG_VERIFIED = "verified"
SIG_NOT_VERIFIED = "signature not verified"
SIG_REDACTED = "redacted"
SIG_UNCHECKED = "unchecked"

_ROW_COLUMNS = (
    "id, occurred_at, actor_id, actor_role, namespace, descriptor_id, action, "
    "from_version, to_version, change_summary, signed_payload, signer_did, "
    "chain_seq, prev_hash, row_hash"
)

#: Content columns a redaction may replace.
REDACTABLE_FIELDS = frozenset({"actor_id", "change_summary", "from_version", "to_version"})


def _summary(row: Any) -> dict[str, Any]:
    s = row["change_summary"]
    if isinstance(s, str):
        try:
            s = json.loads(s)
        except Exception:
            s = {}
    return s if isinstance(s, dict) else {}


def row_hash_for(row: Any, *, chain_seq: int, prev_hash: str) -> str:
    """The row's chain hash over its stored content (see module doc)."""
    from .audit import signed_payload_for_row

    material = {
        "v": CHAIN_VERSION,
        "chain_seq": int(chain_seq),
        "id": str(row["id"]),
        "prev_hash": prev_hash,
        "content": signed_payload_for_row(row),
        "signature": bytes(row["signed_payload"]).hex(),
        "signer_did": row["signer_did"],
    }
    return hashlib.sha256(canonical_json(material)).hexdigest()


async def chain_head(conn: Any) -> tuple[int, str] | None:
    """``(chain_seq, row_hash)`` of the last chained row, or ``None``."""
    r = await conn.fetchrow(
        "SELECT chain_seq, row_hash FROM descriptor_audit_log "
        "WHERE chain_seq IS NOT NULL ORDER BY chain_seq DESC LIMIT 1"
    )
    return (int(r["chain_seq"]), str(r["row_hash"])) if r else None


async def unchained_count(conn: Any) -> int:
    return int(await conn.fetchval(
        "SELECT count(*) FROM descriptor_audit_log WHERE row_hash IS NULL"
    ))


async def chain_insert_values(conn: Any, row: dict[str, Any]) -> tuple[int, str, str] | None:
    """``(chain_seq, prev_hash, row_hash)`` for a row about to be inserted on
    ``conn`` (caller is inside a transaction), or ``None`` while unchained rows
    remain. Takes the transaction-scoped chain lock first."""
    await conn.execute("SELECT pg_advisory_xact_lock($1)", CHAIN_LOCK_KEY)
    if await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM descriptor_audit_log WHERE row_hash IS NULL)"
    ):
        return None
    head = await chain_head(conn)
    seq, prev = (head[0] + 1, head[1]) if head else (1, ZERO_HASH)
    return seq, prev, row_hash_for(row, chain_seq=seq, prev_hash=prev)


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


@dataclass
class BackfillPlan:
    already_chained: int = 0
    to_chain: int = 0
    first_seq: int | None = None
    applied: bool = False
    head: tuple[int, str] | None = None


async def backfill(conn: Any, *, apply: bool = False) -> BackfillPlan:
    """Chain every unchained row, in ``(occurred_at, id)`` order, after the
    current head. Dry-run unless ``apply``; one transaction; idempotent (a
    second run finds nothing to chain). Signed content is never touched — the
    0248 trigger refuses any other change to an unchained row."""
    plan = BackfillPlan()
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute("SELECT pg_advisory_xact_lock($1)", CHAIN_LOCK_KEY)
        plan.already_chained = int(await conn.fetchval(
            "SELECT count(*) FROM descriptor_audit_log WHERE row_hash IS NOT NULL"
        ))
        rows = await conn.fetch(
            f"SELECT {_ROW_COLUMNS} FROM descriptor_audit_log "
            "WHERE row_hash IS NULL ORDER BY occurred_at, id"
        )
        plan.to_chain = len(rows)
        head = await chain_head(conn)
        seq, prev = (head[0], head[1]) if head else (0, ZERO_HASH)
        plan.first_seq = seq + 1 if rows else None
        updates: list[tuple[int, str, str, UUID]] = []
        for r in rows:
            seq += 1
            h = row_hash_for(r, chain_seq=seq, prev_hash=prev)
            updates.append((seq, prev, h, r["id"]))
            prev = h
        if apply and updates:
            await conn.executemany(
                "UPDATE descriptor_audit_log SET chain_seq = $1, prev_hash = $2, "
                "row_hash = $3 WHERE id = $4 AND row_hash IS NULL",
                updates,
            )
        plan.head = (seq, prev) if (rows or head) else None
    except BaseException:
        await tx.rollback()
        raise
    if apply:
        await tx.commit()
        plan.applied = True
    else:
        await tx.rollback()
    return plan


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@dataclass
class ChainReport:
    ok: bool = True
    rows: int = 0
    unchained: int = 0
    head: tuple[int, str] | None = None
    first_break: dict[str, Any] | None = None
    signatures: dict[str, int] = field(default_factory=dict)
    signature_not_verified_ids: list[str] = field(default_factory=list)
    redacted_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "rows": self.rows, "unchained": self.unchained,
            "head": list(self.head) if self.head else None,
            "first_break": self.first_break, "signatures": self.signatures,
            "signature_not_verified_ids": self.signature_not_verified_ids,
            "redacted_ids": self.redacted_ids,
        }


def _redactions(rows: Iterable[Any]) -> dict[str, str]:
    """``{redacted row id: its recorded row_hash}`` from chained redact events."""
    out: dict[str, str] = {}
    for r in rows:
        if r["action"] == REDACT_ACTION and r["row_hash"] is not None:
            s = _summary(r)
            rid, rh = s.get("redacts_row_id"), s.get("redacted_row_hash")
            if isinstance(rid, str) and isinstance(rh, str):
                out[rid] = rh
    return out


def verify_rows(rows: list[Any], *, verify_key: Any | None = None) -> ChainReport:
    """Walk chained rows (any order in; sorted by ``chain_seq`` here) and
    report the first break. Pure — :func:`verify_audit_chain` feeds it."""
    from .audit import signed_payload_for_row
    from .errors import AuditChainError
    from .signing import verify_audit_payload

    rep = ChainReport()
    chained = sorted((r for r in rows if r["row_hash"] is not None),
                     key=lambda r: int(r["chain_seq"]))
    rep.unchained = sum(1 for r in rows if r["row_hash"] is None)
    rep.rows = len(chained)
    redacted = _redactions(chained)
    prev, expect_seq = ZERO_HASH, 1
    for r in chained:
        rid, seq = str(r["id"]), int(r["chain_seq"])
        brk = None
        if seq != expect_seq:
            brk = "sequence gap or reorder"
        elif r["prev_hash"] != prev:
            brk = "prev_hash does not match the previous row's hash"
        elif redacted.get(rid) == r["row_hash"]:
            pass                                  # content tombstoned by a chained event
        elif row_hash_for(r, chain_seq=seq, prev_hash=prev) != r["row_hash"]:
            brk = "row content does not match its row_hash"
        if brk is not None and rep.first_break is None:
            rep.ok = False
            rep.first_break = {"chain_seq": seq, "expected_seq": expect_seq,
                               "id": rid, "reason": brk}
        # signature status
        if rid in redacted:
            status = SIG_REDACTED
            rep.redacted_ids.append(rid)
        elif verify_key is None:
            status = SIG_UNCHECKED
        else:
            try:
                verify_audit_payload(verify_key, signed_payload_for_row(r),
                                     bytes(r["signed_payload"]))
                status = SIG_VERIFIED
            except (AuditChainError, ValueError, TypeError):  # nacl: malformed signature
                status = SIG_NOT_VERIFIED
                rep.signature_not_verified_ids.append(rid)
        rep.signatures[status] = rep.signatures.get(status, 0) + 1
        prev, expect_seq = r["row_hash"], seq + 1
    rep.head = (expect_seq - 1, prev) if chained else None
    return rep


async def verify_audit_chain(conn: Any, *, verify_key: Any | None = None) -> ChainReport:
    """Read the whole log and walk the chain (see :func:`verify_rows`)."""
    rows = await conn.fetch(f"SELECT {_ROW_COLUMNS} FROM descriptor_audit_log")
    return verify_rows(list(rows), verify_key=verify_key)


# ---------------------------------------------------------------------------
# Redaction — an appended, chained event; never a silent rewrite
# ---------------------------------------------------------------------------


async def redact_row(
    conn: Any,
    logger: Any,
    *,
    row_id: UUID | str,
    reason: str,
    replace: dict[str, Any],
    actor_id: str,
) -> Any:
    """Redact chained row ``row_id``: append a signed, chained ``redact`` event
    (``redacts_row_id``, ``redacted_row_hash``, ``reason``, ``fields``), then
    replace ``replace``'s fields on the row. Both in one transaction; the 0248
    trigger refuses the replacement without the event. Returns the event's
    :class:`~.audit.AuditEntry`."""
    if not reason.strip():
        raise ValueError("a redaction needs a reason")
    bad = set(replace) - REDACTABLE_FIELDS
    if bad or not replace:
        raise ValueError(f"redactable fields are {sorted(REDACTABLE_FIELDS)}; got {sorted(replace)}")
    async with conn.transaction():
        target = await conn.fetchrow(
            "SELECT id, row_hash, chain_seq, namespace, descriptor_id "
            "FROM descriptor_audit_log WHERE id = $1 FOR UPDATE", UUID(str(row_id)),
        )
        if target is None:
            raise LookupError(f"no audit row {row_id}")
        if target["row_hash"] is None:
            raise RuntimeError("the row is not chained yet: run scripts/audit_chain.py backfill --apply first")
        entry = await logger.record(
            conn, actor_id=actor_id, actor_role="system",
            namespace=target["namespace"], descriptor_id=target["descriptor_id"],
            action=REDACT_ACTION,
            change_summary={
                "redacts_row_id": str(target["id"]),
                "redacted_row_hash": target["row_hash"],
                "redacted_chain_seq": int(target["chain_seq"]),
                "reason": reason,
                "fields": sorted(replace),
            },
        )
        if entry.row_hash is None:
            raise RuntimeError("the redact event was not chained (unchained rows remain)")
        sets, params = [], []
        for k in sorted(replace):
            v = replace[k]
            params.append(canonical_json(v).decode("utf-8") if k == "change_summary" else v)
            sets.append(f"{k} = ${len(params)}" + ("::jsonb" if k == "change_summary" else ""))
        params.append(target["id"])
        await conn.execute(
            f"UPDATE descriptor_audit_log SET {', '.join(sets)} WHERE id = ${len(params)}",
            *params,
        )
    return entry


__all__ = [
    "CHAIN_LOCK_KEY",
    "CHAIN_VERSION",
    "REDACTABLE_FIELDS",
    "REDACT_ACTION",
    "SIG_NOT_VERIFIED",
    "SIG_REDACTED",
    "SIG_UNCHECKED",
    "SIG_VERIFIED",
    "BackfillPlan",
    "ChainReport",
    "backfill",
    "chain_head",
    "chain_insert_values",
    "redact_row",
    "row_hash_for",
    "unchained_count",
    "verify_audit_chain",
    "verify_rows",
]
