# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bake-off's consult ARMS as runtime variants.

Arms B and C need from the consult kind what the stock route cannot give: a per-arm TOOL SURFACE (six
raw readers for B; verified findings + their receipts for C), a per-arm SYSTEM
PROMPT that does not order finished intelligence first, and a hard per-question
SPEND STOP the harness controls (the live run ceilings are effectively off).

ONE KEY DECIDES THE ARM: the read pack the consult descriptor GRANTS.

  * ``consult_bakeoff_raw``      grants ``substrate_read_raw``      → arm B;
  * ``consult_bakeoff_verified`` grants ``substrate_read_verified`` → arm C;
  * every other consult descriptor grants ``substrate_read``        → stock.

The runtime binds the consult kind's governed read binding over THAT pack
(:func:`consult_read_pack_for`, called by ``runtime/dapr_host``), so the agency
gate refuses any tool outside it at dispatch; the loop OFFERS only that pack's
tools and swaps in the arm's prompt (:func:`variant_for_binding`, called by
``consult_on_demand.run_method``). Two layers, one key — a descriptor cannot be
half one arm and half another.

THE SPEND STOP (:class:`BakeoffSpendGuard`). A request row carrying
``bakeoff_meter`` (only ``scripts/bakeoff/run_arm.py`` sends one — the registry
front door never does) gets a guard that meters PROVIDER-REPORTED usage at the
VERIFIED price the harness passes (design §7: "never the code's PRICE_TABLE"),
cache reads and writes included, and stops drilling while there is still room
for one more round AND the forced synthesis under the hard stop. It keeps every
ceiling of the stock guard as well — it can only stop a run sooner.

Imports only its sibling consult modules; read by the runtime (the deps
resolver and the consult loop), never by a registry route.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from . import consult_bakeoff_prompts as _prompts
from .consult_spend_guard import SpendGuard

__all__ = [
    "BAKEOFF_READ_PACKS",
    "BakeoffSpendGuard",
    "BakeoffVariant",
    "METER_KEY",
    "RAW_PACK_ID",
    "STOCK_READ_PACK_ID",
    "VERIFIED_PACK_ID",
    "consult_read_pack_for",
    "spend_guard_for",
    "variant_for_binding",
]

STOCK_READ_PACK_ID = "substrate_read"
RAW_PACK_ID = "substrate_read_raw"
VERIFIED_PACK_ID = "substrate_read_verified"

#: The request-row key carrying the harness's meter (see the module docstring).
METER_KEY = "bakeoff_meter"


@dataclass(frozen=True)
class BakeoffVariant:
    """One arm's tool surface + prompt, keyed by the read pack it binds."""

    arm: str
    pack_id: str
    tools: tuple[str, ...]
    system_prompt: str


BAKEOFF_READ_PACKS: dict[str, BakeoffVariant] = {
    RAW_PACK_ID: BakeoffVariant(
        arm="B",
        pack_id=RAW_PACK_ID,
        tools=(
            "search_signals", "search_corpus", "read_document",
            "vector_search", "list_targets", "list_sources",
        ),
        system_prompt=_prompts.RAW_SYSTEM_PROMPT,
    ),
    VERIFIED_PACK_ID: BakeoffVariant(
        arm="C",
        pack_id=VERIFIED_PACK_ID,
        tools=("list_findings", "belief_as_of", "read_document", "list_targets"),
        system_prompt=_prompts.VERIFIED_SYSTEM_PROMPT,
    ),
}


def _grant_ids(analyst: Any) -> list[str]:
    out: list[str] = []
    for ref in getattr(analyst, "action_packs", None) or []:
        pid = ref.get("pack_id") if isinstance(ref, Mapping) else getattr(ref, "pack_id", None)
        if pid:
            out.append(str(pid))
    return out


def consult_read_pack_for(analyst: Any) -> str:
    """The read pack a consult descriptor's governed binding is built over.

    A bake-off pack when the descriptor grants exactly one of them; the stock
    ``substrate_read`` otherwise. Granting BOTH bake-off packs is a descriptor
    error (which arm would it be?) and raises — the deps resolver then fails the
    activation loud rather than guessing.
    """
    grants = _grant_ids(analyst)
    hits = [p for p in BAKEOFF_READ_PACKS if p in grants]
    if len(hits) > 1:
        raise ValueError(
            f"consult descriptor grants more than one bake-off read pack {hits!r}; "
            "an arm reads through exactly one"
        )
    return hits[0] if hits else STOCK_READ_PACK_ID


def variant_for_binding(binding: Any) -> BakeoffVariant | None:
    """The arm a run's governed binding belongs to, or None for stock consult."""
    pack = getattr(binding, "pack", None)
    pack_id = getattr(getattr(pack, "identity", None), "id", None)
    return BAKEOFF_READ_PACKS.get(str(pack_id)) if pack_id else None


# ---------------------------------------------------------------------------
# The hard per-question stop
# ---------------------------------------------------------------------------


def _num(raw: Any, name: str, *, minimum: float = 0.0) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{METER_KEY}.{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{METER_KEY}.{name} must be >= {minimum}, got {value}")
    return value


@dataclass
class BakeoffSpendGuard(SpendGuard):
    """The stock guard plus a hard stop metered at the VERIFIED price.

    ``price`` is per million tokens: ``input``, ``output``, ``cache_read``,
    ``cache_write``. ``max_output_tokens`` is the per-call output cap the loop
    sends (``deps.max_tokens``) — the worst case one call can emit, thinking
    included.

    STOP RULE. Checked before each round (the loop's own contract). Drilling
    continues only while ``spent + 2 x next_call_worst < hard_stop`` — room for
    the round about to run AND the forced synthesis that follows a stop. The
    projection is deliberately pessimistic: the next call's input is the last
    call's full prompt side grown by half (at least +20k tokens) at the
    uncached price, and its output is the full cap.
    """

    hard_stop_usd: float = 0.0
    price: dict[str, float] = field(default_factory=dict)
    max_output_tokens: int = 32768
    verified_usd: float = 0.0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    last_prompt_side_tokens: int = 0

    def record(self, llm: Any, response: Any, usage: dict[str, int] | None = None) -> None:
        super().record(llm, response, usage)
        raw = getattr(response, "usage", None)
        prompt = int(getattr(raw, "prompt_tokens", 0) or (usage or {}).get("prompt_tokens", 0) or 0)
        out = int(getattr(raw, "completion_tokens", 0) or (usage or {}).get("completion_tokens", 0) or 0)
        c_read = int(getattr(raw, "cache_read_tokens", 0) or 0)
        c_write = int(getattr(raw, "cache_write_tokens", 0) or 0)
        self.cache_read_tokens += c_read
        self.cache_write_tokens += c_write
        self.last_prompt_side_tokens = prompt + c_read + c_write
        p = self.price
        self.verified_usd = round(
            self.verified_usd
            + (prompt * p["input"] + out * p["output"]
               + c_read * p["cache_read"] + c_write * p["cache_write"]) / 1_000_000,
            6,
        )

    def next_call_worst_usd(self) -> float:
        grown = max(int(self.last_prompt_side_tokens * 1.5), self.last_prompt_side_tokens + 20_000)
        return (grown * self.price["input"] + self.max_output_tokens * self.price["output"]) / 1_000_000

    def exhausted_reason(self) -> str | None:
        stock = super().exhausted_reason()
        if stock is not None:
            return stock
        reserve = 2 * self.next_call_worst_usd()
        if self.verified_usd + reserve >= self.hard_stop_usd:
            return (
                f"the bake-off hard stop leaves no room for another round and a "
                f"synthesis (${self.verified_usd:.2f} spent + ${reserve:.2f} "
                f"reserved >= ${self.hard_stop_usd:.2f})"
            )
        return None

    def meter(self) -> dict[str, Any]:
        """The figures ``scripts/bakeoff/run_arm.py`` records for the question."""
        return {
            "hard_stop_usd": self.hard_stop_usd,
            "verified_usd": round(self.verified_usd, 6),
            "hard_stop_breached": self.verified_usd > self.hard_stop_usd,
            "price_per_m": dict(self.price),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "calls": self.calls,
            "table_estimate_usd": round(self.cost_usd, 6),
            # A call CUT mid-stream (deadline, dropped stream, provider
            # error) never reaches ``record`` — its usage is billed but unseen.
            # The harness charges each such cut this pessimistic projection
            # (the stop rule's own), so the run ledger never under-counts.
            "last_prompt_side_tokens": self.last_prompt_side_tokens,
            "next_call_worst_usd": round(self.next_call_worst_usd(), 6),
        }

    def snapshot(self) -> dict[str, Any]:
        return {**super().snapshot(), METER_KEY: self.meter()}


def spend_guard_for(
    first: Mapping[str, Any], *, stock_kwargs: Mapping[str, Any], max_output_tokens: int,
) -> SpendGuard:
    """The run's guard: the stock one, or the bake-off one when the row asks.

    A malformed meter RAISES (``ValueError`` → a hard run error): a paid
    question whose stop could not be armed must not run on the stock ceilings.
    """
    raw = first.get(METER_KEY)
    if raw is None:
        return SpendGuard(**dict(stock_kwargs))
    if not isinstance(raw, Mapping):
        raise ValueError(f"{METER_KEY} must be an object, got {type(raw).__name__}")
    price_raw = raw.get("price_per_m")
    if not isinstance(price_raw, Mapping):
        raise ValueError(f"{METER_KEY}.price_per_m is required")
    price = {
        k: _num(price_raw.get(k), f"price_per_m.{k}")
        for k in ("input", "output", "cache_read", "cache_write")
    }
    if price["input"] <= 0 or price["output"] <= 0:
        raise ValueError(f"{METER_KEY}.price_per_m input/output must be > 0")
    return BakeoffSpendGuard(
        **dict(stock_kwargs),
        hard_stop_usd=_num(raw.get("hard_stop_usd"), "hard_stop_usd", minimum=0.01),
        price=price,
        max_output_tokens=int(max_output_tokens),
    )
