#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""seed_source_ratings.py — catalog-seed CLI for the source assurance ledger.

P3-1 (A6 layers 1+2). Ingests a curated per-source ratings YAML and upserts
``method='catalog_seed'``, ``visibility_class='public'`` rows into
``source_ratings`` (migration 0094), with supersession history — see
``src/legba/data/seed/source_ratings_loader.py`` for the semantics.

House seed pattern: this MACHINERY ships; the curated DATA does not. The
default input ``seeds/source_ratings.yaml`` is gitignored (``seeds/*.yaml``);
start from the tracked schema doc ``seeds/source_ratings.example.yaml``.
A missing data file degrades gracefully (warn + exit 0, nothing written).

Usage:
    # dry-run: parse + validate only, write NOTHING
    python3 scripts/seed_source_ratings.py --dry-run

    # apply (against the Postgres in the environment)
    python3 scripts/seed_source_ratings.py

    # explicit file
    python3 scripts/seed_source_ratings.py --file /path/to/source_ratings.yaml

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
from legba.data.seed.source_ratings_loader import (
    DEFAULT_YAML,
    parse_ratings_yaml,
    seed_source_ratings,
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

    if dry_run:
        specs, errors = parse_ratings_yaml(path)
        print(json.dumps({
            "dry_run": True,
            "file": str(path),
            "valid_rows": [
                {
                    "source_id": s.source_id,
                    "rater": s.rater,
                    "grade": (
                        f"{s.admiralty_reliability}{s.admiralty_credibility}"
                        if s.admiralty_reliability and s.admiralty_credibility
                        else None
                    ),
                }
                for s in specs
            ],
            "errors": errors,
        }, indent=2))
        return 0 if not errors else 1

    cfg = PostgresConfig.from_env()
    conn = await asyncpg.connect(cfg.dsn)
    try:
        result = await seed_source_ratings(conn, path)
    finally:
        await conn.close()
    print(json.dumps({"file": str(path), **result.as_dict()}, indent=2))
    return 0 if not result.errors else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed catalog source ratings (assurance ledger layer 2).",
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=DEFAULT_YAML,
        help=f"curated ratings YAML (default: {DEFAULT_YAML})",
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
    raise SystemExit(main())
