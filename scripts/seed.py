#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""seed.py — curated/authoritative seeding CLI (flavor b roots).

Runs a registered :class:`~legba.data.seed.SeedSource` adapter: fetch → map →
resolve entities → write seed facts/nexuses (stamped source_type +
seed_batch_id) → record the ``seed_batches`` row. Idempotent: a re-run of the
same source upserts (no duplicate triples) — it DOES record a new batch row
each run (the batch ledger is an audit of imports; the marker on already-open
rows is left untouched by the upsert).

Usage:
    # list registered adapters
    python3 scripts/seed.py --list

    # dry-run (fetch + map only; writes NOTHING, no batch row)
    python3 scripts/seed.py --source world_baseline --dry-run

    # apply (against the Postgres in the environment)
    python3 scripts/seed.py --source world_baseline

    # the structured external adapters (flavor b, tier 1):
    python3 scripts/seed.py --source wikidata_leaders          # live SPARQL
    python3 scripts/seed.py --source acled_conflict \
        --option api_key=… --option email=you@example.com \
        --option country=NGA --option since=2024-01-01

Connection: reads PostgresConfig.from_env() (the same LEGBA_* env the runtime
uses). Run it where that env points at the target DB.
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
from legba.data.seed import get_adapter, list_adapters, run_seed_source


def _parse_options(raw: list[str] | None) -> dict[str, str]:
    """Parse repeated ``--option key=value`` flags into a dict.

    Adapter-specific knobs ride here (e.g. ACLED needs ``api_key`` + ``email``;
    a seed import is an operator one-shot, not a registered descriptor, so the
    key is passed on the command line / env rather than resolved from a vault).
    """
    opts: dict[str, str] = {}
    for item in raw or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise SystemExit(f"--option must be key=value, got {item!r}")
        opts[key.strip()] = value
    return opts


async def _run(source: str, *, dry_run: bool, options: dict[str, str]) -> int:
    adapter = get_adapter(source)
    if dry_run:
        # No DB needed for a pure map-only dry-run.
        result = await run_seed_source(None, adapter, dry_run=True, options=options)
        print(json.dumps(result.as_dict(), indent=2))
        return 0

    cfg = PostgresConfig.from_env()
    pool = await asyncpg.create_pool(cfg.dsn, min_size=1, max_size=4)
    try:
        result = await run_seed_source(pool, adapter, dry_run=False, options=options)
    finally:
        await pool.close()
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if not result.errors else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Legba curated-seed importer.")
    parser.add_argument("--source", help="adapter name (see --list)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch + map only; write nothing",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list registered seed adapters and exit",
    )
    parser.add_argument(
        "--option",
        action="append",
        metavar="KEY=VALUE",
        help="adapter option (repeatable), e.g. --option api_key=… --option email=…",
    )
    args = parser.parse_args()

    if args.list:
        for name, source_type in list_adapters():
            print(f"{name}\t({source_type})")
        return 0

    if not args.source:
        parser.error("one of --source or --list is required")

    return asyncio.run(
        _run(args.source, dry_run=args.dry_run, options=_parse_options(args.option))
    )


if __name__ == "__main__":  # pragma: no cover — manual invocation
    raise SystemExit(main())
