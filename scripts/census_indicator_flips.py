#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Indicator-flip census (X3 debounce lane, W11-F55) — READ-ONLY.

Two columns over the same window, per unit stream ``(target, unit)``:

BEFORE — what the feed carried: every flip mention in the ``indicator_tracker``
    receipts produced in the window (``analyst_outputs.data -> 'data' ->
    'flips'``), and the distinct flips among them, keyed ``(target, unit,
    indicator, transition, current source finding)``. ``repetition`` =
    mentions / distinct. ``oscillation`` = distinct flips per (stream,
    indicator) — how often one indicator changed status in the window.

AFTER — what the debounced tracker records over the same source runs: the
    shipped walk (``indicator_tracker.collect_flips``, the code the handler
    runs) over every indicator-bearing source run in the lookback, counting
    the raw and debounced flips whose evidence falls in the window.
    ``repetition`` after is 1.0 by construction (one row per flip, and the
    feed carries each debounced flip once); ``fed`` = debounced flips.

If the live table ``indicator_flips`` exists (migration 0237 applied), its
counts in the window are printed too.

NEVER writes: one READ ONLY transaction, ``statement_timeout`` 120 s. Run on
the host against the loopback port::

  PYTHONPATH=src LEGBA_DATA_PG_HOST=127.0.0.1 LEGBA_DATA_PG_DB=legba \\
      python3 scripts/census_indicator_flips.py --env-file .env \\
      [--days 7] [--lookback-days 30] [--via-docker legba-postgres-1] \\
      [--hysteresis-runs 2] [--top 15] [--json-out PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from legba.data.analysts.deterministic_handlers import (  # noqa: E402
    indicator_tracker as it,
)
from legba.data.analysts.deterministic_handlers._indicator_flip_store import (  # noqa: E402
    read_cadence_hours,
)

_RECEIPTS_SQL = """
SELECT f AS flip
  FROM analyst_outputs ao,
       jsonb_array_elements(ao.data -> 'data' -> 'flips') AS f
 WHERE ao.analyst_id = 'indicator_tracker'
   AND ao.kind = 'finding'
   AND ao.produced_at > $1
   AND jsonb_typeof(ao.data -> 'data' -> 'flips') = 'array'
"""

_RECEIPT_COUNT_SQL = """
SELECT count(*) FROM analyst_outputs
 WHERE analyst_id = 'indicator_tracker' AND kind = 'finding' AND produced_at > $1
"""

_SOURCE_RUNS_SQL = """
SELECT id, target_id, analyst_id, produced_at,
       jsonb_build_object('data', jsonb_build_object(
           'indicators', data -> 'data' -> 'indicators')) AS data
  FROM analyst_outputs
 WHERE kind = 'finding'
   AND produced_at > $1
   AND jsonb_typeof(data -> 'data' -> 'indicators') = 'array'
   AND jsonb_array_length(data -> 'data' -> 'indicators') > 0
"""

_TABLE_SQL = """
SELECT grade, count(*) AS n, count(*) FILTER (WHERE backfill) AS backfill
  FROM indicator_flips
 WHERE coalesce(confirmed_at, evidence_at) > $1
 GROUP BY grade
"""


def _password_from_env_file(path: str | None) -> str | None:
    """The database password from a ``.env`` file, read without sourcing it
    (a ``.env`` may carry values a shell would expand)."""
    if not path:
        return None
    wanted = ("LEGBA_DATA_PG_PASSWORD=", "LEGBA_DATA_TIMESCALE_PASSWORD=")
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            for key in wanted:
                if line.startswith(key):
                    return line[len(key):].strip().strip('"').strip("'")
    return None


async def _connect(env_file: str | None = None) -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=(
            os.environ.get("LEGBA_DATA_PG_PASSWORD")
            or os.environ.get("LEGBA_DATA_TIMESCALE_PASSWORD")
            or _password_from_env_file(env_file)
            or None
        ),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )

async def _gather(conn: Any, since: datetime, history: datetime) -> tuple:
    receipts = await conn.fetchval(_RECEIPT_COUNT_SQL, since)
    flips = []
    for r in await conn.fetch(_RECEIPTS_SQL, since):
        v = r["flip"]
        flips.append(json.loads(v) if isinstance(v, str) else dict(v))
    runs = [dict(r) for r in await conn.fetch(_SOURCE_RUNS_SQL, history)]
    for r in runs:
        if isinstance(r["produced_at"], str):
            r["produced_at"] = datetime.fromisoformat(r["produced_at"])
    cadence = await read_cadence_hours(conn, {str(r["analyst_id"]) for r in runs})
    table = None
    if await conn.fetchval("SELECT to_regclass('public.indicator_flips') IS NOT NULL"):
        table = {r["grade"]: dict(r) for r in await conn.fetch(_TABLE_SQL, since)}
    return receipts, flips, runs, cadence, table


def _literal(v: Any) -> str:
    """A SQL literal for the few parameter types this script binds."""
    if isinstance(v, datetime):
        return f"'{v.isoformat()}'::timestamptz"
    if isinstance(v, (list, tuple)):
        inner = ",".join("'" + str(x).replace("'", "''") + "'" for x in v)
        return f"ARRAY[{inner}]::text[]"
    if isinstance(v, int):
        return str(int(v))
    raise TypeError(f"unbindable parameter {type(v).__name__}")


class _PsqlReader:
    """``fetch`` / ``fetchval`` over ``docker exec <container> psql`` — for a
    host without the database password. Every statement runs inside
    ``BEGIN READ ONLY`` with a 120 s statement timeout."""

    def __init__(self, container: str) -> None:
        self._container = container

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        for i in range(len(args), 0, -1):
            sql = sql.replace(f"${i}", _literal(args[i - 1]))
        script = (
            "BEGIN READ ONLY; SET LOCAL statement_timeout = '120s'; "
            f"SELECT row_to_json(q) FROM ({sql}) q; COMMIT;"
        )
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", self._container, "psql", "-U", "legba", "-d",
            os.environ.get("LEGBA_DATA_PG_DB", "legba"), "-qAtX", "-v", "ON_ERROR_STOP=1",
            "-c", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(err.decode("utf-8", "replace"))
        return [json.loads(line) for line in out.decode("utf-8").splitlines() if line.strip()]

    async def fetchval(self, sql: str, *args: Any) -> Any:
        rows = await self.fetch(sql, *args)
        return next(iter(rows[0].values())) if rows else None


def _stream(target: Any, unit: Any) -> str:
    return f"{target or 'global'}/{unit or '?'}"


def _before(flips: list[dict[str, Any]]) -> dict[str, Any]:
    mentions: Counter = Counter()
    distinct: dict[str, set] = defaultdict(set)
    per_indicator: dict[tuple[str, str], set] = defaultdict(set)
    up = down = 0
    seen_dir: set = set()
    for f in flips:
        s = _stream(f.get("target_id"), f.get("source_analyst_id"))
        curr = (f.get("source_finding_ids") or {}).get("curr")
        key = (str(f.get("indicator_id")), f.get("from_status"), f.get("to_status"), curr)
        mentions[s] += 1
        distinct[s].add(key)
        per_indicator[(s, key[0])].add(key)
        if (s, key) not in seen_dir:
            seen_dir.add((s, key))
            if f.get("to_status") == "triggered":
                up += 1
            elif f.get("from_status") == "triggered":
                down += 1
    return {
        "mentions": mentions,
        "distinct": {s: len(v) for s, v in distinct.items()},
        "oscillation": {k: len(v) for k, v in per_indicator.items()},
        "up": up, "down": down,
    }


def _after(sweep: Any, since: datetime) -> dict[str, Any]:
    raw: Counter = Counter()
    deb: Counter = Counter()
    per_indicator: Counter = Counter()
    deb_pair: Counter = Counter()
    up = down = 0
    for f in sweep.raw:
        if f["evidence_at"] > since:
            s = _stream(f["target_id"], f["source_analyst_id"])
            raw[s] += 1
            per_indicator[(s, str(f["indicator_id"]))] += 1
    for f in sweep.debounced:
        if f["confirmed_at"] > since:
            s = _stream(f["target_id"], f["source_analyst_id"])
            deb[s] += 1
            deb_pair[(s, str(f["indicator_id"]))] += 1
            if f["to_status"] == "triggered":
                up += 1
            elif f["from_status"] == "triggered":
                down += 1
    return {"raw": raw, "debounced": deb, "oscillation": per_indicator,
            "debounced_pairs": deb_pair, "up": up, "down": down}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--lookback-days", type=int, default=30)
    ap.add_argument("--hysteresis-runs", type=int, default=2)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--json-out")
    ap.add_argument("--env-file", help="read the database password from this .env")
    ap.add_argument("--via-docker", metavar="CONTAINER",
                    help="read through `docker exec CONTAINER psql` instead of asyncpg")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=args.days)
    history = now - timedelta(days=args.lookback_days)

    if args.via_docker:
        reads = await _gather(_PsqlReader(args.via_docker), since, history)
    else:
        conn = await _connect(args.env_file)
        try:
            async with conn.transaction(readonly=True):
                await conn.execute("SET LOCAL statement_timeout = '120s'")
                reads = await _gather(conn, since, history)
        finally:
            await conn.close()
    receipts, flips, runs, cadence, table = reads

    b = _before(flips)
    sweep = it.collect_flips(runs, hysteresis_runs=args.hysteresis_runs, cadence_hours=cadence)
    a = _after(sweep, since)

    tot_m = sum(b["mentions"].values())
    tot_d = sum(b["distinct"].values())
    print(f"# Indicator-flip census — window {since:%Y-%m-%d %H:%MZ} → {now:%Y-%m-%d %H:%MZ} "
          f"({args.days} d); history walked from {history:%Y-%m-%d} "
          f"({len(runs)} source runs); hysteresis_runs={args.hysteresis_runs}, "
          "hysteresis_hours=one cadence of each unit\n")
    print("| | before (receipts as fed) | after (debounced walk) |")
    print("|---|---|---|")
    print(f"| receipts / feed rows | {receipts} | ≤ 1 per sweep that inserted a debounced flip |")
    print(f"| flip mentions fed | {tot_m:,} | {sum(a['debounced'].values()):,} |")
    print(f"| distinct flips | {tot_d:,} | raw {sum(a['raw'].values()):,} · debounced "
          f"{sum(a['debounced'].values()):,} |")
    rep = f"{tot_m / tot_d:.1f}×" if tot_d else "not measured"
    print(f"| repetition (mentions / distinct) | {rep} | 1.0× (one row per flip) |")
    print(f"| up (→ triggered) / down (triggered →) | {b['up']} / {b['down']} | "
          f"{a['up']} / {a['down']} |")
    osc_b = sorted(b["oscillation"].items(), key=lambda kv: -kv[1])
    osc_a = a["oscillation"]
    print(f"| indicators changing ≥ 4× in the window | "
          f"{sum(1 for _, n in osc_b if n >= 4)} | raw {sum(1 for n in osc_a.values() if n >= 4)}"
          f" · debounced {sum(1 for n in a['debounced_pairs'].values() if n >= 4)} |")
    if table is not None:
        print(f"| live `indicator_flips` rows in window | — | {json.dumps(table, default=str)} |")
    else:
        print("| live `indicator_flips` rows in window | — | not measured (0237 not applied) |")

    print(f"\n## Top {args.top} streams by mentions fed (before) — per stream\n")
    print("| stream (desk/unit) | before: mentions | before: distinct | before: repetition "
          "| after: raw | after: debounced (fed) |")
    print("|---|---|---|---|---|---|")
    for s, m in b["mentions"].most_common(args.top):
        d = b["distinct"][s]
        print(f"| {s} | {m} | {d} | {m / d:.1f}× | {a['raw'].get(s, 0)} | "
              f"{a['debounced'].get(s, 0)} |")

    print(f"\n## Top {args.top} oscillating indicators — status changes in the window\n")
    print("| stream / indicator | before: distinct flips | after: raw | after: debounced |")
    print("|---|---|---|---|")
    for (s, ind), n in osc_b[: args.top]:
        print(f"| {s} / {ind} | {n} | {osc_a.get((s, ind), 0)} | "
              f"{a['debounced_pairs'].get((s, ind), 0)} |")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({
                "window": [since.isoformat(), now.isoformat()],
                "receipts": receipts,
                "before": {"mentions": dict(b["mentions"]), "distinct": b["distinct"],
                           "up": b["up"], "down": b["down"]},
                "after": {"raw": dict(a["raw"]), "debounced": dict(a["debounced"]),
                          "up": a["up"], "down": a["down"]},
                "live_table": table,
            }, fh, indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
