# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The forecast ledger's scorecard — Brier against resolutions (ST1-A/B).

Reads ONLY the sealed ledger (questions, numbers, active resolutions) and the
resolution records (0244). It is not the legacy calibration
(:mod:`legba.data.calibration_honesty` labels those figures untrusted; this
module carries :data:`SEPARATION_NOTE` so the two are never read as one).

  * Brier per number: ``(p - o)^2`` on a yes/no resolution; void questions
    are excluded and listed.
  * A baseline row on every question: climatological 0.5 and, where the spec
    declares ``base_rate``, that.
  * Per arm (issuer kind): mean Brier with n over the resolved questions that
    carry two or more arms' numbers; a single-arm question (one issuer) is
    scored and shown as single-arm, never compared.
  * NO Brier figure anywhere — per number, per arm, baseline, skill,
    single-arm — until :data:`SCORES_MIN_RESOLVED` questions have resolved
    (the operator's rule, plan 10 ST1-B): the payload carries
    ``scores_withheld: true`` and the threshold, and the line "n resolved;
    scores appear at 5". Outcomes and evidence still show as they resolve.
    Reliability bins wait for their own larger minimum,
    :data:`CALIBRATION_BINS_MIN_SAMPLE`.

LEAF: stdlib + the leaf ledger and calibration-honesty modules.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from .calibration_honesty import ACUTE_PILOT_MIN_SAMPLE
from .forecast_ledger import ISSUER_KINDS, _instant

__all__ = [
    "CALIBRATION_BINS_MIN_SAMPLE",
    "CLIMATOLOGY_P",
    "SCORING_VERSION",
    "SCORES_MIN_RESOLVED",
    "SEPARATION_NOTE",
    "brier",
    "build_scorecard",
]

SCORING_VERSION = "2026-11/st1"
CLIMATOLOGY_P = 0.5
#: Reliability bins need the same sample the acute pilot's calibration needs:
#: the single home of that threshold is ``calibration_honesty``.
CALIBRATION_BINS_MIN_SAMPLE = ACUTE_PILOT_MIN_SAMPLE
#: No Brier figure is shown until this many ledger questions have resolved
#: yes/no (operator decision 2026-11-13; plan 10 ST1-B "no Brier number is
#: printed under 5 resolutions"). The ledger's own threshold — not the legacy
#: calibration's.
SCORES_MIN_RESOLVED = 5

SEPARATION_NOTE = (
    "This scorecard reads only the sealed forecast ledger (forecast_questions, forecast_numbers, "
    "forecast_resolutions and the resolution records). The legacy calibration figures "
    "(calibration_tracking, the acute hazard pilot) are labelled untrusted and are not shown or "
    "pooled here."
)


def brier(p: float, outcome: str) -> float | None:
    """``(p - o)^2`` for yes/no; None for void or an absent number."""
    if p is None or outcome not in ("yes", "no"):
        return None
    o = 1.0 if outcome == "yes" else 0.0
    return round((float(p) - o) ** 2, 6)


def _mean(xs: Sequence[float]) -> float | None:
    return round(sum(xs) / len(xs), 6) if xs else None


def _arm_order(kind: str) -> tuple[int, str]:
    return (ISSUER_KINDS.index(kind) if kind in ISSUER_KINDS else 99, kind)


def _too_small(n: int, need: int) -> str:
    return f"n too small ({n} < {need})"


def build_scorecard(rows: Sequence[Mapping[str, Any]], records: Mapping[str, Mapping[str, Any]],
                    *, now: datetime | None = None) -> dict[str, Any]:
    """The scorecard payload from :func:`forecast_ledger.build_ledger_rows`
    rows and :func:`forecast_resolution.latest_records`."""
    when = now or datetime.now(timezone.utc)
    questions: list[dict[str, Any]] = []
    per_arm: dict[str, list[tuple[float, float]]] = {}
    per_arm_pairs: dict[str, list[tuple[float, int]]] = {}
    single: list[dict[str, Any]] = []
    voids: list[dict[str, Any]] = []
    due_next: list[dict[str, Any]] = []
    counts = {"n_questions": 0, "n_sealed": 0, "n_open": 0, "n_resolved": 0, "n_void": 0, "n_overdue": 0}
    for r in rows:
        if not r.get("exogenous", True):
            continue
        counts["n_questions"] += 1
        counts["n_sealed"] += 1 if r.get("sealed_at") else 0
        res = r.get("resolution")
        outcome = res["outcome"] if res else None
        status = r.get("status") or "open"
        due = _instant(r["due_at"], name="due_at")
        grace_until = due + timedelta(hours=int(r.get("grace_hours") or 0))
        counts["n_open"] += status == "open"
        counts["n_overdue"] += bool(r.get("overdue"))
        if outcome == "void":
            counts["n_void"] += 1
            voids.append({"slug": r["slug"], "reason": (res or {}).get("note") or "resolved void"})
        elif outcome in ("yes", "no"):
            counts["n_resolved"] += 1
        stated = [n for n in r.get("numbers") or [] if n.get("p") is not None]
        arms = sorted({n["issuer_kind"] for n in stated}, key=_arm_order)
        single_arm = len(arms) == 1
        numbers = [{**{k: n.get(k) for k in ("issuer_kind", "issuer_ref", "p", "absent_reason")},
                    "brier": brier(n.get("p"), outcome) if n.get("p") is not None else None}
                   for n in r.get("numbers") or []]
        ps = [float(n["p"]) for n in stated]
        spec = r.get("resolver_spec") or {}
        baselines = [{"name": "climatological", "p": CLIMATOLOGY_P, "brier": brier(CLIMATOLOGY_P, outcome)}]
        if isinstance(spec, Mapping) and spec.get("base_rate") is not None:
            br = float(spec["base_rate"])
            baselines.append({"name": "base_rate", "p": br, "brier": brier(br, outcome)})
        if outcome in ("yes", "no") and r.get("sealed_at"):
            o = 1 if outcome == "yes" else 0
            if single_arm:
                for n in stated:
                    single.append({"slug": r["slug"], "issuer_kind": n["issuer_kind"], "p": n["p"],
                                   "brier": brier(n["p"], outcome), "outcome": outcome})
            else:
                by_kind: dict[str, list[float]] = {}
                for n in stated:
                    by_kind.setdefault(n["issuer_kind"], []).append(float(n["p"]))
                for kind, plist in by_kind.items():
                    p = sum(plist) / len(plist)  # several refs of one kind: their mean is the arm's number
                    per_arm.setdefault(kind, []).append((brier(p, outcome), brier(CLIMATOLOGY_P, outcome)))
                    per_arm_pairs.setdefault(kind, []).append((p, o))
        elif single_arm and r.get("sealed_at") and outcome is None:
            single.append({"slug": r["slug"], "issuer_kind": arms[0], "p": stated[0]["p"],
                           "brier": None, "outcome": None})
        rec = records.get(str(r["id"]), {})
        if status == "open":
            due_next.append({"slug": r["slug"], "due_at": r["due_at"], "grace_until": grace_until.isoformat(),
                             "resolver_kind": r["resolver_kind"], "status": status})
        questions.append({
            "id": r["id"], "slug": r["slug"], "question": r["question"], "status": status,
            "resolver_kind": r["resolver_kind"], "due_at": r["due_at"], "grace_hours": r.get("grace_hours"),
            "grace_until": grace_until.isoformat(), "overdue": bool(r.get("overdue")),
            "sealed_at": r.get("sealed_at"), "single_arm": single_arm, "numbers": numbers,
            "spread": ({"n": len(ps), "min": min(ps), "max": max(ps), "range": round(max(ps) - min(ps), 3)}
                       if len(ps) >= 2 else None),
            "baselines": baselines, "outcome": outcome, "resolution": res,
            "proposal": rec.get("proposal"), "latest_act": rec.get("act"),
        })
    arms_out = []
    for kind in sorted(set(per_arm) | {n["issuer_kind"] for q in questions for n in q["numbers"]
                                       if n.get("p") is not None and not q["single_arm"]}, key=_arm_order):
        pairs = per_arm.get(kind, [])
        n = len(pairs)
        mean = _mean([b for b, _ in pairs])
        base = _mean([c for _, c in pairs])
        small = n < SCORES_MIN_RESOLVED
        arms_out.append({
            "issuer_kind": kind, "n": n, "mean_brier": mean, "baseline_mean_brier": base,
            "skill_vs_climatology": (None if small or not base else round(1.0 - mean / base, 4)),
            "below_min_sample": small,
            "note": _too_small(n, SCORES_MIN_RESOLVED) if small else f"n={n}",
        })
    by_arm = {}
    for kind, pairs in per_arm_pairs.items():
        n = len(pairs)
        ok = n >= CALIBRATION_BINS_MIN_SAMPLE
        by_arm[kind] = {"n": n, "available": ok, "bins": _bins(pairs) if ok else [],
                        "reason": None if ok else _too_small(n, CALIBRATION_BINS_MIN_SAMPLE)}
    cal_n = max((v["n"] for v in by_arm.values()), default=0)
    cal_ok = any(v["available"] for v in by_arm.values())
    # The top-level bins are ONE arm's (the operator's when it qualifies, else
    # the largest qualifying arm) — never a pool across issuers.
    ready = sorted((k for k, v in by_arm.items() if v["available"]),
                   key=lambda k: (k != "operator", -by_arm[k]["n"], _arm_order(k)))
    bins_arm = ready[0] if ready else None
    scored_n = counts["n_resolved"]
    withheld = scored_n < SCORES_MIN_RESOLVED
    skill_claim = not withheld
    void_note = f" ({counts['n_void']} void, excluded)" if counts["n_void"] else ""
    if withheld:
        statement = f"{scored_n} resolved; scores appear at {SCORES_MIN_RESOLVED}."
        _withhold(questions, arms_out, single)
    else:
        statement = (f"{scored_n} resolved of {counts['n_sealed']} sealed questions{void_note}. "
                     "Brier per arm with its n, the climatology baseline and skill are shown.")
    due_next.sort(key=lambda d: (d["due_at"], d["slug"]))
    return {
        "scoring_version": SCORING_VERSION,
        "as_of": when.isoformat(),
        "scores_withheld": withheld,
        "scores_min_resolved": SCORES_MIN_RESOLVED,
        "header": {**counts, "min_sample": SCORES_MIN_RESOLVED,
                   "scores_withheld": withheld, "scores_min_resolved": SCORES_MIN_RESOLVED,
                   "calibration_min_sample": CALIBRATION_BINS_MIN_SAMPLE,
                   "skill_claim": skill_claim, "statement": statement},
        "separation_note": SEPARATION_NOTE,
        "questions": questions,
        "arms": arms_out,
        "single_arm": single,
        "calibration": {"available": cal_ok, "min_sample": CALIBRATION_BINS_MIN_SAMPLE, "n": cal_n,
                        "reason": None if cal_ok else _too_small(cal_n, CALIBRATION_BINS_MIN_SAMPLE),
                        "bins": by_arm[bins_arm]["bins"] if bins_arm else [], "bins_arm": bins_arm,
                        "by_arm": by_arm},
        "due_next": due_next[:10],
        "void_questions": voids,
    }


def _withhold(questions: list[dict[str, Any]], arms: list[dict[str, Any]],
              single: list[dict[str, Any]]) -> None:
    """Below :data:`SCORES_MIN_RESOLVED` no Brier figure leaves this module:
    every per-number, baseline, arm, skill and single-arm score is nulled."""
    note = f"scores appear at {SCORES_MIN_RESOLVED} resolved"
    for q in questions:
        for n in q["numbers"]:
            n["brier"] = None
        for b in q["baselines"]:
            b["brier"] = None
    for a in arms:
        a.update(mean_brier=None, baseline_mean_brier=None, skill_vs_climatology=None,
                 below_min_sample=True, note=note)
    for s in single:
        s["brier"] = None


def _bins(pairs: Sequence[tuple[float, int]]) -> list[dict[str, Any]]:
    out = []
    for i in range(10):
        lo, hi = i / 10, (i + 1) / 10
        sel = [(p, o) for p, o in pairs if lo <= p < hi or (i == 9 and p == 1.0)]
        out.append({"lo": lo, "hi": hi, "n": len(sel),
                    "mean_p": _mean([p for p, _ in sel]), "observed": _mean([float(o) for _, o in sel])})
    return out
