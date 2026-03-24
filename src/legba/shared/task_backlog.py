"""Redis-backed task queue for the planning layer.

Tasks are scored by priority (higher = more urgent) in a Redis sorted set.
Each cycle type checks for matching tasks before falling back to heuristics.

Task types:
    research_entity         — enrich a specific entity profile
    evaluate_hypothesis     — gather evidence for/against a hypothesis
    deep_dive_situation     — SYNTHESIZE should investigate this situation
    create_watchlist        — create a watchlist for new entities/keywords
    link_events             — link orphan events to a situation
    resolve_contradiction   — facts or signals contradict each other
    review_proposed_edges   — review auto-proposed graph edges
    review_goal             — EVOLVE should review stale goal
    create_investigative_goal — escalation triggered, needs investigative goal
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

logger = logging.getLogger("legba.shared.task_backlog")

BACKLOG_KEY = "legba:task_backlog"
COMPLETED_KEY = "legba:task_backlog:completed"
_MAX_COMPLETED_LOG = 200  # Keep last N completed task IDs for dedup


class TaskBacklog:
    """Redis sorted set backed task queue.

    Tasks are scored by priority (higher = more urgent).
    Each cycle type checks for matching tasks before falling back to heuristics.
    """

    def __init__(self, redis_client):
        self.redis = redis_client

    async def add_task(
        self,
        task_type: str,
        target: dict,
        goal_id: str = None,
        priority: float = 0.5,
        cycle_type: str = None,
        context: str = "",
    ) -> str:
        """Add a task to the backlog.

        Args:
            task_type: One of: research_entity, evaluate_hypothesis,
                deep_dive_situation, create_watchlist, link_events,
                resolve_contradiction, review_proposed_edges, review_goal,
                create_investigative_goal
            target: Identifying data, e.g. {"entity_id": "...", "entity_name": "..."}
                or {"situation_id": "...", "hypothesis_id": "..."}
            goal_id: Optional parent goal this task serves
            priority: 0.0 (low) to 1.0 (urgent). Used as sorted set score.
            cycle_type: Which cycle should handle this
                (RESEARCH, SYNTHESIZE, SURVEY, ANALYSIS, CURATE, EVOLVE)
            context: Free-text context for the agent

        Returns:
            task_id (str)
        """
        # Dedup: skip if an identical task_type + target already exists
        existing = await self._find_duplicate(task_type, target)
        if existing:
            logger.debug(
                "Duplicate task skipped: %s target=%s (existing=%s)",
                task_type, target, existing,
            )
            return existing

        task_id = str(uuid4())
        now = datetime.now(timezone.utc)

        task = {
            "task_id": task_id,
            "task_type": task_type,
            "target": target,
            "goal_id": goal_id,
            "priority": priority,
            "cycle_type": cycle_type,
            "context": context,
            "status": "pending",
            "created_at": now.isoformat(),
            "created_cycle": None,  # Filled by caller if known
        }

        await self.redis.zadd(BACKLOG_KEY, {json.dumps(task, default=str): priority})
        logger.info(
            "Task added: %s type=%s target=%s priority=%.2f cycle=%s",
            task_id[:8], task_type, _summarize_target(target), priority,
            cycle_type or "any",
        )
        return task_id

    async def get_tasks(
        self, cycle_type: str = None, limit: int = 5,
    ) -> list[dict]:
        """Get highest-priority tasks, optionally filtered by cycle_type.

        Returns tasks sorted by priority descending (highest first).
        Does NOT remove them — caller must complete_task() when done.
        """
        # Fetch all members with scores, highest score first
        raw = await self.redis.zrevrangebyscore(
            BACKLOG_KEY, "+inf", "-inf", withscores=True,
        )
        if not raw:
            return []

        results = []
        for member, score in raw:
            try:
                task = json.loads(member)
            except (json.JSONDecodeError, TypeError):
                continue

            if task.get("status") != "pending":
                continue

            # Filter by cycle_type if requested
            if cycle_type:
                task_cycle = task.get("cycle_type")
                if task_cycle and task_cycle.upper() != cycle_type.upper():
                    continue

            task["_score"] = score
            results.append(task)

            if len(results) >= limit:
                break

        return results

    async def complete_task(self, task_id: str, result: str = "") -> None:
        """Mark a task as completed and remove from backlog."""
        raw = await self.redis.zrangebyscore(BACKLOG_KEY, "-inf", "+inf")
        if not raw:
            return

        for member in raw:
            try:
                task = json.loads(member)
            except (json.JSONDecodeError, TypeError):
                continue

            if task.get("task_id") == task_id:
                await self.redis.zrem(BACKLOG_KEY, member)
                # Log completion
                await self.redis.lpush(COMPLETED_KEY, task_id)
                await self.redis.ltrim(COMPLETED_KEY, 0, _MAX_COMPLETED_LOG - 1)
                logger.info(
                    "Task completed: %s type=%s result=%s",
                    task_id[:8], task.get("task_type", "?"),
                    (result[:80] + "...") if len(result) > 80 else result,
                )
                return

        logger.debug("Task %s not found in backlog (already completed?)", task_id[:8])

    async def expire_stale(self, max_age_hours: float = 72.0) -> int:
        """Remove tasks older than max_age_hours. Returns count removed."""
        raw = await self.redis.zrangebyscore(BACKLOG_KEY, "-inf", "+inf")
        if not raw:
            return 0

        now = datetime.now(timezone.utc)
        removed = 0

        for member in raw:
            try:
                task = json.loads(member)
            except (json.JSONDecodeError, TypeError):
                continue

            created_str = task.get("created_at")
            if not created_str:
                continue

            try:
                created = datetime.fromisoformat(created_str)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            age_hours = (now - created).total_seconds() / 3600.0
            if age_hours > max_age_hours:
                await self.redis.zrem(BACKLOG_KEY, member)
                removed += 1
                logger.debug(
                    "Expired stale task: %s type=%s age=%.1fh",
                    task.get("task_id", "?")[:8],
                    task.get("task_type", "?"),
                    age_hours,
                )

        if removed:
            logger.info("Expired %d stale tasks (older than %.0fh)", removed, max_age_hours)
        return removed

    async def task_count(self, cycle_type: str = None) -> int:
        """Count pending tasks, optionally filtered by cycle_type."""
        if not cycle_type:
            return await self.redis.zcard(BACKLOG_KEY)

        # Must scan for cycle_type filter
        raw = await self.redis.zrangebyscore(BACKLOG_KEY, "-inf", "+inf")
        count = 0
        for member in raw:
            try:
                task = json.loads(member)
            except (json.JSONDecodeError, TypeError):
                continue
            if task.get("status") != "pending":
                continue
            task_cycle = task.get("cycle_type")
            if task_cycle and task_cycle.upper() == cycle_type.upper():
                count += 1
            elif not task_cycle:
                count += 1  # Untyped tasks count for all cycle types
        return count

    async def _find_duplicate(self, task_type: str, target: dict) -> str | None:
        """Check if an identical task already exists in the backlog.

        Matches on task_type + target keys. Returns task_id if found, else None.
        """
        raw = await self.redis.zrangebyscore(BACKLOG_KEY, "-inf", "+inf")
        if not raw:
            return None

        # Normalize target for comparison
        target_key = _normalize_target(target)

        for member in raw:
            try:
                task = json.loads(member)
            except (json.JSONDecodeError, TypeError):
                continue

            if task.get("task_type") != task_type:
                continue
            if task.get("status") != "pending":
                continue

            existing_key = _normalize_target(task.get("target", {}))
            if existing_key == target_key:
                return task.get("task_id")

        return None


def _normalize_target(target: dict) -> str:
    """Create a stable string key from target dict for dedup comparison."""
    if not target:
        return ""
    # Sort keys for stability
    return json.dumps(target, sort_keys=True, default=str)


def _summarize_target(target: dict) -> str:
    """Short human-readable summary of a target dict."""
    if not target:
        return "{}"
    # Pick the first identifying field
    for key in ("entity_name", "situation_id", "hypothesis_id", "entity_id", "event_id"):
        if key in target:
            val = str(target[key])
            return f"{key}={val[:30]}"
    # Fallback
    return str(target)[:60]
