#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verify the held family's status log and its cache (lane DM-C).

READ-ONLY BY DEFAULT: recomputes every ``assertion_status_log`` row's hash
from its fields (``legba.data.held_record_chain``), walks each assertion's
chain in ``seq`` order and names the first break per assertion (``seq_gap``,
``prev_hash_mismatch``, ``row_hash_mismatch``); rebuilds the status cache from
the log alone and lists every assertion whose stored cache row differs.
Exit 1 on any break or difference.

``--write`` replaces the cache with the rebuild (the cache is the one
updatable table; the log is never touched), in one transaction, then
verifies again.

    python3 scripts/held_record_verify.py --dsn postgresql://...
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from legba.data.held_record_reads import chain_report  # noqa: E402

_UPSERT_SQL = """
INSERT INTO assertion_status (assertion_id, status, version_id, last_seq, last_row_hash, status_at, entries, batch)
SELECT $1::uuid, $2, $3, $4, $5, $6, $7, (SELECT batch FROM assertions WHERE id = $1::uuid)
ON CONFLICT (assertion_id) DO UPDATE
   SET status = EXCLUDED.status, version_id = EXCLUDED.version_id, last_seq = EXCLUDED.last_seq,
       last_row_hash = EXCLUDED.last_row_hash, status_at = EXCLUDED.status_at, entries = EXCLUDED.entries
"""


async def rebuild(conn, report) -> int:
    """Replace the cache rows that differ with the rebuild; drop rows with no log."""
    n = 0
    async with conn.transaction():
        for aid in report["cache_differs"]:
            row = report["rebuilt"].get(aid)
            if row is None:
                await conn.execute("DELETE FROM assertion_status WHERE assertion_id = $1::uuid", aid)
            else:
                await conn.execute(_UPSERT_SQL, aid, row["status"], row["version_id"], row["last_seq"],
                                   row["last_row_hash"], row["status_at"], row["entries"])
            n += 1
    return n


async def main_async(args: argparse.Namespace) -> int:
    import asyncpg

    settings = {} if args.write else {"default_transaction_read_only": "on"}
    conn = await asyncpg.connect(args.dsn, server_settings=settings)
    try:
        report = await chain_report(conn)
        out = {k: v for k, v in report.items() if k != "rebuilt"}
        if args.write and report["cache_differs"]:
            out["rewritten"] = await rebuild(conn, report)
            again = await chain_report(conn)
            out["after"] = {"breaks": again["breaks"], "cache_differs": again["cache_differs"]}
            report = again
    finally:
        await conn.close()
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 1 if report["breaks"] or report["cache_differs"] else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--dsn", required=True)
    p.add_argument("--write", action="store_true", help="replace the cache with the rebuild")
    return asyncio.run(main_async(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
