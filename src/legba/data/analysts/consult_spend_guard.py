# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A consult run's spend ceiling — the budget that is denominated in money.

Why wall-clock was not enough
=============================

The loop already had three time budgets, and run c8a0105c respected all of
them. It still cost roughly $10, because time is a bad proxy for spend on a
frontier route: the run made 11 calls in 165 seconds, and what made it
expensive was not their duration but their *input* — every round replays the
whole transcript, so the late calls each carried ~100k+ tokens at $15/M.

A clock cannot see that. A token counter can, and it sees it EARLY — the
transcript's growth is visible from round three, long before any wall-clock
guard would fire. So this guard ends drilling on the run's own accumulated
input, and the loop synthesises with what it has.

It never kills a run. Hitting the ceiling means "stop drilling, answer now",
which is the same move the wall-clock guard makes, because a run that has
already spent the money should deliver the answer it paid for.

Two ceilings, checked together
------------------------------

* ``LEGBA_CONSULT_MAX_INPUT_TOKENS_PER_RUN`` (150,000) — provider-reported
  prompt tokens summed across the run. Direct, exact, and the thing that
  actually grows.
* ``LEGBA_CONSULT_MAX_COST_USD_PER_RUN`` (3.00) — the estimate from the
  handler's own ``PRICE_TABLE``, so a cheap plane can drill much further for
  the same money and an expensive one stops sooner. The core plane's table is
  empty by design (self-hosted, $0), so this ceiling never fires there — which
  is correct: there is nothing to spend.

The estimate is the handler's, not a second opinion. Every hosted response
arrives with ``usage.cost_estimate_usd`` already stamped at parse time; this
guard sums those and only computes from the price table when a response did
not carry one.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Provider-reported prompt tokens a single run may accumulate before the loop
#: stops drilling. 150k ~= the point run c8a0105c passed on its way to $10.
DEFAULT_MAX_INPUT_TOKENS_PER_RUN = 150_000

#: Estimated USD a single run may accumulate before the loop stops drilling.
DEFAULT_MAX_COST_USD_PER_RUN = 3.00


def _env_float(name: str, default: float) -> float:
    """A positive float from ``name``, else ``default``.

    A malformed or non-positive pin falls back rather than zeroing the ceiling:
    a zero ceiling would force synthesis before the first round and make every
    consult useless.
    """
    raw = os.getenv(name, "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
        logger.debug("consult.spend.env_ignored name=%s raw=%r", name, raw)
    return default


def max_input_tokens_per_run() -> int:
    """Env ``LEGBA_CONSULT_MAX_INPUT_TOKENS_PER_RUN``."""
    return int(
        _env_float(
            "LEGBA_CONSULT_MAX_INPUT_TOKENS_PER_RUN",
            float(DEFAULT_MAX_INPUT_TOKENS_PER_RUN),
        )
    )


def max_cost_usd_per_run() -> float:
    """Env ``LEGBA_CONSULT_MAX_COST_USD_PER_RUN``."""
    return _env_float(
        "LEGBA_CONSULT_MAX_COST_USD_PER_RUN", DEFAULT_MAX_COST_USD_PER_RUN
    )


def price_table_for(llm: Any) -> dict[str, Any]:
    """The handler's own price table, or ``{}`` for a plane that has none.

    Read off the instance so a test double without one degrades to "free"
    rather than raising — an unpriced plane must never block a run.
    """
    table = getattr(llm, "PRICE_TABLE", None)
    return dict(table) if isinstance(table, dict) else {}


def estimate_call_cost(llm: Any, response: Any) -> float:
    """Estimated USD for one call.

    Prefers the handler's own stamped estimate (computed at parse time, from
    the same table, with the resolved model id — which the caller may not
    know). Falls back to computing it here only when a response carried none,
    which is the shape a test double or a non-stamping plane produces.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0.0
    stamped = getattr(usage, "cost_estimate_usd", 0.0) or 0.0
    if stamped:
        return float(stamped)
    table = price_table_for(llm)
    if not table:
        return 0.0
    model = getattr(usage, "model", "") or getattr(llm, "model", "") or ""
    if not model:
        return 0.0
    try:
        from ..stack.llm.base import estimate_cost

        return float(estimate_cost(model, usage, table))
    except Exception:  # noqa: BLE001 - accounting must never fail a run
        logger.debug("consult.spend.estimate_failed model=%s", model, exc_info=True)
        return 0.0


@dataclass
class SpendGuard:
    """Running spend for one consult run, and the ceilings it must respect.

    Instantiate per run — never shared, never on ``deps`` (which is shared
    across concurrent runs).
    """

    max_input_tokens: int = field(default_factory=max_input_tokens_per_run)
    max_cost_usd: float = field(default_factory=max_cost_usd_per_run)

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0

    def record(self, llm: Any, response: Any, usage: dict[str, int] | None = None) -> None:
        """Fold one completed call into the running total."""
        self.calls += 1
        if usage:
            self.input_tokens += int(usage.get("prompt_tokens", 0) or 0)
            self.output_tokens += int(usage.get("completion_tokens", 0) or 0)
        self.cost_usd = round(self.cost_usd + estimate_call_cost(llm, response), 6)

    @property
    def tokens_exhausted(self) -> bool:
        return self.input_tokens >= self.max_input_tokens

    @property
    def cost_exhausted(self) -> bool:
        # A zero/absent estimate (self-hosted plane) can never exhaust: there
        # is no spend to cap.
        return self.cost_usd > 0 and self.cost_usd >= self.max_cost_usd

    def exhausted_reason(self) -> str | None:
        """Why drilling must stop now, or ``None`` to keep going.

        Checked BEFORE a round rather than after, so the run never issues the
        call that would blow the ceiling — the point is to not spend it.
        """
        if self.tokens_exhausted:
            return (
                f"the run reached its input-token ceiling "
                f"({self.input_tokens:,} of {self.max_input_tokens:,} tokens)"
            )
        if self.cost_exhausted:
            return (
                f"the run reached its estimated-cost ceiling "
                f"(${self.cost_usd:.2f} of ${self.max_cost_usd:.2f})"
            )
        return None

    def snapshot(self) -> dict[str, Any]:
        """The spend figures a stream frame carries.

        Rides on the loop's existing frames rather than one of its own: the
        run's first (``render_prompt``, where the totals are zero and the
        CEILINGS are the point), each ``llm_call``, and
        ``spend_ceiling_reached``. A frame per round of its own would have
        doubled every ``reason`` phase count in the trace.

        Deliberately carries the ceilings alongside the totals: a panel showing
        "$1.90" means nothing without "of $3.00", and the operator deciding
        whether to press STOP is reading exactly that ratio.
        """
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "est_cost_usd": round(self.cost_usd, 4),
            "max_input_tokens": self.max_input_tokens,
            "max_cost_usd": round(self.max_cost_usd, 2),
        }


__all__ = [
    "DEFAULT_MAX_COST_USD_PER_RUN",
    "DEFAULT_MAX_INPUT_TOKENS_PER_RUN",
    "SpendGuard",
    "estimate_call_cost",
    "max_cost_usd_per_run",
    "max_input_tokens_per_run",
    "price_table_for",
]
