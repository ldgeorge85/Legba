# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``competing_hypotheses`` (ACH) DSPy prompt module — v1.

Per L-105 §2.3. The GEPA optimization twin for the PIECE C ACH meta-analyst —
generates the SET of mutually-competing hypotheses (each with a MANDATORY
counter-thesis) over a focal topic's current evidence base. The runtime kind
calls ``chat_complete`` directly with the same instruction (see
``competing_hypotheses._SYSTEM_PROMPT``); this module is the optimizer-compilable
twin, lazy-imported only when an operator opts the analyst into GEPA, so this
file imports cleanly without dspy unless ``build()`` is called.
"""

from __future__ import annotations

from typing import Any

import dspy

__all__ = [
    "CompetingHypothesesSignature",
    "CompetingHypothesesCycle",
    "build",
]


class CompetingHypothesesSignature(dspy.Signature):
    """Analysis of Competing Hypotheses — generation step.

    Inputs:
        topic           — the focal topic / situation name.
        evidence_block  — the current evidence base (temporally-current facts,
                          linked findings, signed relationships), one line per
                          item.

    Outputs:
        rationale       — reasoning about which trajectories the evidence could
                          support, and what would discriminate between them.
        hypotheses      — a JSON list of >= 2 MUTUALLY-EXCLUSIVE hypotheses,
                          each an object {thesis, counter_thesis}. The
                          counter_thesis (the strongest case AGAINST) is
                          MANDATORY for every hypothesis — confirmation bias is
                          structurally forbidden in ACH.
    """

    topic: str = dspy.InputField(desc="The focal topic / situation name")
    evidence_block: str = dspy.InputField(
        desc="Current evidence base — temporally-current facts, linked "
             "findings, signed relationships; one item per line",
    )

    rationale: str = dspy.OutputField(
        desc="Reasoning about candidate trajectories and what evidence would "
             "discriminate between them",
    )
    hypotheses: list[dict] = dspy.OutputField(
        desc="JSON list of >= 2 mutually-exclusive hypotheses; each object is "
             "{thesis, counter_thesis} with a MANDATORY non-empty counter_thesis",
    )


class CompetingHypothesesCycle(dspy.Module):
    """Single-call ACH hypothesis-generation module. Optimizer-compilable."""

    def __init__(self) -> None:
        super().__init__()
        self.generate = dspy.ChainOfThought(CompetingHypothesesSignature)

    def forward(  # type: ignore[override]
        self,
        topic: str,
        evidence_block: str,
    ) -> Any:
        return self.generate(topic=topic, evidence_block=evidence_block)


def build() -> CompetingHypothesesCycle:
    """Return a fresh :class:`CompetingHypothesesCycle` instance."""
    return CompetingHypothesesCycle()
