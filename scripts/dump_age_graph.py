#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dump the Apache AGE graph ``legba_graph`` to a file, prove the file restores,
and say whether the graph is empty. READ ONLY; run BEFORE the drop migration.

The empty graph database may be permanently deleted, dumped to a file
first. The deletion itself is the
migration ``20261115T1829_drop_empty_age_graph.sql``, which drops the graph ONLY
if every label table is empty when it applies and refuses loudly otherwise;
this script is the dump that comes before it. It never drops, deletes or
alters anything.

Modes:

  (no flag)       DRY RUN. Lists the graph's label tables with row counts and
                  bytes and says EMPTY or NOT EMPTY. Writes nothing.
  --dump DIR      does the listing, then writes ``DIR/legba_graph.dump``
                  (``pg_dump --format=custom --schema=legba_graph``, run in the
                  database container), ``DIR/legba_graph_catalog.json`` (the
                  ag_catalog rows that register the graph and its labels) and
                  ``DIR/MANIFEST.json``; then VERIFIES: the dump's table of
                  contents lists every label table, and a restore into a fresh
                  scratch database (dropped afterwards, whatever happens) has
                  the same row count per label table as the live graph. Exit 0
                  only when the dump verified AND the graph is empty; a
                  non-empty graph still gets its dump, and exit 1 says the
                  drop migration will refuse.

    python3 scripts/dump_age_graph.py
    python3 scripts/dump_age_graph.py --dump /path/to/dir

Restore later: ``pg_restore -U legba -d legba DIR/legba_graph.dump`` (the age
extension must exist in the target).
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

GRAPH = "legba_graph"


def empty_manifest(database: str) -> dict[str, Any]:
    return {"graph": GRAPH, "database": database,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "labels": {}, "total_rows": 0}


def _pg_env() -> dict[str, str]:
    return {
        "host": os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        "port": os.environ.get("LEGBA_DATA_PG_PORT", "5432"),
        "user": os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        "password": os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        "database": os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    }


async def _connect():
    import asyncpg

    e = _pg_env()
    return await asyncpg.connect(
        host=e["host"], port=int(e["port"]), user=e["user"],
        password=e["password"], database=e["database"],
        server_settings={"default_transaction_read_only": "on",
                         "statement_timeout": "120000",
                         "application_name": "legba-dump-age-graph"},
    )


async def inspect(conn: Any) -> dict[str, Any] | None:
    """The graph's label tables with exact row counts and bytes, or None when
    the graph does not exist."""
    async with conn.transaction(readonly=True):
        g = await conn.fetchrow(
            "SELECT graphid, name, namespace::text AS namespace "
            "FROM ag_catalog.ag_graph WHERE name = $1", GRAPH)
        if g is None:
            return None
        labels = await conn.fetch(
            "SELECT l.name, l.kind::text AS kind, l.relation::regclass::text AS rel, "
            "pg_total_relation_size(l.relation) AS bytes "
            "FROM ag_catalog.ag_label l WHERE l.graph = $1 ORDER BY l.name",
            g["graphid"])
        out: dict[str, Any] = {"namespace": g["namespace"], "labels": {}}
        for r in labels:
            n = int(await conn.fetchval(f"SELECT count(*) FROM {r['rel']}"))
            out["labels"][r["name"]] = {"kind": r["kind"], "rows": n,
                                        "bytes": int(r["bytes"])}
        out["catalog"] = {
            "graph": dict(g) | {"graphid": int(g["graphid"])},
            "labels": [dict(r) | {"bytes": int(r["bytes"])} for r in labels],
        }
        return out


def _docker(container: str, args: list[str], *, stdin: bytes | None = None
            ) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "exec", "-i", container, *args], input=stdin,
                          capture_output=True, check=False)


def dump_graph(container: str, dest: Path, db: str, user: str) -> str:
    p = _docker(container, ["pg_dump", "-U", user, "-d", db, "--format=custom",
                            "--compress=9", "--no-owner", "--no-privileges",
                            f"--schema={GRAPH}"])
    if p.returncode != 0:
        raise RuntimeError(f"pg_dump {GRAPH}: {p.stderr.decode()[-300:]}")
    dest.write_bytes(p.stdout)
    return hashlib.sha256(p.stdout).hexdigest()


def toc_tables(container: str, dump: Path) -> set[str]:
    """Table names in the dump's table of contents (it must be readable)."""
    p = _docker(container, ["pg_restore", "--list"], stdin=dump.read_bytes())
    if p.returncode != 0:
        raise RuntimeError(f"pg_restore --list: {p.stderr.decode()[-300:]}")
    names: set[str] = set()
    for line in p.stdout.decode().splitlines():
        # "224; 1259 16398 TABLE legba_graph Entity legba"
        parts = line.split(" TABLE ", 1)
        if len(parts) == 2 and parts[1].split()[0] == GRAPH:
            names.add(parts[1].split()[1])
    return names


def scratch_restore_counts(container: str, user: str, dump: Path,
                           rels: dict[str, str]) -> dict[str, int]:
    """Restore the dump into a fresh scratch database and count every label
    table there. The scratch database is always dropped."""
    scratch = f"legba_age_verify_{int(time.time())}"
    mk = _docker(container, ["createdb", "-U", user, scratch])
    if mk.returncode != 0:
        raise RuntimeError(f"createdb {scratch}: {mk.stderr.decode()[-300:]}")
    try:
        # The graph's tables use ag_catalog types, so the scratch database needs
        # the extension before the restore (a stock database has none).
        ext = _docker(container, ["psql", "-U", user, "-d", scratch, "-c",
                                  "CREATE EXTENSION IF NOT EXISTS age"])
        if ext.returncode != 0:
            raise RuntimeError(f"create extension age: {ext.stderr.decode()[-200:]}")
        r = _docker(container, ["pg_restore", "-U", user, "-d", scratch,
                                "--no-owner", "--no-privileges"],
                    stdin=dump.read_bytes())
        # pg_restore exits 1 on ignorable errors (an extension that already
        # exists); the counts below are the real verdict.
        out: dict[str, int] = {}
        for label, rel in rels.items():
            q = _docker(container, ["psql", "-U", user, "-d", scratch, "-At",
                                    "-c", f"SELECT count(*) FROM {rel}"])
            if q.returncode != 0:
                raise RuntimeError(
                    f"restore check {label}: {q.stderr.decode()[-200:]} "
                    f"(pg_restore said: {r.stderr.decode()[-200:]})")
            out[label] = int(q.stdout.decode().strip())
        return out
    finally:
        _docker(container, ["dropdb", "-U", user, "--if-exists", scratch])


def _print_listing(info: dict[str, Any] | None) -> bool:
    """Prints the listing; returns True when the graph is absent or empty."""
    if info is None:
        print(f"{GRAPH}: does not exist (already dropped)")
        return True
    total = sum(v["rows"] for v in info["labels"].values())
    size = sum(v["bytes"] for v in info["labels"].values())
    for name, v in info["labels"].items():
        print(f"  {name} ({v['kind']}): {v['rows']} rows, {v['bytes'] / 1024:.0f} KB")
    print(f"{GRAPH}: {len(info['labels'])} label tables, {total} rows in all, "
          f"{size / 1024:.0f} KB - {'EMPTY' if total == 0 else 'NOT EMPTY'}")
    return total == 0


async def run(args: argparse.Namespace) -> int:
    conn = await _connect()
    try:
        info = await inspect(conn)
    finally:
        await conn.close()
    empty = _print_listing(info)
    if args.dump is None:
        print("DRY RUN: nothing written (--dump DIR dumps and verifies)")
        return 0
    if info is None:
        print("nothing to dump")
        return 0
    e = _pg_env()
    out_dir = Path(args.dump)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = empty_manifest(e["database"])
    manifest["labels"] = {k: v["rows"] for k, v in info["labels"].items()}
    manifest["total_rows"] = sum(manifest["labels"].values())
    (out_dir / "legba_graph_catalog.json").write_text(
        json.dumps(info["catalog"], indent=2, default=str))
    dump = out_dir / "legba_graph.dump"
    manifest["dump_file"] = dump.name
    manifest["dump_sha256"] = dump_graph(args.container, dump, e["database"], e["user"])
    manifest["dump_bytes"] = dump.stat().st_size

    in_toc = toc_tables(args.container, dump)
    missing = sorted(set(info["labels"]) - in_toc)
    manifest["toc_missing_labels"] = missing
    rels = {name: f'{GRAPH}."{name}"' for name in info["labels"]}
    restored = scratch_restore_counts(args.scratch_container or args.container,
                                      e["user"], dump, rels)
    mismatched = {k: (info["labels"][k]["rows"], restored.get(k))
                  for k in info["labels"] if restored.get(k) != info["labels"][k]["rows"]}
    manifest["restore_counts"] = restored
    manifest["restore_mismatch"] = {k: list(v) for k, v in mismatched.items()}
    manifest["verified"] = not missing and not mismatched
    (out_dir / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    print(f"dump: {dump} ({manifest['dump_bytes'] / 1024:.0f} KB), "
          f"table of contents lists {len(in_toc & set(info['labels']))}/"
          f"{len(info['labels'])} label tables, restore "
          f"{'VERIFIED' if manifest['verified'] else 'FAILED'}")
    if missing:
        print(f"  missing from the dump: {missing}")
    if mismatched:
        print(f"  restored row counts differ (live, restored): {mismatched}")
    print(f"manifest: {out_dir / 'MANIFEST.json'}")
    if not manifest["verified"]:
        return 1
    if not empty:
        print("the graph is NOT EMPTY: the drop migration will refuse; the dump "
              "is kept so the rows can be looked at")
        return 1
    print("verified and empty: the drop migration may apply")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--dump", metavar="DIR", default=None)
    ap.add_argument("--container", default="legba-postgres-1",
                    help="the database container pg_dump runs in")
    ap.add_argument("--scratch-container", default=None,
                    help="the Postgres container the restore check runs in "
                         "(default: --container)")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
