#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""r5_ablation_score.py — score the filled R5 sheet and say what it means.

Reads the operator's ``R5_SCORESHEET.csv`` plus the bundle (for the Read-1/Read-2
-> arm mapping, which the operator never sees while grading) and reports:

  * per-axis mean delta, tower minus baseline, with per-pair detail
  * the forced-preference tally and an EXACT two-sided sign test
  * guess accuracy — how much of the result rests on a blind that held
  * an explicit verdict block stating what THIS result implies

WHAT EACH OUTCOME MEANS (review3 §9, review1 §9)
------------------------------------------------
The reviews wrote the interpretation in advance, which is the only reason this
experiment is worth running — the conclusion cannot be negotiated after seeing
the number.

  TOWER SWEEPS (3-0 preference, positive axis deltas)
      review3 §9: *"If Legba wins, the cathedral has a congregation."*
      But n=3 cannot clear significance: a 3-0 sweep is p=0.25 two-sided. This
      is a SCREENING PASS, not a result. It licenses the decision-grade run
      (n>=10 desks via the harness's ``--desks``), and it does not license a
      product claim until that run lands.

  TOWER LOSES OR TIES
      review1 §9: *"If no reported number moves, that is the most important
      finding the project will ever produce and it belongs at the top of
      STATUS.md."*
      review3 §9: everything else — *"the receipts, the seams, the chorus diff —
      is ornamentation on a very well-documented RSS reader with a diary."*
      n=3 is ENOUGH to act on a loss. A pipeline of this size losing to one
      prompt on even one desk out of three is a finding; losing or tying on the
      aggregate is a verdict. No larger n is required to justify investigating.

  MIXED (e.g. tower wins the quiet desk, loses the busy one)
      The most informative outcome and the one to look hardest at. It says the
      pipeline's value is REGIME-DEPENDENT — most plausibly that composition
      earns its keep where signal is thin and a one-shot read has nothing to
      work with, and loses where signal is thick and the pipeline's unit
      summarization discards detail the raw slice still carries. That is an
      actionable architectural finding, not a failure.

USAGE
-----
    python3 scripts/r5_ablation_score.py \
        --sheet R5_SCORESHEET.csv --bundle R5_BUNDLE.json
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path

# Must match ``r5_ablation_render._SHUFFLE_SALT`` — the pack's arm order is
# derived, never stored in the sheet, so the operator cannot accidentally leak it
# into their own grading by reading the CSV.
_SHUFFLE_SALT = "R5-ablation-v1"

AXES = ("useful", "correct", "actionable", "temporal")


def _order_for(target_id: str) -> list[str]:
    digest = hashlib.sha256(f"{_SHUFFLE_SALT}:{target_id}".encode()).digest()
    return ["tower", "baseline"] if digest[0] % 2 == 0 else ["baseline", "tower"]


def _sign_test_two_sided(wins: int, losses: int) -> float:
    """Exact two-sided binomial sign test at p=0.5. Ties are excluded, not split.

    Returns 1.0 for n=0 (nothing to test) rather than raising — an all-ties sheet
    is a legitimate outcome and reads as "no detectable difference".
    """
    n = wins + losses
    if n == 0:
        return 1.0
    k = max(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k, n + 1)) / (2**n)
    return min(1.0, 2 * tail)


def _cell(raw: str) -> float | None:
    """Parse a score cell. Blank / NA / N/A / '-' are legitimate abstentions."""
    v = (raw or "").strip()
    if v == "" or v.upper() in ("NA", "N/A", "-", "SKIP"):
        return None
    try:
        f = float(v)
    except ValueError:
        raise SystemExit(f"R5: cannot parse score cell {raw!r} (expected 1-5 or NA)")
    if not 1.0 <= f <= 5.0:
        raise SystemExit(f"R5: score {f} out of range (expected 1-5 or NA)")
    return f


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sheet", required=True)
    ap.add_argument("--bundle", required=True)
    args = ap.parse_args()

    bundle = json.loads(Path(args.bundle).read_text())
    desks = bundle["desks"]
    pair_of = {f"P{i}": d for i, d in enumerate(desks, start=1)}

    rows = list(csv.DictReader(Path(args.sheet).open()))
    filled = [r for r in rows if any((r.get(f"r1_{a}") or "").strip() for a in AXES)]
    if not filled:
        raise SystemExit(
            "R5: the scoresheet is empty. Grade R5_BLIND_PACK.md first "
            "(see R5_GRADING_PROTOCOL.md)."
        )

    per_axis: dict[str, list[tuple[str, float, float]]] = {a: [] for a in AXES}
    prefs: list[tuple[str, str]] = []       # (pair, 'tower'|'baseline')
    guesses: list[tuple[str, bool | None]] = []
    notes: list[tuple[str, str]] = []

    for r in rows:
        pair = (r.get("pair") or "").strip()
        desk = pair_of.get(pair)
        if desk is None:
            continue
        order = _order_for(desk["target_id"])   # index 0 -> Read 1
        arm_of_read = {"1": order[0], "2": order[1]}

        for axis in AXES:
            v1, v2 = _cell(r.get(f"r1_{axis}", "")), _cell(r.get(f"r2_{axis}", ""))
            if v1 is None or v2 is None:
                continue    # abstention on THIS axis only; never imputed
            scores = {arm_of_read["1"]: v1, arm_of_read["2"]: v2}
            per_axis[axis].append((pair, scores["tower"], scores["baseline"]))

        pref = (r.get("preferred") or "").strip()
        if pref in ("1", "2"):
            prefs.append((pair, arm_of_read[pref]))

        guess = (r.get("tower_guess") or "").strip().lower()
        if guess in ("1", "2"):
            guesses.append((pair, arm_of_read[guess] == "tower"))
        elif guess in ("unsure", "u", ""):
            guesses.append((pair, None))

        note = (r.get("notes") or "").strip()
        if note:
            notes.append((pair, note))

    out: list[str] = []
    A = out.append
    A("=" * 72)
    A("R5 ABLATION — tower composition vs one-shot baseline")
    A("=" * 72)
    A(f"pairs graded : {len(prefs)} of {len(desks)}")
    A(f"desks        : " + ", ".join(
        f"P{i}={d['country']}({d['regime']})" for i, d in enumerate(desks, start=1)
    ))
    A(f"baseline     : {bundle.get('baseline_model')} on "
      f"{bundle['core_component']} (self-hosted, $0), one call per desk")
    A("")

    # ---- axis deltas -----------------------------------------------------
    A("PER-AXIS DELTA (tower - baseline; positive = tower better)")
    A("-" * 72)
    A(f"{'axis':<14}{'n':>3}{'tower':>8}{'base':>8}{'delta':>8}   per-pair")
    axis_means: dict[str, float] = {}
    for axis in AXES:
        obs = per_axis[axis]
        if not obs:
            A(f"{axis:<14}{0:>3}{'-':>8}{'-':>8}{'-':>8}   (no scored pairs)")
            continue
        t = statistics.mean(o[1] for o in obs)
        b = statistics.mean(o[2] for o in obs)
        axis_means[axis] = t - b
        detail = " ".join(f"{o[0]}:{o[1] - o[2]:+.0f}" for o in obs)
        A(f"{axis:<14}{len(obs):>3}{t:>8.2f}{b:>8.2f}{t - b:>+8.2f}   {detail}")
    A("")
    if axis_means:
        A(f"mean delta across axes: {statistics.mean(axis_means.values()):+.2f}")
        A("")

    # ---- forced preference + sign test ------------------------------------
    tower_wins = sum(1 for _, a in prefs if a == "tower")
    base_wins = sum(1 for _, a in prefs if a == "baseline")
    p = _sign_test_two_sided(tower_wins, base_wins)
    A("FORCED PREFERENCE (the decision-relevant number)")
    A("-" * 72)
    A(f"tower preferred    : {tower_wins}")
    A(f"baseline preferred : {base_wins}")
    A(f"exact two-sided sign test: p = {p:.3f}")
    for pair, arm in prefs:
        A(f"  {pair:<4} -> {arm}")
    A("")

    # ---- the blind that may not have held ---------------------------------
    scored_guesses = [g for _, g in guesses if g is not None]
    n_correct = sum(1 for g in scored_guesses if g)
    A("HOW MUCH OF THIS RESTS ON A BLIND THAT HELD")
    A("-" * 72)
    if not scored_guesses:
        A("No arm guesses recorded (all 'unsure'). The blind is untested; treat")
        A("the result as if unblinded and weight it accordingly.")
    else:
        rate = n_correct / len(scored_guesses)
        A(f"arm guessed correctly: {n_correct}/{len(scored_guesses)} ({rate:.0%})")
        if rate >= 0.99:
            A("The grader identified every arm. The scores are UNBLINDED and carry")
            A("whatever prior the grader holds about the pipeline. This does not")
            A("void the result — a preference held while knowing which is which is")
            A("still a preference — but it downgrades a tower WIN substantially and")
            A("makes a tower LOSS *stronger* evidence, since the prior ran against it.")
        elif rate <= 0.34:
            A("Guesses are at or below chance: the blind effectively held. Take the")
            A("preference tally at face value.")
        else:
            A("Partial identification. Treat the result as semi-blinded.")
    A("")

    # ---- verdict -----------------------------------------------------------
    A("VERDICT")
    A("-" * 72)
    n = tower_wins + base_wins
    if n == 0:
        A("No forced preferences recorded — nothing to conclude.")
    elif tower_wins and base_wins:
        # BOTH arms won at least one desk. This is checked FIRST and regardless
        # of which is ahead: a 1-2 split is regime dependence, not a baseline
        # victory, and collapsing it into "the baseline won" would throw away the
        # most actionable reading the experiment can produce.
        ahead = (
            "the baseline is ahead" if base_wins > tower_wins
            else "the tower is ahead" if tower_wins > base_wins
            else "they are level"
        )
        A(f"MIXED — the pipeline won some desks and lost others ({ahead}, "
          f"{tower_wins}-{base_wins}).")
        A("")
        A("The most informative outcome. Read it as REGIME DEPENDENCE, and check")
        A("the per-pair deltas above against desk volume: the live hypothesis is")
        A("that composition earns its keep where signal is thin (a one-shot read")
        A("has nothing to work with) and loses where signal is thick (unit")
        A("summarization discards detail the raw slice still carries).")
        A("")
        A("That is an actionable architectural finding. It argues for routing by")
        A("desk volume rather than for or against the pipeline wholesale.")
        if base_wins > tower_wins:
            A("")
            A("Note: with the baseline ahead on the tally, review1 §9 still bites —")
            A("a pipeline this size losing the aggregate to one prompt belongs at")
            A("the top of STATUS.md, regime dependence or not.")
    elif base_wins >= tower_wins:
        A("THE BASELINE WON OR TIED.")
        A("")
        A("This is the finding. review1 §9: 'If no reported number moves, that is")
        A("the most important finding the project will ever produce and it belongs")
        A("at the top of STATUS.md.' review3 §9: absent a win, the receipts, the")
        A("seams and the chorus diff are 'ornamentation on a very well-documented")
        A("RSS reader with a diary.'")
        A("")
        A("n=3 is sufficient to ACT on this — a pipeline of this size failing to")
        A("beat one prompt does not need a larger sample to justify investigation.")
        A("It is not sufficient to conclude the pipeline has no value; the next")
        A("step is diagnostic (which axis lost, on which regime), not confirmatory.")
    elif tower_wins == n and n >= 1:
        A("THE TOWER SWEPT.")
        A("")
        A(f"review3 §9: 'If Legba wins, the cathedral has a congregation.' But")
        A(f"p={p:.3f} at n={n} — a clean sweep of 3 cannot clear 0.05 on a sign")
        A("test. This is a SCREENING PASS, not a result.")
        A("")
        A("It licenses the decision-grade run: >=10 desks through the same harness")
        A("(scripts/r5_ablation_harness.py run --desks ...). It does NOT license a")
        A("product claim, a README line, or a STATUS.md number until that lands.")
    A("")

    if notes:
        A("OPERATOR NOTES")
        A("-" * 72)
        for pair, note in notes:
            A(f"  {pair}: {note}")
        A("")

    A("CAVEATS THAT DO NOT GO AWAY WITH MORE n")
    A("-" * 72)
    A("* One grader. Inter-rater reliability is unmeasured and unmeasurable here.")
    A("* The tower arm is a LIVE artifact and the baseline a REPLAY: the tower")
    A("  additionally saw graph-structure rows and its own desk-grounding block")
    A("  (prior read, situation register, desk baseline). Withheld from the")
    A("  baseline deliberately — handing them over would leak the tower into the")
    A("  control — but it means the arms differ by slightly more than 'pipeline'.")
    A("* Both arms run on the same model family, so this measures the PIPELINE,")
    A("  not the model. A stronger baseline model is a different experiment.")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
