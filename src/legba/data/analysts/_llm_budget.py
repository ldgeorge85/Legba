# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ONE input-token budget every producer packs against (F-D, 2026-08-03).

``LEGBA_LLM_INPUT_TOKEN_BUDGET`` has bounded the UNIT path's signal slice since
the LLM-planes work: ``inline_target`` packs recency-ordered signals until the
estimated input-token budget is reached (default 32,000), and the core plane
never caps output. That is the window a leaf desk reads its world through.

The COMPOSITION path referenced it NOWHERE. ``meta_findings_synthesizer`` packed
against two fixed character constants — 15 input findings at a 600-char body
excerpt each — which is roughly 2,250 estimated tokens, about **7% of the budget
the leaves get**. Every tier above the units was reading its inputs through a
keyhole, and the tier that is supposed to see across desks saw the least.

This module holds the env name, the default, and the chars/4 estimator once, so
"the composition gets the same budget the leaves get" is a shared definition
rather than a copied constant that can drift. Both producers import from here;
``inline_target`` keeps its historical private aliases so its own internals and
tests are untouched.

The estimator is deliberately crude — chars/4, no tokenizer on the inference hot
path. It has been the house convention since the unit pack was written; making it
exact would cost a tokenizer round-trip per row to move a bound that is already
approximate by design.
"""

from __future__ import annotations

import os

#: Operator override for the assembled INPUT block's token budget.
LLM_INPUT_TOKEN_BUDGET_ENV = "LEGBA_LLM_INPUT_TOKEN_BUDGET"

#: The budget when the env is unset — the core plane's working window.
DEFAULT_INPUT_TOKEN_BUDGET = 32000

#: Rough token estimate divisor; no tokenizer on the inference hot path.
CHARS_PER_TOKEN = 4


def input_token_budget() -> int:
    """Estimated INPUT-token budget for an assembled evidence block.

    Env :data:`LLM_INPUT_TOKEN_BUDGET_ENV` (default
    :data:`DEFAULT_INPUT_TOKEN_BUDGET`). Bounds the EVIDENCE block only — a
    separately-bounded grounding preamble is prepended on top by each producer,
    and output is never capped on the core plane. A non-integer or non-positive
    value falls back to the default rather than raising: a malformed env must not
    take an analyst down.
    """
    raw = os.getenv(LLM_INPUT_TOKEN_BUDGET_ENV)
    if raw and raw.strip():
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    return DEFAULT_INPUT_TOKEN_BUDGET


def estimate_tokens(text: str) -> int:
    """Cheap chars/4 token estimate (ceiling)."""
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def budget_chars() -> int:
    """The budget expressed in CHARACTERS, for a producer that packs by length."""
    return input_token_budget() * CHARS_PER_TOKEN
