#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Replace finally rejected relationship candidates by a marker, then delete the
full rows. DRY RUN by default.

A candidate in ``proposed_edges`` that ended ``rejected`` or ``retired`` keeps
its evidence text and lineage although nothing reads them; the only thing the
row still does is hold the unique key
``uq_proposed_edges_triple`` (lower(source), lower(target), relationship type),
which stops the same pair being proposed and judged again.
``proposed_edge_markers`` keeps just that key (plus the final state, when it was
reached and the confidence), and the producer skips a pair that has a marker.
New final states reach the marker table through a trigger on ``proposed_edges``
(migration 20261125T1000_proposed_edge_markers); this script handles the rows
that were already final when the trigger was created, and removes the full rows.

For each batch of final-state rows (ordered by id, ``--batch`` at a time, one
transaction per batch):

  1. the marker is written for every row in the batch (``ON CONFLICT DO
     NOTHING``, so a re-run and the trigger never clash);
  2. the full row is deleted ONLY IF its marker is present (checked in the
     DELETE itself) and nothing else names it. A row anything names is held
     back and keeps its full row (its marker is still written): the same
     reference check as ``drop_dead_relationship_candidates.py`` (nexus
     promotion, ``derived_from`` of findings, journal, hypotheses, facts,
     nexuses and entity edges, consumption, trace row refs, and an analyst
     output's own text unless ``--skip-text-scan``).

Resumable and idempotent: a batch that stopped midway left markers and no
deletes, or both; a re-run writes no marker twice and finds the deleted rows
gone. The held-back set is computed once per run in one read-only snapshot.

  (no flag)   DRY RUN, read only. Prints how many final rows there are, how many
              already have a marker, how many would get one, how many are held
              back and by which check, and how many full rows would be deleted.
  --apply     does the batches above. ``--max-rows N`` stops after N final rows
              have been looked at (a rehearsal). ``--vacuum-full`` (off) then
              rewrites the table to give the space back to the disk; it takes an
              exclusive lock for the length of the rewrite, so run it in a quiet
              window. A plain DELETE frees space for the table's own reuse but
              does not shrink the file.

Exit status 0 when every row looked at was handled, 1 when a row that should
have been deleted was not (changed under the run), 2 on bad arguments.

    python3 scripts/marker_final_rejections.py
    python3 scripts/marker_final_rejections.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_STATUSES = ("rejected", "retired")

#: Marker for every row in a batch of final-state ids. The key is the lowercased
#: triple the unique index uses; ``reached_at`` is the row's own review time.
INSERT_MARKERS_SQL = """
INSERT INTO proposed_edge_markers
    (source_key, target_key, relationship_type, final_status, reached_at, confidence)
SELECT lower(source_entity), lower(target_entity), relationship_type, status,
       coalesce(reviewed_at, produced_at, now()), coalesce(confidence, 0)
  FROM proposed_edges
 WHERE id = ANY($1::uuid[]) AND status = ANY($2::text[])
ON CONFLICT (source_key, target_key, relationship_type) DO NOTHING
"""

#: The delete that refuses to act without a marker: a row whose key has no
#: marker row is left alone whatever the caller passed. Held-back ids are
#: excluded here as well as by the caller, and the status is re-checked.
DELETE_MARKED_SQL = """
DELETE FROM proposed_edges pe
 WHERE pe.id = ANY($1::uuid[])
   AND pe.id <> ALL($2::uuid[])
   AND pe.status = ANY($3::text[])
   AND EXISTS (
       SELECT 1 FROM proposed_edge_markers m
        WHERE m.source_key = lower(pe.source_entity)
          AND m.target_key = lower(pe.target_entity)
          AND m.relationship_type = pe.relationship_type)
"""

NEXT_BATCH_SQL = """
SELECT id FROM proposed_edges
 WHERE status = ANY($1::text[]) AND id > $2::uuid
 ORDER BY id
 LIMIT $3
"""

_NIL = "00000000-0000-0000-0000-000000000000"


def _load_reference_checks():
    """The held-back check lives in the older candidates script; load it from
    next to this file rather than copying it, so the two cannot drift."""
    path = Path(__file__).resolve().parent / "drop_dead_relationship_candidates.py"
    spec = importlib.util.spec_from_file_location("_drop_dead_candidates", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["_drop_dead_candidates"] = mod
    spec.loader.exec_module(mod)
    return mod


async def write_markers(conn: Any, ids: list[Any], statuses: list[str]) -> int:
    """Insert the marker for each id in ``ids`` that is still final; returns the
    number of markers newly written."""
    res = await conn.execute(INSERT_MARKERS_SQL, ids, statuses)
    return int(res.split()[-1])


async def delete_marked(conn: Any, ids: list[Any], held: list[Any],
                        statuses: list[str]) -> int:
    """Delete the full rows in ``ids`` that have a marker, are not held back and
    are still in a final state; returns how many were deleted."""
    res = await conn.execute(DELETE_MARKED_SQL, ids, held, statuses)
    return int(res.split()[-1])


async def plan(conn: Any, statuses: list[str], held: dict[Any, list[str]]) -> dict[str, Any]:
    held_ids = list(held)
    row = await conn.fetchrow(
        """
        SELECT count(*)::bigint AS final_rows,
               count(*) FILTER (WHERE m.source_key IS NOT NULL)::bigint AS marked,
               count(*) FILTER (WHERE pe.id = ANY($2::uuid[]))::bigint AS held_rows,
               coalesce(sum(pg_column_size(pe.*)), 0)::bigint AS bytes
          FROM proposed_edges pe
          LEFT JOIN proposed_edge_markers m
            ON m.source_key = lower(pe.source_entity)
           AND m.target_key = lower(pe.target_entity)
           AND m.relationship_type = pe.relationship_type
         WHERE pe.status = ANY($1::text[])
        """, statuses, held_ids)
    by_status = [dict(r) for r in await conn.fetch(
        "SELECT status, count(*)::bigint AS rows FROM proposed_edges "
        "WHERE status = ANY($1::text[]) GROUP BY status ORDER BY status", statuses)]
    markers = int(await conn.fetchval("SELECT count(*) FROM proposed_edge_markers"))
    reasons: dict[str, int] = {}
    for checks in held.values():
        for c in checks:
            reasons[c] = reasons.get(c, 0) + 1
    final_rows = int(row["final_rows"])
    return {
        "statuses": statuses, "by_status": by_status, "final_rows": final_rows,
        "already_marked": int(row["marked"]),
        "to_mark": final_rows - int(row["marked"]),
        "held_back": int(row["held_rows"]), "held_by_check": reasons,
        "to_delete": final_rows - int(row["held_rows"]),
        "bytes": int(row["bytes"]), "markers_total": markers,
    }


def _print_plan(p: dict[str, Any]) -> None:
    print(f"final-state candidates ({', '.join(p['statuses'])}): {p['final_rows']} rows, "
          f"{p['bytes'] / 1048576:.0f} MB of row data")
    for r in p["by_status"]:
        print(f"  {r['status']}: {r['rows']} rows")
    print(f"markers in the table now: {p['markers_total']}")
    print(f"  of the final rows, already marked: {p['already_marked']}; to be marked: {p['to_mark']}")
    print(f"HELD BACK (named by something, full row kept): {p['held_back']} rows")
    for name, n in sorted(p["held_by_check"].items()):
        print(f"  {name}: {n}")
    print(f"full rows that would be deleted: {p['to_delete']}")


async def apply_batches(conn: Any, statuses: list[str], held_ids: list[Any], *,
                        batch: int, pause: float, max_rows: int | None) -> dict[str, int]:
    """Marker then delete, one transaction per batch, from the lowest id up.
    ``conn`` is a write connection."""
    held_set = {str(h) for h in held_ids}
    totals = {"looked_at": 0, "markers_written": 0, "deleted": 0,
              "held_kept": 0, "not_deleted": 0}
    cursor = _NIL
    n_batch = 0
    while True:
        want = batch if max_rows is None else min(batch, max_rows - totals["looked_at"])
        if want <= 0:
            break
        async with conn.transaction():
            await conn.execute("SET LOCAL lock_timeout = '5s'")
            rows = await conn.fetch(NEXT_BATCH_SQL, statuses, cursor, want)
            if not rows:
                break
            ids = [r["id"] for r in rows]
            cursor = str(ids[-1])
            marked = await write_markers(conn, ids, statuses)
            eligible = [i for i in ids if str(i) not in held_set]
            deleted = await delete_marked(conn, eligible, held_ids, statuses) if eligible else 0
        n_batch += 1
        totals["looked_at"] += len(ids)
        totals["markers_written"] += marked
        totals["deleted"] += deleted
        totals["held_kept"] += len(ids) - len(eligible)
        totals["not_deleted"] += len(eligible) - deleted
        print(f"  batch {n_batch}: looked at {len(ids)}, markers written {marked}, "
              f"deleted {deleted} (total deleted {totals['deleted']})")
        if pause:
            await asyncio.sleep(pause)
    return totals


async def run(args: argparse.Namespace) -> int:
    statuses = list(dict.fromkeys(args.statuses))
    bad = [s for s in statuses if s not in ("rejected", "retired")]
    if bad:
        raise SystemExit(f"--statuses may only name rejected or retired: {bad}")
    ref = _load_reference_checks()
    conn = await ref._connect(read_only=True, timeout_ms=900000)
    try:
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            held = await ref.held_back(conn, statuses, text_scan=not args.skip_text_scan)
            p = await plan(conn, statuses, held)
    finally:
        await conn.close()
    _print_plan(p)
    if not args.apply:
        print("DRY RUN: nothing written, nothing deleted (add --apply to do it)")
        return 0
    wconn = await ref._connect(read_only=False, timeout_ms=120000)
    try:
        totals = await apply_batches(wconn, statuses, list(held), batch=args.batch,
                                     pause=args.pause, max_rows=args.max_rows)
        left = int(await wconn.fetchval(
            "SELECT count(*) FROM proposed_edges pe WHERE pe.status = ANY($1::text[]) "
            "AND NOT EXISTS (SELECT 1 FROM proposed_edge_markers m "
            "WHERE m.source_key = lower(pe.source_entity) "
            "AND m.target_key = lower(pe.target_entity) "
            "AND m.relationship_type = pe.relationship_type)", statuses))
    finally:
        await wconn.close()
    print(f"looked at {totals['looked_at']} final rows: markers written "
          f"{totals['markers_written']}, full rows deleted {totals['deleted']}, "
          f"held back and kept {totals['held_kept']}, not deleted {totals['not_deleted']}")
    print(f"final-state rows still without a marker: {left}")
    if args.vacuum_full:
        vconn = await ref._connect(read_only=False, timeout_ms=0)
        try:
            t0 = time.time()
            await vconn.execute("VACUUM (FULL, ANALYZE) proposed_edges")
            print(f"VACUUM FULL done in {time.time() - t0:.0f}s")
        finally:
            await vconn.close()
    return 0 if totals["not_deleted"] == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the markers and delete the full rows (default: dry run)")
    ap.add_argument("--statuses", nargs="+", default=list(DEFAULT_STATUSES),
                    help="final states to take (default: rejected retired)")
    ap.add_argument("--skip-text-scan", action="store_true",
                    help="skip the slow check for ids named inside an output's text")
    ap.add_argument("--batch", type=int, default=5000)
    ap.add_argument("--pause", type=float, default=0.2,
                    help="seconds to wait between batches")
    ap.add_argument("--max-rows", type=int, default=None,
                    help="stop after looking at this many final rows")
    ap.add_argument("--vacuum-full", action="store_true",
                    help="after the run, rewrite the table to return space to the disk")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    if args.batch < 1 or (args.max_rows is not None and args.max_rows < 1):
        print("--batch and --max-rows must be at least 1", file=sys.stderr)
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
