#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-G2 step 4 — score the bake-off.

Consumes the per-model verdict files written by ``kg2_typing_bakeoff.py`` and
produces:

  * ``worksheet.csv`` — the full sample with every model's verdict side by side
    (the committed fixture);
  * ``agreement.json`` — pairwise agreement matrices + per-model agreement
    against the 120B reference;
  * ``handcheck_worksheet.csv`` — the SPLIT cases, UNLABELED, for the operator
    (K-4 precedent: the labels are the operator's call, never the agent's);
  * ``economics.json`` — edges/call, tokens/edge, wall/edge, parse-failure rate.

Comparability rule: every agreement statistic is computed over the INTERSECTION
of candidates that ALL scored models answered. A model that ran out of free
quota shrinks the comparison set rather than silently scoring on a different
denominator.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
from collections import Counter
from pathlib import Path
from typing import Any

REFERENCE = "core120b"


def load(dirpath: Path, model: str, tag: str) -> tuple[list[dict], dict]:
    vp = dirpath / f"verdicts_{model}_{tag}.json"
    sp = dirpath / f"summary_{model}_{tag}.json"
    if not vp.exists():
        return [], {}
    return json.loads(vp.read_text()), (
        json.loads(sp.read_text()) if sp.exists() else {}
    )


def decision(v: dict | None) -> str:
    """The comparable unit: REJECT, or the accepted rel_type."""
    if v is None:
        return "NO_VERDICT"
    return v["rel_type"] if v.get("accepted") else "REJECT"


def coarse(v: dict | None) -> str:
    """The coarser unit: does an edge exist at all?"""
    if v is None:
        return "NO_VERDICT"
    return "EDGE" if v.get("accepted") else "REJECT"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--models", default="core120b,slm8b,nemotron,gptoss")
    ap.add_argument("--tag", default="main")
    ap.add_argument("--out", default=None)
    ap.add_argument("--handcheck-max", type=int, default=40)
    args = ap.parse_args()

    d = Path(args.dir)
    rd = d / "results"
    out = Path(args.out) if args.out else rd
    out.mkdir(parents=True, exist_ok=True)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    payloads = {int(p["idx"]): p for p in json.loads((d / "sample_payloads.json").read_text())}

    verdicts: dict[str, dict[int, dict]] = {}
    summaries: dict[str, dict] = {}
    for m in models:
        vs, s = load(rd, m, args.tag)
        verdicts[m] = {int(v["idx"]): v for v in vs}
        summaries[m] = s
        print(f"[score] {m}: {len(vs)} verdicts")

    present = [m for m in models if verdicts[m]]
    common = sorted(set.intersection(*[set(verdicts[m]) for m in present])) if present else []
    print(f"[score] common answered candidates: {len(common)} / {len(payloads)}")

    # ---- worksheet -------------------------------------------------------
    rows: list[dict[str, Any]] = []
    for idx in sorted(payloads):
        p = payloads[idx]
        row: dict[str, Any] = {
            "idx": idx, "id": p["id"], "stratum": p["stratum"],
            "source_entity": p["source_entity"], "target_entity": p["target_entity"],
            "qual_score": p["qual_score"],
            "independent_sources": p["independent_sources"],
            "evidence_excerpt": (p.get("evidence_text") or "")[:300].replace("\n", " "),
        }
        for m in models:
            v = verdicts[m].get(idx)
            row[f"{m}_decision"] = decision(v)
            row[f"{m}_conf"] = (v or {}).get("confidence")
            row[f"{m}_rationale"] = (v or {}).get("rationale", "")
        rows.append(row)
    with (out / "worksheet.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # ---- agreement -------------------------------------------------------
    def agree_rate(a: str, b: str, fn) -> float:
        if not common:
            return 0.0
        hits = sum(1 for i in common if fn(verdicts[a].get(i)) == fn(verdicts[b].get(i)))
        return round(hits / len(common), 4)

    def kappa(a: str, b: str) -> float:
        """Cohen's kappa on the binary EDGE/REJECT call.

        Raw agreement is badly inflated here: models accept at very different
        base rates (45 %–73 % in this run), so two independent raters would
        agree ~50 % of the time by chance alone. Kappa subtracts that floor.
        Scale: <0.20 slight, 0.21–0.40 fair, 0.41–0.60 moderate, 0.61–0.80
        substantial.
        """
        if not common:
            return 0.0
        n = len(common)
        pa = sum(
            1 for i in common if coarse(verdicts[a].get(i)) == coarse(verdicts[b].get(i))
        ) / n
        a_edge = sum(1 for i in common if coarse(verdicts[a].get(i)) == "EDGE") / n
        b_edge = sum(1 for i in common if coarse(verdicts[b].get(i)) == "EDGE") / n
        pe = a_edge * b_edge + (1 - a_edge) * (1 - b_edge)
        if pe >= 1.0:
            return 1.0
        return round((pa - pe) / (1 - pe), 4)

    matrix_exact = {a: {b: agree_rate(a, b, decision) for b in present} for a in present}
    matrix_coarse = {a: {b: agree_rate(a, b, coarse) for b in present} for a in present}
    matrix_kappa = {a: {b: kappa(a, b) for b in present} for a in present}

    vs_ref = {}
    if REFERENCE in present:
        for m in present:
            vs_ref[m] = {
                "exact_rel_type_agreement": matrix_exact[m][REFERENCE],
                "edge_vs_reject_agreement": matrix_coarse[m][REFERENCE],
                "edge_vs_reject_kappa": matrix_kappa[m][REFERENCE],
            }

    # Acceptance behaviour: is a model a typer or a rejecter?
    behaviour = {}
    for m in present:
        dec = [coarse(verdicts[m].get(i)) for i in common]
        c = Counter(dec)
        behaviour[m] = {
            "accept_rate": round(c["EDGE"] / len(common), 4) if common else 0.0,
            "reject_rate": round(c["REJECT"] / len(common), 4) if common else 0.0,
            "rel_type_vocabulary": dict(Counter(
                decision(verdicts[m].get(i)) for i in common
                if coarse(verdicts[m].get(i)) == "EDGE"
            ).most_common()),
        }

    # ---- accept rate by qualification stratum ----------------------------
    # The qualification bar scores EVIDENTIARY strength; the model scores
    # SEMANTIC relatedness. They are orthogonal by construction, and this table
    # is where that shows: a low-stratum candidate can be genuinely related
    # (an alias pair, a trivial containment) and still not be worth an edge.
    by_stratum: dict[str, dict[str, Any]] = {}
    for idx in common:
        s = payloads[idx]["stratum"]
        d = by_stratum.setdefault(s, {"n": 0})
        d["n"] += 1
        for m in present:
            d[m] = d.get(m, 0) + (1 if coarse(verdicts[m].get(idx)) == "EDGE" else 0)
    for s, d in by_stratum.items():
        for m in present:
            d[f"{m}_accept_rate"] = round(d.get(m, 0) / d["n"], 3) if d["n"] else 0.0

    # ---- split cases (the hand-check set) --------------------------------
    # "Split" on the coarse question — does an edge exist. A 2-2 split among
    # four models is maximal disagreement and is exactly what a human must
    # adjudicate; near-splits (3-1) are ranked after them.
    splits: list[dict[str, Any]] = []
    for i in common:
        votes = [coarse(verdicts[m].get(i)) for m in present]
        edges = votes.count("EDGE")
        rejects = votes.count("REJECT")
        if edges == 0 or rejects == 0:
            continue
        splits.append({
            "idx": i,
            "edge_votes": edges,
            "reject_votes": rejects,
            "balance": abs(edges - rejects),
        })
    splits.sort(key=lambda s: (s["balance"], -s["edge_votes"], s["idx"]))
    n_even = sum(1 for s in splits if s["balance"] == 0)

    hand = []
    for s in splits[: args.handcheck_max]:
        i = s["idx"]
        p = payloads[i]
        row = {
            "idx": i,
            "source_entity": p["source_entity"],
            "target_entity": p["target_entity"],
            "independent_sources": p["independent_sources"],
            "qual_score": p["qual_score"],
            "evidence_excerpt": (p.get("evidence_text") or "")[:400].replace("\n", " "),
            "edge_votes": s["edge_votes"],
            "reject_votes": s["reject_votes"],
        }
        for m in present:
            v = verdicts[m].get(i)
            row[f"{m}_decision"] = decision(v)
            row[f"{m}_rationale"] = (v or {}).get("rationale", "")
        # UNLABELED — the operator fills these in (K-4 precedent).
        row["OPERATOR_VERDICT_edge_or_reject"] = ""
        row["OPERATOR_rel_type_if_edge"] = ""
        row["OPERATOR_notes"] = ""
        hand.append(row)
    if hand:
        with (out / "handcheck_worksheet.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(hand[0].keys()))
            w.writeheader()
            w.writerows(hand)

    agreement = {
        "tag": args.tag,
        "models": present,
        "reference": REFERENCE,
        "common_candidates": len(common),
        "sample_size": len(payloads),
        "exact_rel_type_matrix": matrix_exact,
        "edge_vs_reject_matrix": matrix_coarse,
        "edge_vs_reject_kappa_matrix": matrix_kappa,
        "vs_reference": vs_ref,
        "behaviour": behaviour,
        "accept_rate_by_stratum": by_stratum,
        "split_cases_total": len(splits),
        "split_cases_even_2_2": n_even,
        "handcheck_rows": len(hand),
    }
    (out / "agreement.json").write_text(json.dumps(agreement, indent=2))

    # ---- economics -------------------------------------------------------
    econ = {}
    for m in present:
        s = summaries.get(m) or {}
        recovered = s.get("verdicts_recovered") or 0
        calls = s.get("calls") or 0
        econ[m] = {
            "label": s.get("label"),
            "batch_size": s.get("batch_size"),
            "candidates_requested": s.get("candidates_requested"),
            "verdicts_recovered": recovered,
            "recovery_rate": s.get("recovery_rate"),
            "edges_per_call": round(recovered / calls, 2) if calls else None,
            "call_parse_ok_rate": s.get("call_parse_ok_rate"),
            "calls_truncated": s.get("calls_truncated"),
            "calls_errored": s.get("calls_errored"),
            "tokens_per_edge": s.get("tokens_per_edge"),
            "reasoning_tokens": s.get("reasoning_tokens"),
            "wall_per_edge_s": s.get("wall_per_edge"),
            "wall_seconds": s.get("wall_seconds"),
            "usd_cost": s.get("usd_cost"),
            "usd_per_1k_edges": s.get("usd_per_1k_edges"),
            "accepted": s.get("accepted"),
            "aborted": s.get("aborted"),
        }
    (out / "economics.json").write_text(json.dumps(econ, indent=2))

    print(json.dumps({"agreement": agreement, "economics": econ}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
