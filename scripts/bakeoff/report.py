#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The §6 analysis (design v1.1) — the only step that opens the blind key.

Reads ``<D>/run.json``, ``<D>/grading/sheet.json``,
``<D>/grading/grades_primary.json``, ``<D>/grading/operator_sample.json``
(rows the operator filled — a row counts once it validates as a grade),
the sealed ``blind_key.json``, the records (``<D>/records``, axis 8: their
metered ``cost.usd``) and, when present, ``<D>/grading/packet_ceiling.json``
(``{"questions": {"<QID>": {"share_present": 0..1}}}``, §6.3). Writes
``<D>/report.json`` and ``<D>/report.md``.

REFUSED until ``run.json`` carries ``grading_closed_at`` (``grade_run.py
grade`` sets it when every row has a grade or a grade_error), on a recorded
grader-route change, when the key's sha256 is not the one sealed for the sheet,
or when the grades were made against a different sheet. The first report
stamps ``blind_key_opened_at``; after that no grading runs.

Per answer (axis 1–7, §4.2), as computed here:
  * correct = stated-correct gold atoms / gold atoms; error_rate =
    (contradicted + error atoms asserted) / gold atoms. INSUFFICIENT:
    correct = 1 when at least one gold atom (the decline) is stated correctly,
    none is contradicted and no error atom (the specific stated as fact) is
    asserted, else 0. Binary correct = correct ≥ 0.5.
  * usefulness 0–4, capped at 2 when any gold atom is contradicted.
  * novelty = novel atoms (count); share = novel / stated-correct, pooled.
  * traceability = (supports + 0.5 × partial) / sentences, pooled; temporal =
    right / claims, pooled; stale = answers flagged stale.
  * calibration: the stated confidence on the normalised answer's last line
    (``Stated confidence: 0.xx``) against binary correct; ``none`` is reported
    as coverage, never imputed. Brier with a seeded bootstrap 95 % CI (1,000
    resamples); reliability in 3 bins [0, ⅓), [⅓, ⅔), [⅔, 1].
  * coverage: answered share, mean parts addressed, abstentions (answered with
    no part addressed) outside INSUFFICIENT.
  * cost per answer: the mean of the records' metered ``cost.usd``.

Primary endpoint: per question B − A on correctness over the questions
both arms have graded; two-sided paired sign-flip permutation test (10,000
seeded permutations; exact enumeration when 2^n ≤ 10,000), mean difference
with a seeded bootstrap 95 % CI. Secondary S1–S6 descriptive with CIs.
Cohen's κ on binary correct, primary vs operator. §6.4 decision rules, each
read as met / not met / not evaluable. Stdlib only (``random``,
``statistics``): it runs in the slim image.

USAGE
    python3 scripts/bakeoff/report.py --run-dir <D> [--key <D>/sealed/blind_key.json] [--seed S]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import grade_run  # noqa: E402
import grade_sheet  # noqa: E402

REPORT_SCHEMA = "legba.bakeoff.report/1"
ARMS = ("A", "A-r", "B", "C", "D", "E")
CLASSES = grade_sheet.CLASS_ORDER
BOOTSTRAP = 1000
PERMUTATIONS = 10_000
BINS = ((0.0, 1 / 3), (1 / 3, 2 / 3), (2 / 3, 1.0))
CEILING_HIGH = 0.5
_CONF = re.compile(r"Stated confidence:\s*(none|[0-9]*\.?[0-9]+)\s*$")


class ReportRefused(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Statistics (stdlib)
# ---------------------------------------------------------------------------


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 6)


def mean(xs: list[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def quantile(sorted_xs: list[float], q: float) -> float:
    """Linear interpolation between order statistics (``(n-1)·q``)."""
    pos = (len(sorted_xs) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def rng_for(seed: int, label: str) -> random.Random:
    return random.Random(f"{seed}|{label}")


def bootstrap_ci(items: list[Any], stat: Callable[[list[Any]], float | None], rng: random.Random,
                 resamples: int = BOOTSTRAP) -> list[float] | None:
    """Percentile 95 % CI of ``stat`` over ``resamples`` resamples with replacement."""
    if not items:
        return None
    vals = []
    n = len(items)
    for _ in range(resamples):
        v = stat([items[rng.randrange(n)] for _ in range(n)])
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    vals.sort()
    return [_r(quantile(vals, 0.025)), _r(quantile(vals, 0.975))]


def paired_permutation(diffs: list[float], rng: random.Random,
                       permutations: int = PERMUTATIONS) -> tuple[float | None, str | None]:
    """Two-sided paired (sign-flip) permutation p for mean(diffs) = 0. Exact
    when 2^n ≤ ``permutations``; else ``(1 + hits) / (1 + permutations)``."""
    n = len(diffs)
    if n == 0:
        return None, None
    obs = abs(sum(diffs)) - 1e-9
    if 2 ** n <= permutations:
        hits = sum(1 for mask in range(2 ** n)
                   if abs(sum(-d if (mask >> i) & 1 else d for i, d in enumerate(diffs))) >= obs)
        return hits / 2 ** n, "exact"
    hits = sum(1 for _ in range(permutations)
               if abs(sum(-d if rng.random() < 0.5 else d for d in diffs)) >= obs)
    return (hits + 1) / (permutations + 1), "monte_carlo"


def brier(pairs: list[tuple[float, int]]) -> float | None:
    return mean([(c - y) ** 2 for c, y in pairs]) if pairs else None


def reliability(pairs: list[tuple[float, int]]) -> list[dict[str, Any]]:
    out = []
    for i, (lo, hi) in enumerate(BINS):
        inside = [(c, y) for c, y in pairs if lo <= c < hi or (i == len(BINS) - 1 and c == hi)]
        out.append({"bin": [_r(lo), _r(hi)], "n": len(inside),
                    "mean_confidence": _r(mean([c for c, _ in inside])),
                    "observed_correct": _r(mean([float(y) for _, y in inside]))})
    return out


def cohen_kappa(pairs: list[tuple[int, int]]) -> dict[str, Any]:
    """κ on binary correct, ``pairs`` = (primary, operator)."""
    n = len(pairs)
    t = {"both_1": sum(1 for a, b in pairs if a and b), "primary_1_operator_0": sum(1 for a, b in pairs if a and not b),
         "primary_0_operator_1": sum(1 for a, b in pairs if not a and b),
         "both_0": sum(1 for a, b in pairs if not a and not b)}
    if not n:
        return {"n": 0, "table": t, "observed_agreement": None, "expected_agreement": None, "kappa": None}
    po = (t["both_1"] + t["both_0"]) / n
    pa = (t["both_1"] + t["primary_1_operator_0"]) / n
    pb = (t["both_1"] + t["primary_0_operator_1"]) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return {"n": n, "table": t, "observed_agreement": _r(po), "expected_agreement": _r(pe),
            "kappa": None if pe >= 1 else _r((po - pe) / (1 - pe))}


# ---------------------------------------------------------------------------
# Per answer
# ---------------------------------------------------------------------------


def answer_metrics(klass: str, grade: dict[str, Any]) -> dict[str, Any]:
    ga = grade["gold_atoms"]
    n = len(ga)
    sc = sum(1 for a in ga if a["status"] == "stated_correct")
    con = sum(1 for a in ga if a["status"] == "contradicted")
    asserted = sum(1 for a in grade["error_atoms"] if a["asserted"])
    if klass == "INSUFFICIENT":
        correct: float | None = 1.0 if (sc >= 1 and con == 0 and asserted == 0) else 0.0
    else:
        correct = sc / n if n else None
    u = int(grade["usefulness"])
    trace = grade["traceability"]
    temporal = grade["temporal"]
    cov = grade["coverage"]
    return {
        "correct": correct,
        "binary": None if correct is None else int(correct >= 0.5),
        "error_rate": (con + asserted) / n if n else None,
        "usefulness": min(u, 2) if con else u,
        "usefulness_capped": bool(con and u > 2),
        "novelty": len(grade["novelty_atoms"]),
        "correct_atoms": sc,
        "trace_score": sum(1.0 if t["verdict"] == "supports" else 0.5 if t["verdict"] == "partial" else 0.0
                           for t in trace),
        "trace_n": len(trace),
        "temporal_right": sum(1 for t in temporal if t["right"]),
        "temporal_n": len(temporal),
        "stale": bool(grade["stale_state"]),
        "answered": bool(cov["answered"]),
        "parts": float(cov["parts_addressed"]),
        "abstained": klass != "INSUFFICIENT" and bool(cov["answered"]) and float(cov["parts_addressed"]) == 0.0,
    }


def stated_confidence(answer: str) -> float | None:
    """The normalised answer's last line (N7): ``Stated confidence: 0.xx`` | ``none``."""
    lines = (answer or "").strip().splitlines()
    m = _CONF.search(lines[-1]) if lines else None
    if not m or m.group(1) == "none":
        return None
    return float(m.group(1))


def assemble(sheet: dict[str, Any], key: dict[str, Any], grades: dict[str, Any],
             records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per sheet answer, the key applied."""
    by_rec = {(r.get("question_id"), r.get("arm")): r for r in records}
    rows = []
    for it in sheet["items"]:
        bid = it["blind_id"]
        k = key["items"].get(bid)
        if k is None:
            raise ReportRefused(f"the blind key has no {bid}: it is not the sheet's key")
        e = grades.get(bid) or {}
        rec = by_rec.get((it["question_id"], k["arm"])) or {}
        usd = (rec.get("cost") or {}).get("usd")
        row: dict[str, Any] = {
            "blind_id": bid, "question_id": it["question_id"], "class": it["class"], "arm": k["arm"],
            "graded": e.get("grade") is not None, "grade_error": e.get("grade_error"),
            "confidence": stated_confidence(it.get("answer") or ""),
            "grader_confidence": (e.get("grade") or {}).get("stated_confidence"),
            "cost_usd": float(usd) if isinstance(usd, (int, float)) else None,
            "cut": bool(((k.get("cut") or {}).get("dropped"))),
        }
        if e.get("grade") is not None:
            row.update(answer_metrics(it["class"], e["grade"]))
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Groups, comparisons
# ---------------------------------------------------------------------------


def _vals(rows: list[dict[str, Any]], k: str) -> list[float]:
    return [r[k] for r in rows if r.get("graded") and r.get(k) is not None]


def _share(num: float, den: float) -> float | None:
    return num / den if den else None


def _calib(rows: list[dict[str, Any]]) -> list[tuple[float, int]]:
    return [(r["confidence"], r["binary"]) for r in rows
            if r.get("graded") and r["confidence"] is not None and r.get("binary") is not None]


def summarise(rows: list[dict[str, Any]], *, seed: int | None = None, label: str = "") -> dict[str, Any]:
    """Every axis for one group of answers; CIs when ``seed`` is given."""
    g = [r for r in rows if r.get("graded")]
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    calib = _calib(rows)
    out = {
        "n": len(rows), "graded": len(g), "grade_errors": len(rows) - len(g),
        "correct": _r(mean(_vals(rows, "correct"))),
        "error_rate": _r(mean(_vals(rows, "error_rate"))),
        "usefulness": _r(mean(_vals(rows, "usefulness"))),
        "usefulness_capped": sum(1 for r in g if r["usefulness_capped"]),
        "novelty_mean": _r(mean(_vals(rows, "novelty"))),
        "novelty_share": _r(_share(sum(r["novelty"] for r in g), sum(r["correct_atoms"] for r in g))),
        "traceability": _r(_share(sum(r["trace_score"] for r in g), sum(r["trace_n"] for r in g))),
        "traceability_sentences": sum(r["trace_n"] for r in g),
        "temporal": _r(_share(sum(r["temporal_right"] for r in g), sum(r["temporal_n"] for r in g))),
        "temporal_claims": sum(r["temporal_n"] for r in g),
        "stale": sum(1 for r in g if r["stale"]),
        "brier": _r(brier(calib)),
        "confidence_coverage": _r(_share(len(calib), len(g))),
        "reliability": reliability(calib),
        "answered": _r(_share(sum(1 for r in g if r["answered"]), len(g))),
        "parts_addressed": _r(mean(_vals(rows, "parts"))),
        "abstentions": sum(1 for r in g if r["abstained"]),
        "cut_share": _r(_share(sum(1 for r in rows if r["cut"]), len(rows))),
        "cost_per_answer_usd": _r(mean(costs)),
        "cost_unpriced": len(rows) - len(costs),
    }
    if seed is not None:
        corr = _vals(rows, "correct")
        out["correct_ci"] = bootstrap_ci(corr, mean, rng_for(seed, f"correct|{label}"))
        out["usefulness_ci"] = bootstrap_ci(_vals(rows, "usefulness"), mean, rng_for(seed, f"useful|{label}"))
        out["brier_ci"] = bootstrap_ci(calib, brier, rng_for(seed, f"brier|{label}"))
    return out


def _by_q(rows: list[dict[str, Any]], arm: str, klass: str | None) -> dict[str, dict[str, Any]]:
    return {r["question_id"]: r for r in rows
            if r["arm"] == arm and r.get("graded") and (klass is None or r["class"] == klass)}


def compare(rows: list[dict[str, Any]], x: str, y: str, metric: str, *, klass: str | None = None,
            seed: int, permutation: bool = False) -> dict[str, Any]:
    """Paired per question, x − y on ``metric`` over the questions both graded."""
    bx, by = _by_q(rows, x, klass), _by_q(rows, y, klass)
    qs = sorted(q for q in set(bx) & set(by) if bx[q].get(metric) is not None and by[q].get(metric) is not None)
    diffs = [bx[q][metric] - by[q][metric] for q in qs]
    label = f"{x}-{y}|{metric}|{klass}"
    out = {"x": x, "y": y, "metric": metric, "class": klass or "ALL", "n": len(qs), "questions": qs,
           "mean_x": _r(mean([bx[q][metric] for q in qs])), "mean_y": _r(mean([by[q][metric] for q in qs])),
           "mean_diff": _r(mean(diffs)), "ci": bootstrap_ci(diffs, mean, rng_for(seed, label))}
    if permutation:
        p, method = paired_permutation(diffs, rng_for(seed, "perm|" + label))
        out.update(p=_r(p), test=method, permutations=PERMUTATIONS)
    return out


def cost_adjusted(rows: list[dict[str, Any]], arm: str, klass: str, seed: int) -> dict[str, Any]:
    """Correctness per dollar on one class: mean correct / mean metered cost."""
    pts = [(r["correct"], r["cost_usd"]) for r in rows if r["arm"] == arm and r["class"] == klass
           and r.get("graded") and r.get("correct") is not None and r["cost_usd"] is not None]

    def stat(s: list[tuple[float, float]]) -> float | None:
        c = mean([x for _, x in s])
        return None if not c else mean([x for x, _ in s]) / c

    return {"arm": arm, "class": klass, "n": len(pts), "correct": _r(mean([a for a, _ in pts])),
            "cost_per_answer_usd": _r(mean([b for _, b in pts])), "correct_per_usd": _r(stat(pts)) if pts else None,
            "ci": bootstrap_ci(pts, stat, rng_for(seed, f"cpd|{arm}|{klass}"))}


# ---------------------------------------------------------------------------
# §6.4 decision rules
# ---------------------------------------------------------------------------

READINGS = {
    "R1": "On-demand reasoning over the corpus is the product's better reader. The tower's next quarter goes "
          "to what B cannot do (memory, absence, calibration), not to more scheduled prose.",
    "R2": "The tower holds its own. Its weakness is presentation and coverage, not substance.",
    "R3": "Verification earns its place as the reader's diet, not only as a gate.",
    "R4": "The filter removes more truth than error.",
    "R5": "The memory claim falls and is withdrawn from the briefing until it is re-earned.",
    "R6": "The snapshot-diff path is the cheaper reader for slow change.",
}
OUTCOMES = {
    "R1": "B − A ≥ 0.15 on correctness with CI excluding 0, and B ≥ A on usefulness",
    "R2": "A ≥ B (the B − A CI includes 0 or lies below it)",
    "R3": "C ≥ B on correctness with a lower error_rate",
    "R4": "C < B on correctness",
    "R5": "Neither A nor B > D on LONG-MEMORY correctness",
    "R6": "E within 0.05 of B (E ≥ B − 0.05) on SLOW-CHANGE correctness at ≤ 1/3 of B's cost per answer",
}


def decisions(primary: dict[str, Any], s1c: dict[str, Any], s1e: dict[str, Any], s2a: dict[str, Any],
              s2b: dict[str, Any], s3: dict[str, Any], cost_e: dict[str, Any], cost_b: dict[str, Any],
              use: dict[str, float | None]) -> list[dict[str, Any]]:
    def rule(rid: str, met: bool | None, numbers: str) -> dict[str, Any]:
        reading = READINGS[rid] if met else ("Not met." if met is False else "Not evaluable: no paired data.")
        return {"rule": rid, "outcome": OUTCOMES[rid], "met": met, "numbers": numbers, "reading": reading}

    out = []
    d, ci = primary["mean_diff"], primary["ci"]
    ua, ub = use.get("A"), use.get("B")
    if d is None or ci is None or ua is None or ub is None:
        out.append(rule("R1", None, "—"))
        out.append(rule("R2", None, "—"))
    else:
        out.append(rule("R1", d >= 0.15 and ci[0] > 0 and ub >= ua,
                        f"B − A {d:.3f}, CI [{ci[0]:.3f}, {ci[1]:.3f}]; usefulness B {ub:.3f}, A {ua:.3f}"))
        out.append(rule("R2", ci[0] <= 0, f"B − A {d:.3f}, CI [{ci[0]:.3f}, {ci[1]:.3f}]"))
    if s1c["n"] and s1e["n"]:
        nums = (f"correct C {s1c['mean_x']:.3f}, B {s1c['mean_y']:.3f}; "
                f"error_rate C {s1e['mean_x']:.3f}, B {s1e['mean_y']:.3f}")
        out.append(rule("R3", s1c["mean_x"] >= s1c["mean_y"] and s1e["mean_x"] < s1e["mean_y"], nums))
        out.append(rule("R4", s1c["mean_x"] < s1c["mean_y"], nums))
    else:
        out += [rule("R3", None, "—"), rule("R4", None, "—")]
    if s2a["n"] and s2b["n"]:
        out.append(rule("R5", not (s2a["mean_x"] > s2a["mean_y"]) and not (s2b["mean_x"] > s2b["mean_y"]),
                        f"LONG-MEMORY A {s2a['mean_x']:.3f} vs D {s2a['mean_y']:.3f}; "
                        f"B {s2b['mean_x']:.3f} vs D {s2b['mean_y']:.3f}"))
    else:
        out.append(rule("R5", None, "—"))
    ce, cb = cost_e.get("cost_per_answer_usd"), cost_b.get("cost_per_answer_usd")
    if s3["n"] and ce is not None and cb is not None:
        out.append(rule("R6", s3["mean_x"] >= s3["mean_y"] - 0.05 and ce <= cb / 3,
                        f"SLOW-CHANGE E {s3['mean_x']:.3f} vs B {s3['mean_y']:.3f}; "
                        f"cost per answer E ${ce:.4f} vs B ${cb:.4f}"))
    else:
        out.append(rule("R6", None, "—"))
    return out


# ---------------------------------------------------------------------------
# The second rater, the packet ceiling
# ---------------------------------------------------------------------------


def operator_grades(op: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Blind id → the operator's grade, for the rows that validate (filled)."""
    out: dict[str, dict[str, Any]] = {}
    for bid, it in ((op or {}).get("items") or {}).items():
        gold = it.get("gold") or {}
        grade, problems = grade_run.validate_grade(
            it.get("grade"), [a["id"] for a in grade_run.atoms(gold, "gold_atoms")],
            [a["id"] for a in grade_run.atoms(gold, "error_atoms")])
        if grade is not None and not problems:
            out[bid] = grade
    return out


def agreement(rows: list[dict[str, Any]], op: dict[str, Any] | None) -> dict[str, Any]:
    filled = operator_grades(op)
    by_bid = {r["blind_id"]: r for r in rows}
    pairs = []
    for bid, grade in sorted(filled.items()):
        r = by_bid.get(bid)
        if r is None or not r.get("graded") or r.get("binary") is None:
            continue
        ob = answer_metrics(r["class"], grade)["binary"]
        if ob is not None:
            pairs.append((r["binary"], ob))
    k = cohen_kappa(pairs)
    k["operator_rows"] = len((op or {}).get("items") or {})
    k["operator_filled"] = len(filled)
    return k


def packet_ceiling(rows: list[dict[str, Any]], pc: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not pc:
        return []
    a = _by_q(rows, "A", None)
    out = []
    for qid, v in sorted((pc.get("questions") or {}).items()):
        share = v.get("share_present") if isinstance(v, dict) else v
        score = (a.get(qid) or {}).get("correct")
        hi_p = share is not None and share >= CEILING_HIGH
        hi_a = score is not None and score >= CEILING_HIGH
        reading = ("—" if share is None or score is None else
                   "extraction (N6) hid the answer — a presentation finding" if hi_p and not hi_a else
                   "the tower lacks the answer — a substance finding" if not hi_p and not hi_a else
                   "A scored high")
        out.append({"question_id": qid, "packet_ceiling": _r(share), "ceiling": "high" if hi_p else "low",
                    "a_correct": _r(score), "a_score": None if score is None else ("high" if hi_a else "low"),
                    "reading": reading})
    return out


# ---------------------------------------------------------------------------
# The whole report
# ---------------------------------------------------------------------------


def analyse(rows: list[dict[str, Any]], *, seed: int, op: dict[str, Any] | None = None,
            pc: dict[str, Any] | None = None) -> dict[str, Any]:
    arms = [a for a in ARMS if any(r["arm"] == a for r in rows)]
    per_arm = {a: summarise([r for r in rows if r["arm"] == a], seed=seed, label=a) for a in arms}
    per_class = {c: {a: summarise([r for r in rows if r["arm"] == a and r["class"] == c]) for a in arms}
                 for c in CLASSES if any(r["class"] == c for r in rows)}
    primary = compare(rows, "B", "A", "correct", seed=seed, permutation=True)
    s1c = compare(rows, "C", "B", "correct", seed=seed)
    s1e = compare(rows, "C", "B", "error_rate", seed=seed)
    s2a = compare(rows, "A", "D", "correct", klass="LONG-MEMORY", seed=seed)
    s2b = compare(rows, "B", "D", "correct", klass="LONG-MEMORY", seed=seed)
    s3 = compare(rows, "E", "B", "correct", klass="SLOW-CHANGE", seed=seed)
    cost_e = cost_adjusted(rows, "E", "SLOW-CHANGE", seed)
    cost_b = cost_adjusted(rows, "B", "SLOW-CHANGE", seed)
    s4 = {}
    for a in arms:
        v = _vals([r for r in rows if r["arm"] == a and r["class"] == "INSUFFICIENT"], "correct")
        s4[a] = {"n": len(v), "correct": _r(mean(v)), "ci": bootstrap_ci(v, mean, rng_for(seed, f"s4|{a}"))}
    s5 = {a: {"usefulness": per_arm[a]["usefulness"], "usefulness_ci": per_arm[a]["usefulness_ci"],
              "brier": per_arm[a]["brier"], "brier_ci": per_arm[a]["brier_ci"],
              "confidence_coverage": per_arm[a]["confidence_coverage"]} for a in arms}
    s6 = compare(rows, "A-r", "A", "correct", seed=seed)
    use = {a: per_arm[a]["usefulness"] for a in arms}
    return {
        "per_arm": per_arm, "per_class": per_class,
        "primary": primary,
        "secondary": {"S1": {"correct": s1c, "error_rate": s1e}, "S2": {"A_vs_D": s2a, "B_vs_D": s2b},
                      "S3": {"correct": s3, "cost_adjusted": {"E": cost_e, "B": cost_b}},
                      "S4": s4, "S5": s5, "S6": s6},
        "agreement": agreement(rows, op),
        "packet_ceiling": packet_ceiling(rows, pc),
        "decisions": decisions(primary, s1c, s1e, s2a, s2b, s3, cost_e, cost_b, use),
        "confidence_mismatch": sum(1 for r in rows if r.get("graded") and r["grader_confidence"] is not None
                                   and r["confidence"] is not None
                                   and abs(r["grader_confidence"] - r["confidence"]) > 0.005),
    }


def _f(x: Any, nd: int = 3) -> str:
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _ci(ci: list[float] | None) -> str:
    return "—" if not ci else f"[{ci[0]:.3f}, {ci[1]:.3f}]"


_COLS = (("n", "n"), ("graded", "graded"), ("correct", "correct"), ("error_rate", "error rate"),
         ("usefulness", "usefulness"), ("novelty_mean", "novel atoms"), ("novelty_share", "novel share"),
         ("traceability", "traceability"), ("temporal", "temporal"), ("stale", "stale"),
         ("brier", "Brier"), ("confidence_coverage", "conf. stated"), ("answered", "answered"),
         ("parts_addressed", "parts"), ("abstentions", "abstained"), ("cost_per_answer_usd", "$/answer"))


def _table(head: list[str], body: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(r) + " |" for r in body]


def render_md(rep: dict[str, Any]) -> str:
    a = rep["analysis"]
    L = [f"# Bake-off report — run {rep['run']['run_id']}", "",
         f"As of {rep['run']['as_of']}. Design {rep['run']['design_version']} "
         f"(sha256 {str(rep['run']['design_sha256'])[:12]}). Primary grader {rep['run']['grader_model_served']}. "
         f"{rep['counts']['answers']} answers on the sheet, {rep['counts']['graded']} graded, "
         f"{rep['counts']['grade_errors']} grade errors. Seed {rep['seed']}.", ""]
    if rep["void_questions"]:
        L += ["Void questions: " + "; ".join(f"{v['question_id']} ({v['reason']})" for v in rep["void_questions"]) + ".", ""]
    if rep["descriptive_only_classes"]:
        L += ["Reported descriptively only (more than two void questions): "
              + ", ".join(rep["descriptive_only_classes"]) + ".", ""]
    p = a["primary"]
    L += ["## Primary endpoint — B vs A on correctness", ""]
    L += _table(["pairs", "A", "B", "B − A", "95 % CI", "p (two-sided)", "test"],
                [[_f(p["n"]), _f(p["mean_y"]), _f(p["mean_x"]), _f(p["mean_diff"]), _ci(p["ci"]),
                  _f(p.get("p"), 4), _f(p.get("test"))]])
    L += ["", "## Per arm, all classes", ""]
    L += _table(["arm", *[h for _, h in _COLS], "correct 95 % CI", "Brier 95 % CI"],
                [[arm, *[_f(s[k]) for k, _ in _COLS], _ci(s["correct_ci"]), _ci(s["brier_ci"])]
                 for arm, s in a["per_arm"].items()])
    for klass, arms in a["per_class"].items():
        tag = " (descriptive only)" if klass in rep["descriptive_only_classes"] else ""
        L += ["", f"## {klass}{tag}", ""]
        L += _table(["arm", *[h for _, h in _COLS]], [[arm, *[_f(s[k]) for k, _ in _COLS]] for arm, s in arms.items()])
    sec = a["secondary"]
    L += ["", "## Secondary endpoints (descriptive)", ""]
    rows = []
    for sid, comp in (("S1", sec["S1"]["correct"]), ("S1", sec["S1"]["error_rate"]), ("S2", sec["S2"]["A_vs_D"]),
                      ("S2", sec["S2"]["B_vs_D"]), ("S3", sec["S3"]["correct"]), ("S6", sec["S6"])):
        rows.append([sid, f"{comp['x']} − {comp['y']}", comp["metric"], comp["class"], _f(comp["n"]),
                     _f(comp["mean_x"]), _f(comp["mean_y"]), _f(comp["mean_diff"]), _ci(comp["ci"])])
    L += _table(["endpoint", "comparison", "metric", "class", "pairs", "x", "y", "x − y", "95 % CI"], rows)
    L += ["", "S3, cost-adjusted on SLOW-CHANGE:", ""]
    L += _table(["arm", "answers", "correct", "$/answer", "correct per $", "95 % CI"],
                [[c["arm"], _f(c["n"]), _f(c["correct"]), _f(c["cost_per_answer_usd"], 4), _f(c["correct_per_usd"]),
                  _ci(c["ci"])] for c in sec["S3"]["cost_adjusted"].values()])
    L += ["", "S4, INSUFFICIENT correctness, and S5, usefulness and calibration:", ""]
    L += _table(["arm", "INSUFFICIENT n", "INSUFFICIENT correct", "95 % CI", "usefulness", "95 % CI", "Brier",
                 "95 % CI", "conf. stated"],
                [[arm, _f(sec["S4"][arm]["n"]), _f(sec["S4"][arm]["correct"]), _ci(sec["S4"][arm]["ci"]),
                  _f(s5["usefulness"]), _ci(s5["usefulness_ci"]), _f(s5["brier"]), _ci(s5["brier_ci"]),
                  _f(s5["confidence_coverage"])] for arm, s5 in sec["S5"].items()])
    L += ["", "## Calibration — reliability in three bins", ""]
    L += _table(["arm", *[f"[{lo:.2f}, {hi:.2f}{']' if i == len(BINS) - 1 else ')'} n / conf / correct"
                          for i, (lo, hi) in enumerate(BINS)]],
                [[arm, *[f"{b['n']} / {_f(b['mean_confidence'], 2)} / {_f(b['observed_correct'], 2)}"
                         for b in s["reliability"]]] for arm, s in a["per_arm"].items()])
    k = a["agreement"]
    L += ["", "## Agreement — primary grader vs operator, binary correct", "",
          f"{k['operator_filled']} of {k['operator_rows']} operator rows filled; {k['n']} paired. "
          f"Both correct {k['table']['both_1']}, primary only {k['table']['primary_1_operator_0']}, "
          f"operator only {k['table']['primary_0_operator_1']}, neither {k['table']['both_0']}. "
          f"Observed agreement {_f(k['observed_agreement'])}, expected {_f(k['expected_agreement'])}, "
          f"Cohen's κ {_f(k['kappa'])}."]
    if a["packet_ceiling"]:
        L += ["", "## Packet ceiling for A (not blind)", ""]
        L += _table(["question", "packet ceiling", "A correct", "reading"],
                    [[c["question_id"], f"{_f(c['packet_ceiling'], 2)} ({c['ceiling']})",
                      f"{_f(c['a_correct'], 2)} ({c['a_score'] or '—'})", c["reading"]] for c in a["packet_ceiling"]])
    L += ["", "## Decision rules", ""]
    L += _table(["rule", "outcome", "met", "numbers", "reading"],
                [[d["rule"], d["outcome"], {True: "yes", False: "no", None: "—"}[d["met"]], d["numbers"], d["reading"]]
                 for d in a["decisions"]])
    return "\n".join(L) + "\n"


def _load(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def refusal(run: dict[str, Any]) -> str | None:
    if not run.get("grading_closed_at"):
        return "grading is not closed (run.json has no grading_closed_at): the blind key stays sealed"
    if run.get("grader_route_changed"):
        return f"the grader route changed mid-run — the run is void: {run['grader_route_changed']}"
    return None


def build(run_dir: Path, *, key_path: Path | None = None, grading_dir: Path | None = None,
          seed: int | None = None) -> dict[str, Any]:
    run_dir = Path(run_dir)
    gdir = Path(grading_dir or run_dir / "grading")
    run = common.load_run(run_dir)
    why = refusal(run)
    if why:
        raise ReportRefused(why)
    key_path = Path(key_path or run_dir / "sealed" / "blind_key.json")
    sealed = [k for k in run.get("blind_keys") or [] if k.get("purpose") == "sheet"]
    key_sha = common.sha256_file(key_path)
    if not sealed or sealed[-1].get("sha256") != key_sha:
        raise ReportRefused(f"{key_path} (sha256 {key_sha[:12]}) is not the key sealed for the sheet in run.json")
    sheet_path = gdir / "sheet.json"
    sheet = _load(sheet_path)
    grades_doc = _load(gdir / "grades_primary.json")
    if grades_doc.get("sheet_sha256") != common.sha256_file(sheet_path):
        raise ReportRefused("grades_primary.json grades a different sheet.json")
    key = _load(key_path)
    op_path, pc_path = gdir / "operator_sample.json", gdir / "packet_ceiling.json"
    op = _load(op_path) if op_path.is_file() else None
    pc = _load(pc_path) if pc_path.is_file() else None
    records = grade_sheet.load_records(run_dir)
    seed = int(sheet["seed"]) if seed is None else seed
    rows = assemble(sheet, key, grades_doc["items"], records)
    if not run.get("blind_key_opened_at"):
        run["blind_key_opened_at"] = common.utcnow().isoformat()
        common.save_run(run_dir, run)
    return {
        "schema": REPORT_SCHEMA, "seed": seed, "bootstrap_resamples": BOOTSTRAP, "permutations": PERMUTATIONS,
        "run": {k: run.get(k) for k in ("run_id", "as_of", "design_sha256", "design_version", "grader_model_served",
                                         "grading_closed_at", "blind_key_opened_at", "grades_primary_sha256")},
        "counts": {"answers": len(rows), "graded": sum(1 for r in rows if r["graded"]),
                   "grade_errors": sum(1 for r in rows if not r["graded"])},
        "void_questions": sheet.get("void_questions") or [],
        "void_for_arm": sheet.get("void_for_arm") or [],
        "descriptive_only_classes": sheet.get("descriptive_only_classes") or [],
        "excluded": sheet.get("excluded") or [],
        "analysis": analyse(rows, seed=seed, op=op, pc=pc),
        "answers": rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="the bake-off analysis")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--grading-dir", help="default <run-dir>/grading")
    ap.add_argument("--key", help="the sealed blind key (default <run-dir>/sealed/blind_key.json)")
    ap.add_argument("--seed", type=int, help="bootstrap / permutation seed (default: the sheet's seed)")
    a = ap.parse_args(argv)
    rep = build(Path(a.run_dir), key_path=Path(a.key) if a.key else None,
                grading_dir=Path(a.grading_dir) if a.grading_dir else None, seed=a.seed)
    out = Path(a.run_dir)
    common.write_json(out / "report.json", rep)
    (out / "report.md").write_text(render_md(rep), encoding="utf-8")
    for d in rep["analysis"]["decisions"]:
        state = "MET" if d["met"] else "not met" if d["met"] is False else "not evaluable"
        print(f"{d['rule']} {d['outcome']}: {state} — {d['numbers']}." + (f" {d['reading']}" if d["met"] else ""))
    print(f"wrote {out / 'report.json'} and {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
