# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X-1 option spec — the GATHER loop's evidence cap on an ``inline_target``
unit.

``method.options.evidence_char_cap`` bounds the evidence a multi-round GATHER
run carries in its message array: the rendered slice it starts from plus every
tool result folded back since. The code that reads it is
``legba.data.analysts.gather_context_budget``; the arithmetic of the derived
default is in that module's docstring and in docs/TUNABLES.md.

Split into its own leaf for the module-size gate, the ``handler_options_level``
idiom; stdlib + siblings only (this package is a leaf the slim registry
imports).
"""

from __future__ import annotations

from .handler_options_base import OptionSpec

#: The descriptor knob, ``method.options.evidence_char_cap``.
EVIDENCE_CHAR_CAP_OPTION = "evidence_char_cap"

#: Upper bound on an explicit value: a 262,144-token context at the house
#: chars/4 estimate. A value above it cannot bound anything on such a window.
EVIDENCE_CHAR_CAP_MAXIMUM = 1_048_576

GATHER_CONTEXT_OPTIONS: tuple[OptionSpec, ...] = (
    OptionSpec(
        EVIDENCE_CHAR_CAP_OPTION,
        "int",
        "The cap, in characters, on the evidence a GATHER run carries "
        "in its message array (the rendered slice plus every tool result). The "
        "slice is packed to 60% of it; past it, older tool results are "
        "replaced, oldest first and never the newest round, by a digest that "
        "keeps their [N] numbers and document ids; before every round the "
        "whole input is estimated (chars/4) and a round over the input budget "
        "(derived from the route's context window; LEGBA_LLM_INPUT_TOKEN_BUDGET "
        "overrides it) is not sent — the run writes with what it has. Absent ⇒ "
        "derived per run for a multi-round GATHER (max_rounds "
        ">= 2): 0.6 × the input budget × 4 chars, less the gather system "
        "prompt, floor 16,000; a single-round or tool-less run is untouched. "
        "An explicit value engages it on any GATHER run; 0 ⇒ off.",
        minimum=0,
        maximum=EVIDENCE_CHAR_CAP_MAXIMUM,
    ),
)

__all__ = [
    "EVIDENCE_CHAR_CAP_MAXIMUM",
    "EVIDENCE_CHAR_CAP_OPTION",
    "GATHER_CONTEXT_OPTIONS",
]
