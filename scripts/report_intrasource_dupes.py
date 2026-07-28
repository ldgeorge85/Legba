#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Report intra-source exact content-hash duplicate signals (S-4), READ-ONLY.

Quantifies the redundancy the S-4 ingest-time collapse
(:func:`legba.runtime.source_actor.write_canonical_signal`) prevents GOING
FORWARD, so an operator can decide whether to run a one-off HISTORICAL cleanup of
the rows that landed before the fix. This script NEVER writes or deletes — it
only SELECTs and prints.

A "dupe set" is a group of >=1 REDUNDANT rows sharing the SAME
(source_id, content_hash) within one source (content_hash <> '' — the empty
default is skipped). The excess rows (group size minus 1) are the collapsible
redundancy: keeping one row per (source_id, content_hash) preserves the content
and its recency (the newest fetched_at); the rest are re-emissions of identical
content stored as separate rows.

Run in the registry container (runtime deps present) against the live Postgres:

  docker exec -e LEGBA_DATA_PG_DB=legba \\
      -e LEGBA_DATA_PG_HOST=legba-postgres-1 \\
      -e PYTHONPATH=/app/src \\
      <registry-container> python3 /app/scripts/report_intrasource_dupes.py

Or on the host with the repo mounted (PYTHONPATH=src, loopback host):

  set -a; . ./.env; set +a
  PYTHONPATH=src LEGBA_DATA_PG_HOST=127.0.0.1 LEGBA_DATA_PG_DB=legba \\
      python3 scripts/report_intrasource_dupes.py --window-hours 168

Flags:
  --window-hours N   only count rows with fetched_at within the last N hours
                     (default: all time). Match this to the ingest dedup window
                     to preview what the live fix now collapses.
  --top N            show only the N worst sources (default: 40; 0 = all)
  --tenant T         restrict to one owner_tenant (default: all tenants)
  --json             emit a machine-readable JSON summary instead of a table
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os

import asyncpg


async def _connect_pg() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )


# Per-source rollup. The inner grouping counts rows per (source_id,
# content_hash); the outer sums, per source, the total rows, the number of
# duplicated groups, and the EXCESS rows (group_size - 1 summed) — the count the
# S-4 collapse removes. content_hash <> '' excludes the "no hash" rows.
_PER_SOURCE_SQL = """
WITH grp AS (
    SELECT source_id, content_hash, count(*) AS n
    FROM signals
    WHERE content_hash <> ''
      {window}
      {tenant}
    GROUP BY source_id, content_hash
)
SELECT source_id,
       sum(n)                            AS total_rows,
       count(*) FILTER (WHERE n > 1)     AS dup_groups,
       coalesce(sum(n - 1) FILTER (WHERE n > 1), 0) AS excess_rows,
       max(n)                            AS worst_group
FROM grp
GROUP BY source_id
HAVING coalesce(sum(n - 1) FILTER (WHERE n > 1), 0) > 0
ORDER BY excess_rows DESC
"""

# Overall totals across all sources (single-row summary).
_TOTALS_SQL = """
WITH grp AS (
    SELECT source_id, content_hash, count(*) AS n
    FROM signals
    WHERE content_hash <> ''
      {window}
      {tenant}
    GROUP BY source_id, content_hash
)
SELECT coalesce(sum(n), 0)                              AS total_hashed_rows,
       coalesce(sum(n - 1) FILTER (WHERE n > 1), 0)     AS excess_rows,
       count(*) FILTER (WHERE n > 1)                    AS dup_groups
FROM grp
"""


def _clauses(window_hours: int | None, tenant: str | None) -> tuple[str, str, list]:
    params: list = []
    window = ""
    tclause = ""
    if window_hours is not None:
        window = f"AND fetched_at > NOW() - INTERVAL '{int(window_hours)} hours'"
    if tenant is not None:
        params.append(tenant)
        tclause = f"AND owner_tenant = ${len(params)}"
    return window, tclause, params


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Report intra-source exact content-hash duplicate signals "
        "(read-only).",
    )
    ap.add_argument("--window-hours", type=int, default=None)
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--tenant", type=str, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    window, tclause, params = _clauses(args.window_hours, args.tenant)
    per_source_sql = _PER_SOURCE_SQL.format(window=window, tenant=tclause)
    totals_sql = _TOTALS_SQL.format(window=window, tenant=tclause)

    conn = await _connect_pg()
    try:
        totals = await conn.fetchrow(totals_sql, *params)
        rows = await conn.fetch(per_source_sql, *params)
    finally:
        await conn.close()

    total_hashed = int(totals["total_hashed_rows"])
    excess = int(totals["excess_rows"])
    dup_groups = int(totals["dup_groups"])
    pct = (100.0 * excess / total_hashed) if total_hashed else 0.0

    per_source = [
        {
            "source_id": r["source_id"],
            "total_rows": int(r["total_rows"]),
            "dup_groups": int(r["dup_groups"]),
            "excess_rows": int(r["excess_rows"]),
            "worst_group": int(r["worst_group"]),
        }
        for r in rows
    ]
    shown = per_source if args.top in (0, None) else per_source[: args.top]

    if args.json:
        print(json.dumps({
            "window_hours": args.window_hours,
            "tenant": args.tenant,
            "total_hashed_rows": total_hashed,
            "excess_rows": excess,
            "dup_groups": dup_groups,
            "excess_pct_of_hashed_rows": round(pct, 2),
            "sources_with_dupes": len(per_source),
            "per_source": shown,
        }, indent=2, default=str))
        return 0

    scope = []
    if args.window_hours is not None:
        scope.append(f"last {args.window_hours}h")
    if args.tenant is not None:
        scope.append(f"tenant={args.tenant}")
    scope_s = f" ({', '.join(scope)})" if scope else " (all time, all tenants)"

    print("=" * 78)
    print(f" Intra-source exact-hash duplicate signals{scope_s}")
    print("=" * 78)
    print(f" hashed rows scanned : {total_hashed:>10,}")
    print(f" duplicated groups   : {dup_groups:>10,}  (>=1 redundant row each)")
    print(f" EXCESS (collapsible): {excess:>10,}  ({pct:.1f}% of hashed rows)")
    print(f" sources w/ dupes    : {len(per_source):>10,}")
    print("-" * 78)
    if not shown:
        print(" No intra-source exact-hash duplicates found.")
        return 0
    print(f" {'source_id':<44} {'excess':>8} {'groups':>7} {'worst':>6}")
    print("-" * 78)
    for r in shown:
        print(
            f" {r['source_id'][:44]:<44} {r['excess_rows']:>8,} "
            f"{r['dup_groups']:>7,} {r['worst_group']:>6,}"
        )
    if args.top and len(per_source) > len(shown):
        print("-" * 78)
        print(f" … {len(per_source) - len(shown)} more sources (raise --top to see)")
    print("=" * 78)
    print(" READ-ONLY report — no rows changed. Historical cleanup (collapsing")
    print(" the excess rows) is a separate operator-run data migration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
