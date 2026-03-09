"""
Goal Manager

CRUD operations for the goal hierarchy, focus selection, and progress tracking.
Goals are stored in Postgres via the structured memory store.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ...shared.schemas.goals import (
    Goal,
    GoalType,
    GoalStatus,
    GoalSource,
    GoalUpdate,
    create_goal,
    create_subgoal,
    create_task,
)
from ..memory.structured import StructuredStore
from ..log import CycleLogger


class GoalManager:
    """
    Manages the goal hierarchy.

    Provides operations for the cycle phases:
    - ORIENT: load active goals, select focus
    - REASON/ACT: decompose goals, create sub-goals/tasks
    - REFLECT: update progress, complete/abandon goals
    """

    def __init__(self, store: StructuredStore, logger: CycleLogger):
        self._store = store
        self._logger = logger

    # --- Read operations ---

    async def get_active_goals(self) -> list[Goal]:
        return await self._store.get_active_goals()

    async def get_all_goals(self) -> list[Goal]:
        return await self._store.get_all_goals()

    async def get_goal(self, goal_id: UUID) -> Goal | None:
        return await self._store.get_goal(goal_id)

    async def select_focus(self, goals: list[Goal]) -> Goal | None:
        """
        Select which goal to focus on this cycle.

        Simple heuristic: highest priority (lowest number) active goal.
        The LLM can override this in its reasoning.
        """
        active = [g for g in goals if g.status == GoalStatus.ACTIVE]
        if not active:
            return None
        return min(active, key=lambda g: g.priority)

    # --- Write operations ---

    async def create_goal(
        self,
        description: str,
        goal_type: GoalType = GoalType.GOAL,
        priority: int = 5,
        source: GoalSource = GoalSource.AGENT,
        parent_id: UUID | None = None,
        success_criteria: list[str] | None = None,
    ) -> Goal:
        goal = create_goal(
            description=description,
            goal_type=goal_type,
            priority=priority,
            source=source,
            parent_id=parent_id,
            success_criteria=success_criteria,
        )
        await self._store.save_goal(goal)
        self._logger.log("goal_created",
                         goal_id=str(goal.id),
                         description=description,
                         goal_type=goal_type.value)
        return goal

    async def decompose(self, parent: Goal, subtask_descriptions: list[str]) -> list[Goal]:
        """Decompose a goal into sub-goals."""
        children = []
        for desc in subtask_descriptions:
            child = create_subgoal(parent, desc)
            await self._store.save_goal(child)
            children.append(child)
            self._logger.log("goal_decomposed",
                             parent_id=str(parent.id),
                             child_id=str(child.id),
                             description=desc)

        # Update parent's child_ids
        parent.child_ids = [c.id for c in children]
        await self._store.save_goal(parent)
        return children

    async def update_progress(self, goal_id: UUID, progress_pct: float, summary: str | None = None) -> bool:
        goal = await self.get_goal(goal_id)
        if not goal:
            return False

        goal.progress_pct = min(100.0, max(0.0, progress_pct))
        if summary:
            goal.result_summary = summary

        from datetime import datetime, timezone
        goal.last_progress_at = datetime.now(timezone.utc)

        await self._store.save_goal(goal)
        self._logger.log("goal_progress",
                         goal_id=str(goal_id),
                         progress=progress_pct)
        return True

    async def complete_goal(self, goal_id: UUID, reason: str, summary: str) -> bool:
        goal = await self.get_goal(goal_id)
        if not goal:
            return False

        goal.status = GoalStatus.COMPLETED
        goal.progress_pct = 100.0
        goal.completion_reason = reason
        goal.result_summary = summary

        from datetime import datetime, timezone
        goal.completed_at = datetime.now(timezone.utc)

        await self._store.save_goal(goal)
        self._logger.log("goal_completed",
                         goal_id=str(goal_id),
                         reason=reason)
        return True

    async def abandon_goal(self, goal_id: UUID, reason: str) -> bool:
        goal = await self.get_goal(goal_id)
        if not goal:
            return False

        goal.status = GoalStatus.ABANDONED
        goal.completion_reason = reason

        await self._store.save_goal(goal)
        self._logger.log("goal_abandoned",
                         goal_id=str(goal_id),
                         reason=reason)
        return True

    async def defer_goal(
        self,
        goal_id: UUID,
        reason: str,
        revisit_after_cycles: int = 15,
        current_cycle: int = 0,
    ) -> bool:
        goal = await self.get_goal(goal_id)
        if not goal:
            return False

        goal.status = GoalStatus.DEFERRED
        goal.defer_reason = reason
        goal.deferred_until_cycle = current_cycle + revisit_after_cycles

        await self._store.save_goal(goal)
        self._logger.log("goal_deferred",
                         goal_id=str(goal_id),
                         reason=reason,
                         revisit_cycle=goal.deferred_until_cycle)
        return True

    async def get_deferred_goals(self, current_cycle: int) -> list[Goal]:
        return await self._store.get_deferred_goals(current_cycle)
