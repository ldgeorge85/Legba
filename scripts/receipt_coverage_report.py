#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Receipt coverage of new signals, per source kind and source (B-1). READ-ONLY.

For the signals written in the last ``--hours`` (default 24), how many link to
a fetch receipt (``fetch_version_records``, any role a poll or push writes:
``response`` / ``page`` / ``inbound``) — per source kind, then per source.
Counts only; no row content. One read-only transaction, statement timeout
60 s.

The roll uses it to decide when to run the archive register: new GDELT-file
and Telegram signals must show links first.

    docker run --rm --network legba_default --env-file .env -v "$PWD/scripts:/s:ro" \\
        --entrypoint python legba/legba-runtime-dapr:latest /s/receipt_coverage_report.py [--hours 6]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

COVERAGE_SQL = """
SELECT coalesce(d.kind, '(no descriptor)') AS kind, s.source_id,
       count(*) AS signals,
       count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM fetch_version_records r
            WHERE r.record_kind = 'signal' AND r.record_id = s.id
              AND r.role IN ('response', 'page', 'inbound'))) AS linked,
       count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM fetch_version_records r
            WHERE r.record_kind = 'signal' AND r.record_id = s.id
              AND r.role = 'response' AND r.locator IS NOT NULL)) AS located
  FROM signals s
  LEFT JOIN source_descriptors d ON d.descriptor_id = s.source_id AND d.is_head
 WHERE s.created_at > now() - make_interval(hours => $1)
 GROUP BY 1, 2
 ORDER BY 1, 3 DESC
"""

#: Before migration 0266 there is no ``locator`` column.
COVERAGE_SQL_PRE_0266 = COVERAGE_SQL.replace(
    "AND r.role = 'response' AND r.locator IS NOT NULL)) AS located",
    "AND false)) AS located",
)


def summarise(rows: list[dict]) -> dict:
    kinds: dict[str, dict] = {}
    for r in rows:
        k = kinds.setdefault(r["kind"], {"signals": 0, "linked": 0, "located": 0, "sources": []})
        k["signals"] += r["signals"]
        k["linked"] += r["linked"]
        k["located"] += r["located"]
        k["sources"].append({"source_id": r["source_id"], "signals": r["signals"], "linked": r["linked"],
                             "share": round(r["linked"] / r["signals"], 4) if r["signals"] else None})
    total = sum(k["signals"] for k in kinds.values())
    linked = sum(k["linked"] for k in kinds.values())
    for k in kinds.values():
        k["share"] = round(k["linked"] / k["signals"], 4) if k["signals"] else None
    return {"signals": total, "linked": linked,
            "share": round(linked / total, 4) if total else None, "by_kind": kinds}


async def _amain(hours: int) -> int:
    import asyncpg  # noqa: PLC0415 — runtime image only

    conn = await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )
    try:
        async with conn.transaction(readonly=True):
            await conn.execute("SET LOCAL statement_timeout = '60s'")
            has_locator = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'"
                " AND table_name = 'fetch_version_records' AND column_name = 'locator')")
            rows = await conn.fetch(COVERAGE_SQL if has_locator else COVERAGE_SQL_PRE_0266, hours)
    finally:
        await conn.close()
    out = summarise([dict(r) for r in rows])
    out["hours"] = hours
    print(json.dumps(out, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hours", type=int, default=24)
    return asyncio.run(_amain(ap.parse_args(argv).hours))


if __name__ == "__main__":
    raise SystemExit(main())
