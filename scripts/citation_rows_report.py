#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Citations as rows (DM-A): the share of a day's citations that join to a
record, and to a record AND stored bytes, in SQL alone — by desk and by kind.

READ-ONLY (SELECTs only; the session is opened read-only). The rules live in
:mod:`legba.data.citation_rows_audit`.

    # the baseline, from the citations stored in each finding (any day):
    python3 scripts/citation_rows_report.py --day 2026-11-14 --source json
    # the proof after deploy, from the rows (findings written that day):
    python3 scripts/citation_rows_report.py --day 2026-11-16 --source rows
    # print the SQL instead, to run through psql in a read-only session:
    python3 scripts/citation_rows_report.py --day 2026-11-14 --source json --sql

``--day`` defaults to yesterday (UTC). Database env as every script:
``LEGBA_DATA_PG_HOST`` / ``_PORT`` / ``_USER`` / ``_PASSWORD`` / ``_DB``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# This checkout's package first (an editable install elsewhere must not win).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from legba.data.citation_rows_audit import report_sql  # noqa: E402


def _window(day: str | None) -> tuple[str, str, str]:
    d = date.fromisoformat(day) if day else (datetime.now(timezone.utc).date() - timedelta(days=1))
    lo = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return d.isoformat(), lo.isoformat(), (lo + timedelta(days=1)).isoformat()


async def _run(sql: str) -> list[dict]:
    import asyncpg  # noqa: PLC0415

    conn = await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
        server_settings={"default_transaction_read_only": "on"},
    )
    try:
        return [dict(r) for r in await conn.fetch(sql)]
    finally:
        await conn.close()


def _share(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--day", help="UTC day, YYYY-MM-DD (default: yesterday)")
    p.add_argument("--source", choices=("json", "rows"), default="json")
    p.add_argument("--sql", action="store_true", help="print the SQL and exit")
    args = p.parse_args(argv)
    day, lo, hi = _window(args.day)
    sql = report_sql(args.source, day_start=lo, day_end=hi)
    if args.sql:
        print(sql)
        return 0
    rows = asyncio.run(_run(sql))
    for r in rows:
        r["share_joins_record"] = _share(r["joins_record"], r["links"])
        r["share_joins_record_and_bytes"] = _share(r["joins_record_and_bytes"], r["links"])
    print(json.dumps({"day": day, "source": args.source, "rows": rows}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
