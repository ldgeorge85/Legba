# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult answer's EVIDENCE FLOOR — uncertainty the evidence has to earn.

THE DEFECT THIS CLOSES (CONSULT_REVIEW_2026-09-28 §A, §B.3/§B.4). A consult
turn's ``uncertainty`` was the model's own header number, clamped to [0, 1]
and nothing else. It depended on no property of the run: session ``96d7bfc2``
made **two** tool calls, one of which carried a fabricated tool result in its
arguments, and reported ``uncertainty: 0.30`` — a 70 % confidence over
essentially nothing, which the platform published unchallenged.

So the number now has a floor, and the floor is keyed to the one measurement
that says how much substrate the answer actually rests on: the count of cited
refs that RESOLVE to a real row, from ``SubstrateQueryPort.classify_cited_refs``
— the same read the provenance census makes, made once and shared. Not the
count of refs the model wrote: a citation that resolves to nothing is exactly
the case the floor exists for.

    resolved   0        ->  uncertainty is at least 0.85
    resolved < 5        ->  at least 0.60
    resolved < 15       ->  at least 0.45
    resolved >= 15      ->  no floor; the model's own number stands

A floor never LOWERS a number. A model that says 0.9 over 40 resolved refs
keeps 0.9 — this corrects false confidence, it does not manufacture it.

NEVER A FABRICATED FLOOR. When the resolved count cannot be measured — no
classifier on the substrate (a hand-built embedder, a test double), a read
that failed — ``resolved`` is ``None`` and NO floor applies. An unmeasured run
is not a run with no evidence, and flooring it would be the same fabrication
in the other direction.

The floor is applied to the built payload rather than inside
``_build_consult_response`` so that both callers of that function — the live
loop in ``consult_on_demand`` and the re-synthesis path in
``consult_resynthesis``, which writes an answer over evidence already gathered
and paid for — get it from one place, after each has resolved its own cited
refs.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from ..provenance.models import ConsultResponsePayload

logger = logging.getLogger(__name__)

__all__ = [
    "UNCERTAINTY_FLOOR_BANDS",
    "apply_uncertainty_floor",
    "resolved_ref_count",
    "uncertainty_floor_for",
]

#: ``(resolved-ref count is BELOW this, floor)``, in order. The first band the
#: count falls under wins; a count past every band has no floor. Written as a
#: table rather than as an if-chain because it is a policy an operator reads.
UNCERTAINTY_FLOOR_BANDS: tuple[tuple[int, float], ...] = (
    (1, 0.85),
    (5, 0.60),
    (15, 0.45),
)


def uncertainty_floor_for(resolved: int | None) -> float | None:
    """The floor this many resolved refs earns — ``None`` for no floor.

    ``None`` in means ``None`` out: an unmeasured run is not floored.
    """
    if resolved is None:
        return None
    try:
        count = int(resolved)
    except (TypeError, ValueError):
        return None
    if count < 0:
        return None
    for ceiling, floor in UNCERTAINTY_FLOOR_BANDS:
        if count < ceiling:
            return floor
    return None


async def resolved_ref_count(
    substrate: Any, refs: Sequence[Any]
) -> tuple[Mapping[str, Any] | None, int | None]:
    """``(classification, resolved)`` for one answer's cited refs.

    ONE call to ``classify_cited_refs``, whose answer serves both the floor
    here and the provenance census downstream — the review's two fixes read
    the same rows, and reading them twice would let the two disagree.

    ``(None, None)`` when the substrate exposes no classifier: unmeasured.
    ``(None, 0)`` when it does and the answer cites NOTHING — that is a
    measurement, and it is the one the 0.85 band exists for.
    """
    classify = getattr(substrate, "classify_cited_refs", None)
    if not callable(classify):
        return None, None
    cited = [str(r) for r in (refs or ()) if str(r).strip()]
    if not cited:
        return None, 0
    out = await classify(refs=cited)
    if not isinstance(out, Mapping):
        return None, None
    # ``matched`` = cited ids that landed on a real row of ANY table (signals,
    # observations, findings, situations, hypotheses, events, nexuses);
    # ``resolved`` is the narrower signal/observation count. The floor is about
    # evidence that exists, so it reads ``matched`` and falls back to
    # ``resolved`` only for a classifier that predates it.
    raw = out.get("matched", out.get("resolved"))
    try:
        return out, max(0, int(raw))
    except (TypeError, ValueError):
        return out, None


def apply_uncertainty_floor(
    consult: ConsultResponsePayload, *, resolved: int | None
) -> ConsultResponsePayload:
    """Return ``consult`` with the evidence floor applied, or unchanged.

    Stamps ``data["uncertainty_floor_applied"] = {from, to, resolved}`` ONLY
    when the floor actually moved the number — the key's name is a claim that
    something happened, and stamping it on every run would make it noise.

    When the floor pushes uncertainty to the ``>= 0.7`` band and the model
    left ``unanswered_aspects`` empty, the question itself is surfaced as the
    unaddressed aspect — the same rule ``_build_consult_response`` applies to
    a model-declared high uncertainty, kept true for a server-declared one so
    the two cannot disagree about what a high-uncertainty answer looks like.
    """
    floor = uncertainty_floor_for(resolved)
    if floor is None:
        return consult
    current = float(consult.uncertainty)
    if current >= floor:
        return consult
    update: dict[str, Any] = {
        "uncertainty": floor,
        "data": {
            **consult.data,
            "uncertainty_floor_applied": {
                "from": current,
                "to": floor,
                "resolved": int(resolved) if resolved is not None else None,
            },
        },
    }
    if floor >= 0.7 and not consult.unanswered_aspects:
        update["unanswered_aspects"] = [consult.question[:512]]
    logger.info(
        "consult.evidence_floor.applied from=%.2f to=%.2f resolved=%s",
        current, floor, resolved,
    )
    return consult.model_copy(update=update)
