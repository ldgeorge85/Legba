# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-170 ``inline_target`` DSPy prompt module — v1.

Per L-105 §2.2.  Each LLM-based analyst kind exposes its prompt as a
``dspy.Module`` subclass with typed ``dspy.Signature``s.  The runtime
imports this module by string path (``legba.prompts.inline_target.v1``)
and instantiates :class:`InlineTargetCycle`.

This module imports ``dspy`` at top level — callers in environments
without dspy installed should guard with ``importlib.util.find_spec``
or catch ``ModuleNotFoundError`` (the kind handler's
:func:`legba.data.analysts.inline_target.build_prompt_module` does this).

Cycle phases as DSPy signatures
-------------------------------

The full 7-phase cycle (WAKE → ORIENT → PLAN → REASON+ACT → REFLECT →
NARRATE → PERSIST) is implemented in the kind handler
(:mod:`legba.data.analysts.inline_target`).  The DSPy module exposes
**only the LLM-bearing phases** as DSPy signatures so the optimizer
(L-176) can replay them deterministically.  The deterministic phases
(WAKE / PLAN / NARRATE / PERSIST + the ORIENT sort/trim step) are
pre/post-processing — they're inputs and outputs of ``forward``, not
DSPy calls themselves.

For v1 the only LLM-bearing phase is REASON+ACT (single Predict / CoT).
Future versions may split ORIENT into an LLM-driven slice-relevance
filter and REFLECT into an explicit critique-revise step (matching
``dspy.Refine``); both are L-176 / L-175 territory.
"""

from __future__ import annotations

from typing import Any

import dspy

__all__ = [
    "InlineTargetCycle",
    "ReasonSignature",
]


class ReasonSignature(dspy.Signature):
    """REASON+ACT phase — produce a structured FINDING from the substrate slice.

    Inputs:
        target_id     — the target this run is scoped to (free-text id).
        signals_block — pre-rendered, ORIENTed substrate slice (the
                        kind handler's ``_render_user_prompt`` output).
                        Each signal is one block with title +
                        produced_at + source + snippet.

    Outputs:
        rationale   — chain-of-thought rationale.  Captured into the
                      trace's ``intermediate_steps`` for the critic +
                      eval loop.
        title       — finding title (matches FindingPayload.title).
        body        — finding body (matches FindingPayload.body).
        confidence  — 0.0–1.0; matches FindingPayload.confidence.
        evidence    — list of evidence references — short strings the
                      operator UI surfaces alongside the finding.
        tags        — list of free-form tags.
    """

    target_id: str = dspy.InputField(desc="Target descriptor id")
    signals_block: str = dspy.InputField(
        desc="ORIENTed substrate slice — one block per signal with "
             "title, produced_at, source, and snippet",
    )

    rationale: str = dspy.OutputField(
        desc="Step-by-step reasoning identifying the most significant "
             "patterns or events in the slice",
    )
    title: str = dspy.OutputField(desc="Finding title")
    body: str = dspy.OutputField(desc="Finding body — concise, specific")
    confidence: float = dspy.OutputField(desc="0.0–1.0 confidence")
    evidence: list[str] = dspy.OutputField(
        desc="Evidence references (short strings)",
    )
    tags: list[str] = dspy.OutputField(desc="Free-form tags")


class InlineTargetCycle(dspy.Module):
    """The full inline_target cycle as a DSPy module.

    For v1 the cycle is single-shot: one ``ChainOfThought(ReasonSignature)``
    call.  The deterministic envelope (ORIENT trim, PLAN render, REFLECT
    parse, NARRATE stamp) lives in the kind handler — DSPy sees only
    the LLM-bearing call so optimizer replay is bounded to that surface.

    The optimizer (L-176 GEPA) compiles this module against the trace
    set; promoted candidates land as ``legba.prompts.inline_target.v2``,
    ``v3``, … per L-105 §2.3.
    """

    def __init__(self) -> None:
        super().__init__()
        # ChainOfThought adds an explicit `rationale` step before output
        # generation — per L-105 §2.1 the default REASON-phase composer.
        self.reason = dspy.ChainOfThought(ReasonSignature)

    def forward(self, target_id: str, signals_block: str) -> Any:  # type: ignore[override]
        """One forward pass = one inline_target REASON+ACT phase.

        Returns the dspy.Prediction with fields matching
        :class:`ReasonSignature`.  The kind handler's REFLECT step
        coerces this into a :class:`FindingPayload`.
        """
        return self.reason(target_id=target_id, signals_block=signals_block)
