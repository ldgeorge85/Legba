"""
Goal hierarchy schemas.

Seed Goal (immutable) → Meta Goals → Goals → Sub-goals → Tasks

Adapted from AXIS shared/schemas/goals.py, simplified for single-agent use.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class GoalPurpose(str, Enum):
    """Distinguishes standing vs investigative goals."""
    STANDING = "standing"          # Persistent portfolio items, inform priority weighting
    INVESTIGATIVE = "investigative"  # Time-bound, attached to situation/hypothesis


class GoalType(str, Enum):
    META_GOAL = "meta_goal"
    GOAL = "goal"
    SUBGOAL = "subgoal"
    TASK = "task"


class GoalStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    DEFERRED = "deferred"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class GoalSource(str, Enum):
    SEED = "seed"        # Derived from immutable seed goal
    AGENT = "agent"      # Agent-generated
    HUMAN = "human"      # From inbox directive
    SUBGOAL = "subgoal"  # Decomposed from parent


class Milestone(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    description: str
    completed: bool = False
    completed_at: datetime | None = None
    weight: float = 1.0


class Goal(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    description: str
    goal_type: GoalType = GoalType.GOAL
    priority: int = Field(default=5, ge=1, le=10)  # 1 = highest
    status: GoalStatus = GoalStatus.ACTIVE
    source: GoalSource = GoalSource.AGENT

    # Purpose: standing (persistent) vs investigative (time-bound)
    goal_purpose: GoalPurpose = GoalPurpose.STANDING

    # Hierarchy
    parent_id: UUID | None = None
    child_ids: list[UUID] = Field(default_factory=list)

    # Investigation links (for investigative goals)
    linked_situation_id: UUID | None = None
    linked_hypothesis_id: UUID | None = None

    # Context
    context: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)

    # Progress
    progress_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    milestones: list[Milestone] = Field(default_factory=list)

    # Dependencies
    blocked_by: list[UUID] = Field(default_factory=list)
    blocks: list[UUID] = Field(default_factory=list)

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_progress_at: datetime | None = None

    # Deferral
    deferred_until_cycle: int | None = None
    defer_reason: str | None = None

    # Completion
    completion_reason: str | None = None
    result_summary: str | None = None


class GoalUpdate(BaseModel):
    """Partial update to a goal."""

    goal_id: UUID
    status: GoalStatus | None = None
    priority: int | None = None
    progress_pct: float | None = None
    completion_reason: str | None = None
    result_summary: str | None = None


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

def create_goal(
    description: str,
    goal_type: GoalType = GoalType.GOAL,
    priority: int = 5,
    source: GoalSource = GoalSource.AGENT,
    parent_id: UUID | None = None,
    success_criteria: list[str] | None = None,
    goal_purpose: GoalPurpose = GoalPurpose.STANDING,
    linked_situation_id: UUID | None = None,
    linked_hypothesis_id: UUID | None = None,
) -> Goal:
    return Goal(
        description=description,
        goal_type=goal_type,
        priority=priority,
        source=source,
        parent_id=parent_id,
        success_criteria=success_criteria or [],
        goal_purpose=goal_purpose,
        linked_situation_id=linked_situation_id,
        linked_hypothesis_id=linked_hypothesis_id,
    )


def create_subgoal(parent: Goal, description: str, priority: int | None = None) -> Goal:
    return Goal(
        description=description,
        goal_type=GoalType.SUBGOAL,
        priority=priority or parent.priority,
        source=GoalSource.SUBGOAL,
        parent_id=parent.id,
    )


def create_task(parent: Goal, description: str, priority: int | None = None) -> Goal:
    return Goal(
        description=description,
        goal_type=GoalType.TASK,
        priority=priority or parent.priority,
        source=GoalSource.SUBGOAL,
        parent_id=parent.id,
    )
