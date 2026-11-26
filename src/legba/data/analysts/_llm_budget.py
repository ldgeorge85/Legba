# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ONE input-token budget every producer packs against.

The UNIT path (``inline_target`` packs recency-ordered signals until the
estimated budget is reached, and a multi-round GATHER sizes its evidence cap
from it), the COMPOSITION path (``meta_findings_synthesizer`` sizes its
per-input excerpt from it) and the consult loop's derived context ceiling all
read the budget from here, so they cannot drift apart.

The budget follows the route's own context window when that is known
(:func:`derived_input_budget`): the window, less room for the route's largest
maximum output (a thinking alias's reasoning counts against its output), less
a margin. ``LEGBA_LLM_INPUT_TOKEN_BUDGET`` is an explicit override that wins
when set; without either, a modest default applies.

The estimator is deliberately crude — chars/4, no tokenizer on the inference hot
path. It has been the house convention since the unit pack was written; making it
exact would cost a tokenizer round-trip per row to move a bound that is already
approximate by design.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: Explicit override for the input budget; wins over everything when set.
LLM_INPUT_TOKEN_BUDGET_ENV = "LEGBA_LLM_INPUT_TOKEN_BUDGET"

#: The budget when nothing is set and the route's window is unknown. Kept
#: modest on purpose: an unknown window may be a small one, and a call over
#: the window fails outright.
DEFAULT_INPUT_TOKEN_BUDGET = 32000

#: Rough token estimate divisor; no tokenizer on the inference hot path.
CHARS_PER_TOKEN = 4

#: The most of a window the output side is given when the budget is derived.
#: The output reserve is the route's largest declared maximum output (thinking
#: included, since reasoning tokens count against it), but never more than
#: this share, so input always gets at least about half the window. On a
#: 262,144-token window whose thinking alias may write 131,072 tokens, the
#: derived input budget is 262,144 - 131,072 - 4,096 = 126,976.
OUTPUT_RESERVE_MAX_SHARE = 0.5

#: Held back on top of the output reserve: the estimate is chars/4, not a
#: tokenizer, so a derived budget never sits on the window's exact edge.
DERIVED_MARGIN_TOKENS = 4096

#: Where a budget came from (stamped on consult turns, logged on derivation).
SOURCE_INPUT_BUDGET_ENV = "input_token_budget_env"
SOURCE_MODEL_WINDOW = "model_window"
SOURCE_INPUT_BUDGET_DEFAULT = "input_token_budget_default"


def _env_budget() -> int | None:
    raw = os.getenv(LLM_INPUT_TOKEN_BUDGET_ENV)
    if raw and raw.strip():
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    return None


def _probe_int(llm: Any, name: str) -> int | None:
    probe = getattr(llm, name, None) if llm is not None else None
    if not callable(probe):
        return None
    try:
        value = probe()
    except Exception:  # noqa: BLE001 — a probe must never fail a run
        logger.debug("llm_budget.probe_failed name=%s", name, exc_info=True)
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def output_reserve_tokens(llm: Any, window: int) -> int:
    """The output room a derived input budget leaves in ``window``.

    The largest maximum output the route declares across its aliases (a
    thinking alias's maximum includes its reasoning), capped at
    :data:`OUTPUT_RESERVE_MAX_SHARE` of the window; that share when the route
    declares no maximum.
    """
    share = int(window * OUTPUT_RESERVE_MAX_SHARE)
    declared: list[int] = []
    probe = getattr(llm, "max_output_tokens_by_alias", None) if llm is not None else None
    if callable(probe):
        try:
            declared = [
                v for v in (probe() or {}).values()
                if isinstance(v, int) and not isinstance(v, bool) and v > 0
            ]
        except Exception:  # noqa: BLE001 — a probe must never fail a run
            logger.debug("llm_budget.alias_probe_failed", exc_info=True)
    if not declared:
        single = _probe_int(llm, "model_max_output_tokens")
        declared = [single] if single else []
    return min(max(declared), share) if declared else share


def derived_input_budget(llm: Any = None) -> tuple[int, str]:
    """``(budget, source)``: the estimated INPUT tokens a call on ``llm`` may carry.

    1. :data:`LLM_INPUT_TOKEN_BUDGET_ENV` when set — an explicit override wins;
    2. the route's context window (``llm.context_window_tokens()``: declared on
       the stack component, or learned from the endpoint) minus
       :func:`output_reserve_tokens` minus :data:`DERIVED_MARGIN_TOKENS`;
    3. :data:`DEFAULT_INPUT_TOKEN_BUDGET`.

    A malformed env value falls through rather than raising: a bad setting
    must not take an analyst down.
    """
    env = _env_budget()
    if env is not None:
        return env, SOURCE_INPUT_BUDGET_ENV
    window = _probe_int(llm, "context_window_tokens")
    if window is not None:
        budget = window - output_reserve_tokens(llm, window) - DERIVED_MARGIN_TOKENS
        if budget > 0:
            return budget, SOURCE_MODEL_WINDOW
    return DEFAULT_INPUT_TOKEN_BUDGET, SOURCE_INPUT_BUDGET_DEFAULT


def input_token_budget(llm: Any = None) -> int:
    """Estimated INPUT-token budget for an assembled evidence block
    (:func:`derived_input_budget`'s number). Bounds the EVIDENCE block only —
    a separately-bounded grounding preamble is prepended on top by each
    producer. Without ``llm`` the route's window is unknown, so only the
    override or the default applies."""
    return derived_input_budget(llm)[0]


def estimate_tokens(text: str) -> int:
    """Cheap chars/4 token estimate (ceiling)."""
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def budget_chars(llm: Any = None) -> int:
    """The budget expressed in CHARACTERS, for a producer that packs by length."""
    return input_token_budget(llm) * CHARS_PER_TOKEN
