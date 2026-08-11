#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-G2 step 2 — materialise the bake-off sample's full prompt payloads.

READ-ONLY. Takes ``sample_candidates.csv`` from ``kg2_pool_measure.py`` and
attaches everything the typing prompt needs, using the SAME context the live
reifier assembles: the co-mention excerpt, recent facts about either endpoint,
the offered intermediary set (only above the reifier's own pair-confidence
threshold), and the wider sports-gate text (excerpt ∪ backing signal
title+summary).

The output ``sample_payloads.json`` is the frozen bake-off input: every model
sees byte-identical prompts built from it, so a disagreement is a model
difference and never a prompt difference.

    python scripts/kg2_sample_prep.py --dir <dir-from-pool-measure>
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from legba.data.analysts.relationship_reifier import (  # noqa: E402
    MAX_FACTS_CONTEXT,
    MAX_INTERMEDIARY_CANDIDATES,
    MIN_INTERMEDIARY_PAIR_CONFIDENCE,
)

PG_CONTAINER = os.environ.get("KG2_PG_CONTAINER", "legba-postgres-1")


def psql_json(sql: str) -> list[dict]:
    """Run a read-only query, return rows as dicts (via json_agg)."""
    wrapped = (
        "SELECT coalesce(json_agg(t), '[]'::json)::text FROM ( " + sql + " ) t"
    )
    proc = subprocess.run(
        ["docker", "exec", "-i", PG_CONTAINER, "psql", "-U", "legba", "-d",
         "legba", "-tA", "-v", "ON_ERROR_STOP=1", "-c", wrapped],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"psql failed:\n{proc.stderr[:4000]}")
    return json.loads(proc.stdout.strip() or "[]")


def sql_quote_uuid_list(ids: list[str]) -> str:
    safe = [i for i in ids if all(c in "0123456789abcdef-" for c in i.lower())]
    if len(safe) != len(ids):
        raise SystemExit("non-uuid id in sample; refusing to build SQL")
    return ", ".join(f"'{i}'::uuid" for i in safe)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    args = ap.parse_args()
    d = Path(args.dir)

    sample = list(csv.DictReader((d / "sample_candidates.csv").open()))
    ids = [r["id"] for r in sample]
    idlist = sql_quote_uuid_list(ids)
    print(f"[kg2] preparing {len(ids)} candidates", file=sys.stderr)

    # --- evidence + sports-gate text (mirrors reifier._read_candidates) ----
    rows = psql_json(f"""
        SELECT pe.id::text AS id, pe.source_entity, pe.target_entity,
               pe.evidence_text, pe.confidence,
               (
                 SELECT string_agg(btrim(coalesce(s.payload->>'title','') || ' ' ||
                                          coalesce(s.payload->>'summary','')), ' ')
                   FROM signals s WHERE s.id = ANY(pe.derived_from)
               ) AS source_signal_text
          FROM proposed_edges pe
         WHERE pe.id IN ({idlist})
    """)
    by_id = {r["id"]: r for r in rows}

    # --- recent facts for every endpoint, one pass ------------------------
    names = sorted({r["source_entity"] for r in rows} | {r["target_entity"] for r in rows})
    esc = ", ".join("'" + n.replace("'", "''") + "'" for n in names)
    fact_rows = psql_json(f"""
        SELECT lower(subject) AS subj, subject, predicate, value, confidence,
               row_number() OVER (PARTITION BY lower(subject)
                                  ORDER BY confidence DESC, produced_at DESC) AS rn
          FROM facts
         WHERE valid_until IS NULL AND superseded_by IS NULL
           AND lower(subject) IN (SELECT lower(x) FROM unnest(ARRAY[{esc}]) AS x)
    """) if names else []
    facts_by_name: dict[str, list[dict]] = {}
    for f in fact_rows:
        if int(f["rn"]) > MAX_FACTS_CONTEXT:
            continue
        facts_by_name.setdefault(f["subj"], []).append(
            {"subject": f["subject"], "predicate": f["predicate"], "value": f["value"]}
        )

    # --- intermediaries, only for pairs the reifier would offer them for --
    inter_pairs = [
        r for r in rows
        if float(r.get("confidence") or 0) >= MIN_INTERMEDIARY_PAIR_CONFIDENCE
    ]
    print(f"[kg2] intermediary path for {len(inter_pairs)} pairs "
          f"(conf >= {MIN_INTERMEDIARY_PAIR_CONFIDENCE})", file=sys.stderr)
    inter_by_id: dict[str, list[str]] = {}
    for r in inter_pairs:
        a = r["source_entity"].replace("'", "''")
        b = r["target_entity"].replace("'", "''")
        got = psql_json(f"""
            WITH neighbours AS (
                SELECT CASE WHEN lower(source_entity)=lower('{a}')
                            THEN target_entity ELSE source_entity END AS c,
                       confidence, 'a' AS anchor
                  FROM proposed_edges
                 WHERE relationship_type='co_occurs'
                   AND (lower(source_entity)=lower('{a}') OR lower(target_entity)=lower('{a}'))
                UNION ALL
                SELECT CASE WHEN lower(source_entity)=lower('{b}')
                            THEN target_entity ELSE source_entity END AS c,
                       confidence, 'b' AS anchor
                  FROM proposed_edges
                 WHERE relationship_type='co_occurs'
                   AND (lower(source_entity)=lower('{b}') OR lower(target_entity)=lower('{b}'))
            )
            SELECT c, sum(confidence) AS score FROM neighbours
             WHERE lower(c) <> lower('{a}') AND lower(c) <> lower('{b}')
             GROUP BY lower(c), c
            HAVING count(DISTINCT anchor)=2
             ORDER BY score DESC LIMIT {MAX_INTERMEDIARY_CANDIDATES}
        """)
        if got:
            inter_by_id[r["id"]] = [g["c"] for g in got]

    payloads = []
    for s in sample:
        r = by_id.get(s["id"])
        if r is None:
            continue
        src, tgt = r["source_entity"], r["target_entity"]
        excerpt = r.get("evidence_text") or ""
        payloads.append({
            "idx": int(s["idx"]),
            "id": s["id"],
            "stratum": s["stratum"],
            "source_entity": src,
            "target_entity": tgt,
            "evidence_text": excerpt,
            "sports_gate_text": " ".join(
                p for p in [excerpt, r.get("source_signal_text") or ""] if p.strip()
            ),
            "facts": (facts_by_name.get(src.lower(), [])
                      + facts_by_name.get(tgt.lower(), []))[:MAX_FACTS_CONTEXT],
            "intermediaries": inter_by_id.get(s["id"], []),
            "qual_score": float(s["qual_score"]),
            "independent_sources": int(s["independent_sources"]),
            "pe_confidence": float(s["pe_confidence"]),
        })
    payloads.sort(key=lambda p: p["idx"])
    (d / "sample_payloads.json").write_text(json.dumps(payloads, indent=1))
    n_ev = sum(1 for p in payloads if p["evidence_text"].strip())
    print(f"[kg2] wrote {len(payloads)} payloads "
          f"({n_ev} with evidence text, "
          f"{sum(1 for p in payloads if p['intermediaries'])} with intermediaries)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
