#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-G2 step 1 — measure the live candidate pool against the qualification bar.

READ-ONLY. Runs :data:`edge_qualification.POOL_SCORING_SQL` over
``proposed_edges``, scores every row with the module's four components, and
reports how many candidates survive at a sweep of bar settings. Also emits the
deterministic, qualification-STRATIFIED bake-off sample.

    python scripts/kg2_pool_measure.py --out <dir> [--sample-size 200]

Sampling is reproducible: rows are ordered by ``id`` (a stable uuid), bucketed
by score band, and drawn with a fixed-seed ``random.Random``. Re-running against
an unchanged pool yields byte-identical output.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from legba.data.analysts.edge_qualification import (  # noqa: E402
    MIN_INDEPENDENT_SOURCES,
    POOL_SCORING_SQL,
    RETENTION_STALE_DAYS,
    CandidateEvidence,
    components,
    qualification_score,
    qualifies,
    retention_verdict,
)

PG_CONTAINER = os.environ.get("KG2_PG_CONTAINER", "legba-postgres-1")
SAMPLE_SEED = 20260803

#: Bar settings swept in the report.
BAR_SWEEP = [0.0, 0.20, 0.30, 0.35, 0.40, 0.42, 0.45, 0.50, 0.55, 0.60, 0.70]


def psql_csv(sql: str) -> list[dict[str, str]]:
    """Run a read-only query in the live container, return CSV rows."""
    proc = subprocess.run(
        [
            "docker", "exec", "-i", PG_CONTAINER,
            "psql", "-U", "legba", "-d", "legba",
            "--csv", "-v", "ON_ERROR_STOP=1", "-c", sql,
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"psql failed:\n{proc.stderr[:4000]}")
    return list(csv.DictReader(proc.stdout.splitlines()))


def desk_entity_names() -> set[str]:
    """Lowercased subject names of the active L1 desks.

    Country desks carry an ISO code in ``scope.geo`` and a human name in
    ``name`` ('G20 — Germany', 'Watch — Ukraine'); lanes/flows/regions carry
    theme words. We take the part after the em-dash as the desk's subject."""
    rows = psql_csv(
        "SELECT name FROM target_descriptors "
        "WHERE is_head AND state='active' AND abstraction_level='L1'"
    )
    names: set[str] = set()
    for r in rows:
        raw = (r.get("name") or "").strip()
        subject = raw.split("—")[-1].strip() if "—" in raw else raw
        if subject:
            names.add(subject.lower())
            # 'Korea, Republic of' -> also index the leading form
            if "," in subject:
                names.add(subject.split(",")[0].strip().lower())
    return names


def to_evidence(row: dict[str, str], desks: set[str]) -> CandidateEvidence:
    def _i(k: str) -> int:
        try:
            return int(float(row.get(k) or 0))
        except (TypeError, ValueError):
            return 0

    src = (row.get("source_entity") or "").strip().lower()
    tgt = (row.get("target_entity") or "").strip().lower()
    try:
        age = float(row.get("age_days") or 0.0)
    except (TypeError, ValueError):
        age = 0.0
    return CandidateEvidence(
        independent_sources=_i("independent_sources"),
        source_families=_i("source_families"),
        raw_signals=_i("raw_signals"),
        subject_mentions=_i("subject_mentions"),
        object_mentions=_i("object_mentions"),
        desk_geo_hit=(row.get("desk_geo_hit") or "f").lower() in ("t", "true"),
        desk_entity_hit=(src in desks or tgt in desks),
        age_days=age,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--sample-size", type=int, default=200)
    ap.add_argument("--status", default="pending")
    args = ap.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    desks = desk_entity_names()
    print(f"[kg2] active desk subjects: {len(desks)}", file=sys.stderr)

    sql = POOL_SCORING_SQL.format(status_filter=f"pe.status = '{args.status}'")
    print("[kg2] scoring pool (read-only) ...", file=sys.stderr)
    rows = psql_csv(sql)
    print(f"[kg2] pool rows: {len(rows)}", file=sys.stderr)

    scored: list[dict[str, object]] = []
    for r in rows:
        ev = to_evidence(r, desks)
        comp = components(ev)
        score = qualification_score(ev)
        scored.append({
            "id": r["id"],
            "source_entity": r["source_entity"],
            "target_entity": r["target_entity"],
            "pe_confidence": float(r.get("confidence") or 0),
            "independent_sources": ev.independent_sources,
            "source_families": ev.source_families,
            "raw_signals": ev.raw_signals,
            "syndication_ratio": (
                round(ev.raw_signals / ev.independent_sources, 2)
                if ev.independent_sources else 0.0
            ),
            "subject_mentions": ev.subject_mentions,
            "object_mentions": ev.object_mentions,
            "desk_geo_hit": ev.desk_geo_hit,
            "desk_entity_hit": ev.desk_entity_hit,
            "age_days": round(ev.age_days, 1),
            "c_multi_source": round(comp["multi_source"], 4),
            "c_source_diversity": round(comp["source_diversity"], 4),
            "c_salience": round(comp["salience"], 4),
            "c_desk_relevance": round(comp["desk_relevance"], 4),
            "qual_score": round(score, 4),
            "qualifies": qualifies(ev),
            "retention": retention_verdict(ev).action,
        })

    # ---- bar sweep -------------------------------------------------------
    sweep = []
    total = len(scored)
    for bar in BAR_SWEEP:
        n_score_only = sum(1 for s in scored if s["qual_score"] >= bar)
        n_full = sum(
            1 for s in scored
            if s["qual_score"] >= bar
            and s["independent_sources"] >= MIN_INDEPENDENT_SOURCES
        )
        sweep.append({
            "bar": bar,
            "qualifying_score_only": n_score_only,
            "qualifying_with_source_floor": n_full,
            "pct_of_pool": round(100.0 * n_full / total, 3) if total else 0.0,
        })

    retire = sum(1 for s in scored if s["retention"] == "retire")
    summary = {
        "pool_status": args.status,
        "pool_rows": total,
        "min_independent_sources": MIN_INDEPENDENT_SOURCES,
        "retention_stale_days": RETENTION_STALE_DAYS,
        "would_retire_now": retire,
        "would_keep_now": total - retire,
        "source_histogram": {},
        "bar_sweep": sweep,
    }
    hist: dict[int, int] = {}
    for s in scored:
        hist[s["independent_sources"]] = hist.get(s["independent_sources"], 0) + 1
    summary["source_histogram"] = {str(k): hist[k] for k in sorted(hist)}

    (outdir / "pool_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))

    # ---- stratified deterministic sample ---------------------------------
    # Five strata by qualification score. The sample must not be 95 % junk (a
    # naive draw would be, since 92.6 % of the pool is single-sourced), but it
    # must still CONTAIN junk — a bake-off that never shows a model an obvious
    # reject cannot measure rejection.
    strata = {
        "S4_top":    lambda s: s["qual_score"] >= 0.55,
        "S3_high":   lambda s: 0.42 <= s["qual_score"] < 0.55,
        "S2_mid":    lambda s: 0.30 <= s["qual_score"] < 0.42,
        "S1_low":    lambda s: 0.15 <= s["qual_score"] < 0.30,
        "S0_floor":  lambda s: s["qual_score"] < 0.15,
    }
    quota = {"S4_top": 60, "S3_high": 60, "S2_mid": 40, "S1_low": 25, "S0_floor": 15}
    scale = args.sample_size / float(sum(quota.values()))
    quota = {k: max(1, round(v * scale)) for k, v in quota.items()}

    rng = random.Random(SAMPLE_SEED)
    sample: list[dict[str, object]] = []
    for name, pred in strata.items():
        members = sorted([s for s in scored if pred(s)], key=lambda s: s["id"])
        want = min(quota[name], len(members))
        picked = rng.sample(members, want) if want < len(members) else members
        for p in sorted(picked, key=lambda s: s["id"]):
            row = dict(p)
            row["stratum"] = name
            sample.append(row)
        print(f"[kg2] stratum {name}: pool={len(members)} picked={want}",
              file=sys.stderr)

    sample.sort(key=lambda s: (s["stratum"], s["id"]))
    for i, s in enumerate(sample):
        s["idx"] = i

    fields = list(sample[0].keys()) if sample else []
    with (outdir / "sample_candidates.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(sample)
    print(f"[kg2] wrote sample of {len(sample)} to {outdir/'sample_candidates.csv'}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
