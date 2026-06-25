# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-175 ``critic`` DSPy prompt module — v1.

Per L-105 §2.3.  Wraps the rubric-grader LLM call that the critic analyst
kind dispatches against another analyst's output.

The deterministic envelope (read analyzed output + rubric, assemble the
prompt, parse the LLM's scored JSON into a :class:`CritiquePayload`,
enforce the model-heterogeneity guard) lives in the kind handler in
:mod:`legba.data.analysts.critic`. This module is the optimization
surface twin (per Wave B prereq #4 — canonical
``legba.prompts.<kind>.v1`` path).

Cycle shape
-----------
Critic is a single-LLM-call analyst kind. The LLM receives:

  * ``analyzed_output`` — the analyzed analyst's output (title + body +
                          confidence + evidence + tags), rendered as a
                          single text block.
  * ``rubric``          — the analyzed analyst's
                          ``descriptor.eval.rubric`` string (free-form
                          per L-101 §4; convention is a JSON block with
                          named dimensions but plain text is accepted).
  * ``analyzed_analyst_id``
                        — the id of the analyst being graded (the LLM
                          can echo it in the critique narrative so the
                          audit row is grep-able).

And emits:

  * ``scores``          — dict of {dimension_name: 0.0-1.0 score}.
  * ``overall_score``   — 0.0-1.0 aggregated grade.
  * ``revision_delta``  — short string describing what the analyzed
                          analyst's prompt module should change to
                          improve future runs.  Surfaced to the L-176
                          optimizer (DSPy + GEPA) as a candidate
                          mutation hint.
  * ``confidence``      — 0.0-1.0 confidence in the grading itself
                          (separate from the analyzed output's own
                          confidence).
"""

from __future__ import annotations

from typing import Any

import dspy

__all__ = [
    "CriticJudge",
    "CriticSignature",
    "build",
]


class CriticSignature(dspy.Signature):
    """Rubric-graded judgment over one analyst output.

    Inputs:
        analyzed_output      — pre-rendered analyzed-output block (title,
                                body, confidence, evidence, tags).
        rubric               — the analyzed analyst's
                                ``descriptor.eval.rubric`` string.  Free-
                                form per L-101 §4; the LLM is asked to
                                identify named dimensions inside it.
        analyzed_analyst_id  — the id of the analyzed analyst (for the
                                LLM to echo in the critique narrative).

    Outputs:
        rationale            — chain-of-thought reasoning.
        scores               — dict mapping rubric-dimension name → 0.0-1.0
                                score.  Keys derive from the rubric; an
                                empty rubric yields an empty dict.
        overall_score        — 0.0-1.0 aggregated score (the judge picks
                                the aggregation — typically a weighted
                                mean — and explains it in ``rationale``).
        revision_delta       — short, actionable string describing what
                                should change in the analyzed analyst's
                                prompt module to improve future runs.
                                May be empty/None when nothing actionable
                                is surfaced.
        confidence           — 0.0-1.0 confidence in the grading itself.
    """

    analyzed_output: str = dspy.InputField(
        desc="Pre-rendered analyzed-output block (title, body, "
             "confidence, evidence, tags)",
    )
    rubric: str = dspy.InputField(
        desc="Rubric string from the analyzed analyst's "
             "descriptor.eval.rubric block",
    )
    analyzed_analyst_id: str = dspy.InputField(
        desc="Id of the analyst being graded",
    )

    rationale: str = dspy.OutputField(
        desc="Chain-of-thought reasoning for the score assignment",
    )
    scores: dict = dspy.OutputField(
        desc="Mapping {dimension_name: 0.0-1.0 score}; dimensions come "
             "from the rubric",
    )
    overall_score: float = dspy.OutputField(
        desc="0.0-1.0 aggregated grade",
    )
    revision_delta: str = dspy.OutputField(
        desc="Short actionable string: what should change in the "
             "analyzed analyst's prompt module.  May be empty.",
    )
    confidence: float = dspy.OutputField(
        desc="0.0-1.0 confidence in the grading itself",
    )


class CriticJudge(dspy.Module):
    """Single LLM-bearing critic-judge call as a DSPy module.

    The L-176 optimizer compiles candidates against the trace set;
    promoted candidates land as ``legba.prompts.critic.v2``, …
    """

    def __init__(self) -> None:
        super().__init__()
        self.judge = dspy.ChainOfThought(CriticSignature)

    def forward(  # type: ignore[override]
        self,
        analyzed_output: str,
        rubric: str,
        analyzed_analyst_id: str,
    ) -> Any:
        return self.judge(
            analyzed_output=analyzed_output,
            rubric=rubric,
            analyzed_analyst_id=analyzed_analyst_id,
        )


def build() -> CriticJudge:
    """Return a fresh :class:`CriticJudge` instance.

    Called by :func:`legba.data.analysts.critic.build_prompt_module`.
    """
    return CriticJudge()
