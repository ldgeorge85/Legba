#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""load_scheduled_events.py — seed CLI for the scheduled-events calendar.

W11-F141. Ingests a curated events YAML and inserts ``provenance='seed'``
rows into ``scheduled_events`` (migration 0240) — see
``src/legba/data/scheduled_events.py`` for the schema, the validation and
the (title, starts_at) idempotence key.

House seed pattern: this MACHINERY ships; the curated DATA does not. The
default input ``seeds/scheduled_events.yaml`` is gitignored
(``seeds/*.yaml``); start from the tracked schema doc
``seeds/scheduled_events.example.yaml`` (FICTIONAL rows only). A missing
data file degrades gracefully (warn + exit 0, nothing written).

Usage:
    # dry-run: parse + validate only, write NOTHING
    python3 scripts/load_scheduled_events.py --dry-run

    # apply (against the Postgres in the environment)
    python3 scripts/load_scheduled_events.py

    # explicit file
    python3 scripts/load_scheduled_events.py --file /path/to/scheduled_events.yaml

Connection: reads PostgresConfig.from_env() (the same LEGBA_* env the runtime
uses). Run it where that env points at the target DB.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# Make `legba` importable when run from a checkout.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover — dotenv optional
    pass

import asyncpg

from legba.data.config import PostgresConfig
from legba.data.scheduled_events import (
    DEFAULT_YAML,
    insert_event,
    parse_events_yaml,
)


async def _run(path: Path, *, dry_run: bool) -> int:
    if not path.exists():
        # Graceful degrade (the house adapter pattern): no curated data is a
        # supported state, not an error — Legba ships no bundled seed data.
        print(
            f"no seed file at {path} — nothing to do; Legba ships no bundled "
            "seed data, provide your own (see seeds/README.md)",
            file=sys.stderr,
        )
        return 0

    specs, errors = parse_events_yaml(path)

    if dry_run:
        print(json.dumps({
            "dry_run": True,
            "file": str(path),
            "valid_rows": [
                {
                    "title": s["title"],
                    "starts_at": s["starts_at"].isoformat(),
                    "ends_at": (
                        s["ends_at"].isoformat() if s["ends_at"] else None
                    ),
                    "scope": s["scope"],
                    "confidence": s["confidence"],
                }
                for s in specs
            ],
            "errors": errors,
        }, indent=2))
        return 0 if not errors else 1

    cfg = PostgresConfig.from_env()
    conn = await asyncpg.connect(cfg.dsn)
    inserted = 0
    skipped = 0
    try:
        for spec in specs:
            try:
                new_id = await insert_event(conn, provenance="seed", **spec)
            except Exception as exc:  # noqa: BLE001 — one bad row ≠ abort
                errors.append(f"{spec['title']!r}: {exc}")
                continue
            if new_id is None:
                skipped += 1
            else:
                inserted += 1
    finally:
        await conn.close()
    print(json.dumps({
        "file": str(path),
        "inserted": inserted,
        "skipped": skipped,
        "errors": errors,
    }, indent=2))
    return 0 if not errors else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Load the scheduled-events calendar (W11-F141).",
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=DEFAULT_YAML,
        help=f"curated events YAML (default: {DEFAULT_YAML})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse + validate only; write nothing",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(_run(args.file, dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
