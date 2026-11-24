#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""List the tables, indexes and views in the application schema that NO
migration creates — READ-ONLY, nightly-runnable (lane P-8, A-64).

The migration set is parsed by ``legba.data.schema_hygiene`` (create / rename
/ drop, in file order); the live catalog is read inside a read-only
transaction with a 60 s statement timeout. Prints names and sizes only.

    python3 scripts/report_schema_drift.py            # text report
    python3 scripts/report_schema_drift.py --json     # machine-readable

Exit status: 0 when nothing is undeclared, 1 when something is (so a nightly
can page on it), 2 on a connection error. Connection from the usual
``LEGBA_DATA_PG_*`` variables (defaults: 127.0.0.1:5432, legba/legba).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from legba.data import schema_hygiene as sh  # noqa: E402
from legba.data.migrations import MIGRATIONS_DIR  # noqa: E402


async def read_only_connect():
    import asyncpg

    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
        server_settings={
            "default_transaction_read_only": "on",
            "statement_timeout": "60000",
            "application_name": "legba-schema-drift-report",
        },
    )


def _mb(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB" if n >= 1_048_576 else f"{n / 1024:.0f} KB"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    declared = sh.declared_objects(sh.migration_texts(MIGRATIONS_DIR))
    try:
        conn = await read_only_connect()
    except Exception as exc:  # noqa: BLE001 — a report, not a service
        print(f"cannot connect: {type(exc).__name__}", file=sys.stderr)
        return 2
    try:
        async with conn.transaction(readonly=True):
            catalog = await sh.fetch_catalog(conn)
    finally:
        await conn.close()
    extra = sh.undeclared(catalog, declared)
    missing = sh.declared_but_missing(catalog, declared)
    if args.json:
        print(json.dumps({
            "undeclared": [
                {"name": o.name, "kind": o.kind, "table": o.table, "bytes": o.bytes}
                for o in extra
            ],
            "declared_but_missing": missing,
            "catalog_objects": len(catalog),
            "declared_names": len(declared),
        }, indent=2))
    else:
        total = sum(o.bytes for o in extra)
        print(f"schema {sh.APP_SCHEMA}: {len(catalog)} objects, "
              f"{len(declared)} names declared by migrations")
        print(f"NOT created by any migration: {len(extra)} ({_mb(total)})")
        for o in sorted(extra, key=lambda o: (-o.bytes, o.name)):
            on = f" on {o.table}" if o.table else ""
            print(f"  {o.kind:<5} {o.name}{on}  {_mb(o.bytes)}")
        print(f"declared by a migration but absent: {len(missing)}")
        for n in missing:
            print(f"  {n}")
    return 1 if extra else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
