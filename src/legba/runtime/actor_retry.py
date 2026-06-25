# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Retry classification cluster (G) — extracted from dapr_actors.py (#93), behavior-preserving move."""

from __future__ import annotations

from typing import Any


def _classify_exception(exc: BaseException) -> str:
    """Bucket an in-flight exception into transient / budget / hard.

    Resolution order (most specific first):

      * :class:`BudgetExhausted`   → ``"budget"``.
      * :class:`TransientLLMFailure` (5xx, 429, network) → ``"transient"``.
      * :class:`HardLLMFailure` (4xx, validation, auth)  → ``"hard"``.
      * Anything else → ``"hard"`` (conservative — unrecognized failures
        DLQ rather than retry indefinitely).

    Source handlers and filter chains throw bare ``Exception`` subclasses
    rather than typed LLM failures; we still pessimistically classify
    those as ``hard`` so a noisy upstream doesn't loop forever.  Source
    handlers that WANT transient behavior should subclass
    :class:`TransientLLMFailure` (or its sibling source-side class once
    it lands).

    Imports are lazy to avoid a circular-import cycle through
    ``legba.data.stack.llm.*`` (which itself imports from provenance,
    which imports from stack.llm).
    """
    # Lazy import — see docstring.
    from ..data.stack.llm.base import (
        BudgetExhausted,
        HardLLMFailure,
        TransientLLMFailure,
    )

    if isinstance(exc, BudgetExhausted):
        return "budget"
    if isinstance(exc, TransientLLMFailure):
        return "transient"
    if isinstance(exc, HardLLMFailure):
        return "hard"
    return "hard"


def _retry_delay_seconds(
    policy: Any,
    attempt: int,
) -> float:
    """Compute the wait-before-next-attempt for a transient retry policy.

    ``attempt`` is 1-indexed (first retry = 1). The policy carries
    ``backoff`` mode + ``initial_delay_seconds`` + ``multiplier`` +
    ``max_delay_seconds``.

    Modes:

      * exponential: initial * multiplier**(attempt-1)
      * linear:      initial * attempt
      * constant:    initial
    """
    initial = float(getattr(policy, "initial_delay_seconds", 1.0))
    multiplier = float(getattr(policy, "multiplier", 2.0))
    max_delay = float(getattr(policy, "max_delay_seconds", 60.0))
    mode = str(getattr(policy, "backoff", "exponential"))
    if mode == "linear":
        delay = initial * attempt
    elif mode == "constant":
        delay = initial
    else:
        delay = initial * (multiplier ** max(0, attempt - 1))
    return min(delay, max_delay)
