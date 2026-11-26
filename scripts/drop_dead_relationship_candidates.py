#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dump, then delete, the relationship candidates that reached a final state.
DRY RUN by default.

The rejected or retired relationship candidates (``proposed_edges`` rows, about a million, over
a gigabyte) may be permanently deleted, each batch dumped to a file first.
Never anything that was fetched, anything a published sentence rested on, or
any published judgement.

WHAT A CANDIDATE IS. One co-mention of two entities, stored with the snippet
that showed it (``evidence_text``) and the signals it came from
(``derived_from``). A final state is ``rejected`` or ``retired`` (``--statuses``
adds ``orphaned``). Nothing in the platform reads a final-state row except the
unique key ``uq_proposed_edges_triple``: while the row exists the same
(source, target, relationship) cannot be proposed again.

THE REFERENCE CHECK (what holds a row back). A final-state row is HELD BACK, and
never dumped or deleted, when its id appears in any of:

  * ``nexuses.data->>'promoted_from_proposed_edge'`` (it became a nexus);
  * ``derived_from`` of ``analyst_outputs`` (every finding, published or
    superseded), ``journal_entries``, ``hypotheses``, ``facts``, ``nexuses``,
    ``entity_edges``; ``cited_substrate_refs`` of ``journal_entries``;
  * ``output_consumption`` as consumer or consumed;
  * ``input_row_refs`` / ``output_row_refs`` of ``analyst_traces``;
  * the text of any analyst output (its ``title``, ``body`` or ``data``) -
    the only way a sentence could name a candidate; ``--skip-text-scan`` omits
    this one slow scan (about a minute on the reference database).

The SQL is in ``REFERENCE_QUERIES`` below; each one is a join on the candidate
id, run in a READ ONLY transaction. Reading a finding only ever names ids in
those places, so a row that none of them names is not something a sentence
rested on.

Modes:

  (no flag)       DRY RUN. Per status: rows, bytes, how many are safe and how
                  many are held back and by which check. Writes nothing.
  --dump DIR      writes exactly the rows that would be deleted to
                  ``DIR/proposed_edges_final_state.csv.gz`` (one COPY, gzip),
                  reads it back, and checks the row count and an md5 over the
                  sorted ids against the database. Nothing is deleted.
  --dump DIR --apply
                  the same dump-and-verify, then, in the same run, deletes
                  exactly the dumped ids in bounded batches (``--batch``, default
                  5000), one transaction per batch, lock_timeout 5 s, pausing
                  ``--pause`` seconds between batches. ``--apply`` without
                  ``--dump`` is refused. ``--max-rows N`` caps the delete for a
                  rehearsal. ``--vacuum-full`` (off) then rewrites the table to
                  hand the space back to the disk: it takes an EXCLUSIVE lock
                  for the length of the rewrite, so run it in a quiet window.

A row is deleted only when its pair has a row in ``proposed_edge_markers``
(the trigger writes one for every new final state;
``scripts/marker_final_rejections.py`` writes them for the older rows and is the
script to run to remove them). A row without a marker is left in place.

DELETE frees space for the table's own reuse but does not shrink the file; only
VACUUM FULL (or pg_repack) returns it to the disk.

Restore the dump: ``gunzip -c FILE | psql -U legba -d legba -c "\\copy
proposed_edges FROM STDIN WITH (FORMAT csv, HEADER true)"``.

    python3 scripts/drop_dead_relationship_candidates.py
    python3 scripts/drop_dead_relationship_candidates.py --dump /path/to/dir
    python3 scripts/drop_dead_relationship_candidates.py --dump /path/to/dir --apply
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import hashlib
import os
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_STATUSES = ("rejected", "retired")
DUMP_NAME = "proposed_edges_final_state.csv.gz"

#: check name -> SQL returning the candidate ids that check names. ``$1`` is the
#: statuses array: the join is restricted to final-state candidates so the
#: answer is the held-back set itself. All are plain reads.
REFERENCE_QUERIES: dict[str, str] = {
    "nexus_promoted_from": """
        SELECT DISTINCT pe.id FROM nexuses n
          JOIN proposed_edges pe ON pe.id::text = n.data->>'promoted_from_proposed_edge'
         WHERE pe.status = ANY($1::text[])""",
    "analyst_outputs_derived_from": """
        SELECT DISTINCT pe.id FROM analyst_outputs x
          CROSS JOIN LATERAL unnest(x.derived_from) d(id)
          JOIN proposed_edges pe ON pe.id = d.id
         WHERE pe.status = ANY($1::text[])""",
    "journal_entries_derived_from": """
        SELECT DISTINCT pe.id FROM journal_entries x
          CROSS JOIN LATERAL unnest(x.derived_from) d(id)
          JOIN proposed_edges pe ON pe.id = d.id
         WHERE pe.status = ANY($1::text[])""",
    "journal_entries_cited_substrate_refs": """
        SELECT DISTINCT pe.id FROM journal_entries x
          CROSS JOIN LATERAL unnest(x.cited_substrate_refs) d(id)
          JOIN proposed_edges pe ON pe.id = d.id
         WHERE pe.status = ANY($1::text[])""",
    "hypotheses_derived_from": """
        SELECT DISTINCT pe.id FROM hypotheses x
          CROSS JOIN LATERAL unnest(x.derived_from) d(id)
          JOIN proposed_edges pe ON pe.id = d.id
         WHERE pe.status = ANY($1::text[])""",
    "facts_derived_from": """
        SELECT DISTINCT pe.id FROM facts x
          CROSS JOIN LATERAL unnest(x.derived_from) d(id)
          JOIN proposed_edges pe ON pe.id = d.id
         WHERE pe.status = ANY($1::text[])""",
    "nexuses_derived_from": """
        SELECT DISTINCT pe.id FROM nexuses x
          CROSS JOIN LATERAL unnest(x.derived_from) d(id)
          JOIN proposed_edges pe ON pe.id = d.id
         WHERE pe.status = ANY($1::text[])""",
    "entity_edges_derived_from": """
        SELECT DISTINCT pe.id FROM entity_edges x
          CROSS JOIN LATERAL unnest(x.derived_from) d(id)
          JOIN proposed_edges pe ON pe.id = d.id
         WHERE pe.status = ANY($1::text[])""",
    "output_consumption_consumed": """
        SELECT DISTINCT pe.id FROM output_consumption x
          JOIN proposed_edges pe ON pe.id = x.consumed_id
         WHERE pe.status = ANY($1::text[])""",
    "output_consumption_consumer": """
        SELECT DISTINCT pe.id FROM output_consumption x
          JOIN proposed_edges pe ON pe.id = x.consumer_id
         WHERE pe.status = ANY($1::text[])""",
    "analyst_traces_input_row_refs": """
        SELECT DISTINCT pe.id FROM analyst_traces x
          CROSS JOIN LATERAL unnest(x.input_row_refs) d(id)
          JOIN proposed_edges pe ON pe.id = d.id
         WHERE pe.status = ANY($1::text[])""",
    "analyst_traces_output_row_refs": """
        SELECT DISTINCT pe.id FROM analyst_traces x
          CROSS JOIN LATERAL unnest(x.output_row_refs) d(id)
          JOIN proposed_edges pe ON pe.id = d.id
         WHERE pe.status = ANY($1::text[])""",
}

#: The one slow check: an id named inside an analyst output's own text.
TEXT_SCAN_NAME = "analyst_outputs_text_names_id"
TEXT_SCAN_SQL = """
    SELECT DISTINCT pe.id FROM analyst_outputs x
      CROSS JOIN LATERAL regexp_matches(
            coalesce(x.title,'') || ' ' || coalesce(x.body,'') || ' ' || coalesce(x.data::text,''),
            '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', 'g') m(u)
      JOIN proposed_edges pe ON pe.id::text = m.u[1]
     WHERE pe.status = ANY($1::text[])"""

COLUMNS = (
    "id, source_entity, target_entity, relationship_type, confidence, evidence_text,"
    " source_cycle, status, reviewed_at, target_id, target_version, analyst_id,"
    " analyst_version, produced_at, derived_from, schema_uri, run_id, created_at"
)


def _pg_env() -> dict[str, str]:
    return {
        "host": os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        "port": os.environ.get("LEGBA_DATA_PG_PORT", "5432"),
        "user": os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        "password": os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        "database": os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    }


async def _connect(*, read_only: bool, timeout_ms: int):
    import asyncpg

    e = _pg_env()
    settings = {"statement_timeout": str(timeout_ms),
                "application_name": "legba-drop-dead-candidates"}
    if read_only:
        settings["default_transaction_read_only"] = "on"
    return await asyncpg.connect(
        host=e["host"], port=int(e["port"]), user=e["user"],
        password=e["password"], database=e["database"], server_settings=settings,
    )


def _where(min_age_days: int) -> str:
    """The candidate predicate (statuses are $1, the held-back ids are $2)."""
    age = (f" AND coalesce(reviewed_at, produced_at) < now() - interval '{int(min_age_days)} days'"
           if min_age_days else "")
    return f"status = ANY($1::text[]) AND id <> ALL($2::uuid[]){age}"


async def held_back(conn: Any, statuses: list[str], *, text_scan: bool = True
                    ) -> dict[Any, list[str]]:
    """``{candidate id: [check names that name it]}`` for every held-back row.
    Runs inside the caller's (read only, repeatable-read) transaction."""
    held: dict[Any, list[str]] = {}
    queries = dict(REFERENCE_QUERIES)
    if text_scan:
        queries[TEXT_SCAN_NAME] = TEXT_SCAN_SQL
    for name, sql in queries.items():
        for r in await conn.fetch(sql, statuses):
            held.setdefault(r["id"], []).append(name)
    return held


async def plan(conn: Any, statuses: list[str], held: dict[Any, list[str]],
               min_age_days: int) -> dict[str, Any]:
    ids = list(held)
    by_status = [
        dict(r) for r in await conn.fetch(
            "SELECT status, count(*)::bigint AS rows, "
            "coalesce(sum(pg_column_size(t.*)),0)::bigint AS bytes "
            "FROM proposed_edges t WHERE status = ANY($1::text[]) "
            "GROUP BY status ORDER BY status", statuses)
    ]
    safe = dict(await conn.fetchrow(
        "SELECT count(*)::bigint AS rows, "
        "coalesce(sum(pg_column_size(t.*)),0)::bigint AS bytes, "
        "coalesce(md5(string_agg(id::text, ',' ORDER BY id)), '') AS md5 "
        f"FROM proposed_edges t WHERE {_where(min_age_days)}", statuses, ids))
    total = int(await conn.fetchval(
        "SELECT count(*) FROM proposed_edges WHERE status = ANY($1::text[])",
        statuses))
    reasons: dict[str, int] = {}
    for checks in held.values():
        for c in checks:
            reasons[c] = reasons.get(c, 0) + 1
    return {"statuses": statuses, "final_state_rows": total, "by_status": by_status,
            "safe_rows": safe["rows"], "safe_bytes": safe["bytes"],
            "safe_md5": safe["md5"], "held_back_rows": len(held),
            "held_back_by_check": reasons,
            "held_for_age": total - safe["rows"] - len(held)}


def _print_plan(p: dict[str, Any]) -> None:
    print(f"final-state candidates ({', '.join(p['statuses'])}): {p['final_state_rows']} rows")
    for r in p["by_status"]:
        print(f"  {r['status']}: {r['rows']} rows, {r['bytes'] / 1048576:.0f} MB of row data")
    print(f"SAFE to delete: {p['safe_rows']} rows, {p['safe_bytes'] / 1048576:.0f} MB of row data")
    print(f"HELD BACK (named by something): {p['held_back_rows']} rows")
    for name, n in sorted(p["held_back_by_check"].items()):
        print(f"  {name}: {n}")
    if p["held_for_age"]:
        print(f"HELD BACK (younger than --min-age-days): {p['held_for_age']} rows")


async def dump_safe(conn: Any, p: dict[str, Any], held: dict[Any, list[str]],
                    out_dir: Path, min_age_days: int) -> tuple[Path, list[Any]]:
    """COPY exactly the safe rows to a gzip CSV and verify it reads back.
    Same snapshot as the plan (the caller's repeatable-read transaction), so
    the file holds precisely the rows the plan counted."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / DUMP_NAME
    ids = list(held)
    with gzip.open(path, "wb", compresslevel=6) as f:
        await conn.copy_from_query(
            f"SELECT {COLUMNS} FROM proposed_edges WHERE {_where(min_age_days)} ORDER BY id",
            p["statuses"], ids, output=f, format="csv", header=True)
    # Read it back: row count and md5 of the sorted ids must equal the plan.
    got: list[str] = []
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            got.append(row["id"])
    got.sort()
    md5 = hashlib.md5(",".join(got).encode()).hexdigest() if got else ""
    if len(got) != p["safe_rows"] or md5 != p["safe_md5"]:
        raise SystemExit(
            f"dump verification FAILED: file has {len(got)} rows (md5 {md5}), "
            f"plan said {p['safe_rows']} (md5 {p['safe_md5']}); nothing deleted")
    print(f"dump: {path} ({path.stat().st_size / 1048576:.1f} MB gz), "
          f"{len(got)} rows read back, ids md5 matches the database")
    return path, got


async def delete_batches(statuses: list[str], ids: list[str], *, batch: int,
                         pause: float, max_rows: int | None) -> int:
    """Delete exactly ``ids`` (re-checking status) in bounded batches."""
    conn = await _connect(read_only=False, timeout_ms=120000)
    done = 0
    try:
        todo = ids if max_rows is None else ids[:max_rows]
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            async with conn.transaction():
                await conn.execute("SET LOCAL lock_timeout = '5s'")
                # Only a row whose pair has a marker may go: the marker is what
                # keeps the pair from being proposed again.
                res = await conn.execute(
                    "DELETE FROM proposed_edges pe WHERE pe.id = ANY($1::uuid[]) "
                    "AND pe.status = ANY($2::text[]) "
                    "AND EXISTS (SELECT 1 FROM proposed_edge_markers m "
                    "WHERE m.source_key = lower(pe.source_entity) "
                    "AND m.target_key = lower(pe.target_entity) "
                    "AND m.relationship_type = pe.relationship_type)",
                    chunk, statuses)
            n = int(res.split()[-1])
            done += n
            print(f"  batch {i // batch + 1}: deleted {n} (total {done}/{len(todo)})")
            if pause:
                await asyncio.sleep(pause)
    finally:
        await conn.close()
    return done


async def vacuum_full() -> None:
    conn = await _connect(read_only=False, timeout_ms=0)
    try:
        t0 = time.time()
        await conn.execute("VACUUM (FULL, ANALYZE) proposed_edges")
        print(f"VACUUM FULL done in {time.time() - t0:.0f}s")
    finally:
        await conn.close()


async def run(args: argparse.Namespace) -> int:
    statuses = list(dict.fromkeys(args.statuses))
    bad = [s for s in statuses if s not in ("rejected", "retired", "orphaned")]
    if bad:
        raise SystemExit(f"--statuses may only name rejected, retired, orphaned: {bad}")
    conn = await _connect(read_only=True, timeout_ms=900000)
    try:
        # ONE snapshot for the reference check, the plan and the dump.
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            held = await held_back(conn, statuses, text_scan=not args.skip_text_scan)
            p = await plan(conn, statuses, held, args.min_age_days)
            _print_plan(p)
            if args.dump is None:
                print("DRY RUN: nothing dumped, nothing deleted "
                      "(--dump DIR dumps and verifies; --dump DIR --apply also deletes)")
                return 0
            _path, ids = await dump_safe(conn, p, held, Path(args.dump),
                                         args.min_age_days)
    finally:
        await conn.close()
    if not args.apply:
        print("nothing deleted (add --apply to delete the dumped rows)")
        return 0
    done = await delete_batches(statuses, ids, batch=args.batch, pause=args.pause,
                                max_rows=args.max_rows)
    print(f"deleted {done} of {len(ids)} dumped rows")
    if args.vacuum_full:
        await vacuum_full()
    return 0 if done == (len(ids) if args.max_rows is None else min(len(ids), args.max_rows)) else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--dump", metavar="DIR", default=None)
    ap.add_argument("--apply", action="store_true",
                    help="after the dump in THIS run, delete exactly the dumped ids")
    ap.add_argument("--statuses", nargs="+", default=list(DEFAULT_STATUSES),
                    help="final states to take (default: rejected retired)")
    ap.add_argument("--min-age-days", type=int, default=0,
                    help="only rows whose last review (or creation) is older than this")
    ap.add_argument("--skip-text-scan", action="store_true",
                    help="skip the slow check for ids named in an output's text")
    ap.add_argument("--batch", type=int, default=5000)
    ap.add_argument("--pause", type=float, default=0.2)
    ap.add_argument("--max-rows", type=int, default=None)
    ap.add_argument("--vacuum-full", action="store_true")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    if args.apply and args.dump is None:
        print("--apply needs --dump DIR (a delete never rests on an earlier "
              "run's dump)", file=sys.stderr)
        return 2
    if args.batch < 1:
        print("--batch must be at least 1", file=sys.stderr)
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
