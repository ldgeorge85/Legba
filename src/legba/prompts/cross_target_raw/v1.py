# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-171 ``cross_target_raw`` DSPy prompt module — v1.

Per L-105 §2.3.  Mirrors the system+user prompt the kind handler in
:mod:`legba.data.analysts.cross_target_raw` constructs directly — wrapped
here as a typed :class:`dspy.Signature` + :class:`dspy.Module` so the
L-176 optimizer can compile candidates over a recorded trace set.

The kind handler keeps the direct ``chat_complete`` path (no dspy
hard-requirement at runtime) for environments where dspy isn't installed;
this module is the optimization-surface twin.

Cycle shape
-----------
``cross_target_raw`` is a broader-substrate variant of ``inline_target``
that reads signals across N targets and emits one FINDING describing the
cross-target pattern.  Single LLM-bearing call per run.  The
deterministic envelope (slice fetch / target_ids derivation / response
parsing) lives in the kind handler.
"""

from __future__ import annotations

from typing import Any

import dspy

__all__ = [
    "BroaderDataSignature",
    "CrossTargetRawCycle",
    "build",
]


class BroaderDataSignature(dspy.Signature):
    """Multi-target broader-substrate reasoning step.

    Inputs:
        target_ids       — comma-separated list of contributing target ids
                            (the analyst's resolved subscription scope).
        signals_block    — pre-rendered, multi-target signal slice.  Each
                            block is one signal with target_id, title,
                            produced_at, source, and snippet.

    Outputs:
        rationale        — chain-of-thought rationale describing the
                            cross-target pattern detected.
        title            — finding title (matches FindingPayload.title).
        body             — finding body — explicitly references which
                            target_ids contribute to each claim.
        confidence       — 0.0-1.0 confidence.
        evidence         — short evidence strings the operator UI surfaces.
        tags             — free-form tags (cross_target: True is stamped
                            by the kind handler post-call, not here).
    """

    target_ids: str = dspy.InputField(
        desc="Comma-separated contributing target ids",
    )
    signals_block: str = dspy.InputField(
        desc="Multi-target signal slice — one block per signal with "
             "target_id, title, produced_at, source, snippet",
    )

    rationale: str = dspy.OutputField(
        desc="Cross-target reasoning — observations only visible when "
             "N>1 targets are considered together",
    )
    title: str = dspy.OutputField(desc="Finding title")
    body: str = dspy.OutputField(
        desc="Finding body — cite the target_ids that contribute to "
             "each claim; do not state single-target observations",
    )
    confidence: float = dspy.OutputField(desc="0.0-1.0 confidence")
    evidence: list[str] = dspy.OutputField(
        desc="Short evidence strings (references to specific signal titles)",
    )
    tags: list[str] = dspy.OutputField(desc="Free-form tags")


class CrossTargetRawCycle(dspy.Module):
    """The cross_target_raw cycle as a DSPy module — single LLM-bearing call.

    The optimizer (L-176 GEPA) compiles this against the trace set;
    promoted candidates land as ``legba.prompts.cross_target_raw.v2``, …
    """

    def __init__(self) -> None:
        super().__init__()
        self.reason = dspy.ChainOfThought(BroaderDataSignature)

    def forward(self, target_ids: str, signals_block: str) -> Any:  # type: ignore[override]
        return self.reason(target_ids=target_ids, signals_block=signals_block)


def build() -> CrossTargetRawCycle:
    """Return a fresh :class:`CrossTargetRawCycle` instance.

    Called by :func:`legba.data.analysts.cross_target_raw.build_prompt_module`.
    """
    return CrossTargetRawCycle()
