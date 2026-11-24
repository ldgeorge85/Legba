#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build the read-latency indexes out of transaction — DRY RUN by default.

Lane R-1, the out-of-transaction half of migrations 0251 and 0257 (lane P-5).
Those migrations build their indexes only on a small table; on the live tables
(signals ~436k rows, analyst_outputs ~200k, audit_checkpoints ~290k) the build
is this script's job, because the runner holds every migration inside one
transaction and CREATE INDEX CONCURRENTLY cannot run there.

Modes:

  (no flags)   DRY RUN. Reports each index as valid / invalid / missing.
  --create     CREATE INDEX CONCURRENTLY IF NOT EXISTS for each missing index
               (autocommit, outside any transaction; signal writers are never
               blocked). An INVALID leftover from an interrupted concurrent
               build is dropped CONCURRENTLY and rebuilt. Exit 1 unless every
               index ends valid.

Usage (inside the runtime container, DB env from the container)::

      python3 scripts/create_read_latency_indexes.py            # dry run
      python3 scripts/create_read_latency_indexes.py --create   # a few min live
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import Any

#: name -> (table, definition), byte-for-byte the definition in the migration
#: named by MIGRATION_OF (pinned by tests/data_pkg/test_registry_read_latency.py).
INDEXES: dict[str, tuple[str, str]] = {
    "idx_signals_source_created_at": (
        "public.signals",
        "ON public.signals USING btree (source_id, created_at)",
    ),
    "idx_analyst_outputs_critique_created_at": (
        "public.analyst_outputs",
        "ON public.analyst_outputs USING btree (created_at, id)"
        " WHERE (kind = 'critique')",
    ),
    "idx_analyst_outputs_critique_analyzed_analyst": (
        "public.analyst_outputs",
        "ON public.analyst_outputs USING btree"
        " (((data ->> 'analyzed_analyst_id')), created_at, id)"
        " WHERE (kind = 'critique')",
    ),
    "idx_audit_checkpoints_chain_head": (
        "public.audit_checkpoints",
        "ON public.audit_checkpoints USING btree (chain_head_hash)",
    ),
}

#: name -> the migration file that carries the same definition.
MIGRATION_OF: dict[str, str] = {
    "idx_signals_source_created_at": "0251_signals_source_created_at.sql",
    "idx_analyst_outputs_critique_created_at":
        "0257_critique_and_checkpoint_read_indexes.sql",
    "idx_analyst_outputs_critique_analyzed_analyst":
        "0257_critique_and_checkpoint_read_indexes.sql",
    "idx_audit_checkpoints_chain_head":
        "0257_critique_and_checkpoint_read_indexes.sql",
}


async def index_state(conn: Any) -> dict[str, str]:
    """``{index_name: "valid" | "invalid" | "missing"}``."""
    rows = await conn.fetch(
        "SELECT c.relname, i.indisvalid FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = ANY($1::text[])",
        list(INDEXES))
    found = {r["relname"]: ("valid" if r["indisvalid"] else "invalid") for r in rows}
    return {name: found.get(name, "missing") for name in INDEXES}


async def create_indexes(conn: Any, *, log=print) -> dict[str, str]:
    """Build each index CONCURRENTLY. ``conn`` must NOT be in a transaction."""
    await conn.execute("SET lock_timeout = '10s'")
    for name, (_table, definition) in INDEXES.items():
        state = (await index_state(conn))[name]
        if state == "invalid":
            log(f"{name}: INVALID leftover from an interrupted build — dropping CONCURRENTLY")
            await conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{name}")
            state = "missing"
        if state == "missing":
            t0 = time.monotonic()
            await conn.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} {definition}")
            log(f"{name}: built in {time.monotonic() - t0:.1f} s")
        else:
            log(f"{name}: already present and valid")
    return await index_state(conn)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--create", action="store_true")
    args = ap.parse_args()
    import asyncpg

    conn = await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )
    try:
        if not args.create:
            print(f"dry run: {await index_state(conn)} (pass --create to build)")
            return 0
        states = await create_indexes(conn)
        print(f"indexes: {states}")
        return 0 if all(s == "valid" for s in states.values()) else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
