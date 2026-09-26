#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Write the OPERATOR-TRIGGERED top-up packet for one country reference.

R1 measured what the free core-plane lane cannot do. Under the fences it builds
a reference that VERIFIES — 78.9% of spans exact against the archived page, 8 of
8 dimensions carrying two — and still carries only ~33% of a knowledgeable
reader's major developments, with per-dimension counts FLATTER than the record
(3-6 everywhere in a fortnight where escalation carried 13 items and
proliferation 2). Flatness is the tell: it filled a quota instead of following
the record.

Closing that gap costs money, and this file is where that decision stays the
operator's. It is a PACKET WRITER, not an LLM call:

  * nothing in the platform runs it;
  * nothing in the platform schedules it;
  * the scheduled builder runs to completion, and the grader grades against its
    skeleton, whether or not this is ever used;
  * it makes no network call of any kind. It reads one row and writes one file.

The operator hands the file to whatever lane they choose. The result comes back
through ``scripts/load_unit_reference.py --merge-into <reference_id>``, which
applies THE SAME FENCES to the top-up's developments as the core lane's — the
date gate, the span check against the archive, the tier rules — so a paid lane
gets no easier ride than the free one.

  PYTHONPATH=src LEGBA_DATA_PG_HOST=127.0.0.1 LEGBA_DATA_PG_DB=legba \\
      python3 scripts/reference_topup_packet.py --target country_watch_il

  # a specific reference rather than the newest for that target
  PYTHONPATH=src python3 scripts/reference_topup_packet.py \\
      --reference-id 0b7db7fb-… -o /tmp/topup_il.md

  # WHICH COUNTRIES THE FREE LANE CANNOT BUILD AT ALL (R2-FIX(2))
  PYTHONPATH=src python3 scripts/reference_topup_packet.py --candidates

THE CANDIDATES LIST is the other half of the retry fence. A target whose builds
keep committing nothing is flagged ``unbuildable_by_lane`` on the builder's
receipts after three consecutive failures — Argentina, measured, is one: 3 of 16
pages usable to this lane. It is never dropped from the roster and never stops
being retried, because "this lane cannot fetch it" is a statement about today's
fetchers and today's licence classes. What it IS, is the strongest possible
candidate for the paid route this file exists to prepare, and the operator
should not have to read receipts to find that out. ``--candidates`` reads the
same ledger the scheduler reads and names them, with how many times each has
failed and whether there is an existing skeleton to difference against.

Env: ``LEGBA_DATA_PG_HOST`` / ``_PORT`` / ``_USER`` / ``_PASSWORD`` / ``_DB``.
No key is ever printed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
)

from legba.data.analysts.deterministic_handlers import (  # noqa: E402
    _reference_roster as ROSTER,
    _reference_store as STORE,
)
from legba.data.analysts.deterministic_handlers._reference_fences import (  # noqa: E402
    dimension_counts,
)
from legba.data.analysts.deterministic_handlers._reference_instruction import (  # noqa: E402
    build_topup_packet,
)

_LATEST_FOR_TARGET = STORE.LATEST_REFERENCE_SQL.replace(
    "SELECT id, window_start, window_end, built_at, builder, sha256",
    "SELECT id, target_id, window_start, window_end, built_at, builder, "
    "ref_json, thin_dimensions, sha256",
)


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )


def _reference_of(row: Any) -> dict[str, Any]:
    raw = row["ref_json"]
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


async def candidates() -> int:
    """Name the targets the free lane cannot build, from the builder's receipts.

    Reads THE SAME ledger the scheduler orders on
    (``_reference_roster.load_attempts``) rather than a second notion of
    "failing", so the list here and the flag on the receipt can never disagree.
    """
    conn = await _connect()
    try:
        attempts = await ROSTER.load_attempts(
            conn, t0=datetime.now(timezone.utc)
        )
        flagged = ROSTER.unbuildable_targets(attempts)
        have_reference = set()
        if flagged:
            rows = await conn.fetch(
                "SELECT DISTINCT target_id FROM unit_references "
                "WHERE target_id = ANY($1::text[])",
                flagged,
            )
            have_reference = {str(r["target_id"]) for r in rows}
    finally:
        await conn.close()

    if not attempts:
        print("no reference_builder receipts carry an attempt ledger yet.")
        print("Nothing has been attempted, or the builds predate R2-FIX(2).")
        return 0
    if not flagged:
        print(f"no target has failed {ROSTER.UNBUILDABLE_AFTER_FAILURES} "
              f"builds in a row ({len(attempts)} target(s) in the ledger).")
        return 0

    print(f"UNBUILDABLE BY THE FREE LANE ({len(flagged)}):")
    for target_id in flagged:
        record = attempts[target_id]
        skeleton = "has a skeleton" if target_id in have_reference else (
            "NO reference at all"
        )
        print(f"  {target_id}: {record.consecutive_failures} consecutive "
              f"failure(s), last {record.last_status} at "
              f"{record.last_attempt_at.isoformat() if record.last_attempt_at else '—'}"
              f" — {skeleton}")
    print()
    print("Each is still on the roster and still retried at the backoff "
          "cadence; none was dropped.")
    print("With a skeleton:   --target <id>, then load_unit_reference.py "
          "--merge-into <reference_id>.")
    print("With no skeleton:  a top-up is the ONLY route — run the lane from "
          "scratch and load the")
    print("                   result with load_unit_reference.py and NO "
          "--merge-into.")
    return 0


async def run(args: argparse.Namespace) -> int:
    conn = await _connect()
    try:
        if args.reference_id:
            row = await conn.fetchrow(
                STORE.SELECT_REFERENCE_SQL, UUID(args.reference_id)
            )
        else:
            row = await conn.fetchrow(_LATEST_FOR_TARGET, args.target)
        attempts = await ROSTER.load_attempts(
            conn, t0=datetime.now(timezone.utc)
        )
    finally:
        await conn.close()
    flagged = set(ROSTER.unbuildable_targets(attempts))

    if row is None:
        which = args.reference_id or args.target
        hint = ""
        if args.target and args.target in flagged:
            # The case this script used to send the operator in a circle over:
            # "build one first" is exactly what the lane has already tried
            # three times and cannot do.
            hint = (
                f" NOTE: {args.target} is flagged `unbuildable_by_lane` — the "
                "free lane has failed it three or more times running, which is "
                "why there is no skeleton. Do NOT wait for one. Run the top-up "
                "lane from scratch for this country and load the result with "
                "scripts/load_unit_reference.py and NO --merge-into."
            )
        raise SystemExit(
            f"no unit_references row for {which!r}. A top-up packet is a "
            "DIFFERENCE against an existing skeleton; there is nothing to "
            "difference against. Build one first "
            "(scripts/reference_build_now.py --target <id>)." + hint
        )

    reference = _reference_of(row)
    header = reference.get("header") or {}
    developments = reference.get("ref_developments") or []
    bands = reference.get("ref_bands") or {}
    dimensions = sorted(bands) if isinstance(bands, dict) else []
    counts = dimension_counts(developments, dimensions)
    thin = [str(d) for d in (row["thin_dimensions"] or [])]

    window = str(header.get("window") or "")
    window_start, t0 = (
        [p.strip() for p in window.split("->", 1)] if "->" in window
        else (row["window_start"].isoformat(), row["window_end"].isoformat())
    )

    packet = build_topup_packet(
        country_name=str(header.get("country") or row["target_id"]),
        country_code=str(header.get("country_code")
                         or header.get("country") or ""),
        t0=t0,
        window_start=window_start,
        reference_id=str(row["id"]),
        builder=str(row["builder"]),
        dimensions=dimensions,
        established=developments,
        thin=thin,
        bands=bands,
        gaps=[str(g) for g in (reference.get("gaps") or [])],
        counts=counts,
    )

    out = args.out or (
        f"topup_{row['target_id']}_{str(row['id'])[:8]}.md"
    )
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(packet)

    print(f"reference   : {row['id']}")
    print(f"  target    : {row['target_id']}  builder={row['builder']}")
    print(f"  window    : {window_start} -> {t0}")
    print(f"  developments: {len(developments)} over "
          f"{len(dimensions)} banded dimension(s)")
    print(f"  per-dimension: "
          + ", ".join(f"{d}={counts.get(d, 0)}" for d in dimensions))
    print(f"  thin      : {', '.join(thin) or 'none'}")
    standing = attempts.get(str(row["target_id"]))
    if standing is not None and standing.consecutive_failures:
        print(f"  builds    : {standing.consecutive_failures} consecutive "
              f"failure(s), last {standing.last_status}"
              + ("  UNBUILDABLE_BY_LANE" if standing.unbuildable else ""))
    print(f"  packet    : {out} ({len(packet):,} bytes)")
    print()
    print("This packet is an OPERATOR-TRIGGERED lane. Nothing schedules it and")
    print("nothing in the platform will run it. Merge a completed top-up with:")
    print(f"  PYTHONPATH=src python3 scripts/load_unit_reference.py "
          f"<additions.json> --merge-into {row['id']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write the stage-1 style top-up packet for one reference."
    )
    parser.add_argument("--target", default=None,
                        help="country target id; the NEWEST reference for it "
                             "is used")
    parser.add_argument("--reference-id", default=None,
                        help="a specific unit_references id instead")
    parser.add_argument("-o", "--out", default=None,
                        help="output path (default topup_<target>_<id8>.md)")
    parser.add_argument(
        "--candidates", action="store_true",
        help="write no packet; NAME the targets the free lane cannot build "
             "(flagged unbuildable_by_lane on the builder's own receipts after "
             f"{ROSTER.UNBUILDABLE_AFTER_FAILURES} consecutive failures). Those "
             "are the strongest candidates for a paid top-up, and the ones with "
             "no skeleton at all can only be closed that way.",
    )
    args = parser.parse_args()
    if args.candidates:
        if args.target or args.reference_id:
            parser.error("--candidates takes no target")
        return asyncio.run(candidates())
    if not (args.target or args.reference_id):
        parser.error(
            "one of --target, --reference-id or --candidates is required"
        )
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
