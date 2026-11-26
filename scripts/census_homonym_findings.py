#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Homonym census — READ-ONLY, one-off.

For every target whose ``scope.geo`` carries a code the profile gazetteer's
``subject_homonyms`` declares (``src/legba/data/subject_homonyms.py``):

  (1) FINDINGS — every ``analyst_outputs`` finding on the target since
      ``--since`` (default 2026-09-24) whose TITLE names one of the subject's
      homonym forms ("Khyber Pakhtunkhwa" on the North Korea desk), with
      whether the title is homonym-only (names no subject once the forms are
      cut) and how many body sentences are. The orchestrator-verified KP 16
      must appear.
  (2) SLICE — how many canonical signals on the code in the last ``--days``
      the desk's slice now EXCLUDES, counted by running the slice's own
      ``HomonymSliceFilter`` clause (``NOT (<clause>)``) — the SQL the reader
      ships, not a re-spelling — with a few sample titles.

Body sentences are split on sentence punctuation here (a census, not the
critic: the critic runs the shipped verify segmenter).

NEVER writes: one READ ONLY transaction, ``statement_timeout`` 120 s. Run on
the host against the loopback port::

  set -a; . ./.env; set +a
  PYTHONPATH=src LEGBA_DATA_PG_HOST=127.0.0.1 LEGBA_DATA_PG_DB=legba \\
      python3 scripts/census_homonym_findings.py [--since 2026-09-24] [--days 30] [--json-out PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from legba.data.signal_slice_scope import (  # noqa: E402
    CANONICAL_ONLY_SQL,
    HOMONYM_TEXT_SQL,
    homonym_slice_filter,
)
from legba.data.subject_homonyms import rule_for_code  # noqa: E402

_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")

_TARGETS_SQL = """
SELECT descriptor_id, body->'scope'->'geo' AS geo
  FROM target_descriptors
 WHERE is_head AND jsonb_typeof(body->'scope'->'geo') = 'array'
 ORDER BY descriptor_id
"""

_FINDINGS_SQL = """
SELECT id, analyst_id, produced_at, confidence, title, coalesce(body, '') AS body
  FROM analyst_outputs
 WHERE target_id = $1 AND kind = 'finding' AND produced_at >= $2
 ORDER BY produced_at
"""


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=(
            os.environ.get("LEGBA_DATA_PG_PASSWORD")
            or os.environ.get("LEGBA_DATA_TIMESCALE_PASSWORD")
            or None
        ),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )


async def _slice_exclusions(conn: Any, code: str, days: int) -> dict[str, Any]:
    filt = homonym_slice_filter([code])
    params: list[Any] = [[code]]
    clause = filt.sql(params)[0]
    where = (
        f"geo && $1::text[] AND fetched_at > NOW() - INTERVAL '{int(days)} days' "
        f"AND {CANONICAL_ONLY_SQL}"
    )
    total = await conn.fetchval(f"SELECT count(*) FROM signals WHERE {where}", [code])
    rows = await conn.fetch(
        f"SELECT left({HOMONYM_TEXT_SQL}, 140) AS text FROM signals "
        f"WHERE {where} AND NOT {clause} ORDER BY fetched_at DESC",
        *params,
    )
    return {
        "code": code, "days": days, "on_code": total, "excluded": len(rows),
        "samples": [r["text"] for r in rows[:5]],
        "all_excluded": [r["text"] for r in rows],
    }


async def census(since: datetime, days: int) -> dict[str, Any]:
    conn = await _connect()
    try:
        async with conn.transaction(readonly=True):
            await conn.execute("SET LOCAL statement_timeout = '120s'")
            targets = await conn.fetch(_TARGETS_SQL)
            out: dict[str, Any] = {"since": since.isoformat(), "desks": [], "slice": []}
            codes_seen: set[str] = set()
            for t in targets:
                geo = json.loads(t["geo"]) if isinstance(t["geo"], str) else (t["geo"] or [])
                rules = [r for r in (rule_for_code(g) for g in geo) if r is not None]
                if not rules:
                    continue
                desk = {"target_id": t["descriptor_id"], "codes": [r.code for r in rules],
                        "findings_total": 0, "findings": []}
                for f in await conn.fetch(_FINDINGS_SQL, t["descriptor_id"], since):
                    desk["findings_total"] += 1
                    title = f["title"] or ""
                    for r in rules:
                        forms = r.homonym_forms_in(title)
                        if not forms:
                            continue
                        body_off = [s for s in _SENTENCES.split(f["body"]) if s.strip()
                                    and r.homonym_only(s)]
                        desk["findings"].append({
                            "id": str(f["id"]), "analyst_id": f["analyst_id"],
                            "produced_at": f["produced_at"].isoformat(),
                            "confidence": f["confidence"], "code": r.code,
                            "forms": forms, "title_homonym_only": r.homonym_only(title),
                            "body_off_subject_sentences": len(body_off), "title": title,
                        })
                        break
                out["desks"].append(desk)
                for r in rules:
                    if r.geo_code not in codes_seen:
                        codes_seen.add(r.geo_code)
                        out["slice"].append(await _slice_exclusions(conn, r.geo_code, days))
            return out
    finally:
        await conn.close()


def _print(report: dict[str, Any]) -> None:
    print(f"HOMONYM CENSUS — findings since {report['since']}")
    for d in report["desks"]:
        print(f"\n{d['target_id']} (codes {','.join(d['codes'])}): "
              f"{len(d['findings'])} of {d['findings_total']} findings title a homonym form")
        for f in d["findings"]:
            print(f"  {f['produced_at'][:10]} {f['id']} {f['analyst_id']:<24} "
                  f"conf={f['confidence']} only={f['title_homonym_only']} "
                  f"body_off={f['body_off_subject_sentences']} | {f['title'][:100]}")
    print("\nSLICE — signals on the code the desk's slice now excludes")
    for s in report["slice"]:
        print(f"  {s['code']}: {s['excluded']} of {s['on_code']} in {s['days']} d")
        for t in s["samples"]:
            print(f"      - {t}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", default="2026-09-24", help="findings produced_at >= (UTC date)")
    ap.add_argument("--days", type=int, default=30, help="slice window for the signal count")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)
    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    report = asyncio.run(census(since, args.days))
    _print(report)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
