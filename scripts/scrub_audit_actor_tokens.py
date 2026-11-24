#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Re-stamp every stored copy of the registry bearer token as its principal label.

WHY. Until W4 (TOKEN-FIX) the registry's bearer gate (``_deps.require_bearer``)
returned the PRESENTED TOKEN as the principal, and every handler that stamps
"who did this" stored it verbatim: ``descriptor_audit_log.actor_id``,
``consult_sessions.principal``, ``journal_proposals.decided_by``, the vault's
``stack_credentials.created_by``, the DLQ's ``descriptor_dead_letter.actor``,
labels' ``labeled_by``. The gate now returns ``bearer:<sha256(token)[:12]>``
(``_deps.principal_label``); this script rewrites the HISTORICAL rows to that
same label.

IRREVERSIBLE BY DESIGN. There is no down-path: the whole point is that the
secret leaves the database. Nothing records the old value, so nothing can put
it back. The token has nonetheless lived in this database and in every backup
taken before the run — ROTATE IT after the scrub (new value in ``.env``,
registry + runtime recreate). This script does not rotate anything.

WHAT THE SIGNATURE COVERS (established before writing this). Each
``descriptor_audit_log`` row carries an Ed25519 signature
(``signed_payload``) over the canonical JSON of
``{occurred_at, actor_id, actor_role, namespace, descriptor_id, action,
from_version, to_version, change_summary}`` (``events.audit_payload``) —
``actor_id`` IS inside it, so a plain UPDATE would break the signature that
``GET /audit`` re-verifies (``signature_verified``).

SINCE MIGRATION 0248 (audit A-07) the log is a hash chain and this script no
longer re-signs anything: the first run (W3-F3) re-signed rows in place, which
made history rewritable by design. Now each audit row whose actor is the token
is REDACTED through ``audit_chain.redact_row``: a signed, chained ``redact``
event naming the row and its row_hash (with the reason) is appended, then the
row's actor_id becomes the label. The original row_hash stays in the chain,
the old signature stays as it was (it no longer verifies; the verifier reports
the row as ``redacted``), and the 0248 trigger refuses the edit without the
event. The script REFUSES to apply while unchained rows remain (run
``scripts/audit_chain.py backfill --apply`` first).

The unsigned columns are a plain UPDATE with the same predicate. Then ONE audit
row is appended — ``action=scrub_actor_token`` — carrying the per-table COUNTS
(never the value), signed like every other row.

HOW ROWS ARE MATCHED. The token is never sent to Postgres: the predicate is
``length(col) = len(token) AND encode(sha256(convert_to(col,'UTF8')),'hex') =
sha256(token)``, and every fetched audit row is re-checked in Python with a
constant-time compare of the digests. The token is never printed or logged.

IDEMPOTENT. A second run matches zero rows, writes nothing and appends no audit
row. DRY-RUN BY DEFAULT — prints counts only; ``--apply`` executes, in ONE
transaction (all-or-nothing).

REQUIRES the registry's own identity, from the environment:
``LEGBA_REGISTRY_API_TOKEN`` (the token to scrub) and
``LEGBA_REGISTRY_SIGNING_KEY`` / ``LEGBA_REGISTRY_SIGNING_KEY_FILE`` (the
audit-log key). It REFUSES to run on the ephemeral fallback key — those
signatures would never verify against the registry.

Run (one-off container, the bringup idiom — after the roll that ships this):

  docker run --rm --network host --env-file .env \\
      -e LEGBA_DATA_PG_DB=legba -e LEGBA_DATA_PG_HOST=127.0.0.1 \\
      --entrypoint python -v "$PWD/scripts:/app/scripts:ro" \\
      legba/legba-registry:latest scripts/scrub_audit_actor_tokens.py     # dry-run
  ... scripts/scrub_audit_actor_tokens.py --apply                         # execute
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import os
import sys
from typing import Any

import asyncpg

from legba.data.config import PostgresConfig
from legba.data.registry._deps import API_TOKEN_ENV, principal_label
from legba.data.registry.audit import AuditLogger
from legba.data.registry.audit_chain import redact_row, unchained_count
from legba.data.registry.signing import (
    SigningIdentity,
    _load_key_from_env,
    default_signer_did,
)

#: The SIGNED, CHAINED table. Handled row by row as chained redactions.
AUDIT_TABLE = ("descriptor_audit_log", "actor_id")

#: Every UNSIGNED column a bearer principal can reach (a handler stamps the
#: ``require_bearer`` return into it). Tables absent from a deployment are
#: reported ``absent`` and skipped.
PLAIN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("consult_sessions", "principal"),
    ("journal_proposals", "decided_by"),
    ("descriptor_dead_letter", "actor"),
    ("stack_credentials", "created_by"),
    ("unit_reference_labels", "labeled_by"),
    ("correctness_labels", "labeled_by"),
    ("conversion_webhooks", "retired_by"),
)

SCRUB_ACTION = "scrub_actor_token"
SCRUB_ACTOR = "system:scrub_audit_actor_tokens"


def _match_sql(col: str) -> str:
    # $1 = len(token), $2 = sha256(token) hex. The token itself never travels.
    return (
        f"length({col}) = $1 AND "
        f"encode(sha256(convert_to({col}, 'UTF8')), 'hex') = $2"
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _table_exists(conn: asyncpg.Connection, table: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table))


async def run(
    conn: asyncpg.Connection,
    *,
    token: str,
    identity: SigningIdentity,
    apply: bool,
) -> dict[str, Any]:
    """Scrub (or, dry-run, count) every stored copy of ``token``.

    Returns ``{"label", "applied", "audit": {...}, "tables": {...},
    "total", "scrub_row_id"}`` — counts only, never the token.
    """
    if not token:
        raise ValueError("empty token — nothing to scrub")
    label = principal_label(token)
    token_hex = _digest(token)
    n = len(token)
    audit_table, audit_col = AUDIT_TABLE

    result: dict[str, Any] = {
        "label": label,
        "applied": apply,
        "audit": {"matched": 0, "redacted": 0, "unchained_rows": 0},
        "tables": {},
        "total": 0,
        "scrub_row_id": None,
    }

    tx = conn.transaction()
    await tx.start()
    try:
        rows = await conn.fetch(
            f"SELECT id, occurred_at, actor_id, actor_role, namespace, "
            f"descriptor_id, action, from_version, to_version, change_summary, "
            f"signed_payload, signer_did FROM {audit_table} "
            f"WHERE {_match_sql(audit_col)} ORDER BY occurred_at"
            + (" FOR UPDATE" if apply else ""),
            n, token_hex,
        )
        # Migration 0248 — a chained row is never re-signed or rewritten in
        # place: each match is REDACTED (audit_chain.redact_row), i.e. a
        # signed, chained ``redact`` event naming the row and its row_hash is
        # appended, then actor_id is replaced. The original hash stays in the
        # chain; verify_audit_chain reports the row as ``redacted``. Refused
        # while unchained rows remain (the trigger forbids editing those).
        unchained = await unchained_count(conn)
        result["audit"]["unchained_rows"] = unchained
        matched: list[Any] = []
        for r in rows:
            # Belt and braces over the SQL predicate: constant-time digest compare.
            if hmac.compare_digest(_digest(r[audit_col]), token_hex):
                matched.append(r)
        result["audit"]["matched"] = len(matched)
        result["audit"]["redacted"] = 0
        if apply and matched:
            if unchained:
                raise SystemExit(
                    f"refusing: {unchained} audit rows are not chained yet — run "
                    "scripts/audit_chain.py backfill --apply first (a chained "
                    "redaction is the only way an audit row changes)"
                )
            logger = AuditLogger(identity)
            for r in matched:
                await redact_row(
                    conn, logger, row_id=r["id"], actor_id=SCRUB_ACTOR,
                    reason="bearer token replaced by its principal label (W4 TOKEN-FIX)",
                    replace={audit_col: label},
                )
                result["audit"]["redacted"] += 1

        for table, col in PLAIN_COLUMNS:
            key = f"{table}.{col}"
            if not await _table_exists(conn, table):
                result["tables"][key] = "absent"
                continue
            if apply:
                tag = await conn.execute(
                    f"UPDATE {table} SET {col} = $3 WHERE {_match_sql(col)}",
                    n, token_hex, label,
                )
                count = int(tag.rsplit(" ", 1)[-1])
            else:
                count = int(await conn.fetchval(
                    f"SELECT count(*) FROM {table} WHERE {_match_sql(col)}",
                    n, token_hex,
                ))
            result["tables"][key] = count

        result["total"] = result["audit"]["matched"] + sum(
            v for v in result["tables"].values() if isinstance(v, int)
        )

        if apply and result["total"] > 0:
            entry = await AuditLogger(identity).record(
                conn,
                actor_id=SCRUB_ACTOR,
                actor_role="system",
                namespace="registry",
                descriptor_id=audit_table,
                action=SCRUB_ACTION,
                change_summary={
                    "label": label,
                    "audit_rows_redacted": result["audit"]["redacted"],
                    "rows_rewritten": {
                        k: v for k, v in result["tables"].items() if isinstance(v, int)
                    },
                    "total": result["total"],
                    "note": "bearer token replaced by its principal label; "
                    "irreversible by design; rotate the token",
                },
            )
            result["scrub_row_id"] = str(entry.id)

    except BaseException:
        await tx.rollback()
        raise
    if apply:
        await tx.commit()
    else:
        await tx.rollback()
    return result


def _render(result: dict[str, Any]) -> str:
    mode = "APPLIED" if result["applied"] else "DRY-RUN (no writes)"
    a = result["audit"]
    lines = [
        f"scrub_audit_actor_tokens — {mode}",
        f"  label: {result['label']}",
        f"  {AUDIT_TABLE[0]}.{AUDIT_TABLE[1]}: matched={a['matched']} "
        f"redacted={a['redacted']} (unchained rows in the log: {a['unchained_rows']})",
    ]
    for key, v in result["tables"].items():
        lines.append(f"  {key}: {v}")
    lines.append(f"  total rows: {result['total']}")
    if result["scrub_row_id"]:
        lines.append(f"  audit row {SCRUB_ACTION}: {result['scrub_row_id']}")
    if result["applied"] and result["total"]:
        lines.append("  NEXT: rotate LEGBA_REGISTRY_API_TOKEN (.env) and recreate "
                     "registry + runtime — the old value is in every backup.")
    return "\n".join(lines)


def _identity_from_env() -> SigningIdentity:
    key = _load_key_from_env()
    if key is None:
        raise SystemExit(
            "refusing: no LEGBA_REGISTRY_SIGNING_KEY[_FILE] in the environment — "
            "an ephemeral key's signatures would never verify against the registry"
        )
    return SigningIdentity(signing_key=key, signer_did=default_signer_did())


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Replace every stored copy of the registry bearer token with "
        "its principal label, re-signing audit rows (dry-run by default).",
    )
    ap.add_argument("--apply", action="store_true",
                    help="execute in one transaction (default: dry-run, counts only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit dry-run (the default)")
    args = ap.parse_args()
    if args.apply and args.dry_run:
        ap.error("--apply and --dry-run are exclusive")

    token = os.environ.get(API_TOKEN_ENV, "").strip()
    if not token:
        print(f"refusing: {API_TOKEN_ENV} is unset — nothing to match", file=sys.stderr)
        return 2
    identity = _identity_from_env()

    conn = await asyncpg.connect(PostgresConfig.from_env().dsn)
    try:
        result = await run(conn, token=token, identity=identity, apply=args.apply)
    finally:
        await conn.close()
    print(_render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
