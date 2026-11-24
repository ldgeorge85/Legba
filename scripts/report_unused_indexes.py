#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Indexes nobody reads — READ-ONLY, nightly-runnable (lane P-8, 03c §5 #4).

Reads ``pg_stat_user_indexes`` for the application schema inside a read-only
transaction (60 s statement timeout) and classifies every index with zero
scans since the statistics window opened:

  * ``drop-candidate`` — zero scans, not unique, backs no constraint, not on a
    table rebuilt by swap, observed for at least ``--min-days`` (the window
    is the later of the statistics start and the applied_at of the migration
    that created the index), and its name appears nowhere in code outside
    the migrations (a name in code is a planner hint, an ON CONFLICT target
    or a script that rebuilds it — a reason to keep).
  * ``keep`` — with the reason (unique / constraint / swap table / too new /
    named in code / not created by a migration).

Prints names, tables, sizes and reasons only. ``--json`` for machines;
``--definitions`` adds each index's ``pg_get_indexdef`` (what the reverse
script recreates).

    python3 scripts/report_unused_indexes.py [--min-days 30] [--json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from legba.data import schema_hygiene as sh  # noqa: E402
from legba.data.migrations import MIGRATIONS_DIR  # noqa: E402

#: Tables rebuilt and swapped in by code: their index counters restart with
#: every swap, so "zero scans" is one rebuild's evidence and never a verdict.
SWAP_BUILT_TABLES = frozenset({"graph_arcs"})

#: Tables whose future is the owner's (lane P-8 brief, item 5: facts, the
#: forecast generations, the alert and byte stores wait on a merged
#: architecture recommendation) — a hygiene sweep leaves their indexes alone.
OWNER_HELD_TABLES = frozenset({
    "facts", "acute_forecasts", "forecast_ledger", "forecast_numbers",
    "forecast_resolutions", "forecast_resolution_proposals",
    "forecast_resolution_records", "scorecard_entries", "override_log",
    "alert_log", "alerts", "evidence_archive", "evidence_archive_cold",
    "evidence_archive_bundles", "fetch_versions", "fetch_version_records",
})

INDEX_SQL = """
SELECT s.indexrelname AS name, s.relname AS table_name, s.idx_scan,
       pg_relation_size(s.indexrelid) AS bytes,
       x.indisunique AS is_unique,
       EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = s.indexrelid)
           AS backs_constraint,
       pg_get_indexdef(s.indexrelid) AS definition,
       t.n_live_tup AS table_rows
  FROM pg_stat_user_indexes s
  JOIN pg_index x ON x.indexrelid = s.indexrelid
  JOIN pg_stat_user_tables t ON t.relid = s.relid
 WHERE s.schemaname = $1
 ORDER BY pg_relation_size(s.indexrelid) DESC, s.indexrelname
"""

WINDOW_SQL = """
SELECT coalesce(d.stats_reset, pg_postmaster_start_time()) AS since
  FROM pg_stat_database d WHERE d.datname = current_database()
"""


@dataclass
class IndexVerdict:
    name: str
    table: str
    bytes: int
    idx_scan: int
    verdict: str          # drop-candidate | keep
    reason: str
    migration: str | None
    observed_days: float | None
    definition: str


def code_references(names: list[str]) -> dict[str, list[str]]:
    """``{index name: [files outside migrations that mention it]}`` — one
    ``git grep`` over src/ scripts/ deploy/ docker/."""
    if not names:
        return {}
    try:
        out = subprocess.run(
            ["git", "grep", "-l", "-w", "-F", *sum((["-e", n] for n in names), []),
             "--", "src", "scripts", "deploy", "docker"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=False,
        ).stdout.split()
    except OSError:
        return {}
    # deploy/baseline/ is a copy of the flattened 0001 migration, not code.
    files = [f for f in out if "/migrations/" not in f
             and not f.startswith("deploy/baseline/")
             and not f.endswith(("report_unused_indexes.py", "drop_unused_indexes.py"))]
    refs: dict[str, list[str]] = {n: [] for n in names}
    for f in files:
        try:
            text = (REPO_ROOT / f).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for n in names:
            if n in text:
                refs[n].append(f)
    return {n: fs for n, fs in refs.items() if fs}


def test_pins(names: list[str]) -> dict[str, list[str]]:
    """Index names a test checks in the CATALOG (a drop would fail it). A
    test that only greps the migration's text survives a later drop."""
    if not names:
        return {}
    out = subprocess.run(
        ["git", "grep", "-l", "-w", "-F", *sum((["-e", n] for n in names), []),
         "--", "tests"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    ).stdout.split()
    pins: dict[str, list[str]] = {}
    for f in out:
        if f.endswith("test_schema_hygiene.py"):
            continue
        text = (REPO_ROOT / f).read_text(encoding="utf-8", errors="ignore")
        for n in names:
            if n in text and f"CREATE INDEX IF NOT EXISTS {n}" not in text:
                pins.setdefault(n, []).append(f)
    return pins


def classify(
    rows: list[dict[str, Any]],
    *,
    declared: dict[str, str],
    applied_at: dict[str, datetime],
    stats_since: datetime,
    now: datetime,
    min_days: float,
    refs: dict[str, list[str]],
    pins: dict[str, list[str]] | None = None,
) -> list[IndexVerdict]:
    pins = pins or {}
    out: list[IndexVerdict] = []
    for r in rows:
        if int(r["idx_scan"] or 0) != 0:
            continue
        name, table = r["name"], r["table_name"]
        mig = declared.get(name)
        start = stats_since
        if mig and applied_at.get(mig) and applied_at[mig] > start:
            start = applied_at[mig]
        days = (now - start).total_seconds() / 86400.0
        if r["is_unique"]:
            v, why = "keep", "unique (enforces a rule; scans are not its job)"
        elif r["backs_constraint"]:
            v, why = "keep", "backs a constraint"
        elif table in SWAP_BUILT_TABLES:
            v, why = "keep", "table rebuilt by swap every hour; counters cannot judge it"
        elif table in OWNER_HELD_TABLES:
            v, why = "keep", "owner-held table (facts / forecasts / alert or byte stores)"
        elif mig is None:
            v, why = "keep", "not created by a migration (see report_schema_drift.py)"
        elif days < min_days:
            v, why = "keep", f"observed only {days:.0f} d (< {min_days:.0f} d)"
        elif refs.get(name):
            v, why = "keep", "named in code: " + ", ".join(refs[name][:3])
        elif pins.get(name):
            v, why = "keep", "its existence is pinned by " + ", ".join(pins[name][:2])
        elif int(r.get("table_rows") or 0) == 0:
            v, why = "keep", ("table is empty: the index costs one page and the "
                              "table's fate is the owner's (inventory §3)")
        else:
            v, why = "drop-candidate", f"0 scans in {days:.0f} d"
        out.append(IndexVerdict(
            name=name, table=table, bytes=int(r["bytes"] or 0),
            idx_scan=0, verdict=v, reason=why, migration=mig,
            observed_days=round(days, 1), definition=r["definition"],
        ))
    return out


async def read_live(conn: Any) -> tuple[list[dict[str, Any]], dict[str, datetime], datetime]:
    async with conn.transaction(readonly=True):
        rows = [dict(r) for r in await conn.fetch(INDEX_SQL, sh.APP_SCHEMA)]
        applied = {
            r["name"]: r["applied_at"]
            for r in await conn.fetch(
                "SELECT name, applied_at FROM legba_data_migrations")
        }
        since = await conn.fetchval(WINDOW_SQL)
    return rows, applied, since


def _mb(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB" if n >= 1_048_576 else f"{n / 1024:.0f} KB"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--min-days", type=float, default=30.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--definitions", action="store_true")
    args = ap.parse_args()
    from report_schema_drift import read_only_connect  # same dir

    declared = sh.declared_objects(sh.migration_texts(MIGRATIONS_DIR))
    conn = await read_only_connect()
    try:
        rows, applied, since = await read_live(conn)
    finally:
        await conn.close()
    zero = [r["name"] for r in rows if int(r["idx_scan"] or 0) == 0]
    verdicts = classify(
        rows, declared=declared, applied_at=applied, stats_since=since,
        now=datetime.now(timezone.utc), min_days=args.min_days,
        refs=code_references(zero), pins=test_pins(zero),
    )
    if args.json:
        print(json.dumps({
            "stats_since": since.isoformat(),
            "indexes": [asdict(v) for v in verdicts],
        }, indent=2, default=str))
        return 0
    drop = [v for v in verdicts if v.verdict == "drop-candidate"]
    keep = [v for v in verdicts if v.verdict == "keep"]
    print(f"statistics since {since:%Y-%m-%d %H:%MZ}; {len(rows)} indexes, "
          f"{len(verdicts)} never scanned")
    print(f"DROP CANDIDATES: {len(drop)} ({_mb(sum(v.bytes for v in drop))})")
    for v in drop:
        print(f"  {v.name} on {v.table}  {_mb(v.bytes)}  [{v.migration}] {v.reason}")
        if args.definitions:
            print(f"      {v.definition}")
    print(f"KEPT (never scanned, with reason): {len(keep)} ({_mb(sum(v.bytes for v in keep))})")
    for v in keep:
        print(f"  {v.name} on {v.table}  {_mb(v.bytes)}  {v.reason}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    sys.exit(asyncio.run(main()))
