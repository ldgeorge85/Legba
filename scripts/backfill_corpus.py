#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backfill the OpenSearch full-text corpus from existing signals.

Stands up the INDEX PLANE (the ``legba_signals_corpus`` OpenSearch index) from
the ~105k signals already in Postgres, so the ongoing ``corpus_indexer`` sweep
only has to keep pace with new arrivals rather than draining the whole backlog on
its bounded cadence. Both write the same doc (via
:func:`legba.data.opensearch.signal_to_doc`), so this and the sweep are
interchangeable — this is just the bulk drain.

Paginated + resumable: each page is ``WHERE indexed_at IS NULL ORDER BY
fetched_at DESC LIMIT <batch>``; after indexing a page we stamp ``indexed_at =
now()`` on its rows, which advances the cursor (the drained rows fall out of the
``indexed_at IS NULL`` predicate) — so a re-run picks up exactly where it left
off, and the OpenSearch ``_id`` (= the signal id) makes any re-index an in-place
overwrite (fully idempotent). Indexes ALL modalities.

Run in the registry container (which has the runtime deps installed), pointed at
the live Postgres + OpenSearch:

  docker exec -e LEGBA_DATA_PG_DB=legba \\
      -e LEGBA_DATA_PG_HOST=legba-postgres-1 \\
      -e LEGBA_DATA_OPENSEARCH_HOST=opensearch \\
      -e PYTHONPATH=/app/src \\
      <registry-container> python3 /app/scripts/backfill_corpus.py

Or on the host with the repo mounted (PYTHONPATH=src, loopback hosts):

  set -a; . ./.env; set +a
  PYTHONPATH=src LEGBA_DATA_PG_HOST=127.0.0.1 LEGBA_DATA_PG_DB=legba \\
      LEGBA_DATA_OPENSEARCH_HOST=127.0.0.1 \\
      python3 scripts/backfill_corpus.py --batch 1000

Flags:
  --batch N   rows per page (default 1000)
  --limit N   stop after N total rows (default: drain everything)
"""
from __future__ import annotations

import argparse
import asyncio
import os

import asyncpg

# Import the shared index-plane projection + client so the backfill and the
# ongoing sweep write byte-identical docs.
from legba.data.opensearch import CORPUS_INDEX_MAPPING, OpenSearchStore, signal_to_doc

_SELECT_PAGE_SQL = """
    SELECT id, source_id, geo, tags, entity_classes, language, modality,
           retention_class, canonical_url, source_credibility, fetched_at,
           raw_provenance, payload
      FROM signals
     WHERE indexed_at IS NULL
     ORDER BY fetched_at DESC
     LIMIT $1
"""

_STAMP_BULK_SQL = """
    UPDATE signals
       SET indexed_at = now()
     WHERE id = ANY($1::uuid[])
"""


async def _connect_pg() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill the OpenSearch signal corpus.")
    ap.add_argument("--batch", type=int, default=1000, help="rows per page")
    ap.add_argument("--limit", type=int, default=0, help="stop after N rows (0=all)")
    args = ap.parse_args()

    store = OpenSearchStore.from_env()
    await store.connect()
    index = store.cfg.index
    created = await store.ensure_index(index, CORPUS_INDEX_MAPPING)
    print(
        f"OpenSearch index {index!r} on {store.cfg.host}:{store.cfg.port} "
        f"({'created' if created else 'already present'})."
    )

    conn = await _connect_pg()
    total_examined = 0
    total_indexed = 0
    total_failed = 0
    page = 0
    try:
        while True:
            rows = await conn.fetch(_SELECT_PAGE_SQL, args.batch)
            if not rows:
                break
            docs = [signal_to_doc(r) for r in rows]
            indexed = await store.bulk_index(index, docs)
            await conn.execute(_STAMP_BULK_SQL, [r["id"] for r in rows])

            page += 1
            total_examined += len(rows)
            total_indexed += int(indexed)
            total_failed += len(rows) - int(indexed)
            print(
                f"  page {page}: examined={len(rows)} indexed={indexed} "
                f"failed={len(rows) - int(indexed)} | "
                f"running total examined={total_examined} indexed={total_indexed}"
            )
            if args.limit and total_examined >= args.limit:
                print(f"Reached --limit {args.limit}; stopping.")
                break
    finally:
        await conn.close()
        await store.close()

    print(
        f"Done. examined={total_examined} indexed={total_indexed} "
        f"failed={total_failed} over {page} page(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
