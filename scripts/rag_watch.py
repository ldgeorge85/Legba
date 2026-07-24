#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""rag_watch.py — per-unit opportunistic-RAG (``world_context``) safety watch.

The staggered flip that turns opportunistic RAG (``vector:world_context``
"BACKGROUND PRIORS") on for the bounded assessment units, one at a time, must be
MONITORED, not vibes. This read-only reporting script prints, per unit,
BEFORE vs AFTER the RAG flip:

  * ``n``                     — verified product findings in the window
  * trailing-mean faithfulness
  * low-faith (< 0.30) rate
  * avg tokens/run            — cost visibility (input side)
  * avg run latency           — cost visibility (retrieval hop)

and evaluates the PRE-REGISTERED rollback rule
(``planning/RAG_EXPANSION_WATCH_2026-07-03.md``).

WHAT COUNTS (the load-bearing exclusion). A **product finding** is
``kind='finding' AND target_id IS NOT NULL``. The ``target_id IS NOT NULL``
clause EXCLUDES no-target / meta-shaped runs — the earlier
``leadership_transition`` cohort was contaminated by 3 meta-mode findings
(faithfulness 0.14 / 0.44 / 0.83) that a naive average would drag. This watch
never counts them.

FAITHFULNESS is read from the verify pass's critique row (``kind='critique'``,
body ``Faithfulness verify of finding <uuid>`` ... ``faithfulness_score=<x>``),
parsed by regex and joined to the finding by uuid, then to the unit by the
finding's ``analyst_id`` (exact) AND ``target_id IS NOT NULL``.

COST (tokens / latency) is read from ``analyst_traces`` joined to the finding by
``run_id``: the ``llm_call`` step ``tokens`` sum (best available token proxy;
a separate input-token counter is NOT recorded — an absent field prints ``n/a``,
never a guess) and ``run_ended_at - run_started_at`` for latency.

THE FLIP MOMENT is auto-derived from the trace record: the first run whose
``intermediate_steps`` carries a non-empty ``world_context_chunk_ids`` (the
RAG-on signal stamped by the ``inject_preamble`` step). Override with
``--cutoff``.

Read-only. Runs no writes. Connection: ``PostgresConfig.from_env()`` (the
runtime's ``LEGBA_*`` env; defaults to ``legba@localhost:5432/legba``).

Usage:
    python3 scripts/rag_watch.py --unit leadership_transition
    python3 scripts/rag_watch.py --unit escalation --cutoff 2026-07-03T03:31:03+00
    python3 scripts/rag_watch.py --all-units
    python3 scripts/rag_watch.py --unit energy_security --n 20
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover — dotenv optional
    pass

import asyncpg

from legba.data.config import PostgresConfig
from legba.runtime.rag_rollback import (
    DEFAULT_TOKEN_RISE_FRAC,
    RollbackWindow,
    evaluate_rollback,
    record_rollback,
)

# The bounded RAG-expansion candidate units: grounding-enabled per-target
# assessment units that emit citable product findings (kind='finding',
# target_id set). LIVE STATE (2026-07-06): only `internal_stability` is currently
# flipped on (`vector:world_context` in its grounding.sources);
# `leadership_transition` was flipped then ROLLED BACK on 2026-07-03 (it is NOT
# on now — only in its descriptor comments); `proliferation_watch` is off. The
# rest are the staggered flip cohort this watch guards. Override with --units.
BOUNDED_UNITS: tuple[str, ...] = (
    "leadership_transition",
    "escalation",
    "energy_security",
    "economic_coercion",
    "internal_stability",
    "military_posture",
    "narrative_coordination",
)

# Pre-registered thresholds (see planning/RAG_EXPANSION_WATCH_2026-07-03.md).
LOW_FAITH_THRESHOLD = 0.30
DEFAULT_WINDOW = 15
FAITH_DROP_TRIGGER = 0.08  # (a): absolute trailing-mean drop that fires rollback

# The RAG-on signal in a trace's intermediate_steps: the inject_preamble step
# stamps the retrieved chunk ids here. Its FIRST appearance = the flip moment.
_FLIP_MARKER = "world_context_chunk_ids"


# ---------------------------------------------------------------------------
# Queries (read-only)
# ---------------------------------------------------------------------------

# Verified product findings for a unit, each joined to its faithfulness score
# (parsed from the verify critique body) and its run's cost (tokens + latency
# from analyst_traces). target_id IS NOT NULL drops the meta rows. LEFT JOIN on
# the trace so a finding whose trace is missing still contributes its
# faithfulness (tokens/latency degrade to NULL → 'n/a').
_ROWS_SQL = """
WITH fnd AS (
    SELECT id, run_id, produced_at
    FROM analyst_outputs
    WHERE kind = 'finding'
      AND analyst_id = $1
      AND target_id IS NOT NULL
),
crit AS (
    SELECT (regexp_match(body, 'verify of finding ([0-9a-f-]{36})'))[1] AS fid_txt,
           (regexp_match(body, 'faithfulness_score=([0-9.]+)'))[1]::float AS faith
    FROM analyst_outputs
    WHERE kind = 'critique'
      AND body LIKE 'Faithfulness verify of finding%'
),
tok AS (
    SELECT run_id,
           EXTRACT(EPOCH FROM (run_ended_at - run_started_at)) AS latency_s,
           (
               SELECT SUM((s->>'tokens')::numeric)
               FROM jsonb_array_elements(
                   CASE WHEN jsonb_typeof(intermediate_steps) = 'array'
                        THEN intermediate_steps ELSE '[]'::jsonb END
               ) AS s
               WHERE s->>'kind' = 'llm_call' AND (s ? 'tokens')
           ) AS tokens
    FROM analyst_traces
    WHERE analyst_id = $1
)
SELECT f.produced_at, c.faith, tok.tokens, tok.latency_s
FROM fnd f
JOIN crit c ON c.fid_txt = f.id::text
LEFT JOIN tok ON tok.run_id = f.run_id
ORDER BY f.produced_at
"""

# The flip moment: first trace with a non-empty world_context_chunk_ids marker.
_FLIP_SQL = """
SELECT MIN(run_started_at)
FROM analyst_traces
WHERE analyst_id = $1
  AND intermediate_steps::text LIKE '%' || $2 || '%'
"""


@dataclass
class Row:
    produced_at: datetime
    faith: float
    tokens: float | None
    latency_s: float | None


@dataclass
class WindowStats:
    n: int
    mean_faith: float | None
    low_faith_count: int
    low_faith_rate: float | None
    tokens_mean: float | None
    tokens_n: int
    latency_mean: float | None
    latency_n: int


def _stats(rows: Sequence[Row]) -> WindowStats:
    n = len(rows)
    if n == 0:
        return WindowStats(0, None, 0, None, None, 0, None, 0)
    faiths = [r.faith for r in rows]
    low = sum(1 for f in faiths if f < LOW_FAITH_THRESHOLD)
    toks = [float(r.tokens) for r in rows if r.tokens is not None]
    lats = [float(r.latency_s) for r in rows if r.latency_s is not None]
    return WindowStats(
        n=n,
        mean_faith=sum(faiths) / n,
        low_faith_count=low,
        low_faith_rate=low / n,
        tokens_mean=(sum(toks) / len(toks)) if toks else None,
        tokens_n=len(toks),
        latency_mean=(sum(lats) / len(lats)) if lats else None,
        latency_n=len(lats),
    )


def _fmt_faith(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.3f}"


def _fmt_tokens(s: WindowStats) -> str:
    if s.tokens_mean is None:
        return "n/a"
    return f"{s.tokens_mean:,.0f}  ({s.tokens_n}/{s.n} traces)"


def _fmt_latency(s: WindowStats) -> str:
    if s.latency_mean is None:
        return "n/a"
    return f"{s.latency_mean:.1f}s  ({s.latency_n}/{s.n} traces)"


def _print_window(label: str, s: WindowStats, window: int) -> None:
    under = "  [UNDER-FILLED: %d < %d]" % (s.n, window) if 0 < s.n < window else ""
    print(f"  {label} (n={s.n}){under}:")
    if s.n == 0:
        print("    (no verified product findings in this window)")
        return
    lf = "n/a" if s.low_faith_rate is None else (
        f"{s.low_faith_rate:.3f} ({s.low_faith_count}/{s.n})"
    )
    print(f"    trailing-mean faithfulness : {_fmt_faith(s.mean_faith)}")
    print(f"    low-faith(<{LOW_FAITH_THRESHOLD:.2f}) rate     : {lf}")
    print(f"    avg tokens/run             : {_fmt_tokens(s)}")
    print(f"    avg latency/run            : {_fmt_latency(s)}")


def _to_window(s: WindowStats) -> RollbackWindow:
    """Adapt the watch's WindowStats to the shared rag_rollback.RollbackWindow so
    the RULE lives in ONE place (legba.runtime.rag_rollback.evaluate_rollback)."""
    return RollbackWindow(
        n=s.n,
        mean_faith=s.mean_faith,
        low_faith_rate=s.low_faith_rate,
        low_faith_count=s.low_faith_count,
        tokens_mean=s.tokens_mean,
    )


def _evaluate_rollback(
    before: WindowStats, after: WindowStats, window: int
) -> "object":
    """Print the DELTA + the pre-registered rollback verdict, and RETURN the shared
    :class:`RollbackDecision` (so ``--enforce`` can actuate it). The rule itself is
    :func:`legba.runtime.rag_rollback.evaluate_rollback` — the SAME code the runtime
    guard uses, so the reporting verdict and the auto-rollback can never diverge."""
    print("  DELTA (after - before):")
    if before.mean_faith is not None and after.mean_faith is not None:
        print(f"    faithfulness   : {after.mean_faith - before.mean_faith:+.3f}")
    else:
        print("    faithfulness   : n/a (a window is empty)")
    if before.low_faith_rate is not None and after.low_faith_rate is not None:
        if before.low_faith_rate > 0:
            ratio_s = f"x{after.low_faith_rate / before.low_faith_rate:.2f}"
        else:
            ratio_s = (
                "x inf (baseline low-faith rate is 0)"
                if after.low_faith_rate > 0 else "x1.00 (both 0)"
            )
        print(f"    low-faith rate : {ratio_s}")
    else:
        print("    low-faith rate : n/a")
    if before.tokens_mean is not None and after.tokens_mean is not None:
        rise = ""
        if before.tokens_mean > 0:
            rise = f"  ({(after.tokens_mean - before.tokens_mean) / before.tokens_mean * 100:+.0f}%)"
        print(f"    tokens/run     : {after.tokens_mean - before.tokens_mean:+,.0f}{rise}")
    else:
        print("    tokens/run     : n/a")
    if before.latency_mean is not None and after.latency_mean is not None:
        print(f"    latency/run    : {after.latency_mean - before.latency_mean:+.1f}s")
    else:
        print("    latency/run    : n/a")

    decision = evaluate_rollback(
        _to_window(before), _to_window(after), window=window,
    )
    print("  ROLLBACK CHECK (rule: planning/RAG_EXPANSION_WATCH_2026-07-03.md):")
    trig_a = any("faithfulness dropped" in r for r in decision.reasons)
    trig_b = any("low-faith rate" in r for r in decision.reasons)
    trig_c = any("tokens/run" in r for r in decision.reasons)
    da = (
        f"{before.mean_faith - after.mean_faith:+.3f} drop"
        if (before.mean_faith is not None and after.mean_faith is not None)
        else "n/a"
    )
    print(f"    (a) faithfulness drop >= {FAITH_DROP_TRIGGER:.2f}? "
          f"{'YES' if trig_a else 'no'}  [{da}]")
    print(f"    (b) low-faith rate > 2x baseline? {'YES' if trig_b else 'no'}")
    print(f"    (c) avg tokens/run rise >= {DEFAULT_TOKEN_RISE_FRAC * 100:.0f}%? "
          f"{'YES' if trig_c else 'no'}")
    verdict = "ROLLBACK TRIGGERED" if decision.triggered else "PASS (no trigger)"
    suffix = "  [PROVISIONAL — a window is UNDER-FILLED (< %d)]" % window if decision.provisional else ""
    print(f"    => {verdict}{suffix}")
    return decision


def _parse_cutoff(raw: str) -> datetime:
    s = raw.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _report_unit(
    conn: asyncpg.Connection, unit: str, *, cutoff_raw: str | None, window: int,
    enforce: bool = False,
) -> None:
    records = await conn.fetch(_ROWS_SQL, unit)
    rows = [
        Row(
            produced_at=r["produced_at"],
            faith=float(r["faith"]),
            tokens=(float(r["tokens"]) if r["tokens"] is not None else None),
            latency_s=(float(r["latency_s"]) if r["latency_s"] is not None else None),
        )
        for r in records
        if r["faith"] is not None
    ]

    # Resolve the flip cutoff: explicit arg wins, else auto-derive from traces.
    if cutoff_raw:
        cutoff: datetime | None = _parse_cutoff(cutoff_raw)
        cutoff_src = "explicit --cutoff"
    else:
        auto = await conn.fetchval(_FLIP_SQL, unit, _FLIP_MARKER)
        cutoff = auto
        cutoff_src = "auto (first world_context trace)" if auto else "none found"

    print("=" * 72)
    print(f"unit: {unit}   verified product findings: {len(rows)}")
    print(f"flip-cutoff: {cutoff.isoformat() if cutoff else 'NOT YET FLIPPED'}"
          f"  [{cutoff_src}]")

    if cutoff is None:
        # Never flipped: report the current trailing window as the PRE-FLIP
        # baseline to capture BEFORE flipping this unit.
        base = _stats(rows[-window:])
        _print_window("PRE-FLIP BASELINE (not yet flipped; trailing window)", base, window)
        print("  (no post-flip window to compare — capture this baseline, then flip.)")
        return

    before_all = [r for r in rows if r.produced_at < cutoff]
    after_all = [r for r in rows if r.produced_at >= cutoff]
    before = _stats(before_all[-window:])   # N immediately before the flip
    after = _stats(after_all[-window:])     # most recent N after the flip
    _print_window("BEFORE (pre-flip)", before, window)
    _print_window("AFTER  (post-flip)", after, window)
    decision = _evaluate_rollback(before, after, window)
    # --enforce: ACTUATE a triggered rollback — persist the unit into the
    # rag_rollback kill-switch so the runtime suppresses its BACKGROUND PRIORS
    # block on the next grounding build (auto-revert, no descriptor PUT). Read-only
    # against the substrate; the only write is the local rollback state file.
    if enforce and getattr(decision, "triggered", False):
        path = record_rollback(unit, reasons=list(getattr(decision, "reasons", [])))
        if path:
            print(f"  ENFORCE: recorded rollback for {unit!r} -> {path}")
            print(f"           (runtime suppresses vector:world_context for {unit!r} on "
                  "its NEXT run — the grounding hook re-checks per run, no restart needed)")
        else:
            print(f"  ENFORCE: rollback for {unit!r} NOT persisted — set "
                  "LEGBA_RAG_ROLLBACK_STATE (or pin via LEGBA_WORLD_CONTEXT_DISABLED_UNITS)")
    elif enforce:
        print(f"  ENFORCE: no trigger — {unit!r} left enabled.")


async def _amain(args: argparse.Namespace) -> int:
    cfg = PostgresConfig.from_env()
    print(f"# rag_watch — db={cfg.database}@{cfg.host}:{cfg.port} "
          f"window(N)={args.n} low-faith<{LOW_FAITH_THRESHOLD}")
    print("# faithfulness monitors CITATION DISCIPLINE ONLY — a SAFETY net, not a "
          "value proof (S5-T5 A/B is the value measurement).")
    conn = await asyncpg.connect(dsn=cfg.dsn)
    try:
        if args.all_units:
            units = list(args.units) if args.units else list(BOUNDED_UNITS)
            for unit in units:
                await _report_unit(
                    conn, unit, cutoff_raw=None, window=args.n, enforce=args.enforce,
                )
                print()
        else:
            await _report_unit(
                conn, args.unit, cutoff_raw=args.cutoff, window=args.n,
                enforce=args.enforce,
            )
    finally:
        await conn.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Per-unit opportunistic-RAG (world_context) safety watch.",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--unit", help="analyst_id of one bounded unit (e.g. leadership_transition)")
    g.add_argument("--all-units", action="store_true",
                   help="report every bounded unit (auto-derives each flip cutoff)")
    p.add_argument("--cutoff", default=None,
                   help="flip timestamp (ISO-8601); default = auto-derive from traces")
    p.add_argument("--units", default=None,
                   help="comma-separated unit override for --all-units")
    p.add_argument("--n", type=int, default=DEFAULT_WINDOW,
                   help=f"trailing window size N (default {DEFAULT_WINDOW}; rule needs >=15)")
    p.add_argument("--enforce", action="store_true",
                   help="ACTUATE a triggered rollback: persist the unit into the "
                        "rag_rollback kill-switch (needs LEGBA_RAG_ROLLBACK_STATE) so "
                        "the runtime auto-reverts its world_context flip. Read-only "
                        "against the substrate; without it the watch only reports.")
    args = p.parse_args()
    if args.units:
        args.units = [u.strip() for u in args.units.split(",") if u.strip()]
    if args.n < 1:
        p.error("--n must be >= 1")
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
