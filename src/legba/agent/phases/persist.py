"""PERSIST phase — save everything and emit heartbeat."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from ...shared.schemas.cycle import CycleResponse
from ...shared.schemas.comms import InboxMessage, OutboxMessage, Outbox
from ...shared.schemas.memory import Episode, EpisodeType
from . import REPORT_INTERVAL

if TYPE_CHECKING:
    from ..cycle import AgentCycle


class PersistMixin:
    """Phase 6: Save everything and emit heartbeat."""

    async def _persist(self: AgentCycle) -> CycleResponse:
        """
        Phase 6: Save everything and emit heartbeat.

        All data persistence runs first (goal progress, memories, episodes,
        outbox) so that valuable state is saved even if liveness check fails.
        Liveness check is last — it only affects the heartbeat nonce.
        """
        self.logger.log_phase("persist")
        self.state.phase = "persist"

        # Use reflection summary for the episode (much better than raw final response)
        cycle_summary = self._reflection_data.get(
            "cycle_summary",
            self._final_response[:1000] if self._final_response else "empty cycle",
        )
        significance = float(self._reflection_data.get("significance", 0.5))

        # Update goal progress from reflection data
        goal_progress = self._reflection_data.get("goal_progress", {})
        if goal_progress and hasattr(self, 'goals') and self.goals:
            try:
                progress_delta = float(goal_progress.get("progress_delta", 0))
                goal_desc = goal_progress.get("description", "")
                notes = goal_progress.get("notes", "")
                if progress_delta > 0 and goal_desc:
                    # Find matching goal and update (progress_pct is 0-100 scale)
                    for goal in self._active_goals:
                        desc = goal.description if hasattr(goal, 'description') else str(goal)
                        if goal_desc.lower() in str(desc).lower() or str(desc).lower() in goal_desc.lower():
                            current = goal.progress_pct if hasattr(goal, 'progress_pct') else 0
                            new_pct = min(100.0, (current or 0) + progress_delta * 100)
                            await self.goals.update_progress(
                                goal_id=goal.id,
                                progress_pct=new_pct,
                                summary=notes,
                            )
                            self.logger.log("goal_progress_updated",
                                            goal_id=str(goal.id),
                                            progress_pct=new_pct,
                                            delta=progress_delta)
                            break
            except Exception as e:
                self.logger.log_error(f"Failed to update goal progress: {e}")

        # Auto-complete goals that reached 100% progress
        try:
            if hasattr(self, 'goals') and self.goals and hasattr(self, '_active_goals'):
                for goal in list(self._active_goals):
                    if (goal.status.value == "active"
                            and hasattr(goal, 'progress_pct')
                            and goal.progress_pct is not None
                            and goal.progress_pct >= 100):
                        await self.goals.complete_goal(
                            goal.id,
                            reason="Auto-completed: progress reached 100%.",
                            summary=goal.result_summary or "Completed.",
                        )
                        self.logger.log("goal_auto_completed", goal_id=str(goal.id))
        except Exception as e:
            self.logger.log_error(f"Goal auto-complete failed: {e}")

        # Update per-goal work tracker (Phase O)
        try:
            goal_progress = self._reflection_data.get("goal_progress", {})
            goal_desc = goal_progress.get("description", "") if goal_progress else ""
            _progress_delta = float(goal_progress.get("progress_delta", 0)) if goal_progress else 0

            if goal_desc:
                matched_goal_id = None
                for goal in self._active_goals:
                    desc = goal.description if hasattr(goal, 'description') else str(goal)
                    if goal_desc.lower() in str(desc).lower() or str(desc).lower() in goal_desc.lower():
                        matched_goal_id = str(goal.id)
                        break

                if matched_goal_id:
                    tracker = dict(self._goal_work_tracker)
                    entry = tracker.get(matched_goal_id, {
                        "cycles_worked": 0,
                        "last_progress_cycle": 0,
                        "last_worked_cycle": 0,
                    })
                    entry["cycles_worked"] = entry.get("cycles_worked", 0) + 1
                    entry["last_worked_cycle"] = self.state.cycle_number
                    if _progress_delta > 0:
                        entry["last_progress_cycle"] = self.state.cycle_number
                    tracker[matched_goal_id] = entry

                    # Prune entries for goals no longer active
                    active_ids = {str(g.id) for g in self._active_goals}
                    tracker = {k: v for k, v in tracker.items() if k in active_ids}

                    await self.memory.registers.set_json("goal_work_tracker", tracker)
        except Exception as e:
            self.logger.log_error(f"Goal work tracker update failed: {e}")

        # Compute phase-awareness metadata for reflection forward
        _work_pattern = "unknown"
        _stale_goal_count = 0

        try:
            tool_counts: dict[str, int] = {}
            for entry in self.llm.working_memory._entries:
                if entry.get("type") == "tool":
                    t = entry.get("tool", "")
                    tool_counts[t] = tool_counts.get(t, 0) + 1
            research = tool_counts.get("http_request", 0)
            graph_writes = tool_counts.get("graph_store", 0)
            memory_writes = tool_counts.get("memory_store", 0)
            graph_reads = tool_counts.get("graph_query", 0) + tool_counts.get("graph_analyze", 0)
            if research >= 2:
                _work_pattern = "collecting"
            elif graph_writes >= 2 or memory_writes >= 3:
                _work_pattern = "deepening"
            elif graph_reads >= 2:
                _work_pattern = "analyzing"
            else:
                _work_pattern = "mixed"
        except Exception:
            pass

        try:
            from datetime import timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
            for goal in self._active_goals:
                if goal.status.value == "active" and goal.progress_pct < 100:
                    if goal.last_progress_at is None or goal.last_progress_at < cutoff:
                        _stale_goal_count += 1
        except Exception:
            pass

        # Track last_ingestion_cycle — update if event_store was used this cycle
        try:
            tool_counts: dict[str, int] = {}
            for entry in self.llm.working_memory._entries:
                if entry.get("type") == "tool":
                    t = entry.get("tool", "")
                    tool_counts[t] = tool_counts.get(t, 0) + 1
            if tool_counts.get("event_store", 0) > 0:
                await self.memory.registers.set(
                    "last_ingestion_cycle", str(self.state.cycle_number)
                )
        except Exception:
            pass

        # Store reflection-forward data for next cycle's planning
        if self.memory:
            reflection_forward = {}
            self_assessment = self._reflection_data.get("self_assessment", "")
            next_suggestion = self._reflection_data.get("next_cycle_suggestion", "")
            if self_assessment:
                reflection_forward["self_assessment"] = self_assessment[:500]
            if next_suggestion:
                reflection_forward["next_cycle_suggestion"] = next_suggestion[:500]
            reflection_forward["recent_work_pattern"] = _work_pattern
            reflection_forward["stale_goal_count"] = _stale_goal_count
            if reflection_forward:
                await self.memory.registers.set_json("reflection_forward", reflection_forward)

        # Auto-promote memories flagged in reflection
        memories_to_promote = self._reflection_data.get("memories_to_promote", [])
        if memories_to_promote and self.memory and self.llm:
            promoted = 0
            for hint in memories_to_promote:
                if not isinstance(hint, str) or not hint.strip():
                    continue
                try:
                    emb = await self.llm.generate_embedding(hint[:500])
                    candidates = await self.memory.episodic.search_similar(
                        query_vector=emb,
                        collection=self.memory.episodic.SHORT_TERM,
                        limit=1,
                        min_score=0.7,
                    )
                    if candidates:
                        ep_id = str(candidates[0].get("id", ""))
                        if ep_id:
                            points = await self.memory.episodic._client.retrieve(
                                collection_name=self.memory.episodic.SHORT_TERM,
                                ids=[ep_id], with_vectors=True, with_payload=True,
                            )
                            if points:
                                ok = await self.memory.episodic.promote_to_long_term(
                                    episode_id=ep_id,
                                    vector=points[0].vector,
                                    payload=points[0].payload,
                                )
                                if ok:
                                    promoted += 1
                except Exception as e:
                    self.logger.log_error(f"Auto-promote failed for '{hint[:50]}': {e}")
            if promoted:
                self.logger.log("auto_promoted", count=promoted)

        # Auto-promote high-significance short-term memories (significance >= 0.6)
        try:
            if self.memory and self.memory.episodic._available:
                from qdrant_client.models import Filter, FieldCondition, Range
                high_sig = await self.memory.episodic._client.scroll(
                    collection_name=self.memory.episodic.SHORT_TERM,
                    scroll_filter=Filter(must=[
                        FieldCondition(key="significance", range=Range(gte=0.6)),
                    ]),
                    limit=5,
                    with_vectors=True,
                    with_payload=True,
                )
                if high_sig and high_sig[0]:
                    auto_promoted = 0
                    for point in high_sig[0]:
                        ok = await self.memory.episodic.promote_to_long_term(
                            episode_id=str(point.id),
                            vector=point.vector,
                            payload=point.payload,
                        )
                        if ok:
                            auto_promoted += 1
                    if auto_promoted:
                        self.logger.log("auto_promoted_high_significance", count=auto_promoted)
        except Exception as e:
            self.logger.log_error(f"High-significance auto-promote failed: {e}")

        # Store cycle episode
        episode = Episode(
            cycle_number=self.state.cycle_number,
            episode_type=EpisodeType.CYCLE_SUMMARY,
            content=cycle_summary[:1000],
            significance=significance,
        )

        try:
            episode.embedding = await self.llm.generate_embedding(episode.content)
            await self.memory.store_episode(episode)
        except Exception as e:
            self.logger.log_error(f"Failed to store episode: {e}")

        # Generate outbox responses for inbox messages that require them
        for msg_data in self.state.inbox_messages:
            msg = InboxMessage(**msg_data)
            if msg.requires_response:
                self._outbox_messages.append(OutboxMessage(
                    id=str(uuid4()),
                    in_reply_to=msg.id,
                    content=self._final_response[:500] if self._final_response else "Cycle completed.",
                    cycle_number=self.state.cycle_number,
                ))

        # On reporting cycles, add cycle summary to outbox as a fallback report.
        if self.state.cycle_number > 0 and self.state.cycle_number % REPORT_INTERVAL == 0:
            self._outbox_messages.append(OutboxMessage(
                id=str(uuid4()),
                content=f"[STATUS REPORT — Cycle {self.state.cycle_number}]\n\n{cycle_summary}",
                cycle_number=self.state.cycle_number,
                metadata={"type": "status_report"},
            ))

        # Write outbox — NATS first, file fallback
        if self._outbox_messages:
            nats_published = False
            if self.nats and self.nats.available:
                for msg in self._outbox_messages:
                    await self.nats.publish_human_outbound(msg)
                nats_published = True
            if not nats_published:
                outbox_path = Path(self.config.paths.outbox)
                outbox = Outbox(messages=self._outbox_messages)
                outbox_path.write_text(outbox.model_dump_json(indent=2))

        # Liveness check — last step, after all data is persisted
        transformed_nonce = await self._validate_liveness()

        # Build heartbeat response using reflection summary
        response = CycleResponse(
            cycle_number=self.state.cycle_number,
            nonce=transformed_nonce,
            started_at=self.state.started_at or datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            status="completed",
            cycle_summary=cycle_summary[:200],
            actions_taken=self.state.actions_taken,
            goals_active=len(self._active_goals) if hasattr(self, "_active_goals") else 0,
            self_modifications=self.state.self_modifications,
        )

        # Write response file
        response_path = Path(self.config.paths.response)
        response_path.parent.mkdir(parents=True, exist_ok=True)
        response_path.write_text(response.model_dump_json(indent=2))

        self.logger.log("persist_complete",
                        heartbeat_written=True,
                        outbox_messages=len(self._outbox_messages),
                        significance=significance)

        return response

    async def _validate_liveness(self: AgentCycle) -> str:
        """
        Dedicated liveness check: ask the LLM to transform the nonce.

        The LLM outputs nonce:cycle_number — a simple separator-based
        format that avoids character-level precision issues with hex strings.
        Retries once if the first attempt is a partial/truncated match.
        """
        challenge = self._challenge
        expected = f"{challenge.nonce}:{challenge.cycle_number}"

        for attempt in range(2):
            try:
                messages = self.assembler.assemble_liveness_prompt(
                    cycle_number=challenge.cycle_number,
                    nonce=challenge.nonce,
                )
                response = await self.llm.complete(
                    messages,
                    purpose="liveness",
                    temperature=0.0 if attempt > 0 else None,
                )
                raw = response.content.strip()

                # Best case: expected answer appears somewhere in the output
                if expected in raw:
                    self.logger.log("liveness_check", result="exact_match",
                                    attempt=attempt + 1)
                    return expected

                # Clean up noisy output
                cleaned = re.sub(r'<\|[^|]+\|>', '', raw)
                lines = [ln.strip() for ln in cleaned.strip().splitlines() if ln.strip()]
                if lines:
                    cleaned = lines[-1]
                cleaned = re.sub(r'^assistant(?:final|commentary|analysis)\s*', '', cleaned)
                cleaned = cleaned.strip("\"'`., \n\t")

                # If cleaned is a prefix of expected (truncated), retry
                if attempt == 0 and expected.startswith(cleaned) and cleaned != expected:
                    self.logger.log("liveness_check",
                                    result="truncated_retry",
                                    transformed_nonce=cleaned[:60],
                                    expected_prefix=expected[:20])
                    continue

                self.logger.log("liveness_check",
                                transformed_nonce=cleaned[:60],
                                expected_prefix=expected[:20],
                                attempt=attempt + 1)
                return cleaned
            except Exception as e:
                self.logger.log_error(f"Liveness check failed (attempt {attempt + 1}): {e}")
                if attempt == 0:
                    continue
                return self.state.nonce
        return self.state.nonce
