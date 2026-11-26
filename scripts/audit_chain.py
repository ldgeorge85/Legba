#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The administrative audit-log hash chain (migration 0248).

Two subcommands:

  backfill   chain every unchained ``descriptor_audit_log`` row, in
             (occurred_at, id) order, after the current head — the signed
             content is never touched. DRY-RUN BY DEFAULT (prints the plan);
             ``--apply`` writes, in one transaction. Idempotent: a second run
             finds nothing to chain.
  verify     walk the chain and print the first break (a gap or reorder in
             chain_seq, a prev_hash that is not the previous row's hash, a
             row whose content does not match its row_hash) and the
             signature status of every row (verified / signature not
             verified / redacted). Exit 0 when the chain holds, 1 on a break.
             The verify key is the registry's own when
             ``LEGBA_REGISTRY_SIGNING_KEY[_FILE]`` is set; otherwise signatures
             are reported ``unchecked``.

Rows whose signature does not verify under the current key (two June rows
on live) are chained AS THEY ARE and reported ``signature not verified`` —
never dropped, never re-signed.

Run (one-off container, the bringup idiom — after the release that ships migration 0248):

  docker run --rm --network host --env-file .env \\
      -e LEGBA_DATA_PG_DB=legba -e LEGBA_DATA_PG_HOST=127.0.0.1 \\
      --entrypoint python -v "$PWD/scripts:/app/scripts:ro" \\
      legba/legba-registry:latest scripts/audit_chain.py backfill          # plan
  ... scripts/audit_chain.py backfill --apply                             # chain
  ... scripts/audit_chain.py verify                                       # check
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import asyncpg

from legba.data.config import PostgresConfig
from legba.data.registry.audit_chain import backfill, verify_audit_chain


def _verify_key():
    from legba.data.registry.signing import _load_key_from_env

    key = _load_key_from_env()
    return key.verify_key if key is not None else None


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="chain the unchained rows (dry-run by default)")
    b.add_argument("--apply", action="store_true", help="write, in one transaction")
    sub.add_parser("verify", help="walk the chain; exit 1 on a break")
    args = ap.parse_args()

    conn = await asyncpg.connect(PostgresConfig.from_env().dsn)
    try:
        if args.cmd == "backfill":
            plan = await backfill(conn, apply=args.apply)
            mode = "APPLIED" if plan.applied else "DRY-RUN (no writes)"
            print(f"audit_chain backfill — {mode}")
            print(f"  already chained: {plan.already_chained}")
            print(f"  to chain:        {plan.to_chain} (first chain_seq {plan.first_seq})")
            print(f"  head after:      {plan.head}")
            return 0
        rep = await verify_audit_chain(conn, verify_key=_verify_key())
        print(json.dumps(rep.as_dict(), indent=2, default=str))
        return 0 if rep.ok else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
