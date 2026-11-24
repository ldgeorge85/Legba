# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-TURN consult cost ceiling, enforced (lane Q-1, rule E; closes SEAMS #72).

THE TURN THAT SHOWED IT MISSING. Consult turn e749e301 (2026-11-14, route
``llm.anthropic.fable_5_1``) made 15 drilling rounds and a forced synthesis —
16 calls, 1,837,359 input tokens, 30,844 output tokens — and cost $20.26.
Every round re-sent the whole transcript, so each round cost more than the
last ($0.21 for round 1, $2.11 for round 15, $3.28 for the synthesis). The
per-RUN drilling ceilings (``consult_spend_guard``) were present and lifted by
the operator's pins (``LEGBA_CONSULT_MAX_COST_USD_PER_RUN=1000000``,
``LEGBA_CONSULT_MAX_INPUT_TOKENS_PER_RUN=100000000``); the per-TURN knob
(``LEGBA_CONSULT_TURN_COST_CEILING_USD``) was declared and refused every turn
when set, because nothing enforced it.

WHAT IT DOES WHEN IT BITES (the house spend rule, 2026-09-16: a paid path ships
with a ceiling, visible cost, a stop, and the partial delivered). Before each
round on a PRICED route the guard projects two calls — the next drilling round
(the last call's whole prompt side grown by half, at least +20,000 tokens, at
the uncached input price, plus the full per-call output budget) and the forced
SYNTHESIS that would follow it (that prompt grown once more, plus the full
output budget) — and drilling continues only while
``spent + projected round + projected synthesis`` stays under the ceiling.
When it does not, the loop stops calling tools, synthesises from what it
already holds, and the answer opens with "Stopped for cost", the spend when
drilling stopped and the turn's total including the synthesis
(``stop_note``). It is a SOFT limit: nothing is refused and the answer always
arrives. A self-hosted route carries no price table and is never stopped.

The knob: ``LEGBA_CONSULT_TURN_COST_CEILING_USD``. Unset or empty = no per-turn
ceiling (the receipt says so: ``turn_ceiling_usd: null``); $10 is the
recommended value (docs/TUNABLES.md). A positive number = the ceiling. Zero, a negative number or an unparseable value is refused at the
front door (``registry/consult_turn_ceiling.py``) so a typo never reads as off.

THE PROJECTED OUTPUT IS AN ESTIMATE, NOT A CAP (roll 87). Since roll 86's
follow-up no route sends a caller's output limit, so "the per-call output
budget" no longer exists to project from. Assuming the model's whole maximum
(128,000 tokens on the Claude routes) made a priced turn under the owner's soft
ceiling stop after about one call. The projection now takes the LARGEST output
actually observed on that route over the last ``PROJECTION_HISTORY_DAYS`` days
(the consult turns' per-call receipts and the flow runs' ``llm_calls``), else
``PROJECTION_FALLBACK_OUTPUT_TOKENS`` (32,768, the figure it used before) when
the route has no history. Every frame, the stop reason and the turn's receipt
label the figure an estimate and name its basis. Nothing smaller is sent on any
wire because of it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from .consult_spend_guard import SpendGuard, price_table_for

logger = logging.getLogger(__name__)

__all__ = [
    "OutputProjection",
    "PROJECTION_FALLBACK_OUTPUT_TOKENS",
    "PROJECTION_HISTORY_DAYS",
    "TURN_COST_CEILING_ENV",
    "TURN_STOP_PREFIX",
    "TurnCeilingGuard",
    "arm_turn_ceiling",
    "arm_turn_ceiling_observed",
    "configured_turn_ceiling_usd",
    "observed_output_projection",
    "stop_note",
]

TURN_COST_CEILING_ENV = "LEGBA_CONSULT_TURN_COST_CEILING_USD"
#: Every stop reason this guard emits starts with this, so the answer's note
#: and the trace can find it without matching prose.
TURN_STOP_PREFIX = "the turn's cost ceiling"
_GROWTH = 1.5
_MIN_GROWTH_TOKENS = 20_000
#: The projected output of one call when the route has no observed history:
#: the figure the projection used before roll 86's no-output-caps change. An
#: ESTIMATE for the soft ceiling only, never sent on any wire.
PROJECTION_FALLBACK_OUTPUT_TOKENS = 32_768
#: "Recent" for the observed-output history.
PROJECTION_HISTORY_DAYS = 14
#: The history read must never hold a turn up; past this it falls back.
_HISTORY_TIMEOUT_S = 3.0

#: The largest single-call output on one route: the consult turns' per-call
#: receipts (``consult_usage/1`` ``call_receipts``, roll 86) and the flow
#: runs' ``analyst_traces.llm_calls`` (R11). Successful calls only.
_OBSERVED_OUTPUT_SQL = """
SELECT max(o) AS largest, count(*) AS calls FROM (
  SELECT (r->>'completion_tokens')::bigint AS o
    FROM consult_turns t,
         jsonb_array_elements(CASE WHEN jsonb_typeof(t.usage->'call_receipts') = 'array'
                                   THEN t.usage->'call_receipts' ELSE '[]'::jsonb END) r
   WHERE t.created_at >= now() - make_interval(days => $2)
     AND coalesce(r->>'component_id', t.usage->>'route') = $1
     AND coalesce(r->>'status', 'success') = 'success'
     AND r->>'completion_tokens' ~ '^[0-9]+$'
  UNION ALL
  SELECT (c->>'completion_tokens')::bigint
    FROM analyst_traces a, jsonb_array_elements(a.llm_calls) c
   WHERE a.run_started_at >= now() - make_interval(days => $2)
     AND c->>'component_id' = $1
     AND c->>'status' = 'success'
     AND c->>'completion_tokens' ~ '^[0-9]+$'
) x
"""


@dataclass(frozen=True)
class OutputProjection:
    """The per-call output the turn ceiling projects with, and why (an estimate)."""

    tokens: int
    basis: str
    observed_calls: int = 0

    @classmethod
    def fallback(cls, why: str) -> "OutputProjection":
        return cls(PROJECTION_FALLBACK_OUTPUT_TOKENS,
                   f"{why}; fallback {PROJECTION_FALLBACK_OUTPUT_TOKENS:,} tokens")


async def _read_observed(pool: Any, route: str) -> tuple[int | None, int]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_OBSERVED_OUTPUT_SQL, route, PROJECTION_HISTORY_DAYS)
    if row is None:
        return None, 0
    largest = row["largest"]
    return (int(largest) if largest is not None else None), int(row["calls"] or 0)


async def observed_output_projection(pool: Any, route: str | None) -> OutputProjection:
    """The largest output observed on ``route`` lately, else the fallback.

    Never raises and never waits past ``_HISTORY_TIMEOUT_S``: a read that fails
    or is slow projects with the fallback and says so in the basis.
    """
    if not route:
        return OutputProjection.fallback("no route id to read history for")
    if pool is None:
        return OutputProjection.fallback("no database handle on this path to read history")
    try:
        largest, calls = await asyncio.wait_for(_read_observed(pool, route), _HISTORY_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — an estimate must never fail a turn
        logger.warning("consult.turn_ceiling.history_unread route=%s err=%s", route, type(exc).__name__)
        return OutputProjection.fallback(f"history read failed ({type(exc).__name__})")
    if not largest or largest <= 0:
        return OutputProjection.fallback(
            f"no output observed on {route} in the last {PROJECTION_HISTORY_DAYS} days")
    return OutputProjection(
        int(largest),
        f"largest output observed on {route} in the last {PROJECTION_HISTORY_DAYS} days "
        f"({calls:,} calls: consult call receipts and flow runs)",
        calls,
    )


def configured_turn_ceiling_usd() -> float | None:
    """The ceiling in USD, or ``None`` when unset. A bad value reads as unset
    HERE (the front door refuses it before any turn runs)."""
    raw = (os.getenv(TURN_COST_CEILING_ENV) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _price_for(llm: Any) -> Any | None:
    """The handler's ModelPrice for its configured model (longest-prefix), or None."""
    table = price_table_for(llm)
    if not table:
        return None
    cfg = getattr(llm, "_cfg", None)
    name = getattr(getattr(cfg, "model_name", None), "raw", None) or getattr(llm, "model", None)
    if not isinstance(name, str) or not name.strip():
        return None
    from ..stack.llm.base import longest_prefix_price

    return table.get(name) or longest_prefix_price(name.strip(), table)


@dataclass
class TurnCeilingGuard(SpendGuard):
    """The stock guard plus the per-turn ceiling, projected one call ahead."""

    turn_ceiling_usd: float = 0.0
    input_per_m: float = 0.0
    output_per_m: float = 0.0
    #: The PROJECTED output of one call (an estimate, ``OutputProjection``);
    #: the name is kept for every existing caller. Never sent on a wire.
    max_output_tokens: int = PROJECTION_FALLBACK_OUTPUT_TOKENS
    projection_basis: str = "fallback (no history consulted)"
    last_prompt_tokens: int = 0
    stopped_for_cost: bool = field(default=False)

    def record(self, llm: Any, response: Any, usage: dict[str, int] | None = None) -> None:
        super().record(llm, response, usage)
        raw = getattr(response, "usage", None)
        prompt = int(getattr(raw, "prompt_tokens", 0) or (usage or {}).get("prompt_tokens", 0) or 0)
        c_read = int(getattr(raw, "cache_read_tokens", 0) or 0)
        c_write = int(getattr(raw, "cache_write_tokens", 0) or 0)
        self.last_prompt_tokens = prompt + c_read + c_write

    @staticmethod
    def _grow(tokens: int) -> int:
        return max(int(tokens * _GROWTH), tokens + _MIN_GROWTH_TOKENS)

    def _call_usd(self, prompt_tokens: int) -> float:
        return (prompt_tokens * self.input_per_m + self.max_output_tokens * self.output_per_m) / 1_000_000

    def next_call_usd(self) -> float:
        """The next drilling round: the last prompt grown once, the projected output."""
        return self._call_usd(self._grow(self.last_prompt_tokens))

    def synthesis_usd(self) -> float:
        """The forced synthesis AFTER that round: its prompt grown again, the projected output.

        The synthesis is the turn's biggest prompt by construction (it replays
        the whole transcript) — on turn e749e301 it cost $3.28 on its own — so
        it is reserved for separately rather than assumed to cost a round.
        """
        return self._call_usd(self._grow(self._grow(self.last_prompt_tokens)))

    def exhausted_reason(self) -> str | None:
        stock = super().exhausted_reason()
        if stock is not None:
            return stock
        nxt, synth = self.next_call_usd(), self.synthesis_usd()
        if self.cost_usd + nxt + synth >= self.turn_ceiling_usd:
            self.stopped_for_cost = True
            return (
                f"{TURN_STOP_PREFIX} (${self.turn_ceiling_usd:.2f}) leaves no room for another "
                f"round and the synthesis: ${self.cost_usd:.2f} spent + ${nxt:.2f} projected next "
                f"round + ${synth:.2f} projected synthesis ({self.projection_label()})"
            )
        return None

    def projection_label(self) -> str:
        """The projected per-call output, labelled an estimate with its basis."""
        return (f"estimate: each call's output taken as {self.max_output_tokens:,} tokens, "
                f"{self.projection_basis}")

    def snapshot(self) -> dict[str, Any]:
        return {
            **super().snapshot(),
            "turn_ceiling_usd": round(self.turn_ceiling_usd, 2),
            "next_call_projected_usd": round(self.next_call_usd(), 4),
            "synthesis_projected_usd": round(self.synthesis_usd(), 4),
            "projected_output_tokens": int(self.max_output_tokens),
            "projection_basis": self.projection_label(),
        }


def _armable(spend: SpendGuard, llm: Any) -> tuple[float, Any] | None:
    ceiling = configured_turn_ceiling_usd()
    if ceiling is None or type(spend) is not SpendGuard:
        return None
    price = _price_for(llm)
    return None if price is None else (ceiling, price)


def arm_turn_ceiling(
    spend: SpendGuard, llm: Any, *, max_output_tokens: int | None = None,
    projection: OutputProjection | None = None,
) -> SpendGuard:
    """The run's guard with the per-turn ceiling armed, when it applies.

    Unchanged when the knob is unset, when the route is unpriced (nothing to
    spend), or when the guard is already a subclass with its own hard stop (the
    bake-off's pre-registered meter wins; its stop is stricter by design).
    ``projection`` (else an explicit ``max_output_tokens``, else the fallback)
    is the per-call output the projection assumes: an estimate, never a cap.
    """
    armable = _armable(spend, llm)
    if armable is None:
        return spend
    ceiling, price = armable
    if projection is None:
        projection = (OutputProjection(int(max_output_tokens), "the caller's stated figure")
                      if max_output_tokens else OutputProjection.fallback("no history consulted"))
    return TurnCeilingGuard(
        max_input_tokens=spend.max_input_tokens,
        max_cost_usd=spend.max_cost_usd,
        turn_ceiling_usd=ceiling,
        input_per_m=float(price.input_per_m),
        output_per_m=float(price.output_per_m),
        max_output_tokens=int(projection.tokens),
        projection_basis=projection.basis,
    )


async def arm_turn_ceiling_observed(spend: SpendGuard, llm: Any, *, pool: Any) -> SpendGuard:
    """``arm_turn_ceiling`` with the projection read from the route's history.

    Reads the database only when the ceiling would arm (knob set, priced
    route, stock guard): the self-hosted core plane never queries.
    """
    if _armable(spend, llm) is None:
        return spend
    route = getattr(llm, "_instance_id", None)
    projection = await observed_output_projection(pool, route if isinstance(route, str) else None)
    return arm_turn_ceiling(spend, llm, projection=projection)


def stop_note(steps: list[dict[str, Any]], spend: SpendGuard) -> str | None:
    """The answer's visible line when the turn stopped drilling for cost."""
    for step in steps:
        if step.get("kind") == "spend_ceiling_reached":
            reason = str(step.get("reason") or "")
            if reason.startswith(TURN_STOP_PREFIX) or "cost ceiling" in reason or "input-token" in reason:
                spent_at_stop = float(step.get("est_cost_usd") or 0.0)
                return (
                    f"> **Stopped for cost:** drilling stopped after round {step.get('round')} because "
                    f"{reason}. The answer below is synthesised from what had been gathered. "
                    f"Spent when drilling stopped: ${spent_at_stop:.2f}; the turn's total including "
                    f"the final synthesis: ${spend.cost_usd:.2f} over {spend.calls} calls."
                )
    return None
