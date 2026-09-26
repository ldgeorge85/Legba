# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G1 — ADJUDICATION and THE TWO SHARES.

Ported from ``planning/PROGRAM1_2026-09-16/score_step2.py`` (the adjudication
rule and the shares) and ``PROOF_ROUND_2026-09-12/score_r4.py`` (the family
agreement the re-gate scores with).

THE ADJUDICATION RULE is a rule about AGREEMENT, not about truth: the label at
least TWO of the three families gave. No tie-break, no precedence ladder, no
"the strictest family wins" — R4 died of a precedence ladder. Where no two
agree the claim is ``split``, published as such, counted in the coverage
denominator and in NO numerator. An UNPARSEABLE majority is never promoted to a
label: two families that failed to answer have not agreed on anything about the
world.

THE ONE DIVERGENCE FROM THE HAND RUN, and it is deliberate. ``score_step2``
calls a lone family's label a ``split`` ("a lone family is a split, never a
published label") because in step 2 every claim had three families. Here the
ceiling can legitimately leave a claim with ONE — at
``LEGBA_GRADER_DAILY_CEILING_USD=0`` every claim has only F0, and under the
triage every ``silent`` claim does even when the ceiling is open. Calling all of
those ``split`` would publish nothing at all on a $0 deployment, so a
single-family claim takes that family's label and is FLAGGED
``single_family=True`` — carried onto the row, counted on the unit, and named
wherever the number is shown. The flag is the honesty; suppressing the label
would not have been more honest, only emptier.

THE TWO SHARES, each with the denominator it actually rests on:

  * ``correctness_share = contains / (contains + contradicts)`` over the claims
    the reference BEARS ON;
  * ``coverage_share = (contains + contradicts) / n`` — how much of what the
    unit said the reference bears on AT ALL.

A correctness share of 1.0 at a coverage of 0.05 is one claim confirmed and
nineteen the reference never touched. Neither denominator is ever silently
substituted for the other, and both are returned, so a reader never has to
reconstruct one from a percentage.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from ._correctness_grade import UNPARSEABLE

#: The two outcomes that are NOT labels.
SPLIT = "split"
UNPARSEABLE_ADJ = "unparseable"

#: Program 1's pre-registered bars (PREREG_P1 §6 == R4's D4 bars). Used by the
#: RE-GATE only; never re-applied to a live country's own prose.
POOLED_BAR = 0.75
PAIRWISE_BAR = 0.70


def adjudicate(
    verdicts: Mapping[str, str] | Sequence[str],
) -> tuple[str, int, bool]:
    """``(the adjudicated outcome, how many families gave it, single_family)``.

    ``verdicts`` may be a mapping family -> label or a bare sequence of labels;
    only the values matter.
    """
    if isinstance(verdicts, Mapping):
        values = [v for v in verdicts.values() if isinstance(v, str) and v]
    else:
        values = [v for v in verdicts if isinstance(v, str) and v]
    if not values:
        return UNPARSEABLE_ADJ, 0, False
    if len(values) == 1:
        only = values[0]
        if only == UNPARSEABLE:
            return UNPARSEABLE_ADJ, 1, True
        return only, 1, True
    counts = Counter(values)
    top, n_top = counts.most_common(1)[0]
    if n_top < 2:
        return SPLIT, n_top, False
    if top == UNPARSEABLE:
        return UNPARSEABLE_ADJ, n_top, False
    return top, n_top, False


def shares(counts: Mapping[str, int], n_claims: int) -> dict[str, Any]:
    """The two numbers, each with the denominator it actually rests on."""
    contains = int(counts.get("contains", 0))
    contradicts = int(counts.get("contradicts", 0))
    decided = contains + contradicts
    return {
        "contains": contains,
        "contradicts": contradicts,
        "silent": int(counts.get("silent", 0)),
        "split": int(counts.get(SPLIT, 0)),
        "unparseable": int(counts.get(UNPARSEABLE_ADJ, 0)),
        "decided_n": decided,
        "correctness_share": (
            round(contains / decided, 4) if decided else None
        ),
        "correctness_n": decided,
        "coverage_share": (
            round(decided / n_claims, 4) if n_claims else None
        ),
        "coverage_n": n_claims,
    }


def score_unit(
    claim_rows: Sequence[Mapping[str, Any]], *, analyst_id: str, grain: str
) -> dict[str, Any]:
    """One unit's counts, shares and per-family label distribution.

    ``claim_rows`` are the adjudicated per-claim dicts this module's
    :func:`adjudicate` produced, each carrying ``adjudicated``,
    ``label_by_family`` and ``single_family``.
    """
    counts = Counter(str(row["adjudicated"]) for row in claim_rows)
    per_family: dict[str, dict[str, int]] = {}
    for row in claim_rows:
        for family, label in (row.get("label_by_family") or {}).items():
            per_family.setdefault(family, {})
            per_family[family][label] = per_family[family].get(label, 0) + 1
    return {
        "analyst_id": analyst_id,
        "grain": grain,
        "n": len(claim_rows),
        "adjudicated": dict(sorted(counts.items())),
        "per_family_labels": {
            f: dict(sorted(v.items())) for f, v in sorted(per_family.items())
        },
        "n_single_family": sum(
            1 for row in claim_rows if row.get("single_family")
        ),
        **shares(counts, len(claim_rows)),
    }


# ---------------------------------------------------------------------------
# Family agreement — the RE-GATE's arithmetic (score_r4.family_agreement)
# ---------------------------------------------------------------------------


def _disagreeing(family: str, claim_id: str, verdict: Any) -> str:
    """The verdict as the agreement math should see it.

    An UNPARSEABLE becomes unique to its (family, claim), so it can never be
    equal to anything — PREREG_P1 §6's "UNPARSEABLE counts as disagreement",
    enforced in the DATA rather than by special-casing the shared function.
    """
    if verdict == UNPARSEABLE or not isinstance(verdict, str) or not verdict:
        return f"{UNPARSEABLE}::{family}::{claim_id}"
    return verdict


def family_agreement(
    labels_by_family: Mapping[str, Mapping[str, Any]],
    *,
    pooled_bar: float = POOLED_BAR,
    pairwise_bar: float = PAIRWISE_BAR,
) -> dict[str, Any]:
    """Pairwise AND pooled raw exact-label agreement over N grader families.

    ``labels_by_family`` is ``{family: {claim_id: verdict}}``.

    THE PAIRWISE FLOOR IS NOT DECORATION. Pooled >= 0.75 alone can be cleared by
    two families that agree carrying a third that does not; the floor is what
    stops one rogue family being averaged away.
    """
    names = sorted(labels_by_family)
    adjusted = {
        f: {
            c: _disagreeing(f, c, v)
            for c, v in (labels_by_family[f] or {}).items()
        }
        for f in names
    }
    pairs: list[dict[str, Any]] = []
    total_shared = 0
    total_agree = 0
    for i, fam_a in enumerate(names):
        for fam_b in names[i + 1:]:
            shared = sorted(set(adjusted[fam_a]) & set(adjusted[fam_b]))
            agree = sum(
                1 for k in shared if adjusted[fam_a][k] == adjusted[fam_b][k]
            )
            pairs.append({
                "a": fam_a, "b": fam_b, "n_shared": len(shared),
                "n_agree": agree,
                "rate": round(agree / len(shared), 4) if shared else None,
            })
            total_shared += len(shared)
            total_agree += agree
    pooled = round(total_agree / total_shared, 4) if total_shared else None
    weak = [
        p for p in pairs
        if p["rate"] is not None and p["rate"] < pairwise_bar
    ]
    if pooled is None:
        passed: bool | None = None
        verdict = (
            "UNMEASURED — no shared claims across families. A calibration gate "
            "with no overlap is not a gate."
        )
    else:
        passed = pooled >= pooled_bar and not weak
        weak_str = ", ".join(
            "{}x{}={}".format(p["a"], p["b"], p["rate"]) for p in weak
        )
        verdict = (
            f"pooled {pooled} against a {pooled_bar} bar"
            + (
                f" — PAIRWISE FLOOR BREACHED by {weak_str} (< {pairwise_bar}); "
                "one family cannot be averaged away by two that agree."
                if weak else "."
            )
        )
    return {
        "families": names, "n_families": len(names), "pairs": pairs,
        "n_shared": total_shared, "n_agree": total_agree,
        "pooled_rate": pooled, "pooled_bar": pooled_bar,
        "pairwise_bar": pairwise_bar, "pairs_below_floor": weak,
        "pass": passed, "verdict": verdict,
    }


__all__ = [
    "PAIRWISE_BAR",
    "POOLED_BAR",
    "SPLIT",
    "UNPARSEABLE_ADJ",
    "_disagreeing",
    "adjudicate",
    "family_agreement",
    "score_unit",
    "shares",
]
