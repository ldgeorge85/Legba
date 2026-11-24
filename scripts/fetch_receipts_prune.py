#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prune the stored BYTES of fetch responses and ingest-time page copies that
no output references after N days.

DRY-RUN BY DEFAULT: prints how many versions would be pruned and the bytes it
would free, and writes nothing. ``--apply`` unlinks each unreferenced object,
writes a ``fetch_version_tombstones`` row (what, when, why, policy version)
and stamps ``fetch_versions.bytes_pruned_at`` — the version row, its hash,
metadata, record links and extracted text are KEPT. Referenced bytes are
never pruned (the cold-tier mover, ``scripts/archive_tier.py``, retires them).
The rule and its measured share live in :mod:`legba.data.fetch_receipt_retention`.

Bounded I/O (``--batch-size`` x ``--max-batches`` versions per run), idempotent
(a pruned version is never selected again). REFUSES (exit 2) when migration
0245 is not applied.

Policy knob: ``--unreferenced-after-days`` (env
``LEGBA_FETCH_RECEIPT_UNREFERENCED_AFTER_DAYS``, default 30).

Run inside the runtime image with the archive volume and the database env::

    docker run --rm --network legba_default --env-file .env \\
        -v legba_archive:/var/lib/legba/archive -v "$PWD/scripts:/s:ro" \\
        --entrypoint python legba/legba-runtime-dapr:latest /s/fetch_receipts_prune.py [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path

try:
    from legba.data import fetch_receipt_retention as ret
except ImportError:  # a checkout without the package installed
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from legba.data import fetch_receipt_retention as ret
from legba.data.archive import archive_root


async def _amain(args: argparse.Namespace) -> int:
    base = ret.RetentionPolicy.from_env()
    policy = ret.RetentionPolicy(
        unreferenced_after_days=args.unreferenced_after_days or base.unreferenced_after_days,
        batch_size=args.batch_size or base.batch_size,
        max_batches=args.max_batches or base.max_batches,
    )
    import asyncpg  # noqa: PLC0415 — runtime image only

    conn = await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )
    try:
        if not await conn.fetchval("SELECT to_regclass('public.fetch_version_tombstones') IS NOT NULL"):
            print("fetch_receipts_prune: REFUSED — migration 0245 is not applied", file=sys.stderr)
            return 2
        report = await ret.prune_unreferenced(conn, Path(args.root), policy, apply=args.apply)
    finally:
        await conn.close()
    print(json.dumps(dataclasses.asdict(report), indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(archive_root()))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--unreferenced-after-days", type=int, default=0)
    ap.add_argument("--batch-size", type=int, default=0)
    ap.add_argument("--max-batches", type=int, default=0)
    return asyncio.run(_amain(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
