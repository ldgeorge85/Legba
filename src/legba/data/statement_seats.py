# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the two statement seats share: the route they run on, how a seat calls
its model, and how a seat reads the model's reply (plan P1 §4 lane S3).

THE TWO SEATS.

* The **typing seat** (:mod:`legba.data.statement_typing`) turns one cited
  sentence of a desk finding into a typed statement draft — kind, subject,
  valid time and precision, slots, and one support row per cited row with an
  exact quote. The model proposes; :func:`statement_gate.evaluate_code_gates`
  decides.
* The **verifier seat** (:mod:`legba.data.statement_verifier`) checks a
  draft's support in a clean context — code first, then a model that sees only
  the statement and its quotes (plan §3 G4). Only ``supports`` may establish,
  and its planted-error catch rate (:mod:`legba.data.statement_planted`) is
  published beside every figure it feeds.

THE ROUTE. Both run on the $0 core plane exactly as every scheduled analyst
does: stack ref ``llm.primary.openai_compat``, the house temperature, no
``max_tokens``, ``budget_tokens_per_day: 0`` on the descriptor
(``descriptors/analyst_statement_typing_seat.yaml``,
``descriptors/analyst_statement_verifier_seat.yaml``, which also carry the two
system prompts — the prompt manifest's entries ``statement_typing_seat`` and
``statement_verifier_seat``). :func:`assert_core_route` is the check a caller
runs on a built handler before the first call; it refuses an Anthropic
component, a priced component, a non-vLLM subprovider and the ``deep`` alias.

NEITHER SEAT WRITES. A seat returns a typed draft or a report. Status is never
a seat's to set (plan S3 "Must not ... let either seat write a status"); the
store (lane S1) and the gate's lifecycle decide.

A LEAF: stdlib + the shapes leaf + the shared sampling constant.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from legba.shared.sampling import HOUSE_TEMPERATURE

#: The one stack ref both seats run on.
SEAT_COMPONENT = "llm.primary.openai_compat"
#: The subprovider the core plane's handler resolves to (the self-hosted /
#: gateway OpenAI-compatible handler, unpriced unless the component sets one).
CORE_SUBPROVIDER = "vllm"
#: Model aliases a seat must never be routed to. ``deep`` is the consult
#: plane's thinking alias; the seats run on the scheduled plane's default.
FORBIDDEN_MODEL_ALIASES = frozenset({"deep"})

#: Descriptor ids — also the prompt manifest's keys for the two prompts.
TYPING_SEAT_ID = "statement_typing_seat"
VERIFIER_SEAT_ID = "statement_verifier_seat"

#: Principals the seats act as (plan §2.2). Recorded on what they return;
#: stamping a row with them is the store's job.
TYPING_PRINCIPAL = "agent:statement-typing"
VERIFIER_PRINCIPAL = "agent:statement-verifier"


class SeatLLM(Protocol):
    """The slice of an ``LLMProviderHandler`` a seat uses."""

    async def chat_complete(
        self, messages: list[Mapping[str, Any]], **kwargs: Any,
    ) -> Any: ...


class CoreRouteError(RuntimeError):
    """The handler a seat was given is not the unpriced core route."""


@dataclass(frozen=True)
class RouteFacts:
    """What a caller read off a built handler, for :func:`assert_core_route`."""

    component_id: str
    subprovider: str
    model_name: str
    #: The component's own configured price (``None`` = no price set).
    component_price: Any = None
    #: The handler class's code price table (empty for the core handler).
    price_table_entries: int = 0


def assert_core_route(facts: RouteFacts) -> dict[str, Any]:
    """Refuse anything but the unpriced core route; return what was checked.

    Raises :class:`CoreRouteError` naming the first failed condition."""
    checks = {
        "component_is_seat_component": facts.component_id == SEAT_COMPONENT,
        "component_not_anthropic": "anthropic" not in facts.component_id.lower(),
        "subprovider_is_core": facts.subprovider == CORE_SUBPROVIDER,
        "model_not_forbidden_alias": facts.model_name.strip().lower() not in FORBIDDEN_MODEL_ALIASES,
        "component_unpriced": facts.component_price is None,
        "price_table_empty": facts.price_table_entries == 0,
    }
    for name, ok in checks.items():
        if not ok:
            raise CoreRouteError(f"not the unpriced core route: {name} failed")
    return {"component_id": facts.component_id, "subprovider": facts.subprovider,
            "model_name": facts.model_name, **checks}


@dataclass(frozen=True)
class SeatCall:
    """One model call a seat made: enough to account for it, never its text."""

    seat: str
    ok: bool
    error: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    model: str | None = None
    #: S-6 — the call asked for the core plane's thinking alias
    #: (``chat_complete(thinking=True)``); ``None`` = not asked either way.
    thinking: bool | None = None


@dataclass
class CallBudget:
    """A hard cap on model calls, shared by every seat call in one run.

    ``take()`` refuses the call that would exceed the cap — before it is sent,
    never after."""

    limit: int
    used: int = 0
    calls: list[SeatCall] = field(default_factory=list)

    def take(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


async def call_seat(
    llm: SeatLLM,
    *,
    seat: str,
    system_prompt: str,
    user_content: str,
    budget: CallBudget | None = None,
    thinking: bool | None = None,
) -> tuple[str | None, SeatCall]:
    """One clean-context call: the seat's system prompt and ONE user message,
    the house temperature, no ``max_tokens``. Never raises: a refused budget,
    a transport error or an empty reply comes back as ``(None, call)``.

    ``thinking`` (S-6) is passed to ``chat_complete`` only when it is not
    ``None``: ``True`` names the component's thinking alias (T-1, resolved in
    ``stack/llm/thinking``, never here), ``False`` the plain one; ``None``
    sends exactly what an S-5 call sent."""
    if budget is not None and not budget.take():
        call = SeatCall(seat, False, "call_budget_exhausted", thinking=thinking)
        return None, call
    extra: dict[str, Any] = {} if thinking is None else {"thinking": bool(thinking)}
    try:
        response = await llm.chat_complete(
            [{"role": "user", "content": user_content}],
            system=system_prompt,
            temperature=HOUSE_TEMPERATURE,
            **extra,
        )
    except Exception as exc:  # noqa: BLE001 — degrade, never break the caller
        call = SeatCall(seat, False, f"call_failed:{type(exc).__name__}", thinking=thinking)
        if budget is not None:
            budget.calls.append(call)
        return None, call
    usage = getattr(response, "usage", None)
    content = str(getattr(response, "content", "") or "")
    call = SeatCall(
        seat, bool(content.strip()), None if content.strip() else "empty_reply",
        getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None),
        getattr(usage, "model", None) or None, thinking,
    )
    if budget is not None:
        budget.calls.append(call)
    return (content if content.strip() else None), call


# ---------------------------------------------------------------------------
# Reading a reply
# ---------------------------------------------------------------------------

#: ``【3】`` / ``［3］`` / ``〔3〕`` / ``〖3〗`` around a bare integer — the core
#: plane's full-width citation brackets, folded to ASCII before anything keys
#: on a reply (the same rule the write-time marker normaliser applies).
_FULLWIDTH_MARKER_RE = re.compile(r"[【［〔〖](\s*\d+\s*)[】］〕〗]")
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def fold_fullwidth_markers(text: str) -> str:
    """``【3】`` → ``[3]``; everything else untouched."""
    return _FULLWIDTH_MARKER_RE.sub(lambda m: f"[{m.group(1).strip()}]", text or "")


def parse_reply_object(content: str | None) -> dict[str, Any] | None:
    """The ONE JSON object a seat asked for, or ``None`` (never raises).

    Folds full-width markers first, unwraps a ```json fence, then falls back to
    the outermost ``{…}`` span. Guessing at a broken object is not attempted."""
    if not content:
        return None
    text = fold_fullwidth_markers(content).strip()
    m = _FENCE_RE.match(text)
    if m:
        text = m.group(1)
    try:
        parsed = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start:end + 1])
        except ValueError:
            return None
    return parsed if isinstance(parsed, dict) else None


def as_text(value: Any) -> str:
    """A reply field as stripped text (``None`` → ``""``), markers folded."""
    if value is None:
        return ""
    return fold_fullwidth_markers(str(value)).strip()


def first_of(values: Sequence[str], allowed: frozenset[str]) -> str | None:
    """The first value that is in ``allowed`` (case-folded), else ``None``."""
    for v in values:
        key = str(v or "").strip().lower()
        if key in allowed:
            return key
    return None


__all__ = [
    "CORE_SUBPROVIDER",
    "FORBIDDEN_MODEL_ALIASES",
    "SEAT_COMPONENT",
    "TYPING_PRINCIPAL",
    "TYPING_SEAT_ID",
    "VERIFIER_PRINCIPAL",
    "VERIFIER_SEAT_ID",
    "CallBudget",
    "CoreRouteError",
    "RouteFacts",
    "SeatCall",
    "SeatLLM",
    "as_text",
    "assert_core_route",
    "call_seat",
    "first_of",
    "fold_fullwidth_markers",
    "parse_reply_object",
]
