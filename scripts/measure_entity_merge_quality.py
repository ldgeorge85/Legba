#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""measure_entity_merge_quality.py — E4a before/after measurement (#223).

Scores the entity-merge program (P4's one-shot migration + the E-program's
ongoing ``entity_researcher`` LLM merges) against a HUMAN-LABELED sample, using
the pairwise + B-cubed metrics already defined in
:mod:`legba.data._entity_eval` (``pairwise_prf`` / ``bcubed`` — see that
module's docstring for the metric definitions; this script does not
reimplement them, it only samples + drives them).

WHY A TWO-STEP CLI (no ground truth existed to score against — verified by
grepping planning/ and tests/ for any prior E4a label set; none found):

  1. ``sample``  — draws a STRATIFIED, READ-ONLY sample from the live
     entity_profiles graph and writes a labeling WORKSHEET (CSV). A human
     reviewer fills one column (``gold_cluster``) per row.
  2. ``score``   — reads the FILLED worksheet back, reconstructs the
     predicted clustering (what the system currently believes: survivor +
     its folded aliases = one cluster, a tombstone resolves to its terminal
     survivor's cluster, a singleton is its own cluster) and the gold
     clustering (from the human's ``gold_cluster`` column), and reports
     pairwise + B-cubed precision/recall/F1.

SAMPLING STRATA (stratified, not uniform — a uniform sample over the whole
graph would be almost all singletons and rarely surface a merge decision):
  * ``cluster``   — a real merge survivor (``merged_aliases`` non-empty),
    across small (2), medium (3-6), and large (7+) fold sizes so the sample
    is not dominated by the many small clusters nor blind to the rare giant
    ones (a 30-alias cluster exists live — the Khamenei-30 case the E4
    program was built to fix).
  * ``singleton`` — an active keeper with NO folded alias — the negative
    class (did the system correctly NOT fold this into anything).
  * ``verdict``   — an existing ``entity_judgement`` row (an LLM adjudication
    already made), sampled across ``same`` / ``not_same`` / ``unsure`` bands.
    This is the highest-value stratum: it puts a human confirmation directly
    on the adjudicator's OWN calls (not just the graph's current shape), so a
    systematic bias (e.g. over-confident 'same' on a name collision) is
    directly visible rather than averaged away by singleton volume.
  * ``tombstone`` — a merged-away row, to confirm it still resolves to a
    sensible terminal survivor (a redirect-chain sanity check).

SAFETY: ``sample`` opens its connection with ``default_transaction_read_only
= on`` at the Postgres SESSION level (server-enforced — verified live: a
stray write inside this script would raise ``ReadOnlySQLTransactionError``,
not silently succeed) IN ADDITION to only ever issuing ``SELECT`` statements.
No migration, no schema change, no write path. ``score`` touches no DB at all
(it only reads the worksheet CSV + calls the pure ``_entity_eval`` functions).

Connection: ``PostgresConfig.from_env()`` (the runtime's LEGBA_* env; defaults
to the live local instance, same convention as ``scripts/export_substrate.py``).

USAGE
-----
    # 1. Draw the worksheet (read-only; writes ONLY the local CSV file).
    python3 scripts/measure_entity_merge_quality.py sample \\
        --out /tmp/entity_merge_worksheet.csv --per-stratum 25

    # 2. A human fills the `gold_cluster` column (see the worksheet's own
    #    header comment for the labeling convention), then:
    python3 scripts/measure_entity_merge_quality.py score \\
        --worksheet /tmp/entity_merge_worksheet.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv optional
    pass

from legba.data._entity_eval import BCubedScore, PairwiseScore, bcubed, pairwise_prf
from legba.data.config import PostgresConfig

WORKSHEET_SCHEMA_VERSION = "legba/entity-merge-worksheet/1-0-0"

#: Worksheet columns, IN ORDER. `gold_cluster` is the ONLY column a human
#: labeler fills in; every other column is sampler-populated context. Two rows
#: sharing the SAME non-empty `gold_cluster` value are judged the SAME
#: real-world referent by the labeler (any string works as a label — a name,
#: a number, "SAME AS row 4" is fine as long as it is applied consistently
#: within one worksheet). Leaving `gold_cluster` BLANK means "I could not
#: judge this row" — score.py drops blank rows rather than guessing.
WORKSHEET_FIELDS = [
    "row_id", "stratum", "entity_id", "canonical_name", "entity_class",
    "predicted_cluster", "context", "gold_cluster", "labeler_note",
]

_HEADER_COMMENT = (
    "# {schema}\n"
    "# LABELING INSTRUCTIONS: fill `gold_cluster` for each row you can judge.\n"
    "# Two rows that denote the SAME real-world entity get the SAME "
    "gold_cluster value\n"
    "# (any consistent string: a name, 'A', '1' - it only has to match "
    "within this file).\n"
    "# A row that is its own distinct entity (no other row in this file "
    "denotes it) still\n"
    "# needs a gold_cluster value - just one no other row shares. Leave "
    "gold_cluster BLANK\n"
    "# only when you genuinely cannot judge the row (score.py drops blank "
    "rows rather\n"
    "# than guessing). `predicted_cluster` and `context` are sampler-"
    "populated - read\n"
    "# them, do not edit them.\n"
)


# ===========================================================================
# PURE helpers (no DB, no I/O) - independently testable with a tiny fixture.
# ===========================================================================


@dataclass(frozen=True)
class WorksheetRow:
    row_id: str
    stratum: str
    entity_id: str
    canonical_name: str
    entity_class: str
    predicted_cluster: str
    context: str = ""
    gold_cluster: str = ""
    labeler_note: str = ""

    def to_csv_dict(self) -> dict[str, str]:
        return {k: getattr(self, k) for k in WORKSHEET_FIELDS}


def rows_to_predicted_clusters(rows: list[WorksheetRow]) -> dict[str, str]:
    """``{entity_id: predicted_cluster}`` over ``rows`` — the PURE reduction
    ``score`` needs; kept separate from CSV parsing so it is directly
    fixture-testable."""
    return {r.entity_id: r.predicted_cluster for r in rows}


def rows_to_gold_clusters(rows: list[WorksheetRow]) -> dict[str, str]:
    """``{entity_id: gold_cluster}`` over rows with a NON-BLANK gold_cluster.
    A blank gold_cluster means "unjudged" and the row is DROPPED (never
    coerced to a guess) - both :func:`legba.data._entity_eval.bcubed` and
    :func:`legba.data._entity_eval.pairwise_prf` (via
    :func:`legba.data._entity_eval.clusters_to_pairs`) already only score the
    intersection of the two id sets, so a dropped row simply narrows that
    intersection rather than corrupting the comparison."""
    return {
        r.entity_id: r.gold_cluster.strip()
        for r in rows
        if r.gold_cluster and r.gold_cluster.strip()
    }


def score_worksheet_rows(
    rows: list[WorksheetRow],
) -> tuple[PairwiseScore, BCubedScore, int, int]:
    """Score a filled worksheet. Returns ``(pairwise, bcubed, n_labeled,
    n_total)``. Pure - the entire live-scoring path is a thin wrapper around
    this (``score`` reads the CSV, calls this, prints the result)."""
    from legba.data._entity_eval import clusters_to_pairs

    predicted = rows_to_predicted_clusters(rows)
    gold = rows_to_gold_clusters(rows)
    # Score only the labeled subset (bcubed/pairwise already intersect on
    # their own, but scoping `predicted` here keeps clusters_to_pairs from
    # emitting a same-cluster pair between a LABELED and an UNLABELED row).
    predicted_labeled = {k: v for k, v in predicted.items() if k in gold}
    pw = pairwise_prf(
        clusters_to_pairs(predicted_labeled), clusters_to_pairs(gold))
    bc = bcubed(predicted_labeled, gold)
    return pw, bc, len(gold), len(rows)


# ===========================================================================
# SAMPLING (I/O - live DB, read-only session).
# ===========================================================================

#: Fold-size bands for the `cluster` stratum (inclusive alias-count ranges).
_CLUSTER_SIZE_BANDS: tuple[tuple[str, int, int], ...] = (
    ("small", 2, 2), ("medium", 3, 6), ("large", 7, 10_000),
)

_VERDICT_BANDS: tuple[str, ...] = ("same", "not_same", "unsure")


def _seed_to_pg_setseed(seed: int) -> float:
    """Map an integer CLI seed to the ``[-1.0, 1.0)`` float
    ``SELECT setseed(x)`` requires — pure, so it is fixture-testable without
    a DB. Deterministic + total (works for negative/huge/zero seeds): Python's
    ``%`` on a negative operand returns a NON-negative result, which is what
    keeps this in range for a negative seed too."""
    return ((seed % 2_000_000) / 1_000_000.0) - 1.0


async def _open_readonly_conn(cfg: PostgresConfig, *, seed: int | None = None):
    """Connect with the Postgres SESSION set read-only (server-enforced - a
    stray write anywhere in this module raises ``ReadOnlySQLTransactionError``
    rather than silently succeeding; verified live before this script was
    written). This is IN ADDITION to only ever issuing SELECTs below - belt
    and suspenders, not a substitute for reviewing the queries.

    ``seed``, when given, calls Postgres ``setseed()`` on the SESSION so every
    ``ORDER BY random()`` in every query issued over this connection becomes
    deterministic for its lifetime (verified live: two back-to-back
    ``setseed(x); ORDER BY random()`` calls return byte-identical rows). This
    is a session-level effect, not a per-query one - each of the 4 sampling
    functions sharing this connection draws from the SAME seeded stream, so
    re-running ``sample`` with the same ``--seed`` reproduces the same
    worksheet (needed for a stable before/after comparison). ``setseed()``
    takes a float in [-1, 1]; an integer CLI seed is mapped in via a fixed,
    deterministic transform (not cryptographic - this is sampling
    reproducibility, not a security boundary)."""
    import asyncpg

    conn = await asyncpg.connect(
        host=cfg.host, port=cfg.port, user=cfg.user,
        password=cfg.password, database=cfg.database,
        server_settings={"default_transaction_read_only": "on"},
    )
    if seed is not None:
        await conn.execute("SELECT setseed($1)", _seed_to_pg_setseed(seed))
    return conn


async def _sample_clusters(conn, per_band: int) -> list[WorksheetRow]:
    """Sample survivor rows with a folded alias set, across small/medium/large
    band, each row's `context` carrying its full alias list so a labeler can
    judge the fold WITHOUT a second query.

    IMPORTANT (found in review): most `merged_aliases` entries are write-time
    surface-form folds (E1 canonicalization matched a new mention's spelling
    variant onto the existing keeper) - they are STRINGS, not a second
    `entity_profiles` row, so they carry no separate `entity_id` to pair
    against for scoring. Only a MINORITY of survivors also have real
    tombstone rows (`merged_into` pointing at them, from an E4c LLM merge) -
    313 live, verified via `JOIN entity_profiles t ON t.merged_into = s.id`.
    For those, THIS function also emits the tombstone row sharing the
    survivor's `predicted_cluster`, so the `cluster` stratum can actually
    contribute a scoreable pair (without this, every row in this stratum
    predicted its own singleton cluster, and the ~3/8 of the worksheet this
    stratum represents contributed NOTHING to pairwise tp/fp/fn - confirmed
    empirically before this fix). A survivor with no real tombstone still
    gets its own row (context alone remains useful for a labeler judging the
    fold identity), it just cannot form a pair under THIS stratum."""
    out: list[WorksheetRow] = []
    for band_name, lo, hi in _CLUSTER_SIZE_BANDS:
        rows = await conn.fetch(
            """
            SELECT id, canonical_name, entity_class,
                   COALESCE(data->'merged_aliases', '[]'::jsonb) AS aliases
              FROM entity_profiles
             WHERE merged_into IS NULL
               AND jsonb_array_length(COALESCE(data->'merged_aliases','[]'::jsonb))
                   BETWEEN $1 AND $2
             ORDER BY random()
             LIMIT $3
            """,
            lo, hi, per_band,
        )
        for r in rows:
            aliases = r["aliases"]
            if isinstance(aliases, str):
                aliases = json.loads(aliases)
            eid = str(r["id"])
            survivor_name = str(r["canonical_name"] or "")
            predicted = f"survivor:{eid}"
            out.append(WorksheetRow(
                row_id=f"cluster_{band_name}_{eid[:8]}",
                stratum=f"cluster_{band_name}",
                entity_id=eid,
                canonical_name=survivor_name,
                entity_class=str(r["entity_class"] or ""),
                predicted_cluster=predicted,
                context=f"folded aliases ({len(aliases)}): "
                        + " | ".join(str(a) for a in aliases[:15]),
            ))
            # A REAL tombstone (a second entity_profiles row, distinct from
            # the write-time alias strings above) - include it so this
            # stratum can form a pair. At most one per survivor (keeps the
            # worksheet size close to the documented per_band*bands*2 bound).
            tomb = await conn.fetchrow(
                "SELECT id, canonical_name, entity_class FROM entity_profiles "
                "WHERE merged_into = $1::uuid ORDER BY random() LIMIT 1",
                eid,
            )
            if tomb is not None:
                t_id = str(tomb["id"])
                out.append(WorksheetRow(
                    row_id=f"cluster_{band_name}_{eid[:8]}_tomb",
                    stratum=f"cluster_{band_name}",
                    entity_id=t_id,
                    canonical_name=str(tomb["canonical_name"] or ""),
                    entity_class=str(tomb["entity_class"] or ""),
                    predicted_cluster=predicted,  # SAME cluster as its survivor
                    context=f"tombstone -> resolves to survivor {survivor_name!r} ({eid[:8]})",
                ))
    return out


async def _sample_singletons(conn, n: int) -> list[WorksheetRow]:
    """Sample active keepers with NO folded alias - the negative class."""
    rows = await conn.fetch(
        """
        SELECT id, canonical_name, entity_class
          FROM entity_profiles
         WHERE merged_into IS NULL
           AND jsonb_array_length(COALESCE(data->'merged_aliases','[]'::jsonb)) = 0
           AND COALESCE(data->>'gc_status', '') NOT IN ('merged', 'junk')
         ORDER BY random()
         LIMIT $1
        """,
        n,
    )
    out: list[WorksheetRow] = []
    for r in rows:
        eid = str(r["id"])
        out.append(WorksheetRow(
            row_id=f"singleton_{eid[:8]}",
            stratum="singleton",
            entity_id=eid,
            canonical_name=str(r["canonical_name"] or ""),
            entity_class=str(r["entity_class"] or ""),
            predicted_cluster=f"singleton:{eid}",  # unique -> its own cluster
            context="no folded alias (system predicts: distinct entity)",
        ))
    return out


async def _sample_verdicts(conn, per_band: int) -> list[WorksheetRow]:
    """Sample existing entity_judgement rows across same/not_same/unsure,
    surfacing BOTH names + classes so a labeler can confirm the adjudicator's
    OWN calls directly. Both sides of a 'same' verdict get the SAME
    predicted_cluster (so a correct 'same' verdict pairs correctly under
    scoring); a 'not_same'/'unsure' pair gets two DISTINCT predicted clusters
    (the system currently keeps them apart)."""
    out: list[WorksheetRow] = []
    for band in _VERDICT_BANDS:
        rows = await conn.fetch(
            """
            SELECT ej.pair_key, ej.verdict, ej.confidence, ej.entity_a, ej.entity_b,
                   a.canonical_name AS a_name, a.entity_class AS a_cls,
                   b.canonical_name AS b_name, b.entity_class AS b_cls
              FROM entity_judgement ej
              LEFT JOIN entity_profiles a ON a.id = ej.entity_a
              LEFT JOIN entity_profiles b ON b.id = ej.entity_b
             WHERE ej.verdict = $1
               AND ej.entity_a IS NOT NULL AND ej.entity_b IS NOT NULL
               AND a.canonical_name IS NOT NULL AND b.canonical_name IS NOT NULL
             ORDER BY random()
             LIMIT $2
            """,
            band, per_band,
        )
        for r in rows:
            # SCORING-CRITICAL: predicted_cluster must be built from the FULL
            # pair_key, never a truncated prefix — live-verified collisions
            # exist at 12 chars (multiple distinct entity_judgement pairs
            # share a 12-char pair_key prefix), which would have silently
            # fused two unrelated adjudications into one predicted cluster
            # and corrupted pairwise/B-cubed scoring with no error surfaced.
            # `pk_short` is ONLY for the human-facing row_id (a display
            # collision there is harmless — each row still carries its own
            # correct entity_id/predicted_cluster/context).
            pk_full = str(r["pair_key"])
            pk_short = pk_full[:12]
            a_id, b_id = str(r["entity_a"]), str(r["entity_b"])
            # 'same' -> shared predicted cluster keyed on the FULL pair; otherwise
            # each side keeps its own id as its (distinct) predicted cluster.
            a_pred = f"verdict_same:{pk_full}" if band == "same" else f"singleton:{a_id}"
            b_pred = f"verdict_same:{pk_full}" if band == "same" else f"singleton:{b_id}"
            ctx = (f"entity_judgement verdict={r['verdict']} "
                   f"confidence={float(r['confidence'] or 0.0):.2f}")
            out.append(WorksheetRow(
                row_id=f"verdict_{band}_{pk_short}_a", stratum=f"verdict_{band}",
                entity_id=a_id, canonical_name=str(r["a_name"] or ""),
                entity_class=str(r["a_cls"] or ""), predicted_cluster=a_pred,
                context=ctx + f" | paired with: {r['b_name']!r}",
            ))
            out.append(WorksheetRow(
                row_id=f"verdict_{band}_{pk_short}_b", stratum=f"verdict_{band}",
                entity_id=b_id, canonical_name=str(r["b_name"] or ""),
                entity_class=str(r["b_cls"] or ""), predicted_cluster=b_pred,
                context=ctx + f" | paired with: {r['a_name']!r}",
            ))
    return out


async def _sample_tombstones(conn, n: int) -> list[WorksheetRow]:
    """Sample merged-away rows; predicted_cluster is the RESOLVED terminal
    survivor's id (via resolve_entity), so a labeler is judging "does this
    tombstone truly belong with its terminal survivor" - a redirect-chain
    sanity check distinct from the `cluster` stratum (which samples the
    survivor's OWN view of its folded aliases)."""
    rows = await conn.fetch(
        """
        SELECT t.id, t.canonical_name, t.entity_class,
               resolve_entity(t.id) AS survivor_id,
               s.canonical_name AS survivor_name
          FROM entity_profiles t
          JOIN entity_profiles s ON s.id = resolve_entity(t.id)
         WHERE t.merged_into IS NOT NULL
         ORDER BY random()
         LIMIT $1
        """,
        n,
    )
    out: list[WorksheetRow] = []
    for r in rows:
        eid = str(r["id"])
        survivor_id = str(r["survivor_id"])
        out.append(WorksheetRow(
            row_id=f"tombstone_{eid[:8]}", stratum="tombstone",
            entity_id=eid, canonical_name=str(r["canonical_name"] or ""),
            entity_class=str(r["entity_class"] or ""),
            predicted_cluster=f"survivor:{survivor_id}",
            context=f"resolves to survivor: {r['survivor_name']!r} ({survivor_id[:8]})",
        ))
    return out


async def build_worksheet(
    cfg: PostgresConfig, *, per_stratum: int, seed: int | None = None,
) -> list[WorksheetRow]:
    """Draw the full stratified sample (all 4 strata) via ONE read-only
    connection. ``per_stratum`` is the target row count PER SUB-BAND (e.g. for
    `cluster` it is per size band, for `verdict` it is per verdict band) -
    the total worksheet size is therefore roughly ``per_stratum * (3 cluster
    bands + 1 singleton + 3 verdict bands + 1 tombstone) ≈ per_stratum * 8``,
    PLUS up to one extra tombstone-pair row per sampled cluster survivor that
    has a real tombstone (see :func:`_sample_clusters` - a live-observed
    ~10-15% of survivors do), though a thin band (e.g. few `large` clusters
    exist) may return fewer rows than requested.

    ``seed``, when given, is applied via Postgres ``setseed()`` on the shared
    connection (see :func:`_open_readonly_conn`) so the SQL-side ``ORDER BY
    random()`` in every sampling query becomes reproducible; ``seed=None``
    (the default) draws a fresh, non-reproducible sample each run - which is
    also a deliberate choice for repeated `sample` calls over time, since the
    live graph itself changes between runs."""
    conn = await _open_readonly_conn(cfg, seed=seed)
    try:
        rows: list[WorksheetRow] = []
        rows += await _sample_clusters(conn, per_stratum)
        rows += await _sample_singletons(conn, per_stratum)
        rows += await _sample_verdicts(conn, per_stratum)
        rows += await _sample_tombstones(conn, per_stratum)
        return rows
    finally:
        await conn.close()


#: Leading characters Excel/Google Sheets interpret as a FORMULA trigger on
#: open (CWE-1236, "CSV/Excel formula injection"). canonical_name/context come
#: from arbitrary NER-extracted, ingested news text (not a trusted operator
#: input) and this worksheet's whole purpose is being opened by a human in a
#: spreadsheet app to fill gold_cluster — so a garbled live entity name
#: starting with one of these must not silently become a live formula.
_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@")
#: A single leading apostrophe is the standard neutralizer both Excel and
#: Sheets honor (forces text interpretation, invisible in the rendered cell) —
#: reversed on read by :func:`_strip_formula_guard` so a round-trip through
#: write_worksheet/read_worksheet is lossless for the ORIGINAL value.
_FORMULA_GUARD_PREFIX = "'"


def _apply_formula_guard(value: str) -> str:
    """Prefix ``value`` with :data:`_FORMULA_GUARD_PREFIX` when it starts with
    a spreadsheet formula trigger char. Pure — no I/O — so it is directly
    fixture-testable without writing a file."""
    if value and value[0] in _FORMULA_TRIGGER_CHARS:
        return _FORMULA_GUARD_PREFIX + value
    return value


def _strip_formula_guard(value: str) -> str:
    """Reverse :func:`_apply_formula_guard` — strip exactly ONE leading guard
    apostrophe when present. A value that legitimately starts with a literal
    apostrophe (not a guard) is NOT touched UNLESS that apostrophe happens to
    be immediately followed by a formula-trigger char — that one shape (an
    ORIGINAL value that itself starts with ``'=``/``'+``/``'-``/``'@``) is
    genuinely indistinguishable from a guarded value after the fact, so it
    round-trips with the guard apostrophe stripped (a false-positive strip,
    not a false-negative guard — the safe direction: this loses one leading
    apostrophe on an astronomically rare literal shape rather than ever
    leaving a REAL formula-trigger character unguarded on write)."""
    if (
        len(value) >= 2
        and value[0] == _FORMULA_GUARD_PREFIX
        and value[1] in _FORMULA_TRIGGER_CHARS
    ):
        return value[1:]
    return value


def write_worksheet(path: Path, rows: list[WorksheetRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        f.write(_HEADER_COMMENT.format(schema=WORKSHEET_SCHEMA_VERSION))
        writer = csv.DictWriter(f, fieldnames=WORKSHEET_FIELDS)
        writer.writeheader()
        for r in rows:
            guarded = {k: _apply_formula_guard(v) for k, v in r.to_csv_dict().items()}
            writer.writerow(guarded)


#: Exact line count of the header-comment block :func:`write_worksheet`
#: prepends. Used by :func:`read_worksheet` to skip PRECISELY that many lines
#: rather than filtering by a leading ``#`` — a content-based filter would
#: wrongly strip a real CSV record whose (rare, but possible — a sampled
#: ``canonical_name``/``context`` can come from arbitrary ingested text) value
#: contains an embedded newline immediately followed by a ``#`` character.
_HEADER_COMMENT_LINE_COUNT = _HEADER_COMMENT.count("\n")


def read_worksheet(path: Path) -> list[WorksheetRow]:
    """Read a (possibly human-filled) worksheet CSV back, skipping the fixed-
    size leading header-comment block written by :func:`write_worksheet` (by
    LINE COUNT, not by content — see :data:`_HEADER_COMMENT_LINE_COUNT`), and
    reversing the formula-injection guard :func:`write_worksheet` applied (see
    :func:`_strip_formula_guard`) so a round-trip is lossless for the
    ORIGINAL value."""
    with path.open("r", newline="", encoding="utf-8") as f:
        lines = f.readlines()[_HEADER_COMMENT_LINE_COUNT:]
    reader = csv.DictReader(lines)
    out: list[WorksheetRow] = []
    for row in reader:
        out.append(WorksheetRow(**{
            k: _strip_formula_guard(row.get(k) or "") for k in WORKSHEET_FIELDS
        }))
    return out


# ===========================================================================
# CLI
# ===========================================================================


async def _cmd_sample(args: argparse.Namespace) -> int:
    cfg = PostgresConfig.from_env()
    rows = await build_worksheet(
        cfg, per_stratum=args.per_stratum, seed=args.seed)
    out_path = Path(args.out)
    write_worksheet(out_path, rows)
    by_stratum: dict[str, int] = {}
    for r in rows:
        by_stratum[r.stratum] = by_stratum.get(r.stratum, 0) + 1
    print(f"Wrote {len(rows)} rows to {out_path}")
    for stratum, n in sorted(by_stratum.items()):
        print(f"  {stratum}: {n}")
    print(
        "\nNEXT STEP: a human fills the `gold_cluster` column (see the "
        "worksheet's header comment), then run:\n"
        f"  python3 {sys.argv[0]} score --worksheet {out_path}"
    )
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    rows = read_worksheet(Path(args.worksheet))
    pw, bc, n_labeled, n_total = score_worksheet_rows(rows)
    print(f"Worksheet: {args.worksheet}")
    print(f"Rows: {n_total} total, {n_labeled} labeled ({n_total - n_labeled} blank/dropped)")
    if n_labeled == 0:
        print("\nNo labeled rows - fill `gold_cluster` before scoring.")
        return 1
    print("\nPAIRWISE (over the labeled subset):")
    print(f"  precision={pw.precision:.3f}  recall={pw.recall:.3f}  f1={pw.f1:.3f}"
          f"   (tp={pw.tp} fp={pw.fp} fn={pw.fn})")
    print("\nB-CUBED (over the labeled subset):")
    print(f"  precision={bc.precision:.3f}  recall={bc.recall:.3f}  f1={bc.f1:.3f}"
          f"   (n_elements={bc.n_elements})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p_sample = sub.add_parser(
        "sample", help="Draw a read-only stratified sample + write a labeling worksheet CSV.")
    p_sample.add_argument("--out", required=True, help="Output worksheet CSV path.")
    p_sample.add_argument(
        "--per-stratum", type=int, default=20,
        help="Target rows per sub-band (see build_worksheet's docstring for the "
             "resulting total). Default 20.")
    p_sample.add_argument(
        "--seed", type=int, default=None,
        help="Reproducible sample: applies Postgres setseed() on the session "
             "connection, so every ORDER BY random() query this run issues "
             "draws from the same seeded stream (verified: re-running with "
             "the same --seed against an unchanged table reproduces the same "
             "rows). Omit for a fresh, non-reproducible sample each run.")

    p_score = sub.add_parser(
        "score", help="Score a filled worksheet CSV (pairwise + B-cubed).")
    p_score.add_argument("--worksheet", required=True, help="Filled worksheet CSV path.")

    args = ap.parse_args(argv)
    if args.command == "sample":
        return asyncio.run(_cmd_sample(args))
    if args.command == "score":
        return _cmd_score(args)
    ap.error(f"unknown command {args.command!r}")  # pragma: no cover
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
