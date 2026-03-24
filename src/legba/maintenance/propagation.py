"""Reactive state propagation — maintenance daemon task.

Watches for state changes and propagates effects between analytical objects:
situations, hypotheses, watchlists, goals, and the task backlog.

Deterministic, no LLM. Runs on a tick interval alongside other maintenance tasks.
All propagation methods are idempotent — running twice produces the same result.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

import asyncpg

from ..shared.escalation import compute_escalation_score
from ..shared.task_backlog import TaskBacklog

logger = logging.getLogger("legba.maintenance.propagation")

# Redis keys for tracking propagation state
_LAST_TRIGGER_CHECK_KEY = "legba:propagation:last_trigger_check"
_LAST_HYPOTHESIS_SNAPSHOT_KEY = "legba:propagation:hypothesis_snapshot"
_LAST_SITUATION_SEVERITY_KEY = "legba:propagation:situation_severity"
_STALE_GOAL_FLAGGED_KEY = "legba:propagation:stale_goals_flagged"


class StatePropagator:
    """Watches for state changes and propagates effects between analytical objects.

    Runs as a maintenance daemon task. Checks for recent state changes
    and updates related objects.
    """

    def __init__(self, pg_pool: asyncpg.Pool, redis_client, task_backlog: TaskBacklog):
        self.pool = pg_pool
        self.redis = redis_client
        self.backlog = task_backlog

    async def propagate(self) -> int:
        """Run all propagation checks. Returns count of propagations applied."""
        total = 0
        total += await self._propagate_watch_triggers()
        total += await self._propagate_hypothesis_shifts()
        total += await self._propagate_situation_escalation()
        total += await self._propagate_event_lifecycle()
        total += await self._propagate_stale_goals()
        return total

    # ------------------------------------------------------------------
    # Watch trigger propagation
    # ------------------------------------------------------------------

    async def _propagate_watch_triggers(self) -> int:
        """Propagate recent watch triggers to situations and goals.

        For each trigger since last check:
        - Find the watchlist's parent situation (via entity/keyword overlap)
        - Link the triggering event to that situation
        - If watchlist is linked to a goal, note progress
        """
        propagated = 0

        try:
            # Get the last check timestamp
            last_check_str = await self.redis.get(_LAST_TRIGGER_CHECK_KEY)
            if last_check_str:
                try:
                    last_check = datetime.fromisoformat(last_check_str)
                except (ValueError, TypeError):
                    last_check = datetime.now(timezone.utc) - timedelta(minutes=10)
            else:
                last_check = datetime.now(timezone.utc) - timedelta(minutes=10)

            now = datetime.now(timezone.utc)

            # Fetch recent triggers
            triggers = await self.pool.fetch("""
                SELECT wt.id, wt.watch_id, wt.event_id, wt.event_title,
                       wt.triggered_at, w.name AS watch_name, w.data AS watch_data
                FROM watch_triggers wt
                JOIN watchlist w ON w.id = wt.watch_id
                WHERE wt.triggered_at > $1
                ORDER BY wt.triggered_at ASC
            """, last_check)

            if not triggers:
                await self.redis.set(_LAST_TRIGGER_CHECK_KEY, now.isoformat())
                return 0

            for trigger in triggers:
                watch_data = trigger["watch_data"]
                if isinstance(watch_data, str):
                    try:
                        watch_data = json.loads(watch_data)
                    except (json.JSONDecodeError, TypeError):
                        watch_data = {}
                elif not isinstance(watch_data, dict):
                    watch_data = {}

                event_id = trigger["event_id"]
                if not event_id:
                    continue

                # Find matching situation by entity/keyword overlap
                watch_entities = set()
                for e in (watch_data.get("entities") or []):
                    if e:
                        watch_entities.add(e.lower())
                for k in (watch_data.get("keywords") or []):
                    if k:
                        watch_entities.add(k.lower())

                if watch_entities:
                    linked = await self._link_event_to_matching_situation(
                        event_id, watch_entities,
                    )
                    if linked:
                        propagated += 1
                        logger.info(
                            "Watch trigger propagated: event %s linked to situation via '%s'",
                            str(event_id)[:8], trigger["watch_name"],
                        )

            # Update last check timestamp
            await self.redis.set(_LAST_TRIGGER_CHECK_KEY, now.isoformat())

        except Exception as e:
            logger.error("Watch trigger propagation failed: %s", e)

        return propagated

    async def _link_event_to_matching_situation(
        self, event_id, watch_entities: set[str],
    ) -> bool:
        """Find a situation whose key_entities overlap with watch entities, then link."""
        try:
            # Check if already linked
            existing = await self.pool.fetchval(
                "SELECT 1 FROM situation_events WHERE event_id = $1 LIMIT 1",
                event_id,
            )
            if existing:
                return False

            rows = await self.pool.fetch(
                "SELECT id, data FROM situations WHERE status IN ('active', 'escalating')"
            )
            for row in rows:
                raw = row["data"]
                data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
                sit_entities = {
                    e.lower() for e in (data.get("key_entities") or []) if e
                }
                overlap = len(watch_entities & sit_entities)
                if overlap >= 1:
                    await self.pool.execute(
                        "INSERT INTO situation_events (situation_id, event_id) "
                        "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        row["id"], event_id,
                    )
                    # Update event count
                    await self.pool.execute(
                        "UPDATE situations SET event_count = event_count + 1, "
                        "updated_at = NOW() WHERE id = $1",
                        row["id"],
                    )
                    return True
        except Exception as e:
            logger.debug("Event-situation linking failed: %s", e)
        return False

    # ------------------------------------------------------------------
    # Hypothesis shift propagation
    # ------------------------------------------------------------------

    async def _propagate_hypothesis_shifts(self) -> int:
        """Detect significant shifts in hypothesis evidence balance.

        Compares current evidence_balance against last snapshot.
        If balance crosses a significance threshold (delta >= 3),
        flags the parent situation for SYNTHESIZE attention.
        """
        propagated = 0

        try:
            # Load previous snapshot
            snapshot_raw = await self.redis.get(_LAST_HYPOTHESIS_SNAPSHOT_KEY)
            prev_snapshot: dict[str, int] = {}
            if snapshot_raw:
                try:
                    prev_snapshot = json.loads(snapshot_raw)
                except (json.JSONDecodeError, TypeError):
                    pass

            # Fetch current hypothesis states
            rows = await self.pool.fetch("""
                SELECT h.id, h.evidence_balance, h.situation_id, h.thesis,
                       s.name AS situation_name
                FROM hypotheses h
                LEFT JOIN situations s ON s.id = h.situation_id
                WHERE h.status = 'active'
            """)

            new_snapshot: dict[str, int] = {}
            for row in rows:
                hyp_id = str(row["id"])
                current_balance = row["evidence_balance"] or 0
                new_snapshot[hyp_id] = current_balance

                prev_balance = prev_snapshot.get(hyp_id, 0)
                delta = abs(current_balance - prev_balance)

                if delta >= 3 and row["situation_id"]:
                    # Significant shift — add deep_dive task
                    await self.backlog.add_task(
                        task_type="deep_dive_situation",
                        target={
                            "situation_id": str(row["situation_id"]),
                            "situation_name": row["situation_name"] or "unknown",
                            "trigger": f"hypothesis {hyp_id[:8]} balance shifted by {delta:+d}",
                        },
                        priority=min(0.8, 0.5 + delta * 0.05),
                        cycle_type="SYNTHESIZE",
                        context=(
                            f"Hypothesis '{row['thesis'][:80]}' evidence balance "
                            f"shifted from {prev_balance} to {current_balance} "
                            f"(delta={delta:+d}). Situation needs re-assessment."
                        ),
                    )
                    propagated += 1
                    logger.info(
                        "Hypothesis shift detected: %s balance %d -> %d (delta=%+d), "
                        "flagged situation %s for SYNTHESIZE",
                        hyp_id[:8], prev_balance, current_balance, delta,
                        str(row["situation_id"])[:8],
                    )

            # Save new snapshot
            await self.redis.set(
                _LAST_HYPOTHESIS_SNAPSHOT_KEY,
                json.dumps(new_snapshot, default=str),
            )

        except Exception as e:
            logger.error("Hypothesis shift propagation failed: %s", e)

        return propagated

    # ------------------------------------------------------------------
    # Situation escalation propagation
    # ------------------------------------------------------------------

    async def _propagate_situation_escalation(self) -> int:
        """Detect situation severity changes and recommend portfolio actions.

        Compares current severity against last snapshot.
        If severity escalated and no goal covers it, compute escalation score.
        If score >= threshold, add task to create investigative goal.
        """
        propagated = 0

        try:
            # Load previous severity snapshot
            snapshot_raw = await self.redis.get(_LAST_SITUATION_SEVERITY_KEY)
            prev_snapshot: dict[str, dict] = {}
            if snapshot_raw:
                try:
                    prev_snapshot = json.loads(snapshot_raw)
                except (json.JSONDecodeError, TypeError):
                    pass

            _SEVERITY_RANK = {"routine": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

            # Fetch current situation states with event data
            situations = await self.pool.fetch("""
                SELECT s.id, s.name, s.status, s.event_count, s.category,
                       s.data, s.intensity_score
                FROM situations s
                WHERE s.status IN ('active', 'escalating', 'proposed')
            """)

            new_snapshot: dict[str, dict] = {}
            for sit in situations:
                sit_id = str(sit["id"])
                raw = sit["data"]
                data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}

                # Get severity distribution from linked events
                event_rows = await self.pool.fetch("""
                    SELECT e.severity, e.created_at
                    FROM events e
                    JOIN situation_events se ON se.event_id = e.id
                    WHERE se.situation_id = $1
                """, sit["id"])

                severity_dist: dict[str, int] = {}
                for er in event_rows:
                    sev = (er["severity"] or "medium").lower()
                    severity_dist[sev] = severity_dist.get(sev, 0) + 1

                current_max_sev = "low"
                if severity_dist:
                    current_max_sev = max(
                        severity_dist.keys(),
                        key=lambda s: _SEVERITY_RANK.get(s, 0),
                    )

                new_snapshot[sit_id] = {
                    "severity": current_max_sev,
                    "event_count": sit["event_count"] or 0,
                }

                # Compare against previous
                prev = prev_snapshot.get(sit_id, {})
                prev_sev = prev.get("severity", "low")
                prev_count = prev.get("event_count", 0)

                current_rank = _SEVERITY_RANK.get(current_max_sev, 0)
                prev_rank = _SEVERITY_RANK.get(prev_sev, 0)

                # Check for escalation (severity increased or significant event count jump)
                severity_escalated = current_rank > prev_rank
                count_jumped = (sit["event_count"] or 0) >= prev_count + 3

                if not (severity_escalated or count_jumped):
                    continue

                # Check if any goal already covers this situation
                goal_covers = await self._goal_covers_situation(sit_id)
                if goal_covers:
                    continue

                # Compute escalation score
                regions = data.get("regions") or []
                region_novelty = len(regions) > 0  # simplified — true if has region data

                # Count existing situations covering same entities
                key_entities = set(e.lower() for e in (data.get("key_entities") or []) if e)
                existing_count = await self._count_overlapping_situations(
                    sit_id, key_entities,
                )

                # Calculate time window from event timestamps
                time_window = 48.0  # default
                if event_rows:
                    timestamps = []
                    for er in event_rows:
                        if er["created_at"]:
                            ts = er["created_at"]
                            if hasattr(ts, 'timestamp'):
                                timestamps.append(ts)
                    if len(timestamps) >= 2:
                        span = max(timestamps) - min(timestamps)
                        time_window = max(0.1, span.total_seconds() / 3600.0)

                result = compute_escalation_score(
                    event_count=sit["event_count"] or 0,
                    severity_distribution=severity_dist,
                    entity_overlap_with_portfolio=0.0,  # conservative
                    region_novelty=region_novelty,
                    time_window_hours=time_window,
                    existing_situation_count=existing_count,
                )

                if result["recommendation"] in ("full_portfolio", "situation_and_watchlist"):
                    await self.backlog.add_task(
                        task_type="create_investigative_goal",
                        target={
                            "situation_id": sit_id,
                            "situation_name": sit["name"],
                            "escalation_score": result["score"],
                            "recommendation": result["recommendation"],
                        },
                        priority=min(0.9, result["score"]),
                        cycle_type="EVOLVE",
                        context=(
                            f"Situation '{sit['name']}' escalated: "
                            f"{', '.join(result['reasons'][:3])}. "
                            f"Score={result['score']:.2f}, "
                            f"recommendation={result['recommendation']}."
                        ),
                    )
                    propagated += 1
                    logger.info(
                        "Situation escalation: %s score=%.2f rec=%s — task created",
                        sit["name"][:60], result["score"], result["recommendation"],
                    )

            # Save new snapshot
            await self.redis.set(
                _LAST_SITUATION_SEVERITY_KEY,
                json.dumps(new_snapshot, default=str),
            )

        except Exception as e:
            logger.error("Situation escalation propagation failed: %s", e)

        return propagated

    async def _goal_covers_situation(self, situation_id: str) -> bool:
        """Check if any active goal references this situation."""
        try:
            rows = await self.pool.fetch(
                "SELECT id, data FROM goals WHERE status = 'active'"
            )
            for row in rows:
                raw = row["data"]
                data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
                ctx = data.get("context") or {}
                if isinstance(ctx, dict):
                    linked = ctx.get("linked_situation_id", "")
                    if str(linked) == situation_id:
                        return True
                # Also check description for situation reference
                desc = data.get("description", "")
                if situation_id[:8] in desc:
                    return True
        except Exception as e:
            logger.debug("Goal coverage check failed: %s", e)
        return False

    async def _count_overlapping_situations(
        self, exclude_id: str, key_entities: set[str],
    ) -> int:
        """Count other active situations that overlap with the given entities."""
        if not key_entities:
            return 0
        try:
            rows = await self.pool.fetch(
                "SELECT id, data FROM situations "
                "WHERE status IN ('active', 'escalating') AND id != $1",
                UUID(exclude_id),
            )
            count = 0
            for row in rows:
                raw = row["data"]
                data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
                sit_entities = {
                    e.lower() for e in (data.get("key_entities") or []) if e
                }
                if len(key_entities & sit_entities) >= 2:
                    count += 1
            return count
        except Exception as e:
            logger.debug("Overlap count failed: %s", e)
            return 0

    # ------------------------------------------------------------------
    # Event lifecycle propagation
    # ------------------------------------------------------------------

    async def _propagate_event_lifecycle(self) -> int:
        """When events reach ACTIVE status, create research tasks for key actors.

        If the event is linked to a situation with an investigative goal,
        add research_entity tasks for the event's key actors.
        """
        propagated = 0

        try:
            # Find events that recently transitioned to ACTIVE (last tick window)
            # Use a Redis marker to track what we've already processed
            last_check_str = await self.redis.get("legba:propagation:last_event_lifecycle_check")
            if last_check_str:
                try:
                    last_check = datetime.fromisoformat(last_check_str)
                except (ValueError, TypeError):
                    last_check = datetime.now(timezone.utc) - timedelta(minutes=10)
            else:
                last_check = datetime.now(timezone.utc) - timedelta(minutes=10)

            now = datetime.now(timezone.utc)

            rows = await self.pool.fetch("""
                SELECT e.id, e.title, e.data, se.situation_id
                FROM events e
                JOIN situation_events se ON se.event_id = e.id
                WHERE e.lifecycle_status = 'active'
                  AND e.updated_at > $1
            """, last_check)

            for row in rows:
                raw = row["data"]
                data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
                actors = data.get("actors") or []

                if not actors:
                    continue

                # Check if the situation has an investigative goal
                has_goal = await self._goal_covers_situation(str(row["situation_id"]))
                if not has_goal:
                    continue

                # Add research tasks for key actors (limit to 2 per event)
                for actor in actors[:2]:
                    if not actor or len(actor) < 3:
                        continue
                    await self.backlog.add_task(
                        task_type="research_entity",
                        target={
                            "entity_name": actor,
                            "event_id": str(row["id"]),
                            "situation_id": str(row["situation_id"]),
                        },
                        priority=0.5,
                        cycle_type="RESEARCH",
                        context=(
                            f"Key actor '{actor}' from event '{row['title'][:60]}' "
                            f"needs enrichment (event reached ACTIVE status)."
                        ),
                    )
                    propagated += 1

            await self.redis.set(
                "legba:propagation:last_event_lifecycle_check",
                now.isoformat(),
            )

        except Exception as e:
            logger.error("Event lifecycle propagation failed: %s", e)

        return propagated

    # ------------------------------------------------------------------
    # Stale goal propagation
    # ------------------------------------------------------------------

    async def _propagate_stale_goals(self) -> int:
        """Flag goals with no progress in 50+ cycles for EVOLVE review.

        Idempotent: tracks which goals have already been flagged to avoid
        duplicate tasks.
        """
        propagated = 0

        try:
            # Load set of already-flagged goal IDs
            flagged_raw = await self.redis.get(_STALE_GOAL_FLAGGED_KEY)
            flagged_ids: set[str] = set()
            if flagged_raw:
                try:
                    flagged_ids = set(json.loads(flagged_raw))
                except (json.JSONDecodeError, TypeError):
                    pass

            # Get current cycle number from Redis
            cycle_str = await self.redis.hget("legba:state", "cycle_number")
            current_cycle = int(cycle_str) if cycle_str else 0

            if current_cycle < 50:
                return 0

            # Find active goals with stale progress
            rows = await self.pool.fetch("""
                SELECT id, data FROM goals WHERE status = 'active'
            """)

            for row in rows:
                goal_id = str(row["id"])
                if goal_id in flagged_ids:
                    continue

                raw = row["data"]
                data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}

                # Check last_progress_at
                last_progress = data.get("last_progress_at")
                if last_progress:
                    try:
                        lp_dt = datetime.fromisoformat(last_progress)
                        if lp_dt.tzinfo is None:
                            lp_dt = lp_dt.replace(tzinfo=timezone.utc)
                        # Stale if no progress in ~50 cycles (roughly 10+ hours at 5 cycles/hr)
                        stale_threshold = datetime.now(timezone.utc) - timedelta(hours=10)
                        if lp_dt > stale_threshold:
                            continue
                    except (ValueError, TypeError):
                        pass

                desc = data.get("description", "unknown goal")
                await self.backlog.add_task(
                    task_type="review_goal",
                    target={
                        "goal_id": goal_id,
                        "goal_description": desc[:120],
                    },
                    priority=0.3,
                    cycle_type="EVOLVE",
                    context=(
                        f"Goal '{desc[:80]}' has had no progress update in a long time. "
                        f"EVOLVE should assess whether to continue, reprioritize, or abandon."
                    ),
                )
                flagged_ids.add(goal_id)
                propagated += 1
                logger.info(
                    "Stale goal flagged: %s '%s'",
                    goal_id[:8], desc[:60],
                )

            # Save updated flagged set
            await self.redis.set(
                _STALE_GOAL_FLAGGED_KEY,
                json.dumps(list(flagged_ids), default=str),
            )

        except Exception as e:
            logger.error("Stale goal propagation failed: %s", e)

        return propagated
