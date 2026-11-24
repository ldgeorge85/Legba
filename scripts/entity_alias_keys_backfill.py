#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""entity_alias_keys_backfill.py — fill the entity name key table (P-6, 0263).

Walks ``entity_profiles`` in id order in keyset batches and brings each
profile's ``entity_alias_keys`` rows to what ``entity_alias_keys_for`` says
(``legba.data.entity_alias_keys.backfill_batch``). Writes ONLY
``entity_alias_keys``.

MODES
  (default) dry run: read-only; per batch, how many keys would be inserted and
    deleted. Nothing written, no cursor written.
  --apply: one transaction per batch (profile rows locked FOR SHARE for the
    batch's few milliseconds, so a concurrent profile update re-keys itself
    after the batch commits). The cursor file (``--cursor-file``) is
    rewritten after every committed batch; a re-run resumes after the last
    committed id, ``--restart`` ignores it. Safe to re-run: a batch whose keys
    are already right writes nothing.

A progress line per ``--log-every`` batches, and a final summary line:
  ENTITY_ALIAS_KEYS_BACKFILL mode=<dry_run|apply> batches=… profiles=…
    expected=… inserted=… deleted=… seconds=…

USAGE (inside a container that has the runtime's Postgres env)
    python3 scripts/entity_alias_keys_backfill.py                 # dry run
    python3 scripts/entity_alias_keys_backfill.py --apply         # resumable
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

DEFAULT_CURSOR_FILE = "./entity_alias_keys_backfill.cursor.json"


def _read_cursor(path: Path) -> str | None:
    try:
        return json.loads(path.read_text()).get("last_id") or None
    except (OSError, ValueError):
        return None


def _write_cursor(path: Path, state: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True))
    tmp.replace(path)


async def run(args: argparse.Namespace, dsn: str) -> int:
    import asyncpg

    from legba.data.entity_alias_keys import backfill_batch

    cursor_path = Path(args.cursor_file)
    after = None if (args.restart or not args.apply) else _read_cursor(cursor_path)
    conn = await asyncpg.connect(
        dsn, server_settings={"statement_timeout": str(args.statement_timeout_ms),
                              "application_name": "entity_alias_keys_backfill"})
    totals = {"batches": 0, "profiles": 0, "expected": 0, "inserted": 0, "deleted": 0}
    t0 = time.monotonic()
    try:
        if after:
            print(f"resuming after id {after}", flush=True)
        while args.max_batches is None or totals["batches"] < args.max_batches:
            r = await backfill_batch(conn, after_id=after, batch_size=args.batch_size,
                                     apply=args.apply)
            if r.profiles == 0:
                break
            after = r.last_id
            totals["batches"] += 1
            for k in ("profiles", "expected", "inserted", "deleted"):
                totals[k] += getattr(r, k)
            if args.apply:
                _write_cursor(cursor_path, {"last_id": after, **totals})
            if totals["batches"] % args.log_every == 0:
                el = time.monotonic() - t0
                print(f"batch={totals['batches']} profiles={totals['profiles']} "
                      f"inserted={totals['inserted']} deleted={totals['deleted']} "
                      f"last_id={after} seconds={el:.1f} "
                      f"rate={totals['profiles'] / max(el, 1e-9):.0f}/s", flush=True)
    finally:
        await conn.close()
    print("ENTITY_ALIAS_KEYS_BACKFILL mode={} batches={batches} profiles={profiles} "
          "expected={expected} inserted={inserted} deleted={deleted} seconds={s:.1f}".format(
              "apply" if args.apply else "dry_run", s=time.monotonic() - t0, **totals),
          flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--apply", action="store_true", help="write entity_alias_keys")
    p.add_argument("--batch-size", type=int, default=2000)
    p.add_argument("--cursor-file", default=DEFAULT_CURSOR_FILE)
    p.add_argument("--restart", action="store_true", help="ignore an existing cursor")
    p.add_argument("--max-batches", type=int, default=None)
    p.add_argument("--log-every", type=int, default=10)
    p.add_argument("--statement-timeout-ms", type=int, default=60000)
    p.add_argument("--dsn", default=None,
                   help="override the DSN (default: PostgresConfig.from_env())")
    args = p.parse_args(argv)
    if args.batch_size < 1 or args.log_every < 1:
        p.error("--batch-size and --log-every must be >= 1")
    dsn = args.dsn
    if dsn is None:
        from legba.data.config import PostgresConfig
        dsn = PostgresConfig.from_env().dsn
    return asyncio.run(run(args, dsn))


if __name__ == "__main__":
    sys.exit(main())
