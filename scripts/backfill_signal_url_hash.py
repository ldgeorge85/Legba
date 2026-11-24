#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build the canonical_url_hash indexes and backfill the column — DRY RUN by default.

Lane N-S, the out-of-transaction half of migration 0247. The migration adds
``signals.canonical_url_hash`` (nullable, catalog-only) and builds its two
partial indexes only on a small table; on the live table (~430k rows) both
the index builds and the backfill are this script's job, because the runner
holds every migration inside one transaction and neither CREATE INDEX
CONCURRENTLY nor a 400k-row UPDATE belongs there.

Modes (combine as listed in the roll order; nothing is written without
``--create-indexes`` or ``--apply``):

  (no flags)          DRY RUN. Reports whether the column and both indexes
                      exist (and are valid), how many rows still need a hash,
                      and the hashes the first batch would receive.
  --create-indexes    CREATE INDEX CONCURRENTLY IF NOT EXISTS for both indexes
                      (autocommit, outside any transaction; writers are never
                      blocked). An INVALID leftover from an interrupted
                      concurrent build is dropped CONCURRENTLY and rebuilt.
  --apply             Backfill: bounded batches (``--batch-size``, default
                      2000) in their own short transactions, oldest first via
                      the shrinking partial index, each row guarded by
                      ``canonical_url_hash IS NULL AND canonical_url = <the
                      value hashed>`` so a concurrent writer is never
                      overwritten. ``updated_at`` is NOT touched: the hash is
                      a derived key, not a content change.
  --recompute         With --apply: also rewrite hashes that differ from a
                      fresh computation (run after any change to
                      ``legba.data._url_canon.canonical_url``).
  --verify            Read-only: recompute every stored hash and count stale
                      ones and rows still un-hashed. Exit 1 if either is > 0.

Usage (inside the runtime container, DB env from the container)::

      python3 scripts/backfill_signal_url_hash.py                    # dry run
      python3 scripts/backfill_signal_url_hash.py --create-indexes   # ~1 min
      python3 scripts/backfill_signal_url_hash.py --apply            # ~2-4 min
      python3 scripts/backfill_signal_url_hash.py --verify
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from legba.data._url_canon import stored_canonical_url_hash  # noqa: E402

#: The two indexes, byte-for-byte the definitions in migration 0247 (pinned by
#: tests/data_pkg/test_signal_url_hash_migration.py).
INDEXES: dict[str, str] = {
    "idx_signals_canonical_url_hash": (
        "ON public.signals USING btree (canonical_url_hash, owner_tenant, fetched_at, id) "
        "WHERE canonical_url_hash IS NOT NULL"
    ),
    "idx_signals_canonical_url_unhashed": (
        "ON public.signals USING btree (fetched_at, id) "
        "WHERE canonical_url_hash IS NULL "
        "AND canonical_url IS NOT NULL "
        "AND canonical_url <> ''"
    ),
}

DEFAULT_BATCH = 2000

_PENDING_WHERE = (
    "canonical_url_hash IS NULL AND canonical_url IS NOT NULL AND canonical_url <> ''"
)


async def column_exists(conn: Any) -> bool:
    return bool(await conn.fetchval(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = 'signals' AND column_name = 'canonical_url_hash'"))


async def index_state(conn: Any) -> dict[str, str]:
    """``{index_name: "valid" | "invalid" | "missing"}``."""
    rows = await conn.fetch(
        "SELECT c.relname, i.indisvalid FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE i.indrelid = 'public.signals'::regclass AND c.relname = ANY($1::text[])",
        list(INDEXES))
    found = {r["relname"]: ("valid" if r["indisvalid"] else "invalid") for r in rows}
    return {name: found.get(name, "missing") for name in INDEXES}


async def pending_count(conn: Any) -> int:
    return int(await conn.fetchval(f"SELECT count(*) FROM signals WHERE {_PENDING_WHERE}"))


async def create_indexes(conn: Any, *, log=print) -> dict[str, str]:
    """Build both indexes CONCURRENTLY. ``conn`` must NOT be in a transaction."""
    await conn.execute("SET lock_timeout = '10s'")
    for name, definition in INDEXES.items():
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


async def next_batch(conn: Any, size: int, *, after: tuple | None = None,
                     recompute: bool = False) -> list[dict]:
    if recompute:
        where = "canonical_url IS NOT NULL AND canonical_url <> ''"
    else:
        where = _PENDING_WHERE
    params: list[Any] = [size]
    if after is not None:
        where += " AND (fetched_at, id) > ($2::timestamptz, $3::uuid)"
        params += list(after)
    rows = await conn.fetch(
        f"SELECT id, fetched_at, canonical_url, canonical_url_hash FROM signals "
        f"WHERE {where} ORDER BY fetched_at ASC, id ASC LIMIT $1", *params)
    return [dict(r) for r in rows]


async def apply_batch(conn: Any, rows: list[dict], *, recompute: bool = False) -> int:
    """Write the hash for ``rows``; returns rows updated. One transaction."""
    todo = []
    for r in rows:
        h = stored_canonical_url_hash(r["canonical_url"])
        if h != r["canonical_url_hash"]:
            todo.append((r["id"], r["canonical_url"], h))
    if not todo:
        return 0
    guard = ("s.canonical_url_hash IS DISTINCT FROM v.h" if recompute
             else "s.canonical_url_hash IS NULL")
    async with conn.transaction():
        status = await conn.execute(
            f"""
            UPDATE signals s SET canonical_url_hash = v.h
              FROM unnest($1::uuid[], $2::text[], $3::text[]) AS v(id, url, h)
             WHERE s.id = v.id AND s.canonical_url = v.url AND {guard}
            """,
            [t[0] for t in todo], [t[1] for t in todo], [t[2] for t in todo])
    return int(status.split()[-1])


async def backfill(conn: Any, *, batch_size: int = DEFAULT_BATCH, recompute: bool = False,
                   max_batches: int | None = None, pause_s: float = 0.0,
                   log=print) -> dict[str, int]:
    updated = scanned = batches = 0
    after: tuple | None = None
    t0 = time.monotonic()
    while max_batches is None or batches < max_batches:
        rows = await next_batch(conn, batch_size, after=after, recompute=recompute)
        if not rows:
            break
        after = (rows[-1]["fetched_at"], rows[-1]["id"])
        updated += await apply_batch(conn, rows, recompute=recompute)
        scanned += len(rows)
        batches += 1
        if batches % 20 == 0:
            log(f"  batches={batches} scanned={scanned} updated={updated} "
                f"elapsed={time.monotonic() - t0:.0f}s")
        if pause_s:
            await asyncio.sleep(pause_s)
    return {"batches": batches, "scanned": scanned, "updated": updated}


async def verify(conn: Any, *, batch_size: int = 20000) -> dict[str, int]:
    """Read-only: stale hashes (stored != recomputed) and rows still pending."""
    stale = checked = 0
    after: tuple | None = None
    while True:
        params: list[Any] = [batch_size]
        where = "canonical_url_hash IS NOT NULL"
        if after is not None:
            where += " AND (fetched_at, id) > ($2::timestamptz, $3::uuid)"
            params += list(after)
        rows = await conn.fetch(
            f"SELECT id, fetched_at, canonical_url, canonical_url_hash FROM signals "
            f"WHERE {where} ORDER BY fetched_at ASC, id ASC LIMIT $1", *params)
        if not rows:
            break
        after = (rows[-1]["fetched_at"], rows[-1]["id"])
        for r in rows:
            checked += 1
            if stored_canonical_url_hash(r["canonical_url"]) != r["canonical_url_hash"]:
                stale += 1
    return {"checked": checked, "stale": stale, "pending": await pending_count(conn)}


async def dry_run(conn: Any, *, batch_size: int, log=print) -> int:
    async with conn.transaction(readonly=True):
        if not await column_exists(conn):
            log("column signals.canonical_url_hash is MISSING — apply migration 0247 first")
            return 1
        for name, state in (await index_state(conn)).items():
            log(f"index {name}: {state}")
        n = await pending_count(conn)
        log(f"rows needing a hash: {n} (batches of {batch_size}: {-(-n // batch_size)})")
        for r in (await next_batch(conn, min(batch_size, 5)))[:5]:
            log(f"  e.g. {r['id']} {r['canonical_url'][:80]!r} -> "
                f"{stored_canonical_url_hash(r['canonical_url'])[:16]}…")
    log("DRY RUN — nothing written (pass --create-indexes and/or --apply)")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--create-indexes", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--recompute", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    ap.add_argument("--max-batches", type=int, default=None)
    ap.add_argument("--pause", type=float, default=0.0, help="seconds between batches")
    args = ap.parse_args()
    if args.recompute and not args.apply:
        ap.error("--recompute needs --apply")
    import asyncpg

    conn = await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )
    try:
        if not (args.create_indexes or args.apply or args.verify):
            return await dry_run(conn, batch_size=args.batch_size)
        if not await column_exists(conn):
            print("column signals.canonical_url_hash is MISSING — apply migration 0247 first")
            return 1
        if args.create_indexes:
            states = await create_indexes(conn)
            if any(s != "valid" for s in states.values()):
                print(f"index build incomplete: {states}")
                return 1
        if args.apply:
            print(f"backfill: {await pending_count(conn)} rows pending")
            out = await backfill(conn, batch_size=args.batch_size, recompute=args.recompute,
                                 max_batches=args.max_batches, pause_s=args.pause)
            print(f"backfill done: {out}")
        if args.verify:
            async with conn.transaction(readonly=True):
                out = await verify(conn)
            print(f"verify: {out}")
            if out["stale"] or out["pending"]:
                return 1
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
