#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dump the nine hand-made repair backup tables, prove the dumps restore, and
(only in the same run, when told) drop the tables. DRY RUN by default.

Permanent deletion is allowed for these nine tables, each dumped to a file
first. Never anything that
was fetched, anything a published sentence rested on, or any published
judgement - these tables are the pre-images of one-off live repairs, nothing
more. No migration creates them (``scripts/report_schema_drift.py`` lists
them), and nothing in ``src/`` reads them (only the once-scripts that made
them write them).

Modes:

  (no flag)        DRY RUN. Per table: present?, rows, bytes, what it backs up.
                   Touches nothing, writes nothing.
  --dump DIR       per table: row count + md5 over every row (READ ONLY
                   transaction), ``pg_dump --format=custom --compress=9`` to
                   ``DIR/<table>.dump``, then every dump is restored into a
                   FRESH scratch database and re-counted and re-checksummed
                   there (the scratch database is dropped whatever happens);
                   ``DIR/MANIFEST.json`` records it. Nothing is dropped.
  --dump DIR --apply
                   the same dump-and-verify, THEN, in the same run, ``DROP
                   TABLE`` for each table whose dump verified AND whose live
                   rows are still exactly what was dumped (one transaction per
                   table, lock_timeout 10 s). A table that fails any check is
                   KEPT and named. ``--apply`` without ``--dump`` is refused:
                   a drop never rests on a dump from an earlier run.
  --exclude T ...  leave named tables out of every step (the lane's earlier
                   recommendation was to hold signal_alias_backup_n_u,
                   signals_geo_backup_n_o and
                   actor_filter_state_isw_cursor_bak_20261106 until the
                   11-13 repairs have held a week and the ISW check is closed).

    python3 scripts/archive_backup_tables.py
    python3 scripts/archive_backup_tables.py --dump /path/to/dir
    python3 scripts/archive_backup_tables.py --dump /path/to/dir --apply \\
        --exclude signal_alias_backup_n_u signals_geo_backup_n_o \\
                  actor_filter_state_isw_cursor_bak_20261106

Restore one table later: ``pg_restore -U legba -d legba DIR/<table>.dump``.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: table -> what it backs up (the repair, its record, whether it was confirmed).
BACKUP_TABLES: dict[str, str] = {
    "signals_oversize_summary_backup_20261105":
        "11-05 data fix: 3 oversize signal summaries (11 MB, 11 MB, 1.5 MB) "
        "replaced by their distilled_body; verified the same day (full-text "
        "search over signals runs again)",
    "signals_corpus_gap_bak_20261106":
        "indexed_at cleared on 652 signals the corpus indexer had "
        "falsely stamped during the 11-05/06 flood stage; verified 652/652 "
        "re-indexed (PG 992 = OS 992)",
    "signals_salience_relift_bak_20261107":
        "institutional_change salience re-lift on 129 rows (backup "
        "of 11,696 id+salience); confirmed at the 11-07 19:07Z tick",
    "signal_alias_backup_n_u":
        "un-alias of 1,505 lefaso.net rows folded into one "
        "by the old URL canon (scripts/unalias_valueless_id_urls_once.py)",
    "signals_geo_backup_n_o":
        "7-day geo re-tag, 953 rows changed; "
        "sampled review 27 better / 2 neutral / 1 worse "
        "(scripts/regeocode_recent_once.py)",
    "actor_filter_state_cursor_bak_20261023":
        "10-23 ingestion recovery: 54 source cursors rewound (371-row pre-image)",
    "actor_filter_state_isw_cursor_bak_20261106":
        "ISW head cursor rewound to 10-30 (open check after the next "
        "polls was recorded, not closed in the tracker)",
    "actor_filter_state_cursor_bak_f151_20261105":
        "State travel advisories + ICG cursor rewinds (2-row pre-image)",
    "source_descriptors_bak_roll64_20261107":
        "hand source walk of gdelt.files + isw "
        "(poll_budget_seconds); new heads active 11-07 20:33Z",
}

CHECKSUM_SQL = (
    "SELECT count(*)::bigint AS n, "
    "coalesce(md5(string_agg(t::text, E'\\n' ORDER BY t::text)), '') AS md5 "
    "FROM public.{table} t"
)


def _pg_env() -> dict[str, str]:
    return {
        "host": os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        "port": os.environ.get("LEGBA_DATA_PG_PORT", "5432"),
        "user": os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        "password": os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        "database": os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    }


async def _connect(read_only: bool):
    import asyncpg

    e = _pg_env()
    settings = {"statement_timeout": "60000",
                "application_name": "legba-archive-backup-tables"}
    if read_only:
        settings["default_transaction_read_only"] = "on"
    return await asyncpg.connect(
        host=e["host"], port=int(e["port"]), user=e["user"],
        password=e["password"], database=e["database"], server_settings=settings,
    )


async def live_fingerprints(
    conn: Any, tables: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    names = list(tables if tables is not None else BACKUP_TABLES)
    async with conn.transaction(readonly=True):
        present = {
            r["relname"] for r in await conn.fetch(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
                "ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
                "AND c.relkind = 'r' AND c.relname = ANY($1::text[])",
                names)
        }
        for t in names:
            if t not in present:
                out[t] = {"present": False}
                continue
            r = await conn.fetchrow(CHECKSUM_SQL.format(table=t))
            size = await conn.fetchval(
                "SELECT pg_total_relation_size($1::regclass)", f"public.{t}")
            out[t] = {"present": True, "rows": int(r["n"]), "md5": r["md5"],
                      "bytes": int(size)}
    return out


def _docker(container: str, args: list[str], *, stdin: bytes | None = None,
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    cmd = ["docker", "exec", "-i"]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    return subprocess.run(cmd + [container, *args], input=stdin,
                          capture_output=True, check=False)


def dump_table(container: str, table: str, dest: Path, db: str, user: str) -> str:
    """pg_dump one table (custom format, compression 9) to ``dest``; returns
    the file's sha256. The dump runs in the database container."""
    p = _docker(container, ["pg_dump", "-U", user, "-d", db, "--format=custom",
                            "--compress=9", "--no-owner", "--no-privileges",
                            "-t", f"public.{table}"])
    if p.returncode != 0:
        raise RuntimeError(f"pg_dump {table}: {p.stderr.decode()[-300:]}")
    dest.write_bytes(p.stdout)
    return hashlib.sha256(p.stdout).hexdigest()


def verify_restores(container: str, user: str, dumps: dict[str, Path]) -> dict[str, dict[str, Any]]:
    """Restore every dump into one fresh scratch database and fingerprint
    each table there. The scratch database is always dropped."""
    scratch = f"legba_bak_verify_{int(time.time())}"
    out: dict[str, dict[str, Any]] = {}
    mk = _docker(container, ["createdb", "-U", user, scratch])
    if mk.returncode != 0:
        raise RuntimeError(f"createdb {scratch}: {mk.stderr.decode()[-300:]}")
    try:
        for table, path in dumps.items():
            r = _docker(container, ["pg_restore", "-U", user, "-d", scratch,
                                    "--no-owner", "--no-privileges"],
                        stdin=path.read_bytes())
            if r.returncode != 0:
                out[table] = {"restored": False,
                              "error": r.stderr.decode()[-300:]}
                continue
            q = _docker(container, ["psql", "-U", user, "-d", scratch, "-At",
                                    "-F", "|", "-c",
                                    CHECKSUM_SQL.format(table=table)])
            n, md5 = q.stdout.decode().strip().split("|")
            out[table] = {"restored": True, "rows": int(n), "md5": md5}
    finally:
        _docker(container, ["dropdb", "-U", user, "--if-exists", scratch])
    return out


def _selected(args: argparse.Namespace) -> list[str]:
    unknown = [t for t in (args.exclude or []) if t not in BACKUP_TABLES]
    if unknown:
        raise SystemExit(f"--exclude names no known backup table: {unknown}")
    return [t for t in BACKUP_TABLES if t not in set(args.exclude or [])]


async def dry_run(args: argparse.Namespace) -> int:
    conn = await _connect(read_only=True)
    try:
        live = await live_fingerprints(conn, _selected(args))
    finally:
        await conn.close()
    total = 0
    for t, fp in live.items():
        if not fp["present"]:
            print(f"{t}: absent (already dropped?)")
            continue
        total += fp["bytes"]
        print(f"{t}: {fp['rows']} rows, {fp['bytes'] / 1024:.0f} KB - "
              f"{BACKUP_TABLES[t]}")
    print(f"total {total / 1024 / 1024:.1f} MB in "
          f"{sum(1 for f in live.values() if f['present'])} tables")
    print("DRY RUN: nothing dumped, nothing dropped "
          "(--dump DIR dumps and verifies; --dump DIR --apply also drops)")
    return 0


async def dump_and_verify(args: argparse.Namespace) -> tuple[Path, dict[str, Any], bool]:
    out_dir = Path(args.dump)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = await _connect(read_only=True)
    try:
        live = await live_fingerprints(conn, _selected(args))
    finally:
        await conn.close()
    e = _pg_env()
    dumps: dict[str, Path] = {}
    manifest: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": e["database"], "tables": {},
    }
    for t, fp in live.items():
        entry = {"backs_up": BACKUP_TABLES[t], **fp}
        if fp["present"]:
            path = out_dir / f"{t}.dump"
            entry["dump_file"] = path.name
            entry["dump_sha256"] = dump_table(args.container, t, path,
                                              e["database"], e["user"])
            entry["dump_bytes"] = path.stat().st_size
            dumps[t] = path
        manifest["tables"][t] = entry
    restored = verify_restores(args.scratch_container or args.container,
                               e["user"], dumps)
    ok = True
    for t, r in restored.items():
        entry = manifest["tables"][t]
        entry["restore"] = r
        entry["verified"] = bool(
            r.get("restored") and r["rows"] == entry["rows"]
            and r["md5"] == entry["md5"])
        ok &= entry["verified"]
    (out_dir / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    for t, entry in manifest["tables"].items():
        if not entry["present"]:
            print(f"{t}: absent (already dropped?)")
            continue
        print(f"{t}: {entry['rows']} rows, {entry['bytes'] / 1024:.0f} KB live, "
              f"dump {entry['dump_bytes'] / 1024:.0f} KB, restore "
              f"{'VERIFIED' if entry['verified'] else 'FAILED'}")
    print(f"manifest: {out_dir / 'MANIFEST.json'}")
    return out_dir, manifest, ok


async def apply(out_dir: Path, manifest: dict[str, Any]) -> int:
    """Drop every table whose dump verified in THIS run and whose live rows are
    still exactly what was dumped. Returns 0 only when every present table was
    dropped."""
    conn = await _connect(read_only=True)
    try:
        live = await live_fingerprints(conn, list(manifest["tables"]))
    finally:
        await conn.close()
    to_drop: list[str] = []
    for t, entry in manifest["tables"].items():
        fp = live.get(t, {"present": False})
        if not fp["present"]:
            print(f"{t}: absent, nothing to do")
            continue
        path = out_dir / entry.get("dump_file", "")
        problems = []
        if not entry.get("verified"):
            problems.append("the dump never verified")
        if fp["rows"] != entry.get("rows") or fp["md5"] != entry.get("md5"):
            problems.append("the live table changed since the dump")
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() \
                != entry.get("dump_sha256"):
            problems.append("the dump file is missing or changed")
        if problems:
            print(f"{t}: KEPT - {'; '.join(problems)}")
        else:
            to_drop.append(t)
    if to_drop:
        conn = await _connect(read_only=False)
        try:
            for t in to_drop:
                async with conn.transaction():
                    await conn.execute("SET LOCAL lock_timeout = '10s'")
                    await conn.execute(f"DROP TABLE public.{t}")
                print(f"{t}: dropped (dump {out_dir / manifest['tables'][t]['dump_file']})")
        finally:
            await conn.close()
    return 0 if len(to_drop) == sum(
        1 for t in manifest["tables"] if live.get(t, {}).get("present")) else 1


async def run(args: argparse.Namespace) -> int:
    if args.dump is None:
        return await dry_run(args)
    out_dir, manifest, ok = await dump_and_verify(args)
    if not args.apply:
        print("nothing dropped (add --apply to drop what verified)")
        return 0 if ok else 1
    return await apply(out_dir, manifest)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--dump", metavar="DIR", default=None,
                    help="dump every table to DIR and verify the dumps restore")
    ap.add_argument("--apply", action="store_true",
                    help="after the dump in THIS run, drop what verified")
    ap.add_argument("--exclude", nargs="*", default=[], metavar="TABLE")
    ap.add_argument("--container", default="legba-postgres-1",
                    help="the database container pg_dump runs in")
    ap.add_argument("--scratch-container", default=None,
                    help="the Postgres container the restore check runs in "
                         "(default: --container)")
    args = ap.parse_args()
    if args.apply and args.dump is None:
        print("--apply needs --dump DIR (a drop never rests on an earlier "
              "run's dump)", file=sys.stderr)
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
