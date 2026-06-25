# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-172 ``meta_findings_synthesizer`` DSPy prompt module — v1.

Per L-105 §2.3.  Narrower-context second-order synthesis prompt — takes a
set of first-order FINDINGS produced by other analysts and emits ONE
meta-finding describing the higher-order pattern, convergent claim,
contradiction, or emergent narrative across them.

Token budget is narrower than ``inline_target`` (768 vs 1024) because
the input is already pre-distilled into findings — the synthesizer's job
is to compress further, not to elaborate.
"""

from __future__ import annotations

from typing import Any

import dspy

__all__ = [
    "MetaSynthesisSignature",
    "MetaFindingsSynthesizerCycle",
    "build",
]


class MetaSynthesisSignature(dspy.Signature):
    """Second-order synthesis step over OTHER analysts' findings.

    Inputs:
        findings_block          — pre-rendered set of first-order findings,
                                    one block per finding with title +
                                    body + analyst_id + confidence.
        contributing_analysts   — comma-separated list of analyst_ids
                                    contributing inputs to this synthesis.

    Outputs:
        rationale               — chain-of-thought identifying the higher-
                                    order pattern across the inputs.
        title                   — meta-finding title.
        body                    — meta-finding body — DO NOT re-state any
                                    individual finding verbatim; cite which
                                    analysts' findings ground each claim.
                                    If findings disagree, surface the
                                    disagreement rather than averaging.
        confidence              — 0.0-1.0 confidence.
        evidence                — short evidence strings.
        tags                    — free-form tags (meta_finding: True is
                                    stamped by the kind handler).
    """

    findings_block: str = dspy.InputField(
        desc="First-order findings to synthesize — one block per finding "
             "with title, body, analyst_id, confidence",
    )
    contributing_analysts: str = dspy.InputField(
        desc="Comma-separated analyst_ids that contributed inputs",
    )

    rationale: str = dspy.OutputField(
        desc="Reasoning identifying the higher-order pattern, convergent "
             "claim, contradiction, or emergent narrative",
    )
    title: str = dspy.OutputField(desc="Meta-finding title")
    body: str = dspy.OutputField(
        desc="Meta-finding body — cite analysts by id; surface "
             "disagreements explicitly; do not re-state findings verbatim",
    )
    confidence: float = dspy.OutputField(desc="0.0-1.0 confidence")
    evidence: list[str] = dspy.OutputField(desc="Short evidence strings")
    tags: list[str] = dspy.OutputField(desc="Free-form tags")


class MetaFindingsSynthesizerCycle(dspy.Module):
    """Single-call synthesis module.  Optimizer-compilable via L-176."""

    def __init__(self) -> None:
        super().__init__()
        self.synthesize = dspy.ChainOfThought(MetaSynthesisSignature)

    def forward(  # type: ignore[override]
        self,
        findings_block: str,
        contributing_analysts: str,
    ) -> Any:
        return self.synthesize(
            findings_block=findings_block,
            contributing_analysts=contributing_analysts,
        )


def build() -> MetaFindingsSynthesizerCycle:
    """Return a fresh :class:`MetaFindingsSynthesizerCycle` instance."""
    return MetaFindingsSynthesizerCycle()
