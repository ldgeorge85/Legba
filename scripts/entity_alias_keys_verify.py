#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""entity_alias_keys_verify.py — drift between entity_alias_keys and the profiles (P-6).

Read-only. Walks ``entity_profiles`` in keyset batches; for each batch, one
statement computes the keys the profiles should have FROM THE SCAN'S OWN
EXPRESSIONS (not the table's key function) and compares them with the table
(``legba.data.entity_alias_keys.verify_batch``). Reports counts by kind and up
to 20 drifting entity ids (ids only, never names).

Exit 0 = no drift, 1 = drift, 2 = error. Final line:
  ENTITY_ALIAS_KEYS_VERIFY profiles=… expected=… actual=… missing_norm=…
    missing_alias=… extra_norm=… extra_alias=… drift=… seconds=…

USAGE
    python3 scripts/entity_alias_keys_verify.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time


async def run(args: argparse.Namespace, dsn: str) -> int:
    import asyncpg

    from legba.data.entity_alias_keys import verify_all

    conn = await asyncpg.connect(
        dsn, server_settings={"statement_timeout": str(args.statement_timeout_ms),
                              "default_transaction_read_only": "on",
                              "application_name": "entity_alias_keys_verify"})
    t0 = time.monotonic()
    try:
        r = await verify_all(conn, batch_size=args.batch_size)
    finally:
        await conn.close()
    if r.drift_ids:
        print("drift entity ids (first 20): " + " ".join(r.drift_ids), flush=True)
    print(f"ENTITY_ALIAS_KEYS_VERIFY profiles={r.profiles} expected={r.expected} "
          f"actual={r.actual} missing_norm={r.missing_norm} "
          f"missing_alias={r.missing_alias} extra_norm={r.extra_norm} "
          f"extra_alias={r.extra_alias} drift={r.drift} "
          f"seconds={time.monotonic() - t0:.1f}", flush=True)
    return 1 if r.drift else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--batch-size", type=int, default=5000)
    p.add_argument("--statement-timeout-ms", type=int, default=60000)
    p.add_argument("--dsn", default=None,
                   help="override the DSN (default: PostgresConfig.from_env())")
    args = p.parse_args(argv)
    dsn = args.dsn
    if dsn is None:
        from legba.data.config import PostgresConfig
        dsn = PostgresConfig.from_env().dsn
    try:
        return asyncio.run(run(args, dsn))
    except Exception as exc:  # noqa: BLE001 — a failed check is exit 2, named
        print(f"ENTITY_ALIAS_KEYS_VERIFY error={type(exc).__name__}: {exc}", flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
