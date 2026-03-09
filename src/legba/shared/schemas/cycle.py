"""
Cycle-related schemas.

Defines the supervisor ↔ agent protocol: challenges, responses, and cycle state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


class Challenge(BaseModel):
    """Supervisor → Agent: challenge issued at cycle start."""

    cycle_number: int
    nonce: str  # UUID string for liveness check
    issued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    timeout_seconds: int = 300  # 5 minutes default
    metadata: dict[str, Any] = Field(default_factory=dict)


class CycleResponse(BaseModel):
    """Agent → Supervisor: response emitted at cycle end."""

    cycle_number: int
    nonce: str  # Must match challenge nonce
    started_at: datetime
    completed_at: datetime
    status: Literal["completed", "error", "partial"]
    cycle_summary: str
    actions_taken: int = 0
    goals_active: int = 0
    self_modifications: int = 0
    error: str | None = None
    signature: str | None = None  # Ed25519 signature of nonce:cycle_number
    metadata: dict[str, Any] = Field(default_factory=dict)


class CycleState(BaseModel):
    """In-process state tracked during a single cycle."""

    cycle_number: int = 0
    phase: Literal[
        "wake", "orient", "reason", "act", "reflect", "persist", "idle"
    ] = "idle"
    nonce: str = ""
    seed_goal: str = ""
    inbox_messages: list[Any] = Field(default_factory=list)
    actions_taken: int = 0
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    reasoning_steps: int = 0
    self_modifications: int = 0
    errors: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
