#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The host lifecycle overlay, one row per file (R-1).

``descriptors/LIFECYCLE.md`` was a single hand-kept table that fifteen commits
edited in three days: every change that activated a descriptor appended a row to
the same place. The rows now live one per file in ``descriptors/LIFECYCLE.d/``
(``<family>__<id>.md``, the row's table line), and the walker
(``scripts/bringup_register_descriptors.py``, ``load_lifecycle``) reads both
the directory and any rows still in ``LIFECYCLE.md`` (refusing a row named
twice). Two changes recording different flips touch different files.

    python3 scripts/lifecycle_rows.py set analyst kev_digest active draft
    python3 scripts/lifecycle_rows.py unset analyst kev_digest
    python3 scripts/lifecycle_rows.py print          # the whole table, rendered
    python3 scripts/lifecycle_rows.py --check        # valid, and LIFECYCLE.md row-free
    python3 scripts/lifecycle_rows.py split          # move LIFECYCLE.md's rows out
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE_MD = REPO_ROOT / "descriptors" / "LIFECYCLE.md"


def _walker():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    sys.path.insert(0, str(REPO_ROOT / "src"))
    return importlib.import_module("bringup_register_descriptors")


def _row(family: str, did: str, state: str, ships: str) -> str:
    return f"| {family} | {did} | {state} | {ships} |\n"


def _table_rows_in_md(walker, path: Path) -> list[str]:
    return [
        ln for ln in path.read_text(encoding="utf-8").splitlines()
        if (m := walker._STATE_ROW_RE.match(ln)) and m.group(1) != "family"
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--lifecycle", type=Path, default=LIFECYCLE_MD)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("set")
    for name in ("family", "id", "state", "ships"):
        s.add_argument(name)
    u = sub.add_parser("unset")
    u.add_argument("family")
    u.add_argument("id")
    sub.add_parser("print")
    sub.add_parser("split")
    args = ap.parse_args(argv)
    walker = _walker()
    path: Path = args.lifecycle
    rows_dir = walker.lifecycle_rows_dir(path)

    if args.check:
        walker.load_lifecycle(path)  # raises PlanError on any bad / duplicate row
        left = _table_rows_in_md(walker, path) if path.exists() else []
        if left:
            print(f"{path.name} still holds {len(left)} row(s); run "
                  "scripts/lifecycle_rows.py split", file=sys.stderr)
            return 1
        print(f"ok: {len(walker.load_lifecycle(path))} rows in {rows_dir.name}/")
        return 0
    if args.cmd == "set":
        rows_dir.mkdir(parents=True, exist_ok=True)
        target = rows_dir / walker.lifecycle_row_file_name(args.family, args.id)
        target.write_text(_row(args.family, args.id, args.state, args.ships), encoding="utf-8")
        walker.load_lifecycle(path)
        print(target.relative_to(REPO_ROOT))
        return 0
    if args.cmd == "unset":
        target = rows_dir / walker.lifecycle_row_file_name(args.family, args.id)
        target.unlink()
        print(f"removed {target.relative_to(REPO_ROOT)}")
        return 0
    if args.cmd == "print":
        rows = walker.load_lifecycle(path)
        sys.stdout.write("| family | id | state | file ships |\n|---|---|---|---|\n")
        for (family, did), (state, ships) in sorted(rows.items()):
            sys.stdout.write(_row(family, did, state, ships))
        return 0
    if args.cmd == "split":
        rows = _table_rows_in_md(walker, path)
        rows_dir.mkdir(parents=True, exist_ok=True)
        for line in rows:
            family, did, state, ships = walker._STATE_ROW_RE.match(line).groups()
            target = rows_dir / walker.lifecycle_row_file_name(family, did)
            if target.exists():
                raise SystemExit(f"{target} exists: {family}/{did} is named twice")
            target.write_text(_row(family, did, state, ships), encoding="utf-8")
        kept = [
            ln for ln in path.read_text(encoding="utf-8").splitlines(keepends=True)
            if ln.rstrip("\n") not in rows
        ]
        path.write_text("".join(kept), encoding="utf-8")
        walker.load_lifecycle(path)
        print(f"moved {len(rows)} rows into {rows_dir.relative_to(REPO_ROOT)}/")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
