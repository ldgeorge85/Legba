# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The correctness grader's RECEIPT — the sweep's own row (moved, unchanged).

Split out of ``correctness_grader`` (INST-1) to keep that module under the
1,500-line entry threshold of ``tests/test_module_size_gate.py`` when the
non-country roster and the keyed (deterministic) reference path landed — the
section banner was the author's own seam. The function is byte-for-byte the
one that lived there; ``correctness_grader.build_receipt`` re-exports it, so
every caller and test addresses it exactly as before. It reads the grader's
own constants through a lazy import (the grader imports this module at load).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from ...correctness_reference_currency import (
    DEFAULT_REFERENCE_GRACE_DAYS,
    REFERENCE_GRACE_ENV,
)
from ...provenance.models import FindingPayload
from . import _correctness_grade as GRADE
from . import _correctness_segment as SEG
from ._correctness_rubric import RUBRIC_NAME, RUBRIC_SHA256


def build_receipt(
    *,
    as_of: datetime,
    enabled: bool,
    targets: Sequence[str],
    per_target: Sequence[Mapping[str, Any]],
    calibration: Mapping[str, Any] | None,
    refusal: Mapping[str, Any] | None,
    ceiling_usd: float,
    spent_before: float,
    warnings: Sequence[str],
    grace_days: int = DEFAULT_REFERENCE_GRACE_DAYS,
    deferred: Sequence[str] = (),
) -> FindingPayload:
    """The sweep's own row — a counting-not-repairing receipt.

    It NEVER gates and NEVER alerts. It carries the caveat on its own face so a
    reader who sees only this row sees the limits with the numbers.
    """
    from .correctness_grader import (
        _RECEIPT_UNIT_CAP,
        _RECEIPT_WARNING_CAP,
        ENABLED_ENV,
        GRADER_PIPELINE_VERSION,
        SUB_HANDLER_NAME,
    )

    written = sum(int(t.get("n_units_written") or 0) for t in per_target)
    cost = round(sum(float(t.get("cost_usd") or 0.0) for t in per_target), 6)
    if not enabled:
        headline = f"correctness grading DISABLED ({ENABLED_ENV} is off)"
    elif refusal:
        headline = f"REFUSED — {refusal.get('reason')}"
    elif written:
        headline = (
            f"graded {written} unit(s) across {len(per_target)} target(s) "
            f"for ${cost:.4f}"
        )
    else:
        statuses = sorted({str(t.get("status")) for t in per_target})
        headline = f"wrote no unit numbers ({', '.join(statuses) or 'no targets'})"

    body = [
        f"Correctness grader — {headline}.",
        f"  as_of={as_of.isoformat()} rubric={RUBRIC_NAME} "
        f"sha={RUBRIC_SHA256[:16]}… pipeline={GRADER_PIPELINE_VERSION}",
        f"  ceiling=${ceiling_usd:.2f}/day spent_before=${spent_before:.4f} "
        f"targets={list(targets)}",
        f"  reference grace={grace_days}d ({REFERENCE_GRACE_ENV}) — a "
        "reference stays current until window_end + grace, or until a newer "
        "one supersedes it",
    ]
    if refusal:
        body.append(f"  REFUSED: {refusal.get('detail')}")
    if calibration:
        body.append(
            f"  calibration: pooled={calibration.get('pooled')} "
            f"packet={str(calibration.get('packet_sha'))[:16]}… "
            f"n_atoms={calibration.get('n_atoms')}"
        )
    for target in per_target[:_RECEIPT_UNIT_CAP]:
        age = target.get("reference_age_days")
        body.append(
            f"  - [{target.get('status')}] {target.get('target_id')}: "
            f"{target.get('n_units_written')} unit(s), "
            f"{target.get('n_claims')} claim(s), "
            f"${float(target.get('cost_usd') or 0.0):.4f}"
            + ("" if age is None else f", reference_age={float(age):.2f}d")
        )
        for unit in (target.get("units") or [])[:_RECEIPT_UNIT_CAP]:
            share = unit.get("correctness_share")
            coverage = unit.get("coverage_share")
            body.append(
                f"      {unit.get('analyst_id')}: correctness="
                + ("—" if share is None else f"{float(share) * 100:.1f}%")
                + f" (n={unit.get('correctness_n')})  coverage="
                + ("—" if coverage is None else f"{float(coverage) * 100:.1f}%")
                + f" (n={unit.get('coverage_n')})"
            )
    prior_relative = sum(
        n for target in per_target
        for reason, n in (
            (target.get("exclusions") or {}).get("excluded_by_reason") or {}
        ).items()
        if str(reason).startswith(f"{SEG.PRIOR_RELATIVE_REASON}:")
    )
    if prior_relative:
        body.append(
            f"  prior-relative spans excluded: {prior_relative} — claims about "
            "this read's relation to its OWN previous read. The reference "
            "carries no prior read, so grading them measured nothing "
            "(see per_target[].exclusions.excluded_by_reason)."
        )
    if deferred:
        body.append(
            f"  deferred ({len(deferred)}): {list(deferred)} — the turn "
            f"budget or the daily ceiling stopped the sweep before they ran; "
            "the least-recently-graded-first rotation puts them at the head "
            "of the next sweep's queue"
        )
    for warning in list(warnings)[:_RECEIPT_WARNING_CAP]:
        body.append(f"  WARNING: {warning}")

    return FindingPayload(
        title=f"Correctness grader — {headline}"[:2048],
        body="\n".join(body)[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME, "correctness_measurement",
              "severity:low"],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "pipeline_version": GRADER_PIPELINE_VERSION,
            "enabled": bool(enabled),
            "as_of": as_of.isoformat(),
            "rubric": RUBRIC_NAME,
            "rubric_sha256": RUBRIC_SHA256,
            "ceiling_usd": ceiling_usd,
            "ceiling_env": GRADE.CEILING_ENV,
            "reference_grace_days": grace_days,
            "reference_grace_env": REFERENCE_GRACE_ENV,
            "spent_before_run_usd": round(spent_before, 6),
            "run_cost_usd": cost,
            "targets": list(targets),
            "deferred_targets": list(deferred),
            "n_units_written": written,
            "calibration": dict(calibration) if calibration else None,
            "refusal": dict(refusal) if refusal else None,
            "per_target": [dict(t) for t in per_target],
            "n_prior_relative_excluded": prior_relative,
            "warnings": list(warnings),
            "caveat": (
                "Correctness here is correctness AGAINST ONE REFERENCE, not "
                "against the world: a claim the reference does not mention is "
                "`silent`, which is why every share is published beside its "
                "coverage. A `silent` claim carries ONE family's label by "
                "design (the cost triage), and at a $0 ceiling every claim "
                "does — see n_single_family. The producer family (F0) grades "
                "reads its own model wrote; that is disclosed, and "
                "VERDICT_P1v4 measured it not to be the outlier."
            ),
        },
    )


__all__ = ["build_receipt"]
