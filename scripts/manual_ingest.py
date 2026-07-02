#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""manual_ingest.py — the manual-ingest batch loader CLI (S4-T3).

Loads a validated manual batch directory (see docs/MANUAL_INGEST_FORMAT.md +
legba.data.seed.manual_schema, S4-T1) into the knowledge layer through the seed
plane (legba.data.seed.manual_batch, S4-T2). Reconciliation MODE is declared on
the batch manifest (``skip`` default / ``merge`` / ``force``) and overridable
here; nothing is ever mutated in place or hard-deleted — a value change is a
temporal supersession (the prior row is closed, not gone).

Run it in the registry container exactly like ``migrate`` / ``seed`` — a mounted
repo + ``LEGBA_DATA_PG_DB=legba`` (the known gotcha: the registrar defaults to
``legba_pivot_test``; the live DB is ``legba``):

    docker run --rm --network legba_default \
        -e LEGBA_DATA_PG_HOST=legba-postgres-1 \
        -e LEGBA_DATA_PG_DB=legba \
        -e LEGBA_DATA_PG_USER=legba -e LEGBA_DATA_PG_PASSWORD=… \
        -v "$(pwd)":/work -w /work --entrypoint python \
        legba-registry scripts/manual_ingest.py --batch ./batches/my_backfill --dry-run

Usage:
    # dry-run — print the create/merge/supersede/conflict diff; write NOTHING
    python3 scripts/manual_ingest.py --batch ./batches/my_backfill --dry-run

    # apply (mode from the manifest, or override)
    python3 scripts/manual_ingest.py --batch ./batches/my_backfill
    python3 scripts/manual_ingest.py --batch ./batches/my_backfill --mode force

Connection: reads PostgresConfig.from_env() (the same LEGBA_* env the runtime
uses). ALWAYS needs a live DB — the dry-run diff is computed by comparing each
record against the current open rows (unlike ``seed.py --dry-run``, which is
map-only). Exit codes: 0 success (a wet run with no errors, or any dry-run); 1
a wet run that hit a per-record error / DLQ; 2 a malformed batch (validation).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

# Make `legba` importable when run from a checkout.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover — dotenv optional
    pass

import asyncpg

from legba.data.config import PostgresConfig
from legba.data.seed import BatchMode, BatchValidationError, run_manual_batch


async def _run(batch_dir: str, *, mode: str | None, dry_run: bool) -> int:
    cfg = PostgresConfig.from_env()
    pool = await asyncpg.create_pool(cfg.dsn, min_size=1, max_size=4)
    try:
        report = await run_manual_batch(
            pool, batch_dir=batch_dir, mode=mode, dry_run=dry_run
        )
    finally:
        await pool.close()

    print(json.dumps(report.as_dict(), indent=2))
    if report.conflicts:
        print(
            f"\n{len(report.conflicts)} conflict(s) — a higher-tier prior blocks "
            "a merge supersession; re-run with --mode=force to override:",
            file=sys.stderr,
        )
        for line in report.conflicts:
            print(f"  - {line}", file=sys.stderr)
    # A dry-run always exits 0 (it wrote nothing). A wet run fails only on a
    # real per-record error / DLQ — a merge conflict is informational.
    return 0 if (dry_run or not report.errors) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Legba manual-ingest batch loader (skip/merge/force + dry-run).",
    )
    parser.add_argument(
        "--batch",
        required=True,
        metavar="DIR",
        help="path to the manual-ingest batch directory (batch_manifest.yaml + lane JSONLs)",
    )
    parser.add_argument(
        "--mode",
        choices=[m.value for m in BatchMode],
        default=None,
        help="override the manifest's reconciliation mode (default: use the manifest)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute + print the diff report; write NOTHING (transaction rolled back)",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(
            _run(args.batch, mode=args.mode, dry_run=args.dry_run)
        )
    except BatchValidationError as exc:
        print(f"batch validation failed: {exc}", file=sys.stderr)
        for err in exc.errors:
            print(f"  - {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover — manual invocation
    raise SystemExit(main())
