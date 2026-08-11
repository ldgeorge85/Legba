#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seed ``corpus_tombstones`` for the OpenSearch corpus docs that ALREADY have no
substrate row — the historical orphan population (W2-C, 2026-08-03).

WHY THIS IS A SCRIPT AND NOT A MIGRATION
----------------------------------------
The orphan set can only be computed by reading OpenSearch: it is
``{every _id in the index} - {every signals.id}``, and a SQL migration cannot
reach the index. So the census lives here, and the only thing it WRITES is
Postgres rows — the actual deletes stay with the ``corpus_retention`` sweep, so
the platform has exactly ONE code path that removes a corpus doc, not two.

MEASURED READ-ONLY BEFORE THIS WAS WRITTEN, 2026-08-03 (exhaustive scroll,
not a sample):

    corpus docs                                       182,648
    signals rows                                      111,537
    ORPHAN docs (no signals row)                       75,871   (41.5%)
    live docs                                         106,777
    indexed signals carrying NO doc                     4,743

The 41.5% corrects the 54% in the 2026-08-02 engine review §3.3, which was a
200-doc sample (108/200). The orphan total equals, to the row, the 07-28
``collapse_intrasource_dupes --apply`` run — the corpus index postdates the
06-26 remediation, so that collapse is the entire population.

WHAT IT DOES
------------
1. Scrolls every ``_id`` out of the corpus (read-only).
2. Set-differences against ``signals.id``.
3. DRY-RUN by default: prints the census and writes nothing.
   ``--apply`` INSERTs one ``corpus_tombstones`` row per orphan
   (``reason='orphan_backfill'``, ``ON CONFLICT DO NOTHING`` so a re-run is a
   no-op). The ``corpus_retention`` sweep drains them on its own cadence.

REVERSIBLE UNTIL THE SWEEP RUNS. The seeded queue is plain rows:

    DELETE FROM corpus_tombstones
     WHERE purged_at IS NULL AND reason = 'orphan_backfill';

cancels the whole backfill. After the sweep the OpenSearch delete is real, but a
doc whose Postgres row is gone is a projection of nothing — there is nothing to
restore and nothing to lose, and every dropped id stays queryable in the table.

IT ALSO REPORTS THE REVERSE DEFECT. 4,743 signals are stamped ``indexed_at`` but
have no doc — the mirror-image inconsistency, caused by the same missing
reconciliation. ``--requeue-missing`` nulls their ``indexed_at`` so
``corpus_indexer`` re-indexes them through its normal dirty-marker path. Off by
default: it is a different repair with a different cost (re-indexing work), and
it should be a deliberate choice.

USAGE
    python scripts/seed_corpus_orphan_tombstones.py                  # census only
    python scripts/seed_corpus_orphan_tombstones.py --apply
    python scripts/seed_corpus_orphan_tombstones.py --apply --requeue-missing
    python scripts/seed_corpus_orphan_tombstones.py --ids-out orphans.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

import asyncpg

from legba.data.config import OpenSearchConfig
from legba.data.opensearch import OpenSearchStore

#: Scroll page size. Big enough that 182k ids is ~37 round-trips, small enough
#: that no single response is unreasonable.
_SCROLL_SIZE = 5_000
_SCROLL_TTL = "5m"

#: Tombstone inserts per statement.
_INSERT_CHUNK = 10_000


def _dsn() -> str:
    return (
        os.getenv("LEGBA_DATA_PG_DSN")
        or "postgresql://{u}:{p}@{h}:{P}/{d}".format(
            u=os.getenv("LEGBA_DATA_PG_USER", "legba"),
            p=os.getenv("LEGBA_DATA_PG_PASSWORD", "legba"),
            h=os.getenv("LEGBA_DATA_PG_HOST", "postgres"),
            P=os.getenv("LEGBA_DATA_PG_PORT", "5432"),
            d=os.getenv("LEGBA_DATA_PG_DB", "legba"),
        )
    )


async def _scroll_all_ids(store: OpenSearchStore, index: str) -> set[str]:
    """Every ``_id`` in ``index``. Read-only (search + scroll only)."""
    client = store.client
    resp = await client.search(
        index=index,
        body={"query": {"match_all": {}}, "_source": False},
        size=_SCROLL_SIZE,
        scroll=_SCROLL_TTL,
    )
    ids: set[str] = set()
    scroll_id = resp.get("_scroll_id")
    try:
        while True:
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                break
            ids.update(h["_id"] for h in hits)
            resp = await client.scroll(
                body={"scroll": _SCROLL_TTL, "scroll_id": scroll_id}
            )
            scroll_id = resp.get("_scroll_id", scroll_id)
    finally:
        if scroll_id:
            try:
                await client.clear_scroll(body={"scroll_id": [scroll_id]})
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
    return ids


async def _run(args: argparse.Namespace) -> int:
    cfg = OpenSearchConfig.from_env()
    index = args.index or cfg.index

    store = OpenSearchStore.from_env()
    await store.connect()
    try:
        doc_ids = await _scroll_all_ids(store, index)
    finally:
        await store.close()

    conn = await asyncpg.connect(_dsn())
    try:
        pg_ids = {
            str(r["id"]) for r in await conn.fetch("SELECT id FROM signals")
        }
        indexed_ids = {
            str(r["id"])
            for r in await conn.fetch(
                "SELECT id FROM signals WHERE indexed_at IS NOT NULL"
            )
        }

        orphans = sorted(doc_ids - pg_ids)
        missing = sorted(indexed_ids - doc_ids)

        pct = 100.0 * len(orphans) / max(len(doc_ids), 1)
        print(f"index                         {index}")
        print(f"corpus docs                   {len(doc_ids):>9,}")
        print(f"signals rows                  {len(pg_ids):>9,}")
        print(f"ORPHAN docs (no signals row)  {len(orphans):>9,}   ({pct:.1f}%)")
        print(f"live docs                     {len(doc_ids & pg_ids):>9,}")
        print(f"indexed signals with NO doc   {len(missing):>9,}")

        already = int(
            await conn.fetchval(
                "SELECT count(*) FROM corpus_tombstones WHERE purged_at IS NULL"
            )
            or 0
        )
        print(f"tombstones already queued     {already:>9,}")

        if args.ids_out:
            with open(args.ids_out, "w", encoding="utf-8") as fh:
                json.dump({"orphans": orphans, "indexed_missing_doc": missing}, fh)
            print(f"\nids written to {args.ids_out}")

        if not args.apply:
            print(
                "\nDRY RUN — nothing written. Re-run with --apply to queue these "
                "orphans for the corpus_retention sweep."
            )
            return 0

        inserted = 0
        for i in range(0, len(orphans), _INSERT_CHUNK):
            chunk = orphans[i : i + _INSERT_CHUNK]
            status = await conn.execute(
                """
                INSERT INTO corpus_tombstones (doc_id, index_name, reason)
                SELECT id, $2, 'orphan_backfill'
                  FROM unnest($1::uuid[]) AS t(id)
                ON CONFLICT (doc_id) DO NOTHING
                """,
                chunk,
                index,
            )
            try:
                inserted += int(str(status).split()[-1])
            except (ValueError, IndexError):
                pass
        print(f"\ntombstones inserted           {inserted:>9,}")

        if args.requeue_missing and missing:
            # The dirty-marker contract (corpus_indexer): null indexed_at AND
            # bump updated_at in the SAME statement, or an in-flight indexer
            # batch clobbers the re-null and the row never re-indexes.
            requeued = 0
            for i in range(0, len(missing), _INSERT_CHUNK):
                chunk = missing[i : i + _INSERT_CHUNK]
                status = await conn.execute(
                    """
                    UPDATE signals
                       SET indexed_at = NULL, updated_at = now()
                     WHERE id = ANY($1::uuid[])
                    """,
                    chunk,
                )
                try:
                    requeued += int(str(status).split()[-1])
                except (ValueError, IndexError):
                    pass
            print(f"signals re-queued for index   {requeued:>9,}")

        return 0
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply", action="store_true",
        help="Write the tombstones (default: census only, writes nothing).",
    )
    ap.add_argument(
        "--requeue-missing", action="store_true",
        help=(
            "Also null indexed_at on signals that are stamped indexed but have "
            "no corpus doc, so corpus_indexer re-indexes them. Requires --apply."
        ),
    )
    ap.add_argument(
        "--index", default=None,
        help="Override the corpus index name (default: the configured one).",
    )
    ap.add_argument(
        "--ids-out", default=None,
        help="Write the orphan + missing id lists to this JSON path.",
    )
    args = ap.parse_args()
    if args.requeue_missing and not args.apply:
        ap.error("--requeue-missing requires --apply")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
