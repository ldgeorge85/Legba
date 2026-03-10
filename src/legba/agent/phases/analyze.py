"""ANALYZE phase — analytical tools, pattern detection, metrics."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ...shared.schemas.comms import InboxMessage

if TYPE_CHECKING:
    from ..cycle import AgentCycle


class AnalyzeMixin:
    """Analysis cycle: pattern detection, anomaly detection, graph mining."""

    def _is_analysis_cycle(self: AgentCycle) -> bool:
        """Check if this is an analysis cycle.

        Runs every ANALYSIS_INTERVAL cycles, but NOT on introspection cycles.
        """
        from . import ANALYSIS_INTERVAL
        cn = self.state.cycle_number
        return (ANALYSIS_INTERVAL > 0
                and cn > 0
                and cn % ANALYSIS_INTERVAL == 0
                and not self._is_introspection_cycle())

    async def _analyze(self: AgentCycle) -> None:
        """Analysis cycle: run analytical tools on accumulated data."""
        self.logger.log_phase("analyze")

        analysis_context = await self._build_analysis_context()

        from ..prompt import templates as _tpl
        allowed_tools = _tpl.ANALYSIS_TOOLS

        inbox_messages = [InboxMessage(**m) for m in self.state.inbox_messages]
        analysis_messages = self.assembler.assemble_analysis_cycle_prompt(
            cycle_number=self.state.cycle_number,
            seed_goal=self.state.seed_goal,
            active_goals=[g.model_dump() for g in self._active_goals],
            analysis_context=analysis_context,
            allowed_tools=allowed_tools,
            inbox_messages=inbox_messages if inbox_messages else None,
        )

        async def analysis_executor(tool_name: str, arguments: dict) -> str:
            if tool_name not in allowed_tools:
                return (f"Tool '{tool_name}' is not available during analysis cycles. "
                        f"Available tools: {', '.join(sorted(allowed_tools))}")
            return await self.executor.execute(tool_name, arguments)

        self._cycle_plan = "ANALYSIS CYCLE: Pattern detection, graph mining, anomaly detection, trend analysis."

        try:
            self._final_response, self._conversation = await self.llm.reason_with_tools(
                messages=analysis_messages,
                tool_executor=analysis_executor,
                purpose="analysis",
                max_steps=self.config.agent.max_reasoning_steps,
                stop_check=self._make_stop_checker(),
            )

            self.state.actions_taken = sum(
                1 for m in self._conversation
                if m.role == "user" and m.content.startswith("[Tool Result:")
            )

            self.logger.log("analysis_complete",
                           actions=self.state.actions_taken,
                           response_length=len(self._final_response))
        except Exception as e:
            self._final_response = f"Analysis cycle failed: {e}"
            self._conversation = []
            self.logger.log_error(f"Analysis cycle failed: {e}")

    async def _build_analysis_context(self: AgentCycle) -> str:
        """Build context data for the analysis prompt."""
        lines = []
        try:
            async with self.memory.structured._pool.acquire() as conn:
                # Event counts by category (last 50 events)
                cat_rows = await conn.fetch("""
                    SELECT category, count(*) as cnt
                    FROM events
                    ORDER BY cnt DESC
                """)
                if cat_rows:
                    lines.append("### Event Distribution by Category")
                    for r in cat_rows:
                        lines.append(f"- {r['category']}: {r['cnt']}")
                    lines.append("")

                # Top entities by event involvement
                entity_rows = await conn.fetch("""
                    SELECT ep.canonical_name, ep.entity_type,
                           COUNT(eel.event_id) as event_count
                    FROM entity_profiles ep
                    LEFT JOIN event_entity_links eel ON eel.entity_id = ep.id
                    GROUP BY ep.id, ep.canonical_name, ep.entity_type
                    HAVING COUNT(eel.event_id) > 0
                    ORDER BY COUNT(eel.event_id) DESC
                    LIMIT 20
                """)
                if entity_rows:
                    lines.append("### Top Entities by Event Involvement")
                    for r in entity_rows:
                        lines.append(f"- {r['canonical_name']} ({r['entity_type']}): {r['event_count']} events")
                    lines.append("")

                # Total counts for threshold checking
                total_events = await conn.fetchval("SELECT count(*) FROM events")
                total_entities = await conn.fetchval("SELECT count(*) FROM entity_profiles")
                total_rels = 0
                try:
                    rel_row = await conn.fetchrow(
                        "SELECT * FROM cypher('legba_graph', $$ MATCH ()-[r]->() RETURN count(r) as cnt $$) as (cnt agtype)"
                    )
                    if rel_row:
                        import re
                        total_rels = int(re.sub(r'[^0-9]', '', str(rel_row['cnt'])))
                except Exception:
                    pass

                lines.append(f"### Data Thresholds")
                lines.append(f"- Total events: {total_events} {'(enough for anomaly_detect)' if total_events >= 30 else '(need 30+ for anomaly_detect)'}")
                lines.append(f"- Total entities: {total_entities}")
                lines.append(f"- Total relationships: {total_rels} {'(enough for graph_analyze)' if total_rels >= 20 else '(need 20+ for graph_analyze)'}")

                # Active situations if they exist
                try:
                    sit_rows = await conn.fetch(
                        "SELECT name, status, event_count, intensity_score FROM situations WHERE status != 'resolved' ORDER BY intensity_score DESC LIMIT 10"
                    )
                    if sit_rows:
                        lines.append("\n### Active Situations")
                        for r in sit_rows:
                            lines.append(f"- {r['name']} [{r['status']}] — {r['event_count']} events, intensity {r['intensity_score']:.2f}")
                except Exception:
                    pass  # Table may not exist yet

        except Exception as e:
            lines.append(f"(Could not load analysis context: {e})")

        return "\n".join(lines)
