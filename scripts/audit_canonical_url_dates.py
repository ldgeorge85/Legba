#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Audit ``signals.canonical_url`` against the row's own dates, READ-ONLY.

K-4 R4 §9 **F4**: a FRESH signal carrying a STALE ``canonical_url`` — a Yonhap
item whose body was unambiguously 6 Aug 2026 stored
``…/AEN20250710005751315``, a 2025-dated article id. That is a provenance
defect in the ingest, not a matching defect, and its cost is concrete: a
labeller trusting the URL grades the pair ``temporal_stale``, and a K-5 closer
citing that URL cites the wrong article. The report routes the fix to the
data-quality queue as exactly this: "audit ``canonical_url`` against body
dates."

Many publishers embed the publication date in the URL (``/2026/08/06/``
segments, ``2026-08-05`` slug dates, ``YYYYMMDD``-prefixed wire article ids),
which makes the defect detectable at scale. This script parses the date each
stored URL CLAIMS (:func:`legba.data._url_canon.url_embedded_date` — the
shared, tested helper) and compares it against the dates the row actually
carries: ``payload->>'published_at'`` when present, else ``fetched_at``. A row
is SUSPECT when the URL's claimed date is more than ``--stale-days`` older
than the row's date (the F4 shape), or in the row's future by more than
``--future-days`` (an impossible claim). This script NEVER writes — it only
SELECTs and prints; acting on a hit (re-scrape, canonical repair, source
descriptor fix) is an operator decision per source, not a heuristic rewrite.

Run in the registry container (runtime deps present) against the live
Postgres:

  docker exec -e LEGBA_DATA_PG_DB=legba \\
      -e LEGBA_DATA_PG_HOST=legba-postgres-1 \\
      -e PYTHONPATH=/app/src \\
      <registry-container> python3 /app/scripts/audit_canonical_url_dates.py

Or on the host with the repo mounted (PYTHONPATH=src, loopback host):

  set -a; . ./.env; set +a
  PYTHONPATH=src LEGBA_DATA_PG_HOST=127.0.0.1 LEGBA_DATA_PG_DB=legba \\
      python3 scripts/audit_canonical_url_dates.py --limit 100000

Flags:
  --limit N         newest N signals scanned (default 100000; 0 = all rows)
  --stale-days N    URL date older than the row date by more than N days is
                    suspect (default 45 — generous against slow syndication
                    and evergreen re-publishes)
  --future-days N   URL date ahead of the row date by more than N days is
                    suspect (default 2 — feeds cross midnight, never months)
  --top N           show the N worst hosts (default 25; 0 = all)
  --samples N       sample rows printed per direction (default 15)
  --json            machine-readable summary instead of the table
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import asyncpg

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
)

from legba.data._url_canon import url_embedded_date  # noqa: E402


async def _connect_pg() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )


_SQL = """
SELECT id::text AS id,
       source_id,
       canonical_url,
       fetched_at,
       payload->>'published_at' AS published_at
  FROM signals
 WHERE canonical_url IS NOT NULL AND canonical_url <> ''
 ORDER BY fetched_at DESC
 {limit_clause}
"""


def _host(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return "(unparseable)"
    return host[4:] if host.startswith("www.") else (host or "(no host)")


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="READ-ONLY audit: canonical_url embedded dates vs row dates"
    )
    ap.add_argument("--limit", type=int, default=100_000)
    ap.add_argument("--stale-days", type=int, default=45)
    ap.add_argument("--future-days", type=int, default=2)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--samples", type=int, default=15)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    limit_clause = f"LIMIT {int(args.limit)}" if args.limit > 0 else ""
    conn = await _connect_pg()
    try:
        rows = await conn.fetch(_SQL.format(limit_clause=limit_clause))
    finally:
        await conn.close()

    scanned = len(rows)
    dated = 0
    stale: list[dict] = []
    future: list[dict] = []
    per_host: dict[str, dict[str, int]] = defaultdict(
        lambda: {"scanned": 0, "dated": 0, "stale": 0, "future": 0}
    )

    stale_delta = timedelta(days=args.stale_days)
    future_delta = timedelta(days=args.future_days)

    for r in rows:
        url = str(r["canonical_url"])
        host = _host(url)
        per_host[host]["scanned"] += 1
        claimed = url_embedded_date(url)
        if claimed is None:
            continue
        dated += 1
        per_host[host]["dated"] += 1
        # The row's own date: what the source SAID it published, else when we
        # actually fetched it. Compared at date granularity — the defect class
        # is measured in months and years, not hours.
        row_dt = r["fetched_at"]
        pub_raw = r["published_at"]
        if pub_raw:
            try:
                row_dt = datetime.fromisoformat(str(pub_raw))
            except ValueError:
                pass
        row_date = row_dt.date()
        record = {
            "id": r["id"],
            "source_id": r["source_id"],
            "host": host,
            "canonical_url": url,
            "url_date": claimed.isoformat(),
            "row_date": row_date.isoformat(),
        }
        if claimed < row_date - stale_delta:
            per_host[host]["stale"] += 1
            stale.append(record)
        elif claimed > row_date + future_delta:
            per_host[host]["future"] += 1
            future.append(record)

    summary = {
        "scanned": scanned,
        "url_carries_a_date": dated,
        "stale_suspects": len(stale),
        "future_suspects": len(future),
        "stale_days_threshold": args.stale_days,
        "future_days_threshold": args.future_days,
    }

    if args.json:
        print(
            json.dumps(
                {
                    **summary,
                    "per_host": {
                        h: c
                        for h, c in sorted(per_host.items())
                        if c["stale"] or c["future"]
                    },
                    "stale": stale,
                    "future": future,
                },
                indent=2,
            )
        )
        return 0

    print("canonical_url date audit (READ-ONLY) — K-4 R4 F4")
    for k, v in summary.items():
        print(f"  {k} = {v}")

    offenders = sorted(
        (
            (h, c)
            for h, c in per_host.items()
            if c["stale"] or c["future"]
        ),
        key=lambda hc: (hc[1]["stale"] + hc[1]["future"]),
        reverse=True,
    )
    shown = offenders[: args.top] if args.top > 0 else offenders
    if shown:
        print("\nworst hosts (suspects / url-dated / scanned):")
        for host, c in shown:
            print(
                f"  {host:40s} {c['stale'] + c['future']:6d} / "
                f"{c['dated']:6d} / {c['scanned']:6d}"
                f"   (stale {c['stale']}, future {c['future']})"
            )
    for label, bucket in (("STALE", stale), ("FUTURE", future)):
        if bucket:
            print(f"\nsample {label} rows (url claims vs row date):")
            for rec in bucket[: args.samples]:
                print(
                    f"  {rec['url_date']} vs {rec['row_date']}  "
                    f"[{rec['source_id']}] {rec['canonical_url'][:110]}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
