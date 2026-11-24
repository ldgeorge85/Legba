# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult loop's clocks — ONE set of env readers for both sides.

Why a leaf module
=================

Two processes read these numbers. The consult loop (the runtime image) runs
under them; the registry's detached run (the slim registry image) has to sit
its invoke timeout ABOVE them. Run 9b902fec (2026-10-29) is what happens when
the two sides read different things: the operator pinned
``LEGBA_CONSULT_BUDGET_SECONDS=900`` and the registry's timeout stayed at 900,
so the registry cut the run one second before the loop's own final slice would
have delivered a 26 KB partial.

The registry derives its floor from the SAME readers the loop uses. They live
here, stdlib-only, so the registry can import them without pulling in
``legba.data.analysts`` (which the slim image cannot fully import);
``consult_round_protocol`` re-exports every name, so the loop's spelling
(``_cp.default_total_budget_seconds()``) is unchanged and is literally this
function.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("legba.data.analysts.consult_round_protocol")

#: Hard ceiling on the WHOLE ReAct loop — with ONE deliberate exception, the
#: final-synthesis floor below, which may carry a run past this rather than
#: throw away an answer in progress.
#:
#: The registry's invoke timeout is DERIVED from this plus the final floor
#: (``consult_runs.invoke_timeout_seconds``), so raising it raises that too.
DEFAULT_TOTAL_BUDGET_SECONDS = 480.0

#: Per-LLM-call deadline. Above the slowest observed healthy Opus consult turn
#: (~101s on run 3ae77c64) with headroom, so it catches a stuck call without
#: cutting a legitimately slow one.
DEFAULT_ROUND_DEADLINE_SECONDS = 150.0

#: FLOOR under the final synthesis's slice — never a ceiling.
#:
#: This is the 2026-09-16 c8a0105c fix. The synthesis used to get
#: ``min(round_deadline, remaining)``, i.e. the same 150s slice as any drilling
#: round. But the synthesis is not a drilling round: it is the only turn that
#: produces the product, it is the longest generation of the run by
#: construction (it writes the whole answer), and it happens when the
#: transcript is at its largest. Capping it at a drilling round's deadline is
#: exactly backwards — it cut a 13,000-character answer off at 150s and
#: DISCARDED it.
#:
#: The synthesis now gets the REMAINING total budget, floored here so a loop
#: that overran still gets a real slice rather than the scraps. That the floor
#: can push the run past ``total_budget_seconds`` is deliberate and is the
#: whole point: finishing the answer 60s late beats throwing it away on time.
DEFAULT_FINAL_FLOOR_SECONDS = 240.0


def env_seconds(name: str, default: float) -> float:
    """A positive float from ``name``, else ``default``.

    Unset / empty / non-numeric / non-positive all fall back: a malformed pin
    must never zero a budget, which would make every consult degrade instantly.
    """
    raw = os.getenv(name, "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    logger.debug("consult.budget.env_ignored name=%s raw=%r", name, raw)
    return default


def default_total_budget_seconds() -> float:
    """Total wall-clock budget for the loop. Env ``LEGBA_CONSULT_BUDGET_SECONDS``."""
    return env_seconds("LEGBA_CONSULT_BUDGET_SECONDS", DEFAULT_TOTAL_BUDGET_SECONDS)


def default_round_deadline_seconds() -> float:
    """Per-LLM-call deadline. Env ``LEGBA_CONSULT_ROUND_DEADLINE_SECONDS``."""
    return env_seconds(
        "LEGBA_CONSULT_ROUND_DEADLINE_SECONDS", DEFAULT_ROUND_DEADLINE_SECONDS
    )


def default_final_floor_seconds() -> float:
    """Floor under the final synthesis. Env ``LEGBA_CONSULT_FINAL_FLOOR_SECONDS``."""
    return env_seconds(
        "LEGBA_CONSULT_FINAL_FLOOR_SECONDS", DEFAULT_FINAL_FLOOR_SECONDS
    )


__all__ = [
    "DEFAULT_FINAL_FLOOR_SECONDS",
    "DEFAULT_ROUND_DEADLINE_SECONDS",
    "DEFAULT_TOTAL_BUDGET_SECONDS",
    "default_final_floor_seconds",
    "default_round_deadline_seconds",
    "default_total_budget_seconds",
    "env_seconds",
]
