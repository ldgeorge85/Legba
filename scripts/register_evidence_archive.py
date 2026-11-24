#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the evidence archive's objects into ``fetch_versions`` BY HASH (B-1).

No byte is copied, moved or deleted, and nothing in ``evidence_archive`` is
written: each archived object becomes (or is recorded on) a version labelled
ARCHIVED LATER and the signal is linked to it with role ``archived``. The
rules live in :mod:`legba.data.archive_register`.

DRY-RUN BY DEFAULT: walks the whole archive and prints counts (objects that
would register, versions new / reused / given the archive's object, signals
that would gain a link) plus the three-label measurement, and verifies a
sample of objects' hashes through the archive's reader. ``--apply`` writes in
bounded batches (``--batch-size`` x ``--max-batches``), each in its own
transaction; ``--after <signal id>`` resumes (the report prints
``last_signal_id``). Idempotent. REFUSES (exit 2) when migration 0266 is not
applied. Rollback of a run: its ``batch`` id names every row it wrote
(``fetch_version_records.register_batch``, ``fetch_versions.registered->>'batch'``).

Run inside the runtime image with the archive volume READ-ONLY and the
database env::

    docker run --rm --network legba_default --env-file .env \\
        -v legba_archive:/var/lib/legba/archive:ro -v "$PWD/scripts:/s:ro" \\
        --entrypoint python legba/legba-runtime-dapr:latest /s/register_evidence_archive.py [--apply]
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
    from legba.data import archive_register as reg
except ImportError:  # a checkout without the package installed
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from legba.data import archive_register as reg


async def _connect() -> object:
    import asyncpg  # noqa: PLC0415 — runtime image only

    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )


async def _amain(args: argparse.Namespace) -> int:
    conn = await _connect()
    try:
        if not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'"
            " AND table_name = 'fetch_version_records' AND column_name = 'register_batch')"
        ):
            if args.apply:
                print("register_evidence_archive: REFUSED — migration 0266 is not applied", file=sys.stderr)
                return 2
            print("register_evidence_archive: migration 0266 not applied — dry run only", file=sys.stderr)
        store = reg.default_store(Path(args.root)) if args.root else reg.default_store()
        if args.apply:
            report = await reg.register(
                conn, apply=True, batch_size=args.batch_size, max_batches=args.max_batches,
                after=args.after, verify_per_batch=args.verify_per_batch, store=store,
            )
            out = {"register": report.as_dict()}
        else:
            # A dry run is read-only end to end.
            async with conn.transaction(readonly=True):
                await conn.execute(f"SET LOCAL statement_timeout = '{int(args.statement_timeout_s)}s'")
                report = await reg.register(
                    conn, apply=False, batch_size=args.batch_size, max_batches=args.max_batches,
                    after=args.after, verify_per_batch=args.verify_per_batch, store=store,
                )
                measure = await reg.measure_states(conn)
            out = {"register": report.as_dict(), "labels_after_full_register": dataclasses.asdict(measure)}
    finally:
        await conn.close()
    print(json.dumps(out, indent=2, default=str))
    return 0 if not out["register"]["verify_mismatch"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--batch-size", type=int, default=2000)
    ap.add_argument("--max-batches", type=int, default=0, help="0 = to the end (dry run); set it for --apply")
    ap.add_argument("--after", default=None, help="resume after this signal id")
    ap.add_argument("--verify-per-batch", type=int, default=2)
    ap.add_argument("--root", default=None, help="archive root (default: LEGBA_ARCHIVE_ROOT)")
    ap.add_argument("--statement-timeout-s", type=int, default=60)
    args = ap.parse_args(argv)
    if args.apply and not args.max_batches:
        ap.error("--apply needs --max-batches (bounded runs)")
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
