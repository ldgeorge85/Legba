# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-TURN consult cost ceiling — the front door's check (X2 lane C1; SEAMS #72 closed by Q-1).

WHAT IT IS. A dollar figure no single consult turn should exceed on a priced
route — distinct from the per-RUN drilling ceilings in
``analysts.consult_spend_guard`` (``LEGBA_CONSULT_MAX_COST_USD_PER_RUN``),
which the operator pinned off on 2026-09-16 (W2-F4).

WHAT IT DOES WHEN IT BITES (lane Q-1, after consult turn e749e301 cost $20.26):
the SINK is ``analysts.consult_turn_cost.TurnCeilingGuard``. Before each round
the runner projects the next call and the synthesis that must follow; when
they would cross the ceiling it stops calling tools, synthesises from what it
already holds, and the answer opens with a "Stopped for cost" line naming the
figures. The receipt carries ``turn_ceiling_usd``. Nothing is refused.

THIS MODULE checks the knob at the front door. Unset/empty = no per-turn
ceiling (the default; the turn runs exactly as before). A positive number is
accepted and enforced by the runner. Zero, a negative number or an unparseable
value is REFUSED with 503 before anything runs or is written — a typo must not
read as "off" and silently leave a paid turn unguarded.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

__all__ = [
    "TURN_COST_CEILING_ENV",
    "TurnCeilingRefusal",
    "check_turn_ceiling",
    "configured_turn_ceiling",
]

TURN_COST_CEILING_ENV = "LEGBA_CONSULT_TURN_COST_CEILING_USD"


class TurnCeilingRefusal(RuntimeError):
    """The knob is set to something that cannot be enforced — the turn must not run."""


def configured_turn_ceiling() -> str | None:
    """The raw knob value, or ``None`` when it is off (unset / empty)."""
    raw = os.getenv(TURN_COST_CEILING_ENV)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def check_turn_ceiling() -> None:
    """No-op when off or a positive number; raise :class:`TurnCeilingRefusal` otherwise."""
    raw = configured_turn_ceiling()
    if raw is None:
        return
    try:
        value = float(raw)
    except ValueError:
        value = None
    if value is not None and value > 0:
        return
    what = f"set to an unparseable value {raw!r}" if value is None else f"set to ${value:.2f}"
    msg = (
        f"{TURN_COST_CEILING_ENV} is {what}; a per-turn ceiling must be a positive "
        f"dollar amount. Refusing the turn rather than running it unguarded; fix or "
        f"unset the knob to consult."
    )
    logger.error("consult.turn_ceiling.invalid %s", msg)
    raise TurnCeilingRefusal(msg)
