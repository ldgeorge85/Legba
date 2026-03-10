"""ACQUIRE phase — dedicated data ingestion from sources."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ...shared.schemas.comms import InboxMessage

if TYPE_CHECKING:
    from ..cycle import AgentCycle


class AcquireMixin:
    """Acquire cycle: dedicated source fetching and event ingestion."""

    def _is_acquire_cycle(self: AgentCycle) -> bool:
        """Check if this is an acquire cycle.

        Runs every ACQUIRE_INTERVAL cycles, but NOT on introspection,
        research, or analysis cycles (they take priority).
        """
        from . import ACQUIRE_INTERVAL
        cn = self.state.cycle_number
        return (ACQUIRE_INTERVAL > 0
                and cn > 0
                and cn % ACQUIRE_INTERVAL == 0
                and not self._is_introspection_cycle()
                and not self._is_analysis_cycle()
                and not self._is_research_cycle())

    async def _acquire(self: AgentCycle) -> None:
        """Acquire cycle: fetch feeds, store events, resolve entities.

        Uses a filtered tool set focused on data ingestion. No graph
        enrichment, no deep research — just get data in.
        """
        self.logger.log_phase("acquire")

        # Build source status for the acquire prompt
        source_status = await self._build_source_status()

        from ..prompt import templates as _tpl
        allowed_tools = _tpl.ACQUIRE_TOOLS

        inbox_messages = [InboxMessage(**m) for m in self.state.inbox_messages]
        acquire_messages = self.assembler.assemble_acquire_prompt(
            cycle_number=self.state.cycle_number,
            seed_goal=self.state.seed_goal,
            active_goals=[g.model_dump() for g in self._active_goals],
            source_status=source_status,
            allowed_tools=allowed_tools,
            inbox_messages=inbox_messages if inbox_messages else None,
        )

        async def acquire_executor(tool_name: str, arguments: dict) -> str:
            if tool_name not in allowed_tools:
                return (f"Tool '{tool_name}' is not available during acquire cycles. "
                        f"Available tools: {', '.join(sorted(allowed_tools))}")
            return await self.executor.execute(tool_name, arguments)

        self._cycle_plan = "ACQUIRE CYCLE: Source fetching, event ingestion, entity resolution."

        try:
            self._final_response, self._conversation = await self.llm.reason_with_tools(
                messages=acquire_messages,
                tool_executor=acquire_executor,
                purpose="acquire",
                max_steps=self.config.agent.max_reasoning_steps,
                stop_check=self._make_stop_checker(),
            )

            self.state.actions_taken = sum(
                1 for m in self._conversation
                if m.role == "user" and m.content.startswith("[Tool Result:")
            )

            self.logger.log("acquire_complete",
                           actions=self.state.actions_taken,
                           response_length=len(self._final_response))
        except Exception as e:
            self._final_response = f"Acquire cycle failed: {e}"
            self._conversation = []
            self.logger.log_error(f"Acquire cycle failed: {e}")

    async def _build_source_status(self: AgentCycle) -> str:
        """Build source status summary for the acquire prompt."""
        try:
            async with self.memory.structured._pool.acquire() as conn:
                # Get sources ordered by: never fetched first, then least recently fetched
                rows = await conn.fetch("""
                    SELECT s.id, s.name, s.url, s.source_type, s.status,
                           s.data->>'last_fetched_at' as last_fetched,
                           s.data->>'events_produced_count' as event_count,
                           s.data->>'consecutive_failures' as failures
                    FROM sources s
                    WHERE s.status = 'active'
                    ORDER BY
                        CASE WHEN s.data->>'last_fetched_at' IS NULL THEN 0 ELSE 1 END,
                        s.data->>'last_fetched_at' ASC NULLS FIRST
                    LIMIT 40
                """)

                if not rows:
                    return "(No active sources)"

                never_fetched = [r for r in rows if not r['last_fetched']]
                stale = [r for r in rows if r['last_fetched'] and r not in never_fetched]

                lines = []
                lines.append(f"**{len(rows)} active sources**, **{len(never_fetched)} never fetched**\n")

                if never_fetched:
                    lines.append("### PRIORITY: Never-fetched sources (fetch these first!)")
                    for r in never_fetched[:10]:
                        lines.append(f"- **{r['name']}** ({r['source_type']}) — `{r['url'][:80]}`")
                    lines.append("")

                if stale[:10]:
                    lines.append("### Stale sources (least recently fetched)")
                    for r in stale[:10]:
                        events = r['event_count'] or '0'
                        lines.append(f"- {r['name']} — {events} events, last fetched: {r['last_fetched'] or 'never'}")

                return "\n".join(lines)
        except Exception as e:
            return f"(Could not load source status: {e})"
