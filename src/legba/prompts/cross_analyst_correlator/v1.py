# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-177 ``cross_analyst_correlator`` DSPy prompt module — v1.

Per L-105 §2.3.  Three-detector priority-ordered prompt: the LLM reads
a slice of recent analyst outputs (findings, predictions, meta-findings)
and picks the single most important cross-analyst relationship by
priority:

  1. CONTRADICTION — highest priority; two+ outputs claim mutually
                      exclusive facts about the same target/topic/time.
  2. BLIND_SPOT    — middle priority; a topic clearly present across
                      multiple inputs has no analyst output addressing it.
  3. AGREEMENT     — lowest priority; three+ outputs converge on the
                      same claim (confidence reinforcement).

The DSPy module exposes one ChainOfThought signature with the
``correlation_type`` discriminator as an output field — the optimizer
can compile candidates and the critic can grade per-type accuracy.
"""

from __future__ import annotations

from typing import Any

import dspy

__all__ = [
    "CorrelationSignature",
    "CrossAnalystCorrelatorCycle",
    "build",
]


class CorrelationSignature(dspy.Signature):
    """Priority-ordered cross-analyst relationship detection.

    Inputs:
        outputs_block          — pre-rendered slice of recent analyst
                                   outputs (one block per output with
                                   analyst_id, title, body, kind,
                                   target_id, tags, output_id).

    Outputs:
        rationale              — chain-of-thought walking the priority
                                   order: did a contradiction match?  If
                                   not, did a blind spot?  If not, an
                                   agreement?
        correlation_type       — one of: contradiction / blind_spot /
                                   agreement.  The kind handler downgrades
                                   contradictions to lower priority types
                                   if the citation defenses fail.
        title                  — finding title.
        body                   — finding body — explicitly reference
                                   analyst_ids by id.  For contradiction +
                                   agreement, cite at least TWO distinct
                                   analyst_id values (an analyst
                                   contradicting itself is out of scope).
        referenced_outputs     — list of analyst-output UUIDs cited.
        referenced_analyst_ids — list of analyst_ids cited.
        confidence             — 0.0-1.0 confidence.
        tags                   — free-form tags.
    """

    outputs_block: str = dspy.InputField(
        desc="Slice of recent analyst outputs — one block per output with "
             "analyst_id, title, body, kind, target_id, tags, output_id",
    )

    rationale: str = dspy.OutputField(
        desc="Priority-order walk: contradiction? blind spot? agreement? "
             "Explain the highest-priority hit",
    )
    correlation_type: str = dspy.OutputField(
        desc="One of: contradiction / blind_spot / agreement",
    )
    title: str = dspy.OutputField(desc="Correlation finding title")
    body: str = dspy.OutputField(
        desc="Body — reference analyst_ids by id; cite >=2 distinct "
             "analyst_ids for contradiction + agreement",
    )
    referenced_outputs: list[str] = dspy.OutputField(
        desc="Analyst-output UUIDs cited (must be drawn from outputs_block)",
    )
    referenced_analyst_ids: list[str] = dspy.OutputField(
        desc="Distinct analyst_ids cited",
    )
    confidence: float = dspy.OutputField(desc="0.0-1.0 confidence")
    tags: list[str] = dspy.OutputField(desc="Free-form tags")


class CrossAnalystCorrelatorCycle(dspy.Module):
    """Three-detector priority-ordered correlator.  Single-call surface.

    The kind handler in
    :mod:`legba.data.analysts.cross_analyst_correlator` runs the citation
    defenses (hallucinated-UUID + hallucinated-analyst checks +
    downgrade-with-audit-tag) post-call; this module's job is to surface
    the LLM-bearing step so the optimizer (L-176) can compile against it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.correlate = dspy.ChainOfThought(CorrelationSignature)

    def forward(self, outputs_block: str) -> Any:  # type: ignore[override]
        return self.correlate(outputs_block=outputs_block)


def build() -> CrossAnalystCorrelatorCycle:
    """Return a fresh :class:`CrossAnalystCorrelatorCycle` instance."""
    return CrossAnalystCorrelatorCycle()
