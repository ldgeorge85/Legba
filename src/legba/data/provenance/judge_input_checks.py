# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Input checks — the judge subsystem's third brick (R2 + R3, R-train 2026-08-05).

Every other verify check grades a finding against its CITATIONS. These two grade
it against what its producer was SHOWN — the composition's input set — and they
exist because the same sentence keeps turning up in review after review:

    *the instrument exists, the instrument is right, the instrument is advisory.*

Both checks were already computed, correctly, by deterministic code, and both
filed their verdicts in ``data.eval`` where nothing read them:

* **R3 — the buried lead.** ``_build_salience_check`` compares the composition's
  LEAD citation against its highest-consequence input and flags a burial past a
  0.30 magnitude gap. Its own worked example in the source is "a kinetic 0.9 lead
  vs a routine-procurement 0.2 lead" — which is, exactly, the live failure where
  250 howitzers led over a war. It was almost certainly stamping a FAILED verdict
  on that very composition, and by design it changed nothing.

* **R2 — the unsurfaced contradiction.** ``claim_contradiction`` compares the
  input findings' verified claims to each other and detects P ∧ ¬P. The
  composition prompt has always carried a rule about naming such disagreement in
  its ``## Tension`` section. A rule is a request; this is the check that the
  request was honoured.

Both fold as SOFT failures in the ``unhedged_periphery_citation`` shape — one
extra checkable-but-unsupported claim, one ledger row, one counter. Soft because
neither is a fabrication: the composition said something true and ordered or
framed it badly. That is a real defect with a real cost, and now a real number.

NOT prompt rules. A prompt rule asks a model to hold an ordering in prose; the
reason confidence became the sort key in the first place is that it was the only
number on the page. These put a consequence behind the computation.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)


def _verify():
    from . import verify
    return verify


#: R3 — the composition led on an input materially less consequential than its
#: top one. SOFT: the lead is a framing failure, not an invented fact.
BURIED_LEAD_SALIENCE = "buried_lead_salience"

#: R2 — the input set asserted P and ¬P and the composition did not surface it.
#: SOFT for the same reason, and it is the more serious of the two: composing a
#: contradiction into agreement is how "Hormuz is shut" and "no closure is in
#: place" became one confident paragraph.
UNSURFACED_CONTRADICTION = "unsurfaced_input_contradiction"

#: Words that mark a body as having ACTUALLY engaged a disagreement, rather than
#: merely owning a ``## Tension`` heading. Checked case-insensitively.
_TENSION_MARKERS = (
    "tension",
    "contradict",
    "disagree",
    "diverge",
    "conflict",
    "inconsist",
    "at odds",
    "incompatible",
)


def _eval_section(eval_block: Any, key: str) -> Any:
    if not isinstance(eval_block, Mapping):
        return None
    return eval_block.get(key)


def _fold_soft(
    report: Any, *, text: str, reason: str, markers: list[Any], counter: str
) -> Any:
    """Add ONE checkable-but-unsupported claim + its ledger row + its counter.

    Byte-identical arithmetic to ``verify._fold_guard_spans``: the denominator
    grows by one, the numerator does not, and the score is recomputed over the new
    denominator. Deliberately the same shape, so a soft failure raised here costs
    exactly what a soft failure raised anywhere else costs.
    """
    v = _verify()
    span = v.UnsupportedSpan(text=text[:2000], reason=reason, markers=list(markers))
    checkable = report.checkable_claims + 1
    supported = report.supported_claims
    out = v.FaithfulnessReport(
        faithfulness_score=(1.0 if checkable == 0 else supported / checkable),
        checkable_claims=checkable,
        supported_claims=supported,
        unsupported_spans=list(report.unsupported_spans) + [span],
        judge_status=report.judge_status,
        judge_unavailable_reason=report.judge_unavailable_reason,
        confidence_ceiling=report.confidence_ceiling,
        branch_scores=report.branch_scores,
        claim_verdicts=list(report.claim_verdicts)
        + [v.ClaimVerdict.failed(span.text, reason, list(markers))],
        counters=dict(report.counters),
        score_denominator=checkable,
        score_state=report.score_state,
        score_state_reason=report.score_state_reason,
    )
    out.bump(counter)
    return out


def fold_salience_lead(report: Any, *, eval_block: Any, body: str) -> Any:
    """R3 — promote ``data.eval.salience_check`` from advisory to COUNTED.

    Only a verdict of ``pass is False`` costs anything. ``pass is None`` is the
    check's own honest "not judgeable" (no resolvable lead citation, or an
    unscored lead) and must never be scored as a failure — treating an
    unmeasurable thing as a miss is the mistake this whole train is about.

    The span's TEXT is the lead claim itself where the body offers one, so the
    ledger row points at the sentence that opened the composition rather than at
    an abstraction. A missing lead claim degrades to the check's own reason
    string; it never blocks the fold.
    """
    check = _eval_section(eval_block, "salience_check")
    if not isinstance(check, Mapping) or check.get("pass") is not False:
        return report

    lead_ref = check.get("lead_ref")
    lead_text = ""
    if isinstance(lead_ref, int):
        marker = f"[[ref:{lead_ref}]]"
        for claim in _verify()._segment_claims(body or ""):
            if marker in claim:
                lead_text = claim
                break
    reason_note = str(check.get("reason") or "")[:400]
    logger.warning(
        "verify.salience.buried_lead lead_ref=%s gap=%s top=%r — counted as a "
        "soft faithfulness failure",
        lead_ref, check.get("gap"), str(check.get("top_title") or "")[:120],
    )
    return _fold_soft(
        report,
        text=lead_text or f"[lead citation ref {lead_ref}] {reason_note}",
        reason=BURIED_LEAD_SALIENCE,
        markers=[lead_ref] if isinstance(lead_ref, int) else [],
        counter="salience_lead_buried",
    )


def _surfaces_contradiction(body: str, a_ref: Any, b_ref: Any) -> bool:
    """Did the body actually engage this pair?

    Requires BOTH handles to appear AND some disagreement vocabulary somewhere in
    the prose. Citing both refs while describing them as agreeing is precisely the
    live failure, so ref presence alone cannot be the test; and disagreement
    vocabulary alone would pass a body that hedged about something else entirely.
    """
    low = (body or "").lower()
    if not any(m in low for m in _TENSION_MARKERS):
        return False
    return f"[[ref:{a_ref}]]" in body and f"[[ref:{b_ref}]]" in body


def fold_input_contradictions(report: Any, *, eval_block: Any, body: str) -> Any:
    """R2 — one soft failure per detected contradiction the body did not surface.

    A pair the composition DID name and adjudicate costs nothing — that is the
    behaviour the check wants, and charging for it would punish the fix. Only
    silence is charged, and it is charged per pair, because two ignored
    contradictions are worse than one.
    """
    detected = _eval_section(eval_block, "contradictions")
    if not isinstance(detected, (list, tuple)) or not detected:
        return report

    out = report
    for pair in detected:
        if not isinstance(pair, Mapping):
            continue
        a_ref, b_ref = pair.get("a_ref"), pair.get("b_ref")
        if _surfaces_contradiction(body, a_ref, b_ref):
            out.bump("input_contradiction_surfaced")
            continue
        subject = " ".join(str(s) for s in (pair.get("subject") or []))[:120]
        logger.warning(
            "verify.contradiction.unsurfaced refs=%s/%s state=%s subject=%r — the "
            "composition was shown P and not-P and did not name the disagreement",
            a_ref, b_ref, pair.get("group"), subject,
        )
        out = _fold_soft(
            out,
            text=(
                f"[[ref:{a_ref}]] vs [[ref:{b_ref}]] — the input set asserts "
                f"incompatible states of '{subject}' ({pair.get('group')}) and "
                f"this composition does not name the disagreement"
            ),
            reason=UNSURFACED_CONTRADICTION,
            markers=[r for r in (a_ref, b_ref) if isinstance(r, int)],
            counter="input_contradiction_unsurfaced",
        )
    return out


def fold_input_checks(report: Any, *, eval_block: Any, body: str) -> Any:
    """Both checks, in one call. No-op without an ``eval`` block (every unit
    finding, every pre-R2 composition) — byte-identical for those callers."""
    if not isinstance(eval_block, Mapping) or not eval_block:
        return report
    report = fold_salience_lead(report, eval_block=eval_block, body=body)
    return fold_input_contradictions(report, eval_block=eval_block, body=body)


__all__ = [
    "BURIED_LEAD_SALIENCE",
    "UNSURFACED_CONTRADICTION",
    "fold_input_checks",
    "fold_input_contradictions",
    "fold_salience_lead",
]
