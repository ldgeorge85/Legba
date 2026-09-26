# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The WIDTH plane's WRITES — one critique per graded READ, alerts per claim.

A sibling of :mod:`standing_auditor` rather than a section inside it, for the
reason the size gate exists: the handler stood at 1,492 lines against a
1,500-line entry threshold, and the house answer to a file at its ceiling is a
seam, never a raised ceiling. This module owns the two payload shapes the width
plane writes and the loop that persists them; it imports the plane's vocabulary
from :mod:`_external_audit_sampling` and nothing from the handler, so the
dependency runs ONE WAY.

THE ROW-COUNT ARITHMETIC THAT CHOSE THE CRITIQUE SHAPE (design 0.10, ruled F-4).
Per-claim critiques at width would be ~470 ``kind='critique'`` rows a day against
a fleet that produces ~580 faithfulness critiques a day — an ~80% shift in every
fleet critique aggregate, and no consumer splits on the
``title LIKE 'External audit%'`` prefix, so the shift would be silent. The
per-claim detail therefore lives in ``external_grades``, where it has SQL,
strata and append-only guarantees a JSON blob cannot give, and the critique
became the read-level roll-up (~70 rows/day) that keeps ``analyzed_output_id``
meaningful for the finding-critique join and the reads-API surface.

ALERTS STAY PER CLAIM, and they stay rare by construction: five preconditions
must all hold (CONTRADICTED · a registered Tier-1/2 decisive source · the span
resolved verbatim · the audited claim standing at high/critical · a SECOND model
family confirming over the same cached evidence). A contradiction is an operator
event, not a statistic — and, even confirmed, not a retraction.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from ...provenance import AnalystContext, write_analyst_output
from ...provenance.kinds import OutputKind
from ...provenance.models import AlertPayload, CritiquePayload
from ._external_audit_sampling import (
    ALERT_TRIGGER_CLASS,
    CRITIQUE_TITLE_PREFIX,
    EXTERNAL_AUDIT_DATA_KEY,
    SUB_HANDLER_NAME,
    pipeline_version,
)
from ._external_audit_width import (
    build_read_rollup,
    confirmations,
    group_by_read,
    pageable,
)

logger = logging.getLogger(__name__)


def analyst_ctx(
    *, analyst_id: str, analyst_version: str | None, run_id: UUID,
    target_id: str | None,
) -> AnalystContext:
    return AnalystContext(
        analyst_id=analyst_id,
        analyst_version=analyst_version or "0" * 16,
        run_id=run_id,
        target_id=target_id,
    )


def build_width_critique_payload(
    output_id: str, rollup: Mapping[str, Any], grades: Sequence[Any]
) -> CritiquePayload:
    """The ONE critique row for a graded read (design 0.10, ruled F-4).

    THE ARITHMETIC THAT CHOSE THIS SHAPE. At width the per-claim form would write
    ~470 ``kind='critique'`` rows a day against a fleet producing ~580
    faithfulness critiques a day — an ~80% shift in every fleet critique
    aggregate, bought for detail that no consumer reads off the title. So the
    per-claim detail moved to ``external_grades``, where it has SQL, strata and
    append-only guarantees a JSON blob cannot give, and this row became the
    read-level roll-up (~70/day) that keeps ``analyzed_output_id`` meaningful.

    ``overall_score`` keeps the shipped compute-and-show contract exactly: 1.0
    unless the read carried a CONTRADICTED claim. ``effective_confidence =
    min(confidence, overall_score)`` is a faithfulness-path gate, and a read
    whose claims were merely NOT_FOUND — the common case, and only ever a
    statement about the SEARCH — must not be quietly demoted by a degraded
    search plane.
    """
    first = grades[0]
    claim = first.claim
    contradicted = int(rollup.get("contradicted") or 0)
    accuracy = rollup.get("accuracy")
    desk = claim.desk_key or claim.analyst_id
    body_lines = [
        f"External audit of {claim.analyst_id} @ {desk} (output {output_id})",
        f"  claims: {rollup.get('n_claims')} · searched: "
        f"{rollup.get('n_searched')} · decided: {rollup.get('n_decided')}",
        f"  supported: {rollup.get('supported')} · contradicted: {contradicted} "
        f"· unchecked: {rollup.get('n_unchecked')} · uncheckable: "
        f"{rollup.get('n_uncheckable')}",
        f"  accuracy: {'unmeasured' if accuracy is None else round(accuracy, 4)} "
        f"· decided_rate: {rollup.get('decided_rate')}",
        f"  {rollup.get('status')}",
        # THE WHOLE ROUTE, NOT JUST THE FAMILY (2026-09-20). This line used to
        # read "grader: mistral (model unrecorded)" — and it said exactly that
        # for 2.7 days while every grader call 404'd, because OpenRouter had
        # removed the model the component pointed at and nothing on the row
        # named either the component or the model. Family stays (it is the
        # cross-family fence's own vocabulary); the component id is what an
        # operator repoints; the model id is what was on the wire.
        f"  grader: {rollup.get('grader_family')} "
        f"· {rollup.get('grader_component_id') or 'component unresolved'} "
        f"({rollup.get('grader_model_name') or 'model unrecorded'}"
        + (f", served by {rollup['grader_served_by']}"
           if rollup.get("grader_served_by") else "")
        + ")",
        f"  rubric: {rollup.get('rubric_version')} · pipeline: "
        f"{pipeline_version()}",
    ]
    if float(rollup.get("sample_fraction") or 1.0) < 1.0:
        body_lines.append(
            f"  SAMPLED: this read was graded at a fraction of "
            f"{rollup.get('sample_fraction')} — the day's budget could not "
            f"cover the population, and this is not a whole-read number"
        )
    if rollup.get("tier_unknown"):
        body_lines.append(
            f"  tier_unknown: {rollup['tier_unknown']} decisive verdict(s) rest "
            f"on a domain that is not in the source register"
        )
    for entry in rollup.get("claims") or []:
        body_lines.append(f"  - [{entry['verdict']}] {entry['claim']}")

    tags = [
        "external_audit",
        "external_audit_width",
        f"desk:{desk}",
        f"audited_analyst:{claim.analyst_id}",
    ]
    if claim.claim_severity:
        tags.append(f"audited_severity:{claim.claim_severity}")
    if contradicted:
        tags.append("verdict:contradicted")
    return CritiquePayload(
        title=f"{CRITIQUE_TITLE_PREFIX} — {claim.analyst_id} @ {desk}"[:2048],
        body="\n".join(body_lines)[:65536],
        confidence=1.0,
        evidence=sorted({
            u for g in grades for u in g.source_urls if u
        })[:25],
        tags=tags,
        rubric="external_world_check",
        analyzed_output_id=as_uuid(output_id),
        analyzed_analyst_id=claim.analyst_id[:256],
        judge_model=str(rollup.get("grader_model_name") or "")[:128],
        overall_score=0.0 if contradicted else 1.0,
        data={
            EXTERNAL_AUDIT_DATA_KEY: {
                "external_audit": True,
                "width": True,
                "pipeline_version": pipeline_version(),
                "sub_handler": SUB_HANDLER_NAME,
                **dict(rollup),
            }
        },
    )


def build_width_alert_payload(grade: Any) -> AlertPayload:
    """The ``kind='alert'`` row for a CONFIRMED high-severity contradiction.

    Five preconditions had to hold before this is reached (see
    ``_external_audit_width.pageable``) and the fifth is the one the rounds paid
    for: two model families agreed. Even then this is an OPERATOR EVENT, not a
    retraction — the row says so, because a page that reads as a retraction will
    be treated as one.
    """
    claim = grade.claim
    severity = str(claim.claim_severity or "high")
    return AlertPayload(
        title=(
            f"External audit CONTRADICTED a {severity} claim on "
            f"{claim.desk_key or claim.analyst_id}"
        )[:2048],
        body=(
            f"The standing external auditor checked a claim published by "
            f"{claim.analyst_id} @ {claim.desk_key} (read "
            f"{claim.graded_output_id}, desk head {claim.origin_head_id}) "
            f"against external search and found reporting that CONTRADICTS "
            f"it.\n\n"
            f"CLAIM: {claim.claim_text}\n"
            f"VERDICT RATIONALE: {grade.rationale}\n"
            f'EVIDENCE: "{grade.decisive_span}"\n'
            f"SOURCE: {grade.decisive_url} (tier "
            f"{grade.decisive_source_tier})\n"
            f"CONFIRMED BY: a second, independent model family over the same "
            f"cached evidence.\n"
            "\nThis is an EXTERNAL check, not a faithfulness verdict: the read "
            "may be perfectly faithful to the signals it cited and still be "
            "wrong about the world. It is also not a retraction — it is one "
            "contradiction, sourced and span-verified, that an operator should "
            "look at. Under the assembly the claim is a DESK's own sentence "
            "quoted verbatim, so the thing to check is the desk."
        )[:65536],
        confidence=1.0,
        evidence=list(grade.source_urls),
        tags=[
            "deterministic",
            SUB_HANDLER_NAME,
            f"trigger:{ALERT_TRIGGER_CLASS}",
            f"severity:{severity}",
            f"desk:{claim.desk_key}",
            "external_audit_width",
        ],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "trigger_class": ALERT_TRIGGER_CLASS,
            EXTERNAL_AUDIT_DATA_KEY: grade.as_dict(),
        },
        severity="critical" if severity == "critical" else "high",
        routing_hint=ALERT_TRIGGER_CLASS,
    )


def as_uuid(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


async def write_width_rows(
    conn: Any, result: Any, *, analyst_id: str, analyst_version: str | None,
    run_id: UUID,
) -> tuple[int, int, int]:
    """One critique per graded read + one alert per confirmed contradiction.

    Returns ``(critiques, alerts, write_failures)``. A rejected write is counted
    and logged, never raised: one malformed roll-up must not cost the tick its
    heartbeat, which is the row that proves the auditor is alive.
    """
    critiques = alerts = failures = 0
    confirmed = confirmations(result.graded)
    for output_id, grades in group_by_read(result.graded).items():
        claim = grades[0].claim
        ctx = analyst_ctx(
            analyst_id=analyst_id, analyst_version=analyst_version,
            run_id=run_id, target_id=claim.target_id,
        )
        derived = [u for u in (as_uuid(output_id),) if u is not None]
        rollup = build_read_rollup(output_id, grades)
        try:
            row, dead = await write_analyst_output(
                conn,
                analyst_ctx=ctx,
                kind=OutputKind.CRITIQUE,
                output_payload=build_width_critique_payload(
                    output_id, rollup, grades
                ),
                derived_from=derived,
                row_id=uuid4(),
            )
        except Exception as exc:
            logger.warning("standing_auditor.width_critique_raised err=%s", exc)
            failures += 1
            continue
        if dead is not None or row is None:
            logger.warning(
                "standing_auditor.width_critique_rejected read=%s reason=%s",
                output_id, getattr(dead, "reason", "schema_fail"),
            )
            failures += 1
            continue
        critiques += 1

        for grade in grades:
            if not pageable(grade, confirmed):
                continue
            try:
                arow, adead = await write_analyst_output(
                    conn,
                    analyst_ctx=ctx,
                    kind=OutputKind.ALERT,
                    output_payload=build_width_alert_payload(grade),
                    derived_from=derived,
                    row_id=uuid4(),
                )
            except Exception as exc:
                logger.warning("standing_auditor.width_alert_raised err=%s", exc)
                failures += 1
                continue
            if adead is not None or arow is None:
                failures += 1
                continue
            alerts += 1
    return critiques, alerts, failures


__all__ = [
    "analyst_ctx",
    "as_uuid",
    "build_width_alert_payload",
    "build_width_critique_payload",
    "write_width_rows",
]
