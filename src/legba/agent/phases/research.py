"""RESEARCH phase — entity enrichment via external sources."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ...shared.schemas.comms import InboxMessage

if TYPE_CHECKING:
    from ..cycle import AgentCycle


class ResearchMixin:
    """Research cycle: fill entity data gaps using external sources."""

    def _is_research_cycle(self: AgentCycle) -> bool:
        """Check if this cycle should be a research/enrichment cycle.

        Runs every RESEARCH_INTERVAL cycles, but yields to all higher-priority
        cycle types (EVOLVE, INTROSPECTION, SYNTHESIZE, ANALYSIS).
        """
        from . import RESEARCH_INTERVAL
        cn = self.state.cycle_number
        return (RESEARCH_INTERVAL > 0
                and cn > 0
                and cn % RESEARCH_INTERVAL == 0
                and not self._is_evolve_cycle()
                and not self._is_introspection_cycle()
                and not self._is_synthesize_cycle()
                and not self._is_analysis_cycle())

    async def _research(self: AgentCycle) -> None:
        """Research cycle: fill entity data gaps using external sources.

        Runs a REASON+ACT loop with a curated tool set that includes
        http_request for external research plus internal query/update tools.
        """
        self.logger.log_phase("research")

        # Build entity health summary for the research prompt
        entity_health = await self._build_entity_health_summary()

        # Get allowed tools from the template
        from ..prompt import templates as _tpl
        allowed_tools = _tpl.RESEARCH_TOOLS

        # Build research prompt
        inbox_messages = [InboxMessage(**m) for m in self.state.inbox_messages]
        research_messages = self.assembler.assemble_research_prompt(
            cycle_number=self.state.cycle_number,
            seed_goal=self.state.seed_goal,
            active_goals=[g.model_dump() for g in self._active_goals],
            entity_health=entity_health,
            allowed_tools=allowed_tools,
            inbox_messages=inbox_messages if inbox_messages else None,
        )

        # Create a filtered executor
        async def research_executor(tool_name: str, arguments: dict) -> str:
            if tool_name not in allowed_tools:
                return (f"Tool '{tool_name}' is not available during research cycles. "
                        f"Available tools: {', '.join(sorted(allowed_tools))}")
            return await self.executor.execute(tool_name, arguments)

        self._cycle_plan = "RESEARCH CYCLE: Entity enrichment, data gap filling, profile completion via external research."

        try:
            self._final_response, self._conversation = await self.llm.reason_with_tools(
                messages=research_messages,
                tool_executor=research_executor,
                purpose="research",
                max_steps=self.config.agent.max_reasoning_steps,
                stop_check=self._make_stop_checker(),
            )

            self.state.actions_taken = sum(
                1 for m in self._conversation
                if m.role == "user" and m.content.startswith("[Tool Result:")
            )

            self.logger.log("research_complete",
                            actions=self.state.actions_taken,
                            response_length=len(self._final_response))

        except Exception as e:
            self._final_response = f"Research cycle failed: {e}"
            self._conversation = []
            self.logger.log_error(f"Research cycle failed: {e}")

    async def _build_entity_health_summary(self: AgentCycle) -> str:
        """Build a summary of entity completeness for the research prompt."""
        context_parts = []

        # Check task backlog for goal-driven research targets
        try:
            _redis = self.memory.registers._redis if self.memory and self.memory.registers else None
            if _redis:
                from ...shared.task_backlog import TaskBacklog
                backlog = TaskBacklog(_redis)
                research_tasks = await backlog.get_tasks(cycle_type="RESEARCH", limit=3)
                if research_tasks:
                    goal_entities = []
                    for task in research_tasks:
                        target = task.get('target', {})
                        if target.get('entity_name'):
                            goal_entities.append(
                                f"**[GOAL-DRIVEN]** {target['entity_name']} — "
                                f"{task.get('context', '')}"
                            )
                        else:
                            goal_entities.append(
                                f"**[GOAL-DRIVEN]** {task.get('task_type', '?')} — "
                                f"{task.get('context', '')}"
                            )
                    if goal_entities:
                        context_parts.append(
                            "### Priority Research Targets (from active goals)\n"
                            + "\n".join(f"- {e}" for e in goal_entities)
                        )
        except Exception:
            pass

        try:
            async with self.memory.structured._pool.acquire() as conn:
                # Get entities with lowest completeness that appear in signals
                rows = await conn.fetch("""
                    SELECT ep.canonical_name, ep.entity_type,
                           ep.completeness_score,
                           COUNT(eel.event_id) as event_count
                    FROM entity_profiles ep
                    LEFT JOIN signal_entity_links eel ON eel.entity_id = ep.id
                    GROUP BY ep.id, ep.canonical_name, ep.entity_type, ep.completeness_score
                    ORDER BY
                        COUNT(eel.event_id) DESC,
                        ep.completeness_score ASC
                    LIMIT 30
                """)

                if not rows:
                    prefix = "\n\n".join(context_parts) + "\n\n" if context_parts else ""
                    return prefix + "(No entity profiles found)"

                # Summary stats
                stats_row = await conn.fetchrow("""
                    SELECT count(*) as total,
                           round(avg(completeness_score)::numeric, 2) as avg_complete,
                           count(*) FILTER (WHERE completeness_score < 0.3) as low
                    FROM entity_profiles
                """)

                lines = []
                total = stats_row["total"]
                avg_c = stats_row["avg_complete"]
                low = stats_row["low"]
                lines.append(f"**{total} entities total**, average completeness **{avg_c}**, **{low} below 30%**\n")
                lines.append("Top entities by signal involvement (lowest completeness first within each tier):")
                lines.append("| Entity | Type | Completeness | Signals |")
                lines.append("|--------|------|-------------|--------|")
                for r in rows:
                    lines.append(f"| {r['canonical_name']} | {r['entity_type']} | {r['completeness_score']:.0%} | {r['event_count']} |")

                prefix = "\n\n".join(context_parts) + "\n\n" if context_parts else ""
                return prefix + "\n".join(lines)

        except Exception as e:
            prefix = "\n\n".join(context_parts) + "\n\n" if context_parts else ""
            return prefix + f"(Could not load entity health: {e})"

    def _parse_json_with_key(self: AgentCycle, text: str, required_key: str) -> dict:
        """Extract first JSON object from text that contains the required key."""
        pos = 0
        while pos < len(text):
            start = text.find("{", pos)
            if start < 0:
                break
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            candidate = json.loads(text[start:i + 1])
                            if isinstance(candidate, dict) and required_key in candidate:
                                return candidate
                        except (json.JSONDecodeError, ValueError):
                            pass
                        pos = i + 1
                        break
            else:
                break
        return {}
