# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""D4b — FLOOR-TRIGGERED JUDGING: the composition floor may only exclude on
JUDGED evidence.

THE DEFECT (CAMPAIGN_2026-08-29 / PREMISE_GRADING_LOOP §A-3). ``J2`` made LLM
judging a SAMPLE: ``JudgeSamplingPolicy.should_judge()`` is a SHA-256 coin flip
over the finding's own UUID, content-independent by design. Measured live over
14 days on ``score_state='scored'`` rows:

===============  =====  =================  =====================
arm              n      mean overall       % below the 0.50 floor
===============  =====  =================  =====================
llm (judged)     1,898  0.853 (raw 0.740)  3.2%  (61)
unsampled        4,807  0.571 (raw 0.554)  24.2% (1,165)
===============  =====  =================  =====================

Same direction on 8/8 desks with n>20 in both arms, mean gap +0.24. The judge
RESCUES claims the deterministic floor structurally cannot support — it is TOLD
that a markerless claim is synthesis, where the floor calls it ``no_citation``
(93.7% of the unsampled arm's failure mass, and 0.0% of the judged arm's).

That asymmetry would be a measurement curiosity if the score were only
published. It is not: ``meta_findings_synthesizer.read_other_analyst_findings``
INNER-JOINs the latest faithfulness critique and admits a sub-claim only when
``LEAST(f.confidence, v.overall_score) >= verify_floor`` (default 0.50). So an
unjudged finding is **7.6x** more likely to be excluded from every composition
that would otherwise consume it — 1,263 of 6,905 scored findings in 14 days,
decided substantially by the low 64 bits of a UUID.

THE RULE THIS MODULE IMPLEMENTS, in one line:

    a finding that the sampling gate did NOT judge, and that would therefore be
    EXCLUDED by the composition floor, is sent to the judge BEFORE the floor
    gets to exclude it.

Two properties that make this a selection fix and NOT a judge-behavior change:

  * **The judge does the SAME thing to an escalated finding as to a sampled
    one.** The escalation is literally a re-entry into
    ``verify_finding_faithfulness`` with the same arguments and the sampling
    policy opened (``rate=1.0``) — the same prompts, the same rubrics, the same
    folds, in the same order. Nothing about how any individual finding is
    GRADED changes, so ``JUDGE_PIPELINE_VERSION`` is NOT bumped: that key
    partitions populations graded by different PIPELINES, and there is only one
    pipeline here. What changes is WHICH findings the pipeline sees.
  * **The population stays legible.** Every critique the live seam writes now
    carries ``data.verification.judge_trigger``:
    ``'sampled'`` (the ordinary policy decided — hash gate, always-list, or no
    gate at all) or ``'floor_escalation'`` (the judge ran ONLY because the
    finding was about to be floored). An escalated row is judge_status='llm'
    but is NOT a member of the sampled arm, and any A-3-style arm comparison
    must exclude it. Escalated rows additionally carry
    ``pre_escalation_overall_score`` / ``pre_escalation_judge_status`` — the
    verdict the unsampled arm WOULD have published — so the unsampled arm's
    score distribution stays reconstructable after this ships. Without that key
    the fix would silently destroy the very measurement that found the defect
    (the unsampled arm would become truncated at the floor).

BOUNDED COST. Only the would-be-floored slice of the unsampled arm escalates:
measured 66.6/day over the last 7 days, 58.9/day after the confidence guard
below, against ~136 judge calls/day today.

RELATION TO THE SAMPLE-RATE RAISE (D4a). At ``judge_sample_rate=1.0`` this path
goes QUIET by construction — nothing is unsampled, so nothing escalates. That
is the point. D4a buys coverage and can be walked back the moment the judge
plane's budget moves; this rule is the STRUCTURAL INVARIANT that keeps the
floor honest at ANY rate, including the one an outage or a budget cut imposes.

REJECTED ALTERNATIVES (recorded so they are not re-proposed):

  * **(b) Band-neutral inclusion** — admit the below-floor unjudged finding
    into the composition with an ``unverified`` marker. Rejected: it does not
    remove the coin flip, it moves it downstream onto the composer's prompt,
    and it weakens the README-level claim that only VERIFIED sub-claims
    compose. The floor exists because an unsupported claim entering a
    composition is laundered into the system's own voice; a marker in a
    JSON field does not survive that laundering.
  * **(c) Per-arm thresholds** — recalibrate a separate, lower floor for the
    unsampled arm's distribution. Rejected: it CONCEALS the defect instead of
    fixing it. The two arms' distributions differ because one is graded by a
    rule measured (n=10, PANEL_2026-08-16) at a 64% false-alarm rate; fitting a
    threshold to a miscalibrated grader's output blesses the miscalibration,
    and it makes the published floor a function of which arm a finding landed
    in — i.e. still a property of the UUID hash, now with a second number to
    keep calibrated per desk, per stamp, forever.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .judge_assessability import (
    JUDGE_STATUS_UNSAMPLED,
    SCORE_STATE_SCORED,
    JudgeSamplingPolicy,
    gate_score,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .verify import FaithfulnessReport

logger = logging.getLogger(__name__)


#: ``judge_trigger`` for a critique whose judge decision was the ORDINARY
#: policy: the J2 hash gate selected it, the always-list covered it, or no gate
#: was configured at all. This is the SAMPLED population — the one every arm
#: comparison (A-3) is entitled to pool.
JUDGE_TRIGGER_SAMPLED = "sampled"

#: ``judge_trigger`` for a critique the judge produced ONLY because the
#: deterministic verdict was about to be excluded by the composition floor.
#: NOT a member of the sampled population: escalation conditions on the SCORE,
#: so pooling these with sampled rows biases any arm mean downward-then-upward
#: in a way no confidence interval can express.
JUDGE_TRIGGER_FLOOR_ESCALATION = "floor_escalation"

#: Counter (sparse, lands in ``data.verification.counters``) on a critique the
#: escalation actually produced.
COUNTER_ESCALATED = "judge_floor_escalation"

#: Counter on the ORIGINAL unsampled report when escalation was warranted but
#: the judge did not grade (down, flag off, errored). The row stays in the
#: unsampled arm — J2's invariant that population membership never depends on
#: judge health is preserved deliberately, and this counter is how the operator
#: sees the floor going un-adjudicated instead of it happening silently.
COUNTER_ESCALATION_UNAVAILABLE = "judge_floor_escalation_unavailable"

#: Counter when the escalation attempt RAISED. Degrade-not-drop: the original
#: verdict ships, the run never breaks.
COUNTER_ESCALATION_FAILED = "judge_floor_escalation_failed"

#: The OPS kill switch. Default ON (D4b is an approved production fix); set to
#: ``0``/``false``/``no``/``off`` to restore pre-D4b behavior with no deploy —
#: the escalation is the only thing that stops, the sampling gate is untouched.
ESCALATION_ENABLED_ENV = "LEGBA_JUDGE_FLOOR_ESCALATION"

#: THE FLOOR THIS RULE DEFENDS. Deliberately the SAME env var and the SAME
#: default as ``meta_findings_synthesizer.VERIFY_FLOOR_ENV`` /
#: ``DEFAULT_VERIFY_FLOOR``, because an escalation floor that drifted from the
#: composition floor would either escalate findings nothing excludes (waste) or
#: leave excluded findings un-adjudicated (the defect, back again).
#:
#: The constants are COPIED rather than imported: ``data.provenance`` is a
#: layer BELOW ``data.analysts`` (the analysts import provenance, never the
#: reverse), and the kind module's own docstring pins it as standalone. The
#: copy is held to the original by
#: ``tests/data_pkg/test_judge_floor_escalation.py::
#: test_escalation_floor_tracks_the_composition_floor``, which imports both and
#: asserts they are the same env name and the same number — so a future change
#: to one turns that test red instead of silently splitting the two.
ESCALATION_FLOOR_ENV = "LEGBA_COMPOSITION_VERIFY_FLOOR"
DEFAULT_ESCALATION_FLOOR: float = 0.50

_FALSEY = {"0", "false", "no", "off", ""}


def escalation_enabled() -> bool:
    """Is floor-triggered judging ON? (default yes; env kill switch)."""
    raw = os.getenv(ESCALATION_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSEY


def resolve_escalation_floor(default: float = DEFAULT_ESCALATION_FLOOR) -> float:
    """The composition floor this rule defends, clamped to ``[0, 1]``.

    Same parse as ``meta_findings_synthesizer._resolve_verify_floor``: a
    malformed env value WARNS and falls back to the default rather than
    silently disabling the guard (a floor of 0 read from a typo would turn the
    escalation off and nothing would say so).
    """
    raw = os.getenv(ESCALATION_FLOOR_ENV)
    if raw is not None:
        try:
            return max(0.0, min(1.0, float(raw)))
        except (ValueError, TypeError):
            logger.warning(
                "judge_floor_escalation.floor.bad_env value=%r — using default %.2f",
                raw, default,
            )
    return default


def published_overall(report: "FaithfulnessReport") -> float:
    """The number the composition floor will actually compare.

    ``read_other_analyst_findings`` reads ``cr.data->>'overall_score'``, which
    is ``gate_score(...)`` — the T7 ceiling, the unassessable cap and the
    PROVISIONAL cap folded in — NOT the raw tally. Deciding escalation on the
    raw ``faithfulness_score`` would escalate the wrong findings in both
    directions (a zero-claim finding's raw tally is 1.0; a provisional row's
    published number is capped at 0.85).
    """
    return gate_score(
        score=report.faithfulness_score,
        ceiling=report.confidence_ceiling,
        score_state=report.score_state,
        provisional=report.provisional,
    )


def should_escalate(
    report: "FaithfulnessReport",
    *,
    policy: JudgeSamplingPolicy | None,
    floor: float | None,
    judge_llm: Any | None,
    floor_confidence: float | None,
) -> bool:
    """Would the composition floor exclude this finding on UNJUDGED evidence?

    Every one of these gates is a reason the judge could not change the
    outcome, so each one is a judge call NOT spent:

    ``policy``/``floor`` absent
        No sampling gate configured, or no floor to defend → nothing to fix.
        (An UNGATED caller — every replay harness and pre-J2 test — is
        byte-identical.)
    ``judge_llm`` is ``None``
        Nothing to escalate TO. Re-running would publish a ``deterministic``
        label in place of the honest ``unsampled`` one, i.e. it would move the
        row's arm on judge availability — the exact thing J2 forbids.
    ``judge_status != 'unsampled'``
        Only the SAMPLING arm is escalated. A row the judge already graded is
        excluded on judged evidence (correct, by definition), and a
        ``deterministic`` row is a judge-HEALTH state, not a coin flip — its
        fix is the judge coming back, not a second call into a dead plane.
    ``score_state != 'scored'``
        An UNASSESSABLE finding extracted no gradeable claim. There is nothing
        for the judge to adjudicate, and its ``UNASSESSABLE_GATE_SCORE`` (0.5)
        cap is an honest statement of absence, not a coin flip.
    ``floor_confidence < floor``
        The floor is ``LEAST(confidence, overall_score)``. A finding whose OWN
        asserted confidence is already under the bar is excluded no matter what
        the judge says — 297 of 1,165 such rows in the measured 14 days, i.e.
        ~25% of the naive escalation volume, spent on a foregone conclusion.
    """
    if policy is None or floor is None or judge_llm is None:
        return False
    if report.judge_status != JUDGE_STATUS_UNSAMPLED:
        return False
    if report.score_state != SCORE_STATE_SCORED:
        return False
    if floor_confidence is not None and float(floor_confidence) < floor:
        return False
    return published_overall(report) < floor


def open_policy(policy: JudgeSamplingPolicy) -> JudgeSamplingPolicy:
    """The SAME policy with the gate opened (``rate=1.0`` ⇒ judge everything).

    ``rate=1.0`` rather than ``policy=None`` on purpose: the identity fields
    (finding id, kind, analyst id) ride along unchanged, so the escalated pass
    is the byte-identical call a SAMPLED finding of this identity would have
    made. Nothing else about the pass differs.
    """
    return dataclasses.replace(policy, rate=1.0)


@dataclass(frozen=True)
class EscalationOutcome:
    """What the seam should publish: the report, plus the provenance that says
    HOW the judge decision was reached."""

    report: "FaithfulnessReport"
    #: :data:`JUDGE_TRIGGER_SAMPLED` or :data:`JUDGE_TRIGGER_FLOOR_ESCALATION`.
    trigger: str = JUDGE_TRIGGER_SAMPLED
    #: ESCALATED ROWS ONLY — the published number the unsampled arm would have
    #: carried. ``None`` on the ordinary path. Keeps the A-3 arm comparison
    #: computable after this ships.
    pre_escalation_overall_score: float | None = None
    #: ESCALATED ROWS ONLY — the arm label that verdict would have carried
    #: (always ``'unsampled'`` today; recorded rather than assumed).
    pre_escalation_judge_status: str | None = None

    @property
    def escalated(self) -> bool:
        return self.trigger == JUDGE_TRIGGER_FLOOR_ESCALATION


async def verify_with_floor_escalation(
    *,
    escalation_floor: float | None = None,
    floor_confidence: float | None = None,
    **kwargs: Any,
) -> EscalationOutcome:
    """``verify_finding_faithfulness``, with the floor's exclusion made
    conditional on JUDGED evidence.

    Runs the ordinary pass first. If (and only if) the result is an UNSAMPLED
    verdict that the composition floor would exclude, re-runs the IDENTICAL
    pass with the sampling gate opened, and publishes THAT verdict instead.

    Parameters
    ----------
    escalation_floor:
        The composition floor to defend (see :func:`resolve_escalation_floor`).
        ``None`` ⇒ no escalation, byte-identical to calling
        ``verify_finding_faithfulness`` directly.
    floor_confidence:
        The finding's OWN ``confidence``, used ONLY for the escalation
        predicate (the floor folds ``LEAST(confidence, overall_score)``).
        Deliberately SEPARATE from the ``finding_confidence`` verify argument,
        which is the T7 hedge-laundering input the unit path must keep at
        ``None`` — passing one as the other would silently switch on T7 for
        every unit finding in the fleet.
    kwargs:
        Passed through to ``verify_finding_faithfulness`` unchanged.

    Never raises anything the plain pass would not: an escalation that fails
    degrades to the original verdict with a counter, because a floor that
    cannot be adjudicated is a worse outcome than a broken run, but a broken
    run is worse than both.
    """
    from .verify import verify_finding_faithfulness

    policy: JudgeSamplingPolicy | None = kwargs.get("judge_sampling")
    report = await verify_finding_faithfulness(**kwargs)

    if escalation_floor is not None and not escalation_enabled():
        return EscalationOutcome(report=report)
    if not should_escalate(
        report,
        policy=policy,
        floor=escalation_floor,
        judge_llm=kwargs.get("judge_llm"),
        floor_confidence=floor_confidence,
    ):
        return EscalationOutcome(report=report)

    assert policy is not None  # narrowed by should_escalate
    was_status = report.judge_status
    was_overall = round(published_overall(report), 4)
    try:
        escalated = await verify_finding_faithfulness(
            **{**kwargs, "judge_sampling": open_policy(policy)}
        )
    except Exception as exc:  # pragma: no cover — verify must never break a run
        report.bump(COUNTER_ESCALATION_FAILED)
        logger.warning(
            "judge_floor_escalation.failed finding_id=%s overall=%.3f floor=%.2f "
            "err=%s — the ORIGINAL unsampled verdict ships and the floor will "
            "exclude it on unjudged evidence",
            getattr(policy, "finding_id", "?"), was_overall, escalation_floor, exc,
        )
        return EscalationOutcome(report=report)

    if escalated.judge_status != "llm":
        # The judge was wired but did not grade (flag off, down, degraded). Keep
        # the ORIGINAL verdict: J2's invariant is that arm membership never
        # depends on judge health, and republishing this as 'deterministic'
        # would break exactly that. Counted so the gap is visible.
        report.bump(COUNTER_ESCALATION_UNAVAILABLE)
        logger.warning(
            "judge_floor_escalation.judge_unavailable finding_id=%s overall=%.3f "
            "floor=%.2f reason=%s — floor exclusion stays UN-ADJUDICATED",
            getattr(policy, "finding_id", "?"), was_overall, escalation_floor,
            escalated.judge_unavailable_reason,
        )
        return EscalationOutcome(report=report)

    escalated.bump(COUNTER_ESCALATED)
    logger.info(
        "judge_floor_escalation.judged finding_id=%s floor=%.2f pre=%.3f post=%.3f "
        "rescued=%s",
        getattr(policy, "finding_id", "?"), escalation_floor, was_overall,
        published_overall(escalated),
        published_overall(escalated) >= escalation_floor,
    )
    return EscalationOutcome(
        report=escalated,
        trigger=JUDGE_TRIGGER_FLOOR_ESCALATION,
        pre_escalation_overall_score=was_overall,
        pre_escalation_judge_status=was_status,
    )


__all__ = [
    "COUNTER_ESCALATED",
    "COUNTER_ESCALATION_FAILED",
    "COUNTER_ESCALATION_UNAVAILABLE",
    "DEFAULT_ESCALATION_FLOOR",
    "ESCALATION_ENABLED_ENV",
    "ESCALATION_FLOOR_ENV",
    "EscalationOutcome",
    "JUDGE_TRIGGER_FLOOR_ESCALATION",
    "JUDGE_TRIGGER_SAMPLED",
    "escalation_enabled",
    "open_policy",
    "published_overall",
    "resolve_escalation_floor",
    "should_escalate",
    "verify_with_floor_escalation",
]
