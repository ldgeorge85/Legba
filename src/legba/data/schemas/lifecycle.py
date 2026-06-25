# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lifecycle state machine (per L-101 §6)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


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


class LifecycleTransition(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    descriptor_id: str
    descriptor_kind: Literal["target", "analyst", "stack_component", "wiring"]
    from_state: LifecycleState
    to_state: LifecycleState
    at: datetime
    actor: str
    reason: str | None = None

    @model_validator(mode="after")
    def _legal(self) -> "LifecycleTransition":
        if self.to_state not in ALLOWED_TRANSITIONS[self.from_state]:
            raise ValueError(
                f"illegal transition {self.from_state.value} → {self.to_state.value}"
            )
        return self
