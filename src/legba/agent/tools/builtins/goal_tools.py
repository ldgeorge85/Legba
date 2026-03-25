"""
Goal CRUD tools: goal_create, goal_list, goal_update, goal_decompose

Expose the GoalManager to the LLM so the agent can create, decompose,
track, and complete goals via tool calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ....shared.schemas.tools import ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from ...goals.manager import GoalManager
    from ..registry import ToolRegistry


def register(
    registry: ToolRegistry,
    *,
    goals: GoalManager,
    state: Any = None,
    redis: Any = None,
) -> None:
    """Register goal CRUD tools wired to the live GoalManager."""
    from ....shared.schemas.goals import GoalType, GoalStatus
    from uuid import UUID as _UUID
    import logging as _logging
    _goal_logger = _logging.getLogger("legba.tools.goal")

    def _word_overlap(a: str, b: str) -> float:
        """Fraction of words shared between two strings."""
        wa = set(a.lower().split())
        wb = set(b.lower().split())
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / min(len(wa), len(wb))

    async def goal_create_handler(args: dict) -> str:
        description = args.get("description", "")
        if not description:
            return "Error: description is required."

        # Dedup check — reject if an active/paused goal covers the same scope
        existing = await goals.get_all_goals()
        for g in existing:
            if g.status.value not in ("active", "paused"):
                continue
            if _word_overlap(description, g.description) > 0.75:
                return (
                    f"Duplicate detected: existing goal {g.id} "
                    f"({g.progress_pct:.0f}%) already covers this scope: "
                    f"'{g.description[:100]}'. "
                    f"Use goal_update to update it instead."
                )

        goal_type_str = args.get("goal_type", "goal")
        type_map = {
            "meta_goal": GoalType.META_GOAL,
            "goal": GoalType.GOAL,
            "subgoal": GoalType.SUBGOAL,
            "task": GoalType.TASK,
        }
        goal_type = type_map.get(goal_type_str, GoalType.GOAL)
        priority = int(args.get("priority", 5))
        parent_id_str = args.get("parent_id")
        parent_id = _UUID(parent_id_str) if parent_id_str else None
        criteria_str = args.get("success_criteria", "")
        criteria = [c.strip() for c in criteria_str.split(",") if c.strip()] if criteria_str else []

        goal = await goals.create_goal(
            description=description,
            goal_type=goal_type,
            priority=priority,
            parent_id=parent_id,
            success_criteria=criteria,
        )

        # Auto-complete matching tasks in the backlog
        completed_tasks = []
        if redis:
            try:
                from ....shared.task_backlog import TaskBacklog
                backlog = TaskBacklog(redis)
                pending = await backlog.get_tasks(limit=50)
                desc_words = set(description.lower().split())
                for task in pending:
                    target = task.get("target", {})
                    # Build a text representation of the task target
                    target_text = " ".join(str(v) for v in target.values()) if target else ""
                    context_text = task.get("context", "")
                    task_text = f"{target_text} {context_text}"
                    if _word_overlap(description, task_text) > 0.5:
                        await backlog.complete_task(
                            task["task_id"],
                            result=f"Auto-completed: goal {goal.id} created",
                        )
                        completed_tasks.append(task["task_id"][:8])
            except Exception as exc:
                _goal_logger.debug("Task auto-complete failed: %s", exc)

        result_msg = f"Goal created: id={goal.id}, type={goal.goal_type.value}, priority={goal.priority}"
        if completed_tasks:
            result_msg += f" (auto-completed {len(completed_tasks)} backlog task(s): {', '.join(completed_tasks)})"
        return result_msg

    async def goal_list_handler(args: dict) -> str:
        status_filter = args.get("status", "active").lower()

        if status_filter == "all":
            all_goals = await goals.get_all_goals()
        else:
            all_goals = await goals.get_all_goals()
            all_goals = [g for g in all_goals if g.status.value == status_filter]

        if not all_goals:
            return f"No {status_filter} goals found."

        lines = [f"Goals ({status_filter}, {len(all_goals)} total):"]
        for g in all_goals:
            parent = f" parent={g.parent_id}" if g.parent_id else ""
            progress = f" {g.progress_pct:.0f}%" if g.progress_pct > 0 else ""
            children = f" children={len(g.child_ids)}" if g.child_ids else ""
            lines.append(
                f"  [{g.goal_type.value}] id={g.id} p={g.priority} "
                f"status={g.status.value}{progress}{parent}{children}"
            )
            lines.append(f"    {g.description[:120]}")
            if g.success_criteria:
                for sc in g.success_criteria[:3]:
                    lines.append(f"    - {sc}")
        return "\n".join(lines)

    async def goal_update_handler(args: dict) -> str:
        goal_id_str = args.get("goal_id", "")
        action = args.get("action", "")
        reason = args.get("reason", "")

        if not goal_id_str:
            return "Error: goal_id is required."

        try:
            goal_id = _UUID(goal_id_str)
        except ValueError:
            return f"Error: invalid goal_id '{goal_id_str}'"

        if action == "progress":
            progress = float(args.get("progress_pct", 0))
            ok = await goals.update_progress(goal_id, progress, reason or None)
            return f"Progress updated to {progress:.0f}%." if ok else "Error: goal not found."

        elif action == "complete":
            if not reason:
                return "Error: reason is required for completion."
            ok = await goals.complete_goal(goal_id, reason, reason)
            return f"Goal {goal_id} completed." if ok else "Error: goal not found."

        elif action == "abandon":
            if not reason:
                return "Error: reason is required for abandonment."
            ok = await goals.abandon_goal(goal_id, reason)
            return f"Goal {goal_id} abandoned." if ok else "Error: goal not found."

        elif action == "pause":
            goal = await goals.get_goal(goal_id)
            if not goal:
                return "Error: goal not found."
            goal.status = GoalStatus.PAUSED
            await goals._store.save_goal(goal)
            return f"Goal {goal_id} paused."

        elif action == "resume":
            goal = await goals.get_goal(goal_id)
            if not goal:
                return "Error: goal not found."
            goal.status = GoalStatus.ACTIVE
            await goals._store.save_goal(goal)
            return f"Goal {goal_id} resumed."

        elif action == "reprioritize":
            priority = int(args.get("priority", 5))
            goal = await goals.get_goal(goal_id)
            if not goal:
                return "Error: goal not found."
            goal.priority = max(1, min(10, priority))
            await goals._store.save_goal(goal)
            return f"Goal {goal_id} reprioritized to {goal.priority}."

        elif action == "defer":
            if not reason:
                return "Error: reason is required for deferral."
            revisit_after = int(args.get("revisit_after_cycles", 15))
            current_cycle = state.cycle_number if state else 0
            ok = await goals.defer_goal(
                goal_id, reason,
                revisit_after_cycles=revisit_after,
                current_cycle=current_cycle,
            )
            if ok:
                return (
                    f"Goal {goal_id} deferred. Reason: {reason}. "
                    f"Will be revisited in {revisit_after} cycles."
                )
            return "Error: goal not found."

        return f"Unknown action: {action}. Use: progress, complete, abandon, pause, resume, reprioritize, defer."

    async def goal_decompose_handler(args: dict) -> str:
        goal_id_str = args.get("goal_id", "")
        subtasks_str = args.get("subtasks", "")

        if not goal_id_str:
            return "Error: goal_id is required."
        if not subtasks_str:
            return "Error: subtasks is required (pipe-separated descriptions)."

        try:
            goal_id = _UUID(goal_id_str)
        except ValueError:
            return f"Error: invalid goal_id '{goal_id_str}'"

        parent = await goals.get_goal(goal_id)
        if not parent:
            return "Error: parent goal not found."

        descriptions = [d.strip() for d in subtasks_str.split("|") if d.strip()]
        if not descriptions:
            return "Error: no valid subtask descriptions."

        children = await goals.decompose(parent, descriptions)
        lines = [f"Decomposed into {len(children)} sub-goals:"]
        for c in children:
            lines.append(f"  - {c.id}: {c.description[:80]}")
        return "\n".join(lines)

    registry.register(GOAL_CREATE_DEF, goal_create_handler)
    registry.register(GOAL_LIST_DEF, goal_list_handler)
    registry.register(GOAL_UPDATE_DEF, goal_update_handler)
    registry.register(GOAL_DECOMPOSE_DEF, goal_decompose_handler)


GOAL_CREATE_DEF = ToolDefinition(
    name="goal_create",
    description=(
        "Create a new goal in the goal hierarchy. Use for strategic objectives, "
        "operational goals, or specific tasks derived from the seed goal."
    ),
    parameters=[
        ToolParameter(name="description", type="string",
                      description="What this goal aims to achieve"),
        ToolParameter(name="goal_type", type="string",
                      description="Type: meta_goal, goal, subgoal, task. Default: goal",
                      required=False),
        ToolParameter(name="priority", type="number",
                      description="Priority 1-10 (1=highest). Default: 5",
                      required=False),
        ToolParameter(name="parent_id", type="string",
                      description="UUID of the parent goal (for subgoals/tasks)",
                      required=False),
        ToolParameter(name="success_criteria", type="string",
                      description="Comma-separated success criteria",
                      required=False),
    ],
)


GOAL_LIST_DEF = ToolDefinition(
    name="goal_list",
    description=(
        "List goals. By default shows active goals. "
        "Use to review the current goal hierarchy and pick focus."
    ),
    parameters=[
        ToolParameter(name="status", type="string",
                      description="Filter: active, paused, blocked, deferred, completed, abandoned, all. Default: active",
                      required=False),
    ],
)


GOAL_UPDATE_DEF = ToolDefinition(
    name="goal_update",
    description=(
        "Update a goal's progress, status, or priority. Use to track progress, "
        "pause/resume/defer goals, or mark goals as completed/abandoned. "
        "Use 'defer' to park a goal for later revisit when it has diminishing returns."
    ),
    parameters=[
        ToolParameter(name="goal_id", type="string",
                      description="UUID of the goal to update"),
        ToolParameter(name="action", type="string",
                      description="Action: progress, complete, abandon, pause, resume, reprioritize, defer"),
        ToolParameter(name="progress_pct", type="number",
                      description="New progress percentage 0-100 (for 'progress' action)",
                      required=False),
        ToolParameter(name="reason", type="string",
                      description="Reason for completion/abandonment/deferral, or progress summary",
                      required=False),
        ToolParameter(name="priority", type="number",
                      description="New priority 1-10 (for 'reprioritize' action)",
                      required=False),
        ToolParameter(name="revisit_after_cycles", type="number",
                      description="Number of cycles before revisiting a deferred goal (default: 15, for 'defer' action)",
                      required=False),
    ],
)


GOAL_DECOMPOSE_DEF = ToolDefinition(
    name="goal_decompose",
    description=(
        "Decompose a goal into sub-goals or tasks. Breaks a high-level objective "
        "into actionable steps."
    ),
    parameters=[
        ToolParameter(name="goal_id", type="string",
                      description="UUID of the parent goal to decompose"),
        ToolParameter(name="subtasks", type="string",
                      description="Pipe-separated list of sub-goal descriptions (e.g. 'Do X|Do Y|Do Z')"),
    ],
)


# Stubs — only used if register() is not called
async def goal_create(args: dict) -> str:
    return "Error: Goal manager not initialized."


async def goal_list(args: dict) -> str:
    return "Error: Goal manager not initialized."


async def goal_update(args: dict) -> str:
    return "Error: Goal manager not initialized."


async def goal_decompose(args: dict) -> str:
    return "Error: Goal manager not initialized."
