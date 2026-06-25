# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lifecycle FSM for runtime actors (per legba_runtime_spec.md §6).

States: ``draft`` → ``configured`` → ``active`` ⇆ ``paused`` → ``retired``;
any → ``error``; ``error`` → ``configured`` via operator ``reset``.

This module is pure (no I/O). The actor classes call into it to evaluate
transitions; per-state hooks are dispatched separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable


class LifecycleEvent(str, Enum):
    """The set of events that can cause a transition."""

    CONFIGURE = "configure"  # draft -> configured
    ACTIVATE = "activate"    # configured -> active
    PAUSE = "pause"          # active -> paused
    RESUME = "resume"        # paused -> active
    RETIRE = "retire"        # active/paused -> retired
    ERROR = "error"          # any -> error
    RESET = "reset"          # error -> configured


# State strings match `LifecycleState` in legba.data.schemas.lifecycle —
# kept as bare strings here so the runtime module doesn't drag in the
# schema dependency for this pure-FSM bit.
DRAFT = "draft"
CONFIGURED = "configured"
ACTIVE = "active"
PAUSED = "paused"
RETIRED = "retired"
ERROR = "error"

_VALID_STATES = frozenset(
    {DRAFT, CONFIGURED, ACTIVE, PAUSED, RETIRED, ERROR}
)


# Transition table per spec §6.1. (from_state, event) -> to_state.
_TRANSITIONS: dict[tuple[str, LifecycleEvent], str] = {
    (DRAFT, LifecycleEvent.CONFIGURE): CONFIGURED,
    (CONFIGURED, LifecycleEvent.ACTIVATE): ACTIVE,
    (ACTIVE, LifecycleEvent.PAUSE): PAUSED,
    (PAUSED, LifecycleEvent.RESUME): ACTIVE,
    (ACTIVE, LifecycleEvent.RETIRE): RETIRED,
    (PAUSED, LifecycleEvent.RETIRE): RETIRED,
    (CONFIGURED, LifecycleEvent.RETIRE): RETIRED,
    (ERROR, LifecycleEvent.RESET): CONFIGURED,
}

# `error` is reachable from ANY non-retired state on an ERROR event.
_ERROR_REACHABLE = {DRAFT, CONFIGURED, ACTIVE, PAUSED}


class IllegalTransition(Exception):
    """Raised when (from_state, event) has no defined target."""


@dataclass(frozen=True)
class Transition:
    """A successful transition record — the FSM emits one per state change."""

    from_state: str
    to_state: str
    event: LifecycleEvent
    occurred_at: datetime
    initiated_by: str = "system"
    detail: str = ""


@dataclass
class LifecycleFSM:
    """Tracks the current state + history. Pure (no I/O).

    The actor calls :meth:`transition` after running per-state hooks. The
    hook order is per spec §6.2; this object only validates legality and
    records the resulting transition.
    """

    state: str = DRAFT
    history: list[Transition] = field(default_factory=list)

    def can(self, event: LifecycleEvent) -> bool:
        """Return True if the current state has a transition for `event`."""
        if event == LifecycleEvent.ERROR:
            return self.state in _ERROR_REACHABLE
        return (self.state, event) in _TRANSITIONS

    def next_state(self, event: LifecycleEvent) -> str:
        """Return the state that would result. Raises on illegal transition."""
        if event == LifecycleEvent.ERROR:
            if self.state not in _ERROR_REACHABLE:
                raise IllegalTransition(
                    f"illegal ERROR transition from state={self.state!r}"
                )
            return ERROR
        try:
            return _TRANSITIONS[(self.state, event)]
        except KeyError as exc:
            raise IllegalTransition(
                f"no transition for ({self.state!r}, {event.value!r})"
            ) from exc

    def transition(
        self,
        event: LifecycleEvent,
        *,
        initiated_by: str = "system",
        detail: str = "",
    ) -> Transition:
        """Apply a transition. Raises :class:`IllegalTransition` on illegal."""
        target = self.next_state(event)
        rec = Transition(
            from_state=self.state,
            to_state=target,
            event=event,
            occurred_at=datetime.now(tz=timezone.utc),
            initiated_by=initiated_by,
            detail=detail,
        )
        self.state = target
        self.history.append(rec)
        return rec

    def legal_events(self) -> Iterable[LifecycleEvent]:
        """Yield the events that are legal from the current state."""
        for (src, ev), _dst in _TRANSITIONS.items():
            if src == self.state:
                yield ev
        if self.state in _ERROR_REACHABLE:
            yield LifecycleEvent.ERROR


def is_valid_state(state: str) -> bool:
    return state in _VALID_STATES
