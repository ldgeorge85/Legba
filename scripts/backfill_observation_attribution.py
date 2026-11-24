#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Check that every LIVE observation row resolves a licence + attribution — DRY RUN ONLY.

Lane N-R. Live ``observations`` rows are append-only by trigger (migration
0246), so the 8,036 PortWatch rows written on 2026-11-13 before the sink
stamped ``license_class`` / ``attribution`` into ``provenance`` are NOT
rewritten. Three ways a "backfill" could be done, and why this script takes
the first:

1. **Read-time join (chosen; no write).** ``legba.data.observation_attribution``
   resolves a row's licence from its own stamp, else from the source
   descriptor version current at the row's ``loaded_at``, else the head; the
   attribution likewise; the subject kind from the series registry. Every
   reader of the series gets all three in one query. Nothing is written, the
   trigger is never touched, ``record_time`` and the vintage chain stay true.
2. Disabling the trigger and UPDATE-ing ``provenance`` — refused: it breaks
   the append-only guarantee the citation chain rests on, for a value that is
   derivable at read time anyway.
3. Writing a new vintage per row with the stamp — refused: it fabricates a
   provider revision (a new ``record_time`` for an unchanged value) and the
   series_change detector would read 8,036 phantom revisions.

So this script WRITES NOTHING, by design (there is no ``--apply``). It runs the
read-time query inside a READ ONLY transaction and reports, per source: rows,
rows stamped on the row vs resolved from the descriptor, rows that resolve no
licence or no attribution (those need a descriptor edit — e.g. add
``scope.attribution`` and register a new head), and stored vs declared
subject kinds. Exit status 1 when any row resolves no licence.

Usage (inside the runtime container, DB env from the container)::

      python3 scripts/backfill_observation_attribution.py
      python3 scripts/backfill_observation_attribution.py --source source.imf.portwatch_chokepoints
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from legba.data.observation_attribution import fetch_attributed  # noqa: E402


def summarise(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Pure: the report for one source's attributed rows."""
    rows = list(rows)
    return {
        "rows": len(rows),
        "stamped_on_row": sum(1 for r in rows if r["licence_basis"] == "row"),
        "resolved_from_descriptor": sum(1 for r in rows if r["licence_basis"] == "descriptor"),
        "no_licence": sum(1 for r in rows if not r["license_class"]),
        "no_attribution": sum(1 for r in rows if not r["attribution"]),
        "licence_classes": dict(Counter(r["license_class"] for r in rows)),
        "attributions": dict(Counter(r["attribution"] for r in rows)),
        "subject_kind_stored_to_declared": dict(Counter(
            f"{r['stored_subject_kind']}->{r['subject_kind']}" for r in rows)),
    }


async def run(conn: Any, sources: list[str] | None) -> int:
    if not sources:
        sources = [r["source_id"] for r in await conn.fetch(
            "SELECT DISTINCT source_id FROM observations WHERE origin_class = 'live' ORDER BY 1")]
    worst = 0
    for source_id in sources:
        report = summarise(await fetch_attributed(conn, source_id))
        print(f"[{source_id}]")
        for key, value in report.items():
            print(f"  {key}: {value}")
        if report["no_licence"]:
            worst = 1
    print("DRY RUN — nothing written (read-time attribution; see the module docstring)")
    return worst


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--source", action="append", help="source id (repeatable; default: every live source)")
    args = ap.parse_args()
    import asyncpg

    conn = await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )
    try:
        async with conn.transaction(readonly=True):
            return await run(conn, args.source)
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
