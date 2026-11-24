#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Citations as rows (DM-A): derive sentence and support rows for findings
written before the dual write, from their stored fields.

DRY-RUN BY DEFAULT (read-only session): the count of findings without rows and
a plan over a bounded sample, extrapolated. ``--apply`` writes in bounded
batches (``--batch-size`` x ``--max-batches``), each batch one transaction,
every row tagged ``--batch-id``; resume with ``--after-time/--after-id`` (the
report prints ``next_cursor``); idempotent (a finding with rows is skipped).
``--rollback <batch-id>`` deletes one batch's rows. Rules:
:mod:`legba.data.citation_rows_backfill`. ``--apply`` / ``--rollback`` REFUSE
(exit 2) when the migration ``20261115T1828_citations_as_rows`` is not
applied; the dry run works before it (every finding is then pending).

    python3 scripts/citation_rows_backfill.py                       # dry run
    python3 scripts/citation_rows_backfill.py --since 2026-11-01 --max-findings 5000
    python3 scripts/citation_rows_backfill.py --apply --batch-id bf-20261116a --max-batches 10
    python3 scripts/citation_rows_backfill.py --rollback bf-20261116a

Database env as every script (``LEGBA_DATA_PG_*``).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# This checkout's package first (an editable install elsewhere must not win).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from legba.data import citation_rows_backfill as bf  # noqa: E402


def _ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    d = datetime.fromisoformat(raw)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


async def _connect(read_only: bool) -> object:
    import asyncpg  # noqa: PLC0415

    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
        server_settings={"default_transaction_read_only": "on"} if read_only else None,
    )


async def _amain(args: argparse.Namespace) -> int:
    conn = await _connect(read_only=not (args.apply or args.rollback))
    try:
        writes = args.apply or args.rollback
        if writes and not await conn.fetchval("SELECT to_regclass('public.finding_citation_receipts') IS NOT NULL"):
            print("migration 20261115T1828_citations_as_rows is not applied", file=sys.stderr)
            return 2
        if args.rollback:
            out = await bf.rollback_batch(conn, args.rollback)
        elif args.apply:
            out = await bf.apply_batches(
                conn, batch_id=args.batch_id, since=_ts(args.since), until=_ts(args.until),
                batch_size=args.batch_size, max_batches=args.max_batches,
                cursor=bf.BackfillCursor(_ts(args.after_time), args.after_id))
        else:
            out = await bf.dry_run(conn, since=_ts(args.since), until=_ts(args.until),
                                   max_findings=args.max_findings)
    finally:
        await conn.close()
    print(json.dumps({"mode": "rollback" if args.rollback else "apply" if args.apply else "dry_run",
                      **out}, indent=1, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--since", help="produced_at >= (ISO)")
    p.add_argument("--until", help="produced_at < (ISO)")
    p.add_argument("--max-findings", type=int, default=2000, help="dry run: sample size")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--batch-id", default=f"bf-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}")
    p.add_argument("--batch-size", type=int, default=200)
    p.add_argument("--max-batches", type=int, default=1)
    p.add_argument("--after-time", help="resume cursor: produced_at")
    p.add_argument("--after-id", help="resume cursor: finding id")
    p.add_argument("--rollback", metavar="BATCH_ID")
    args = p.parse_args(argv)
    if args.apply and args.rollback:
        p.error("--apply and --rollback are exclusive")
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
