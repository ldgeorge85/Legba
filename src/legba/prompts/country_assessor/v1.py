# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``country_assessor`` DSPy prompt module — v1 (the GEPA parent).

The L-176 optimizer resolves ``country_assessor``'s parent prompt module
to ``legba.prompts.country_assessor.v1`` (convention
``legba.prompts.<analyst_id>.v1``). Before this module existed the path
failed to import and the optimizer emitted the honest declared-seam
``<<missing prompt module>>`` placeholder (SEAMS #12).

``country_assessor`` runs the ``inline_target`` analyst method, so its I/O
signature is identical to :class:`legba.prompts.inline_target.v1.ReasonSignature`
(``target_id`` + ``signals_block`` → finding fields). What differs is the
**instructions** — its baseline is the live country-assessor system prompt
(``inline_target._SYSTEM_PROMPT``), which is exactly what production inference
uses today. GEPA evolves *these instructions*; promoted candidate instruction
text becomes the analyst's live system prompt (the promotion loop), so the
thing optimized and the thing run stay one and the same.

This module imports ``dspy`` at top level — it lives in the ``legba.prompts``
package, which is imported ONLY by the optimizer / GEPA worker (where dspy is
installed), never on the runtime inference hot path.
"""

from __future__ import annotations

from typing import Any

import dspy

from legba.data.analysts.inline_target import _SYSTEM_PROMPT as _BASELINE_PROMPT

__all__ = [
    "CountryAssessorCycle",
    "CountryAssessmentSignature",
    "build",
    "BASELINE_INSTRUCTIONS",
]

#: GEPA's parent instructions — the live country-assessor system prompt.
#: Single source of truth: imported from the analyst so the prompt GEPA
#: evolves is byte-for-byte the prompt inference uses by default.
BASELINE_INSTRUCTIONS: str = _BASELINE_PROMPT


class CountryAssessmentSignature(dspy.Signature):
    """Placeholder docstring — the real instructions are bound at build time
    via ``with_instructions(BASELINE_INSTRUCTIONS)``; GEPA then evolves them.

    The field SHAPE matches the optimizer's training set, which the GEPA loop
    builds as ``dspy.Example(input=<rendered signals>, gold=<reference
    finding>).with_inputs("input")`` and scores via ``pred.answer`` vs gold
    (see ``gepa._run_gepa_loop`` + ``gepa._metric``). A single ``input →
    answer`` signature is therefore the correct optimization shape — GEPA
    evolves the *instructions*, and only that evolved instruction text is
    promoted into the analyst's live system prompt (inference still runs its
    own direct chat_complete; the field structure here never reaches it).
    """

    input: str = dspy.InputField(
        desc="Rendered substrate slice — recent signals for the target.",
    )
    answer: str = dspy.OutputField(
        desc="A single concise FINDING (strict JSON per the instructions).",
    )


class CountryAssessorCycle(dspy.Module):
    """country_assessor's prompt as a GEPA-optimizable DSPy module.

    One ``dspy.Predict`` over :class:`CountryAssessmentSignature` bound to the
    country-assessor baseline instructions, which GEPA optimizes. Construct
    with explicit ``instructions`` to seed from a promoted champion instead of
    the baseline.
    """

    def __init__(self, instructions: str | None = None) -> None:
        super().__init__()
        signature = CountryAssessmentSignature.with_instructions(
            instructions or BASELINE_INSTRUCTIONS
        )
        self.assess = dspy.Predict(signature)

    def forward(self, input: str) -> Any:  # type: ignore[override]  # noqa: A002
        return self.assess(input=input)


def build() -> CountryAssessorCycle:
    """Prompts-package convention entrypoint — a fresh module instance.

    Called by the optimizer's ``_import_prompt_module`` (GEPA student) and
    ``_load_parent_prompt_text`` (parent-text extraction).
    """
    return CountryAssessorCycle()
