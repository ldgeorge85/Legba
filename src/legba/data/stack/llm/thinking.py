# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Thinking on the core plane: THE ONE PLACE a caller's choice becomes an alias.

The owner's gateway serves one model under two aliases: ``fast`` (thinking
off) and ``deep`` (thinking on). "Deep is just thinking on" (2026-11-14). The
gateway decides thinking BY ALIAS ONLY — "any thinking setting you send is
overridden" — so the switch Legba controls is which alias a call names.

How a caller asks
-----------------
* A call passes ``thinking=True`` to ``LLMProviderHandler.chat_complete``.
  The handler resolves the alias here (:func:`resolve_call_model`) from its
  component row's ``thinking_model_name`` — the alias string lives in registry
  data, never in code. A component that declares none refuses a thinking
  call (:class:`ThinkingUnavailable`, raised by the handler as a hard
  failure): asking for thinking on a plane that cannot think is an error, not
  a silent thinking-off answer.
* A descriptor declares ``method.llm.thinking: "on"`` (:data:`DESCRIPTOR_KEY`,
  parsed by :func:`parse_thinking`). The runtime deps builder binds the
  analyst's primary handler through :class:`ThinkingBoundHandler`, so every
  call that handler makes thinks unless the call itself says
  ``thinking=False``. The consult kinds read the same key but apply it per
  call role (``consult_thinking``).
* Callers ask :func:`thinking_available` before asking, when "no thinking on
  this plane" is a legitimate answer (the consult route can be on the paid
  plane, which has no thinking alias).

What thinking never changes
---------------------------
Temperature handling (the house 1.0 is sent as before; the vendor forces
1.0 in thinking mode anyway). The output limit sent is the thinking alias's
own declared maximum (``thinking_max_output_tokens``), never a smaller one. The reasoning-token budget parameter is unknown (the owner has not
supplied its name or value): the component may declare it
(``reasoning_budget_param`` + ``reasoning_budget_tokens``) and
:func:`reasoning_budget_fields` sends it on thinking calls only when BOTH are
set. Unset — the shipped state — sends nothing (SEAMS #88).

Per call role (T-2)
-------------------
A seat may think on some of its calls and not others: ``method.llm.thinking``
is the default for every primary call, ``method.gather.thinking`` overrides it
for the GATHER tool rounds and ``method.llm.narrate.thinking`` for the VOICE.
:meth:`ThinkingPlan.for_role` (``shared/thinking_choice``) is the decision;
:func:`bind_role` applies it to the handler each phase calls.

The safety net (T-2)
--------------------
:func:`call_with_thinking_fallback` (used by ``base.chat_complete``): a
thinking call that fails for a reason the thinking alias can cause
(:func:`thinking_failure_reason`) is retried ONCE on the non-thinking alias,
recorded on the retry's receipt (``thinking_fallback``), in the run's steps,
in a WARNING and to every fallback sink (the runtime raises the logged-only
``thinking_fallback`` alert). Never silent, never twice.

Stdlib-only leaf (``run_accounting``, itself a leaf, is imported lazily).
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping

# The spelling lives in a stdlib leaf so the descriptor schema can validate it
# without importing this package (httpx); re-exported here.
from legba.shared.thinking_choice import (
    DESCRIPTOR_KEY,
    ROLE_DEFAULT,
    ROLE_GATHER,
    ROLE_NARRATE,
    THINKING_OFF,
    THINKING_ON,
    ThinkingPlan,
    parse_thinking,
    thinking_plan,
)

logger = logging.getLogger(__name__)


class ThinkingUnavailable(RuntimeError):
    """A thinking call on a component that declares no thinking alias."""


def _raw(cfg: Any, name: str) -> Any:
    knob = getattr(cfg, name, None) if cfg is not None else None
    return getattr(knob, "raw", None) if knob is not None else None


def configured_thinking_alias(cfg: Any) -> str | None:
    """The component's ``thinking_model_name``, or ``None`` when undeclared."""
    raw = _raw(cfg, "thinking_model_name")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def resolve_call_model(
    cfg: Any, *, model: str | None, thinking: bool | None, component_id: str = "",
) -> str:
    """The model id one call sends: THE decision, made here and nowhere else.

    Thinking off (``False`` or ``None``): the caller's explicit ``model`` or
    the component's ``model_name`` — exactly as before this module existed.
    Thinking on: the component's ``thinking_model_name``. A thinking call that
    also names a different ``model`` is refused rather than guessed.
    """
    if not thinking:
        return model or str(_raw(cfg, "model_name"))
    alias = configured_thinking_alias(cfg)
    if alias is None:
        raise ThinkingUnavailable(
            f"thinking requested on {component_id or 'a component'} which declares "
            f"no thinking_model_name"
        )
    if model and model != alias:
        raise ThinkingUnavailable(
            f"thinking requested on {component_id or 'a component'} together with an "
            f"explicit model {model!r}; the thinking alias is {alias!r}"
        )
    return alias


def reasoning_budget_fields(cfg: Any, *, thinking: bool | None) -> dict[str, Any]:
    """The declared reasoning-budget field for a thinking call, else ``{}``.

    Sent only when the call thinks AND the component declares both the
    parameter name and the value. Never on a thinking-off call.
    """
    if not thinking:
        return {}
    param = _raw(cfg, "reasoning_budget_param")
    tokens = _raw(cfg, "reasoning_budget_tokens")
    if not (isinstance(param, str) and param.strip()) or tokens is None:
        return {}
    return {param.strip(): int(tokens)}


def thinking_available(llm: Any) -> bool:
    """Can ``llm`` take a ``thinking=True`` call? (False for any handler or
    double that does not say so.)"""
    probe = getattr(llm, "thinking_available", None)
    if not callable(probe):
        return False
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001 — an unconfigured handler cannot think
        return False


class ThinkingBoundHandler:
    """A handler whose calls think by default (a descriptor's declared choice).

    Wraps the shared, cached handler without mutating it (other analysts use
    the same instance). A call that passes ``thinking`` explicitly keeps its
    own choice. Every other attribute is the wrapped handler's.
    """

    def __init__(self, handler: Any) -> None:
        self._legba_wrapped = handler

    @property
    def wrapped(self) -> Any:
        return self._legba_wrapped

    def __getattr__(self, name: str) -> Any:
        return getattr(self._legba_wrapped, name)

    async def chat_complete(
        self, messages: list[Mapping[str, Any]], **kwargs: Any,
    ) -> Any:
        kwargs.setdefault("thinking", True)
        return await self._legba_wrapped.chat_complete(messages, **kwargs)

    async def stream_complete(
        self, messages: list[Mapping[str, Any]], **kwargs: Any,
    ) -> AsyncIterator[Any]:
        kwargs.setdefault("thinking", True)
        async for chunk in self._legba_wrapped.stream_complete(messages, **kwargs):
            yield chunk


def bind_role(
    handler: Any, plan: ThinkingPlan, role: str, *, analyst_id: str = "",
) -> Any:
    """``handler`` bound to the seat's choice for one call ROLE (T-2).

    ``plan.for_role(role)`` is the decision (``thinking_choice``); this only
    applies it, exactly as :func:`bind_thinking` applies the T-1 default.
    """
    try:
        return bind_thinking(handler, plan.for_role(role), analyst_id=analyst_id)
    except ThinkingUnavailable as exc:
        raise ThinkingUnavailable(f"{exc} (call role: {role})") from exc


# ---------------------------------------------------------------------------
# T-2 — the safety net: a thinking call that fails FOR THINKING retries once
# ---------------------------------------------------------------------------
#
# Only failures the thinking alias itself can cause qualify. A transient
# failure (429, 5xx, a dropped stream, a timeout) is not one — the same call
# on the non-thinking alias would meet the same plane — and neither is a
# component that declares no alias (ThinkingUnavailable: a configuration
# error that stays loud). A qualifying failure retries ONCE on the
# non-thinking alias; the retry's receipt carries ``thinking_fallback`` (the
# reason), the run's steps get a ``thinking_fallback`` step when the kind
# bound them, a WARNING is logged, and every registered sink is told (the
# runtime host raises the logged-only ``thinking_fallback`` alert from it).

FALLBACK_TOOLS_REFUSED = "tools_refused"
FALLBACK_UNSUPPORTED_PARAMETER = "unsupported_parameter"
FALLBACK_ALIAS_UNKNOWN = "alias_unknown"
FALLBACK_REASONING_PARSE = "reasoning_parse"
FALLBACK_REQUEST_REJECTED = "request_rejected"
FALLBACK_REASONS: tuple[str, ...] = (
    FALLBACK_TOOLS_REFUSED,
    FALLBACK_UNSUPPORTED_PARAMETER,
    FALLBACK_ALIAS_UNKNOWN,
    FALLBACK_REASONING_PARSE,
    FALLBACK_REQUEST_REJECTED,
)

#: Exception classes (by name, anywhere in the MRO) that are never a thinking
#: failure: the provider plane's transient/budget classes.
_NOT_THINKING = frozenset({
    "TransientLLMFailure", "StreamInterruptedFailure", "BudgetExhausted",
})
#: Plain exception classes a malformed (reasoning) stream or body raises
#: while it is parsed.
_PARSE_ERRORS = (ValueError, KeyError, TypeError, IndexError, UnicodeDecodeError)
_CONTEXT_MARKERS = (
    "context length", "context_length", "maximum context", "too many tokens",
    "prompt is too long", "input is too long",
)
_PARAM_MARKERS = (
    "unsupported", "not supported", "unrecognized", "unknown parameter",
    "unknown field", "extra inputs", "extra_forbidden", "unexpected keyword",
    "invalid parameter", "not allowed", "not permitted",
)
_MODEL_MARKERS = ("not found", "does not exist", "unknown model", "invalid model", "no such")


def thinking_failure_reason(exc: BaseException, *, had_tools: bool = False) -> str | None:
    """Why ``exc`` (raised by a THINKING call) is the thinking alias's fault,
    or ``None`` when it is not — then nothing is retried."""
    if not isinstance(exc, Exception):
        return None  # cancellation, interpreter exit
    names = {cls.__name__ for cls in type(exc).__mro__}
    if names & _NOT_THINKING or isinstance(exc, TimeoutError):
        return None
    if isinstance(exc, ThinkingUnavailable) or isinstance(exc.__cause__, ThinkingUnavailable):
        return None
    text = f"{exc} {getattr(exc, 'body', '') or ''}".lower()
    if "HardLLMFailure" in names:
        status = getattr(exc, "status", None)
        code = int(status) if isinstance(status, int) else None
        if code is None:
            if any(m in text for m in ("non-json", "parse", "decode", "malformed")):
                return FALLBACK_REASONING_PARSE
            return None
        if code not in (400, 404, 422):
            return None  # auth, payment, rate: the route, not the alias
        if any(m in text for m in _CONTEXT_MARKERS):
            return None  # the prompt is too long on either alias
        if code == 404 or ("model" in text and any(m in text for m in _MODEL_MARKERS)):
            return FALLBACK_ALIAS_UNKNOWN
        if had_tools and ("tool" in text or "function" in text):
            return FALLBACK_TOOLS_REFUSED
        if any(m in text for m in _PARAM_MARKERS):
            return FALLBACK_UNSUPPORTED_PARAMETER
        return FALLBACK_REQUEST_REJECTED
    if isinstance(exc, _PARSE_ERRORS):
        return FALLBACK_REASONING_PARSE
    return None


#: Process-wide listeners told of every fallback (the runtime host installs the
#: alert raiser). A sink must not block; a raising sink is logged and skipped.
_FALLBACK_SINKS: list[Callable[[Mapping[str, Any]], None]] = []


def add_thinking_fallback_sink(sink: Callable[[Mapping[str, Any]], None]) -> None:
    if sink not in _FALLBACK_SINKS:
        _FALLBACK_SINKS.append(sink)


def remove_thinking_fallback_sink(sink: Callable[[Mapping[str, Any]], None]) -> None:
    if sink in _FALLBACK_SINKS:
        _FALLBACK_SINKS.remove(sink)


def note_thinking_fallback(
    *, component_id: str, reason: str, error: BaseException,
) -> dict[str, Any]:
    """Record one fallback everywhere it must be seen; returns the step."""
    step = {
        "phase": "thinking",
        "kind": "thinking_fallback",
        "reason": reason,
        "component_id": component_id or None,
        "error": type(error).__name__,
        "http_status": (
            int(getattr(error, "status")) if isinstance(getattr(error, "status", None), int)
            else None
        ),
    }
    logger.warning(
        "llm.thinking_fallback component=%s reason=%s error=%s status=%s — "
        "retrying ONCE on the non-thinking alias",
        component_id, reason, step["error"], step["http_status"],
    )
    try:
        from ...run_accounting import append_run_step

        append_run_step(step)
    except Exception:  # pragma: no cover — instrumentation never fails a call
        logger.debug("thinking_fallback.step failed", exc_info=True)
    for sink in list(_FALLBACK_SINKS):
        try:
            sink(dict(step))
        except Exception:  # noqa: BLE001 — a sink never fails the call
            logger.warning("thinking_fallback.sink_failed sink=%r", sink, exc_info=True)
    return step


async def call_with_thinking_fallback(
    once: Callable[[bool | None, str | None], Awaitable[Any]],
    *,
    thinking: bool | None,
    had_tools: bool,
    component_id: str = "",
) -> Any:
    """Run ``once(thinking, None)``; on a thinking-specific failure run
    ``once(False, reason)`` exactly once. Never retries a thinking-off call,
    never retries twice, and the second failure propagates as it is."""
    try:
        return await once(thinking, None)
    except Exception as exc:
        reason = thinking_failure_reason(exc, had_tools=had_tools) if thinking else None
        if reason is None:
            raise
        note_thinking_fallback(component_id=component_id, reason=reason, error=exc)
    return await once(False, reason)


def bind_thinking(handler: Any, declared: bool | None, *, analyst_id: str = "") -> Any:
    """``handler`` bound to its descriptor's thinking choice.

    ``None``/``False`` returns the handler untouched. ``True`` on a handler
    that cannot think raises :class:`ThinkingUnavailable` — the descriptor
    declared something the plane cannot do, and the activation says so.
    """
    if not declared:
        return handler
    if not thinking_available(handler):
        raise ThinkingUnavailable(
            f"analyst {analyst_id or '?'} declares {DESCRIPTOR_KEY}: on (method.llm or a per-block choice) but its "
            f"primary component {getattr(handler, '_instance_id', '') or '?'} declares no "
            f"thinking_model_name"
        )
    return ThinkingBoundHandler(handler)


__all__ = [
    "DESCRIPTOR_KEY",
    "FALLBACK_REASONS",
    "ROLE_DEFAULT",
    "ROLE_GATHER",
    "ROLE_NARRATE",
    "THINKING_OFF",
    "THINKING_ON",
    "ThinkingBoundHandler",
    "ThinkingPlan",
    "ThinkingUnavailable",
    "add_thinking_fallback_sink",
    "bind_role",
    "bind_thinking",
    "call_with_thinking_fallback",
    "configured_thinking_alias",
    "note_thinking_fallback",
    "parse_thinking",
    "reasoning_budget_fields",
    "remove_thinking_fallback_sink",
    "resolve_call_model",
    "thinking_available",
    "thinking_failure_reason",
    "thinking_plan",
]
