# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lifecycle state machine (per L-101 §6)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class LifecycleState(str, Enum):
    DRAFT = "draft"
    CONFIGURED = "configured"
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"


class AbstractionLevel(str, Enum):
    L1 = "L1"  # raw descriptor
    L2 = "L2"  # curated template
    L3 = "L3"  # blessed pattern (expands at registration)


ALLOWED_TRANSITIONS: dict[LifecycleState, set[LifecycleState]] = {
    LifecycleState.DRAFT:      {LifecycleState.CONFIGURED, LifecycleState.RETIRED},
    LifecycleState.CONFIGURED: {LifecycleState.ACTIVE, LifecycleState.DRAFT, LifecycleState.RETIRED},
    LifecycleState.ACTIVE:     {LifecycleState.PAUSED, LifecycleState.RETIRED},
    LifecycleState.PAUSED:     {LifecycleState.ACTIVE, LifecycleState.RETIRED},
    LifecycleState.RETIRED:    set(),  # terminal
}


class WireEnumCoercion(BaseModel):
    """Mixin: accept the BARE STRING form of ``state`` / ``abstraction_level``.

    Every descriptor-identity model in this package is ``strict=True``, which
    switches pydantic's ``str`` → ``Enum`` coercion OFF: an enum-typed field
    then only accepts an actual enum member. But the WIRE form of these models
    is always the bare string — ``model_dump(mode="json")`` serializes
    ``LifecycleState.ACTIVE`` to ``"active"``, and that is exactly what the
    registry's ``/typed`` route serves and what the descriptor JSONB rows
    store. So a plain ``Model.model_validate(typed_dump)`` fails
    ``is_instance_of`` on a field that round-tripped through the system's own
    serializer.

    That is not hypothetical: it is the 2026-08-01 unit-fleet outage.
    ``AnalystIdentity.state`` had no coercer, every typed-descriptor parse
    raised, and actor activation spun in a hot descriptor-refetch loop. The fix
    there (``analyst.py:_coerce_state``) was written inline for the one model
    that had already broken; this mixin generalizes it so the remaining
    identity models cannot reproduce the same outage.

    Until now those models were held up only by every call site remembering to
    pass ``strict=False`` — a convention, not an invariant. One
    ``model_validate`` without it and the family is down.

    ``check_fields=False`` lets a subclass carry either field, both, or (for a
    future model) neither. In-process construction with the real enum member
    still works — the validators pass non-``str`` values straight through — and
    an unknown string still REJECTS, because ``LifecycleState("bogus")`` raises
    and pydantic surfaces it as a ``ValidationError``.
    """

    @field_validator("state", mode="before", check_fields=False)
    @classmethod
    def _coerce_state(cls, v: Any) -> Any:
        if isinstance(v, str) and not isinstance(v, LifecycleState):
            return LifecycleState(v)
        return v

    @field_validator("abstraction_level", mode="before", check_fields=False)
    @classmethod
    def _coerce_abstraction_level(cls, v: Any) -> Any:
        if isinstance(v, str) and not isinstance(v, AbstractionLevel):
            return AbstractionLevel(v)
        return v


class LifecycleTransition(WireEnumCoercion):
    model_config = ConfigDict(strict=True, extra="forbid")

    descriptor_id: str
    descriptor_kind: Literal["target", "analyst", "stack_component", "wiring"]
    from_state: LifecycleState
    to_state: LifecycleState
    at: datetime
    actor: str
    reason: str | None = None

    # ``from_state`` / ``to_state`` are the same wire hazard under different
    # names, so the mixin's ``state`` validator does not reach them.
    @field_validator("from_state", "to_state", mode="before")
    @classmethod
    def _coerce_transition_states(cls, v: Any) -> Any:
        if isinstance(v, str) and not isinstance(v, LifecycleState):
            return LifecycleState(v)
        return v

    @model_validator(mode="after")
    def _legal(self) -> "LifecycleTransition":
        if self.to_state not in ALLOWED_TRANSITIONS[self.from_state]:
            raise ValueError(
                f"illegal transition {self.from_state.value} → {self.to_state.value}"
            )
        return self
