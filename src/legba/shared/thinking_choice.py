# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The spelling of a thinking choice (lanes T-1, T-2) — a stdlib leaf.

``method.llm.thinking`` on a descriptor, the per-block choices
(``method.gather.thinking``, ``method.llm.narrate.thinking``; T-2) and a
consult request row's ``thinking`` are parsed here so the descriptor schema can
validate them without importing the provider stack (httpx). Which call role
thinks is :meth:`ThinkingPlan.for_role`; what a thinking call DOES (the alias)
is ``legba.data.stack.llm.thinking``.
"""

from __future__ import annotations

from typing import Any, Mapping

#: The descriptor key (``method.llm.thinking``) and the request-row key.
DESCRIPTOR_KEY = "thinking"
THINKING_ON = "on"
THINKING_OFF = "off"

_TRUE = frozenset({"on", "true", "yes", "1"})
_FALSE = frozenset({"off", "false", "no", "0"})


def parse_thinking(value: Any) -> bool | None:
    """``True`` / ``False`` for a declared choice, ``None`` when absent.

    Accepts ``"on"``/``"off"`` (the documented spelling), booleans (YAML reads
    an unquoted ``on`` as ``True``) and a ``{"raw": ...}`` factory dump.
    Anything else raises :class:`ValueError` — a typo must not read as "off".
    """
    if isinstance(value, Mapping) and "raw" in value:
        value = value["raw"]
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE:
            return True
        if word in _FALSE:
            return False
    raise ValueError(
        f"method.llm.{DESCRIPTOR_KEY}: {value!r} is not a thinking choice; "
        f"use {THINKING_ON!r} or {THINKING_OFF!r}"
    )


# ---------------------------------------------------------------------------
# T-2 — per-block thinking: which CALL ROLE of a seat thinks
# ---------------------------------------------------------------------------
#
# A descriptor already separates some of its calls into blocks. Each such
# block may carry its own ``thinking``; a block that declares none inherits
# ``method.llm.thinking`` (the T-1 default for every primary call):
#
#   role      the calls                                    where it is declared
#   default   synthesis / reason / propose / every other   method.llm.thinking
#             call on the primary handler
#   gather    the GATHER tool rounds (inline_target,       method.gather.thinking
#             journal_assessor, inquiry)
#   narrate   the VOICE: field notes, narrate, its retry,  method.llm.narrate.thinking
#             the inquiry coda (journal_assessor, inquiry)
#
# ``method.llm.verify`` is NOT a role: the judge route can be repointed by
# ``LEGBA_JUDGE_STACK_REF`` and walks a fallback chain onto other families, so
# a thinking choice there could name a plane that has no thinking alias. It is
# refused at registration.

ROLE_DEFAULT = "default"
ROLE_GATHER = "gather"
ROLE_NARRATE = "narrate"
ROLES: tuple[str, ...] = (ROLE_DEFAULT, ROLE_GATHER, ROLE_NARRATE)

#: The analyst kinds whose builder reads each per-block choice. A per-block
#: choice on any other kind would be read by nothing and is refused.
KINDS_READING_ROLE: Mapping[str, frozenset[str]] = {
    ROLE_GATHER: frozenset({"inline_target", "journal_assessor", "inquiry"}),
    ROLE_NARRATE: frozenset({"journal_assessor", "inquiry"}),
}


class ThinkingPlan:
    """One seat's thinking choice per call role (``for_role`` decides)."""

    __slots__ = ("default", "gather", "narrate")

    def __init__(
        self, default: bool | None = None, gather: bool | None = None,
        narrate: bool | None = None,
    ) -> None:
        self.default = default
        self.gather = gather
        self.narrate = narrate

    def declared(self, role: str) -> bool | None:
        if role not in ROLES:
            raise ValueError(f"unknown thinking role {role!r}; one of {ROLES}")
        return getattr(self, role)

    def for_role(self, role: str) -> bool:
        """THE decision: does a call in ``role`` think? A block's own choice,
        else ``method.llm.thinking``, else off."""
        own = self.declared(role)
        if own is not None:
            return own
        return bool(self.default)

    def any_on(self) -> bool:
        return any(self.for_role(r) for r in ROLES)

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"ThinkingPlan(default={self.default}, gather={self.gather}, narrate={self.narrate})"


def _gather_value(gather: Any) -> Any:
    if gather is None:
        return None
    if isinstance(gather, Mapping):
        return gather.get(DESCRIPTOR_KEY)
    return getattr(gather, DESCRIPTOR_KEY, None)


def thinking_plan(llm: Mapping[str, Any] | None, gather: Any = None) -> ThinkingPlan:
    """The seat's :class:`ThinkingPlan` from ``method.llm`` and ``method.gather``
    (a mapping or the typed block). A bad value raises :class:`ValueError`."""
    llm = llm or {}
    narrate = llm.get(ROLE_NARRATE)
    return ThinkingPlan(
        default=parse_thinking(llm.get(DESCRIPTOR_KEY)),
        gather=parse_thinking(_gather_value(gather)),
        narrate=parse_thinking(
            narrate.get(DESCRIPTOR_KEY) if isinstance(narrate, Mapping) else None
        ),
    )


def thinking_declaration_errors(
    kind: str | None, llm: Mapping[str, Any] | None, gather: Any = None,
) -> list[str]:
    """Registration-time refusals for a seat's thinking declarations.

    Every value must parse; ``method.llm.<block>.thinking`` is accepted only on
    ``narrate``; a per-block choice is accepted only on a kind whose builder
    reads it (:data:`KINDS_READING_ROLE`) — that last check is skipped when
    ``kind`` is ``None`` (the method block alone cannot see the kind).
    """
    errors: list[str] = []
    llm = llm or {}
    for key, val in llm.items():
        if isinstance(val, Mapping) and DESCRIPTOR_KEY in val and key != ROLE_NARRATE:
            why = (
                "the judge route can be repointed (LEGBA_JUDGE_STACK_REF, judge_chain) "
                "onto a plane with no thinking alias" if key in ("verify", "judge")
                else "nothing reads it"
            )
            errors.append(
                f"method.llm.{key}.{DESCRIPTOR_KEY} is not a thinking block ({why}); "
                f"declare method.llm.{DESCRIPTOR_KEY}, method.gather.{DESCRIPTOR_KEY} "
                f"or method.llm.{ROLE_NARRATE}.{DESCRIPTOR_KEY}"
            )
    try:
        plan = thinking_plan(llm, gather)
    except ValueError as exc:
        errors.append(str(exc))
        return errors
    where = {ROLE_GATHER: f"method.gather.{DESCRIPTOR_KEY}",
             ROLE_NARRATE: f"method.llm.{ROLE_NARRATE}.{DESCRIPTOR_KEY}"}
    for role, kinds in KINDS_READING_ROLE.items():
        if kind is not None and plan.declared(role) is not None and kind not in kinds:
            errors.append(
                f"{where[role]} is read only by the kinds {sorted(kinds)}; "
                f"kind {kind!r} has no {role} calls"
            )
    return errors


__all__ = [
    "DESCRIPTOR_KEY",
    "KINDS_READING_ROLE",
    "ROLES",
    "ROLE_DEFAULT",
    "ROLE_GATHER",
    "ROLE_NARRATE",
    "THINKING_OFF",
    "THINKING_ON",
    "ThinkingPlan",
    "parse_thinking",
    "thinking_declaration_errors",
    "thinking_plan",
]
