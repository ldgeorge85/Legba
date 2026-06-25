# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-178 ``consult_on_demand`` DSPy prompt module — v1.

Per L-105 §2.3.  The kind is a ReAct loop with ``MAX_TOOL_ROUNDS = 6``
rounds (then one forced-final = 7 LLM calls max).  Each round the LLM
either:

  * emits a tool call as strict JSON ``{"tool": "<name>", "args": {...}}``, or
  * emits the final answer as strict JSON
    ``{"final": true, "answer": "...", "uncertainty": 0.0-1.0,
       "cited_refs": ["<uuid>", ...], "unanswered_aspects": [...]}``

The DSPy module exposes one signature: the *per-round* decision step.
The kind handler's outer loop in
:mod:`legba.data.analysts.consult_on_demand` dispatches tool calls
between rounds and accumulates the resulting substrate context — that
orchestration stays in Python; only the LLM-bearing step is here so the
optimizer can compile candidates over the trace set.

The output shape models BOTH branches (tool call vs final answer) so a
single Predict step can serve either.  The kind handler reads
``response.is_final`` to route.
"""

from __future__ import annotations

from typing import Any

import dspy

__all__ = [
    "ConsultRoundSignature",
    "ConsultOnDemandRound",
    "build",
]


class ConsultRoundSignature(dspy.Signature):
    """One round of the ReAct loop.

    The LLM is given the operator's question + the context accumulated
    from previous tool calls + the available-tools catalogue, and emits
    EITHER a tool call OR the final answer.  Discrimination happens via
    ``is_final``: when true, the answer fields are populated and the tool
    fields are empty; when false, the tool fields are populated and the
    answer fields are empty.

    Inputs:
        question            — operator's question (free text).
        scope_predicate     — optional Starlark predicate scoping
                                substrate queries (passed through to
                                search_signals etc. unchanged).
        accumulated_context — rendered context block from previous tool
                                calls (concatenated tool-result summaries).
                                Empty on round 1.
        rounds_remaining    — int — number of LLM rounds left in the loop
                                (caps at MAX_TOOL_ROUNDS).  Set to 0 on the
                                forced-final round so the prompt nudges
                                the model toward a final answer.

    Outputs (branch A — tool call):
        is_final            — false on this branch.
        tool                — tool name (one of:
                                search_signals / query_facts /
                                inspect_entity / vector_search).
        tool_args_json      — JSON-encoded args dict for the chosen tool.

    Outputs (branch B — final answer):
        is_final            — true on this branch.
        answer              — final natural-language answer.
        uncertainty         — 0.0-1.0; >=0.7 when substrate doesn't fully
                                support the answer.
        cited_refs          — list of substrate UUIDs cited in the answer.
                                MUST be drawn from the accumulated context;
                                hallucinated UUIDs are dropped post-call.
        unanswered_aspects  — list of question-aspects not addressed.
    """

    question: str = dspy.InputField(desc="Operator's question")
    scope_predicate: str = dspy.InputField(
        desc="Optional Starlark scope predicate (empty string if absent)",
    )
    accumulated_context: str = dspy.InputField(
        desc="Rendered context from prior tool calls — empty on round 1",
    )
    rounds_remaining: int = dspy.InputField(
        desc="Rounds left in the loop; 0 means this is the forced final",
    )

    # Discriminator
    is_final: bool = dspy.OutputField(
        desc="True if this round emits the final answer; false to call a tool",
    )

    # Branch A — tool call
    tool: str = dspy.OutputField(
        desc="Tool name (search_signals / query_facts / "
             "inspect_entity / vector_search); empty when is_final=true",
    )
    tool_args_json: str = dspy.OutputField(
        desc="JSON-encoded args for the chosen tool; empty when is_final=true",
    )

    # Branch B — final answer
    answer: str = dspy.OutputField(
        desc="Final natural-language answer; empty when is_final=false",
    )
    uncertainty: float = dspy.OutputField(
        desc="0.0-1.0; >=0.7 when substrate evidence is thin",
    )
    cited_refs: list[str] = dspy.OutputField(
        desc="Substrate UUIDs cited (must be drawn from accumulated context)",
    )
    unanswered_aspects: list[str] = dspy.OutputField(
        desc="Question-aspects this round could not address from substrate",
    )


class ConsultOnDemandRound(dspy.Module):
    """Per-round LLM step for the consult_on_demand ReAct loop.

    The outer loop in :mod:`legba.data.analysts.consult_on_demand` calls
    :meth:`forward` once per round.  Optimizer-compilable via L-176.
    """

    def __init__(self) -> None:
        super().__init__()
        # ChainOfThought to expose the per-round rationale to the
        # critic + optimizer; the kind handler can either capture or
        # discard rationale text depending on the trace recording mode.
        self.step = dspy.ChainOfThought(ConsultRoundSignature)

    def forward(  # type: ignore[override]
        self,
        question: str,
        scope_predicate: str,
        accumulated_context: str,
        rounds_remaining: int,
    ) -> Any:
        return self.step(
            question=question,
            scope_predicate=scope_predicate,
            accumulated_context=accumulated_context,
            rounds_remaining=rounds_remaining,
        )


def build() -> ConsultOnDemandRound:
    """Return a fresh :class:`ConsultOnDemandRound` instance."""
    return ConsultOnDemandRound()
