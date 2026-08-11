#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Replay the K-4 R3 gold labels through the CW precision train's NEW logic.

THE PRE-REGISTERED ACCEPTANCE TEST for the CW train. K-4 round 3 labeled 120
(signal, open-question) pairs sampled off the LIVE gated stream and measured
the population-weighted precision of what ``claim_watch/3.3.0`` wrote at
**0.267** (planning/K4_LABELS_R3.csv, planning/K4_R3_README.md). This script
re-runs those same 120 rows through the guards 4.0.0 added and reports what
each one does to precision AND to true-positive retention, per stratum and per
failure class.

The bar, declared before the code was written:

    population-weighted stream precision >= 0.60
      AND >= 60% retention of the correct_match rows

WHAT IS AND IS NOT REPLAYABLE — the honesty that makes the number mean
something
----------------------------------------------------------------------
Replayed EXACTLY (deterministic, no model in the loop):

  * CW-1, the blocking confirm leg — the labels carry each row's RECORDED
    ``bearing_confirm`` stamp, so "would this row survive a blocking confirm"
    is a lookup, not a simulation.
  * CW-3, the deictic guard — a pure function of the stored thesis.
  * CW-4, contention liveness — needs the substrate; ``--db`` runs the real
    query, otherwise the filter is treated as passing everything.
  * CW-5, the subject anchor — a pure function of the subject and the signal
    text. ``--db`` additionally supplies the signal's canonical entity names,
    which is what production sees; without it only the worksheet's title +
    (truncated) summary are available and the anchor reads stricter than
    production. Run with ``--db`` for the real number.

NOT replayed, and taking NO credit:

  * CW-2, desk identity in the gate + confirm prompts. A prompt change moves
    what the MODEL says; the labels carry what the OLD prompt made it say.
    Re-running the 8B and the 120B over 120 pairs would produce a number this
    file could not distinguish from a fresh measurement, so the honest move is
    to score CW-2 as a no-op here and let the next K-4 round measure it. Every
    number below is therefore a LOWER BOUND on the shipped pipeline.

Usage::

    PYTHONPATH=src python3 scripts/replay_k4_r3.py            # offline
    PYTHONPATH=src python3 scripts/replay_k4_r3.py --db       # full fidelity

Read-only throughout: it opens the labeled CSVs for reading and, with
``--db``, issues SELECTs. It writes nothing anywhere.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from legba.data.analysts.deterministic_handlers.claim_watch_guards import (  # noqa: E402
    DEFAULT_CONTENTION_LIVENESS_DAYS,
    anchor_text,
    contention_key,
    live_contention_ids,
    subject_anchored,
)
from legba.data.analysts.question_text import is_deictic  # noqa: E402

DEFAULT_WORKSHEET = REPO / "planning" / "K4_R3_WORKSHEET_LABELED.csv"

#: The R3 population, from planning/K4_R3_README.md §2a. The SAMPLE
#: over-represents confirm_yes 47.5% vs 28.0% by design, so an unweighted
#: pooled number off this sheet is a sampler artifact (the README calls it a
#: trap, and the 0.400 it produces is 1.5x the true 0.267). Every headline
#: figure below is weighted back to these shares.
POPULATION = {"confirm_yes": 119, "confirm_no": 278, "confirm_absent": 28}
POPULATION_TOTAL = sum(POPULATION.values())

#: The pre-registered bar. Not derived from anything computed here.
PRECISION_BAR = 0.60
RETENTION_BAR = 0.60


@dataclass
class Row:
    pair_id: str
    stratum: str
    desk: str
    thesis: str
    signal_title: str
    signal_summary: str
    signal_id: str
    confirm: str
    label: str
    #: Filled by --db: the signal's resolved canonical entity names, i.e. what
    #: the production matcher's anchor actually compares against.
    entity_names: set[str] = field(default_factory=set)

    @property
    def correct(self) -> bool:
        return self.label == "correct_match"

    @property
    def text(self) -> str:
        return anchor_text(
            {"title": self.signal_title, "summary": self.signal_summary}
        )


def load(worksheet: Path) -> list[Row]:
    raw = [ln for ln in worksheet.read_text().splitlines(True)
           if not ln.startswith("#")]
    out: list[Row] = []
    for r in csv.DictReader(io.StringIO("".join(raw))):
        out.append(
            Row(
                pair_id=r["pair_id"],
                stratum=r["sample_stratum"],
                desk=r["desk"],
                thesis=r["question_thesis"],
                signal_title=r["signal_title"],
                signal_summary=r["signal_summary"],
                signal_id=r["signal_id"],
                confirm=(r["bearing_confirm"] or "").strip(),
                label=r["label"],
            )
        )
    return out


# ---------------------------------------------------------------------------
# The metric. Population-weighted, because the sample is not the population.
# ---------------------------------------------------------------------------


def weighted(rows: Iterable[Row], kept: Iterable[Row]) -> tuple[float, float, float]:
    """(precision, tp_retention, volume_share) of ``kept`` within ``rows``.

    Each stratum contributes its POPULATION share, not its sample share. All
    three come out of one pass so they are always mutually consistent.
    """
    rows, kept = list(rows), list(kept)
    kept_ids = {r.pair_id for r in kept}
    kept_mass = tp_mass = total_tp_mass = 0.0
    for stratum, pop in POPULATION.items():
        sample = [r for r in rows if r.stratum == stratum]
        if not sample:
            continue
        w = (pop / POPULATION_TOTAL) / len(sample)
        total_tp_mass += w * sum(1 for r in sample if r.correct)
        for r in sample:
            if r.pair_id not in kept_ids:
                continue
            kept_mass += w
            if r.correct:
                tp_mass += w
    precision = tp_mass / kept_mass if kept_mass else 0.0
    retention = tp_mass / total_tp_mass if total_tp_mass else 0.0
    return precision, retention, kept_mass


# ---------------------------------------------------------------------------
# The guards, as REPLAYABLE predicates
# ---------------------------------------------------------------------------


def cw1_blocked(row: Row) -> bool:
    """CW-1: a recorded confirm-NO is dropped. Absent bands are written."""
    return row.confirm == "no"


def cw3_blocked(row: Row) -> bool:
    """CW-3: a still-deictic thesis is not matched."""
    return is_deictic(row.thesis)


def cw5_blocked(row: Row) -> bool:
    """CW-5: a contention pair whose SUBJECT is absent from the signal."""
    key = contention_key(row.thesis)
    if key is None:
        return False
    return not subject_anchored(
        key.subject, signal_text=row.text, signal_names=row.entity_names
    )


async def enrich_from_db(rows: list[Row], liveness_days: float) -> set[str]:
    """Fill in what only the substrate knows; return the CW-4-blocked ids.

    Read-only. Two reads: each signal's resolved canonical entity names (the
    anchor surface production compares against) and the contention-group
    liveness for the questions in the sheet.
    """
    import asyncpg

    conn = await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )
    try:
        names = await conn.fetch(
            """
            SELECT sel.signal_id::text AS sid,
                   array_agg(DISTINCT lower(btrim(ep2.canonical_name))) AS names
              FROM signal_entity_links sel
              LEFT JOIN entity_profiles ep ON ep.id = sel.entity_id
              LEFT JOIN entity_profiles ep2
                     ON ep2.id = COALESCE(ep.merged_into, sel.entity_id)
             WHERE sel.signal_id = ANY($1::uuid[])
             GROUP BY sel.signal_id
            """,
            list({r.signal_id for r in rows}),
        )
        by_sid = {r["sid"]: {n for n in (r["names"] or []) if n} for r in names}
        for row in rows:
            row.entity_names = by_sid.get(row.signal_id, set())

        # CW-4: map each contention thesis back to its live group. The sheet
        # has no group id, so match on (subject, predicate) — the arbiter's
        # unique key.
        keys = {
            (k.subject, k.predicate)
            for k in (contention_key(r.thesis) for r in rows)
            if k is not None
        }
        if not keys:
            return set()
        # Two parallel arrays rather than a record[]: PostgreSQL has no
        # anonymous-composite INPUT, so asyncpg cannot bind a list of tuples.
        groups = await conn.fetch(
            "SELECT id::text AS id, subject_key, predicate_key "
            "  FROM fact_contention "
            " WHERE (subject_key, predicate_key) IN ("
            "   SELECT unnest($1::text[]), unnest($2::text[]))",
            [k[0] for k in keys],
            [k[1] for k in keys],
        )
        gid_of = {(g["subject_key"], g["predicate_key"]): g["id"] for g in groups}
        live = await live_contention_ids(
            conn, set(gid_of.values()), liveness_days=liveness_days
        )
        blocked: set[str] = set()
        for row in rows:
            key = contention_key(row.thesis)
            if key is None:
                continue
            gid = gid_of.get((key.subject, key.predicate))
            # A group that no longer exists was COLLAPSED or garbage-collected
            # — the dispute is over, so the question is not watchable.
            if gid is None or gid not in live:
                blocked.add(row.pair_id)
        return blocked
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _line(name: str, rows: list[Row], kept: list[Row]) -> str:
    p, ret, vol = weighted(rows, kept)
    n_tp = sum(1 for r in kept if r.correct)
    return (
        f"  {name:<34s} kept={len(kept):3d}/{len(rows):3d} "
        f"tp={n_tp:3d}/{sum(1 for r in rows if r.correct):3d}  "
        f"precision={p:.3f}  tp_retained={ret:5.1%}  volume={vol:5.1%}"
    )


def report(rows: list[Row], cw4_blocked: set[str], *, db: bool) -> bool:
    print("K-4 R3 REPLAY — the CW precision train against the 120 gold labels")
    print(f"  worksheet rows : {len(rows)}   correct_match: "
          f"{sum(1 for r in rows if r.correct)}")
    surface = (
        "live entity links (production)" if db
        else "worksheet text only (stricter than production)"
    )
    print(f"  anchor surface : {surface}")
    print()

    guards = [
        ("CW-1 confirm blocking", cw1_blocked),
        ("CW-3 deictic guard", cw3_blocked),
        ("CW-5 contention subject anchor", cw5_blocked),
    ]

    print("BASELINE (what claim_watch/3.3.0 wrote)")
    print(_line("3.3.0 gated stream", rows, rows))
    print()

    print("EACH GUARD ALONE")
    for name, pred in guards:
        print(_line(name, rows, [r for r in rows if not pred(r)]))
    print(_line(
        "CW-4 contention liveness",
        rows,
        [r for r in rows if r.pair_id not in cw4_blocked],
    ))
    print()

    print("CUMULATIVE (the shipped 4.0.0 pipeline, CW-2 scored as a no-op)")
    kept = list(rows)
    for name, pred in guards:
        kept = [r for r in kept if not pred(r)]
        print(_line(f"+ {name}", rows, kept))
    kept = [r for r in kept if r.pair_id not in cw4_blocked]
    print(_line("+ CW-4 contention liveness", rows, kept))
    print()

    print("BY STRATUM (population share -> what survives)")
    for stratum, pop in POPULATION.items():
        s_rows = [r for r in rows if r.stratum == stratum]
        s_kept = [r for r in kept if r.stratum == stratum]
        base = sum(1 for r in s_rows if r.correct) / len(s_rows) if s_rows else 0
        now = sum(1 for r in s_kept if r.correct) / len(s_kept) if s_kept else 0
        print(
            f"  {stratum:<16s} pop={pop / POPULATION_TOTAL:5.1%}  "
            f"before {len(s_rows):3d} rows @ {base:.3f}   "
            f"after {len(s_kept):3d} rows @ {now:.3f}"
        )
    print()

    print("BY QUESTION CLASS")
    def cls(r: Row) -> str:
        return "fact_contention" if contention_key(r.thesis) else "open_question"
    for name in ("fact_contention", "open_question"):
        c_rows = [r for r in rows if cls(r) == name]
        c_kept = [r for r in kept if cls(r) == name]
        base = sum(1 for r in c_rows if r.correct) / len(c_rows) if c_rows else 0
        now = sum(1 for r in c_kept if r.correct) / len(c_kept) if c_kept else 0
        print(
            f"  {name:<16s} before {len(c_rows):3d} rows @ {base:.3f}   "
            f"after {len(c_kept):3d} rows @ {now:.3f}"
        )
    print()

    print("WATCH-HIT STREAM (data.bearing_watch='confirmed' only — what a "
          "K-5 closer would act on)")
    confirmed = [r for r in kept if r.confirm == "yes"]
    print(_line("confirmed band", rows, confirmed))
    print()

    precision, retention, _ = weighted(rows, kept)
    ok = precision >= PRECISION_BAR and retention >= RETENTION_BAR
    print("PRE-REGISTERED BAR")
    print(f"  precision      {precision:.3f}  >= {PRECISION_BAR:.2f}  "
          f"{'PASS' if precision >= PRECISION_BAR else 'FAIL'}")
    print(f"  tp_retention   {retention:.3f}  >= {RETENTION_BAR:.2f}  "
          f"{'PASS' if retention >= RETENTION_BAR else 'FAIL'}")
    print(f"  => {'MET' if ok else 'NOT MET'}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--worksheet", type=Path, default=DEFAULT_WORKSHEET)
    ap.add_argument(
        "--db", action="store_true",
        help="enrich from the live substrate (READ-ONLY): signal entity links "
             "for the anchor, contention groups for liveness",
    )
    ap.add_argument(
        "--liveness-days", type=float, default=DEFAULT_CONTENTION_LIVENESS_DAYS
    )
    args = ap.parse_args()

    if not args.worksheet.exists():
        print(f"worksheet not found: {args.worksheet}", file=sys.stderr)
        print("(planning/ is gitignored — this file is evidence, not code)",
              file=sys.stderr)
        return 2

    rows = load(args.worksheet)
    cw4_blocked: set[str] = set()
    if args.db:
        cw4_blocked = asyncio.run(enrich_from_db(rows, args.liveness_days))
    return 0 if report(rows, cw4_blocked, db=args.db) else 1


if __name__ == "__main__":
    raise SystemExit(main())
