# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which Consult calls think (lane T-1): one table, read by both consult paths.

The owner (2026-11-14): "Deep is just thinking on … We should use it for a
lot, especially consult." The consult descriptors declare
``method.llm.thinking: "on"`` (``consult_default``, ``deep_consult``); the kind
default is OFF, so hand-built deps and the bake-off arms' descriptors — which
declare nothing — call exactly as before.

Per call role (:data:`THINKING_ROLES`):

* ``round`` — ON. On the native tool route a round reply with no tool call IS
  the answer, so a turn whose rounds did not think would mostly answer
  without thinking; and choosing what to read next is the planning step
  thinking is for.
* ``final`` — ON. The forced synthesis writes the answer.
* ``recovery`` — ON. The synthesis-only replay over a cut run's evidence is
  the same job as ``final``.
* ``repair`` — OFF. The contract's one bounded repair (typed hypotheses /
  explanations block) is a format correction over text already written; a
  person is waiting and thinking buys nothing there.
* ``deep_plan`` — ON. Deep consult's plan stage decomposes the question into
  a tool plan: one call, reasoning-shaped, nobody watching it stream.
* ``deep_extract`` — OFF. Lifting candidate facts / hypotheses out of an
  answer already written is extraction, not reasoning.

Not here, deliberately: the settle-point / consult JUDGE runs on a different
model family (``LEGBA_CONSULT_JUDGE_STACK_REF``, the Gemma route — "no judge
on the same model as the layer it judges"), which this lane does not touch;
its think alias is a draft component with an 8,192-token default that cut
thinking off in the bake-off.

Thinking needs a plane that can think (:func:`thinking.thinking_available`):
the paid plane, picked per request, has no thinking alias, so a turn there
runs as it always did and its trace says why. A bake-off ARM never thinks,
whatever any row or descriptor says — its frozen configuration is the
pre-registered one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..stack.llm.thinking import DESCRIPTOR_KEY, parse_thinking, thinking_available

ROLE_ROUND = "round"
ROLE_FINAL = "final"
ROLE_RECOVERY = "recovery"
ROLE_REPAIR = "repair"
ROLE_DEEP_PLAN = "deep_plan"
ROLE_DEEP_EXTRACT = "deep_extract"

#: The roles that think when a turn's thinking is active. Every other role
#: (``repair``, ``deep_extract``) never does.
THINKING_ROLES: frozenset[str] = frozenset(
    {ROLE_ROUND, ROLE_FINAL, ROLE_RECOVERY, ROLE_DEEP_PLAN},
)

#: The request-row key that overrides the descriptor for ONE turn
#: (``"on"`` / ``"off"``). Not on the consult front door's request model — no
#: UI exists for it; the bake-off harness and scripts that build rows can set it.
ROW_KEY = DESCRIPTOR_KEY

REASON_DECLARED = "declared"
REASON_ROW = "request_row"
REASON_ARM = "bakeoff_arm_frozen"
REASON_NO_ALIAS = "plane_has_no_thinking_alias"


@dataclass(frozen=True)
class ConsultThinking:
    """One turn's thinking decision."""

    requested: bool
    active: bool
    reason: str

    @classmethod
    def decide(
        cls, declared: bool, *, first: Mapping[str, Any], arm: bool, llm: Any,
    ) -> "ConsultThinking":
        if arm:
            return cls(requested=False, active=False, reason=REASON_ARM)
        row = parse_thinking(first.get(ROW_KEY)) if isinstance(first, Mapping) else None
        requested = declared if row is None else row
        reason = REASON_DECLARED if row is None else REASON_ROW
        if requested and not thinking_available(llm):
            return cls(requested=True, active=False, reason=REASON_NO_ALIAS)
        return cls(requested=bool(requested), active=bool(requested), reason=reason)

    def for_role(self, role: str) -> bool:
        return self.active and role in THINKING_ROLES

    def step(self) -> dict[str, Any]:
        """The trace step a turn that asked for thinking records."""
        return {
            "phase": "plan",
            "kind": "thinking",
            "requested": self.requested,
            "active": self.active,
            "reason": self.reason,
            "roles": sorted(THINKING_ROLES) if self.active else [],
        }


__all__ = [
    "ConsultThinking",
    "REASON_ARM",
    "REASON_DECLARED",
    "REASON_NO_ALIAS",
    "REASON_ROW",
    "ROLE_DEEP_EXTRACT",
    "ROLE_DEEP_PLAN",
    "ROLE_FINAL",
    "ROLE_RECOVERY",
    "ROLE_REPAIR",
    "ROLE_ROUND",
    "ROW_KEY",
    "THINKING_ROLES",
]
