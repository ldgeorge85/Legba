"""EVOLVE phase — operational self-assessment and self-improvement."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ...shared.schemas.comms import InboxMessage

if TYPE_CHECKING:
    from ..cycle import AgentCycle

_EVOLVE_LOG_KEY = "evolve_log"


class EvolveMixin:
    """Evolve cycle: self-assessment, prompt/tool evaluation, self-improvement."""

    def _is_evolve_cycle(self: AgentCycle) -> bool:
        """Check if this is an evolve cycle.

        Runs every EVOLVE_INTERVAL cycles. Takes priority over all other
        cycle types — the agent's ability to improve itself is foundational.
        """
        from . import EVOLVE_INTERVAL
        cn = self.state.cycle_number
        return (EVOLVE_INTERVAL > 0
                and cn > 0
                and cn % EVOLVE_INTERVAL == 0)

    async def _evolve(self: AgentCycle) -> None:
        """Evolve cycle: assess operational effectiveness, improve prompts/tools.

        Unlike introspection (which audits the knowledge base), evolve audits
        the agent itself — are my prompts working? Are my tools effective?
        Am I getting better at my job?
        """
        self.logger.log_phase("evolve")

        evolve_context = await self._build_evolve_context()

        from ..prompt import templates as _tpl
        allowed_tools = _tpl.EVOLVE_TOOLS

        inbox_messages = [InboxMessage(**m) for m in self.state.inbox_messages]
        evolve_messages = self.assembler.assemble_evolve_prompt(
            cycle_number=self.state.cycle_number,
            seed_goal=self.state.seed_goal,
            active_goals=[g.model_dump() for g in self._active_goals],
            evolve_context=evolve_context,
            allowed_tools=allowed_tools,
            inbox_messages=inbox_messages if inbox_messages else None,
        )

        async def evolve_executor(tool_name: str, arguments: dict) -> str:
            if tool_name not in allowed_tools:
                return (f"Tool '{tool_name}' is not available during evolve cycles. "
                        f"Available tools: {', '.join(sorted(allowed_tools))}")
            return await self.executor.execute(tool_name, arguments)

        self._cycle_plan = "EVOLVE CYCLE: Operational self-assessment, prompt/tool evaluation, self-improvement."

        try:
            self._final_response, self._conversation = await self.llm.reason_with_tools(
                messages=evolve_messages,
                tool_executor=evolve_executor,
                purpose="evolve",
                max_steps=self.config.agent.max_reasoning_steps,
                stop_check=self._make_stop_checker(),
            )

            self.state.actions_taken = sum(
                1 for m in self._conversation
                if m.role == "user" and m.content.startswith("[Tool Result:")
            )

            # Store evolve log for tracking changes across evolve cycles
            await self._store_evolve_log()

            self.logger.log("evolve_complete",
                           actions=self.state.actions_taken,
                           response_length=len(self._final_response))
        except Exception as e:
            self._final_response = f"Evolve cycle failed: {e}"
            self._conversation = []
            self.logger.log_error(f"Evolve cycle failed: {e}")

    async def _build_evolve_context(self: AgentCycle) -> str:
        """Build self-assessment context for the evolve prompt."""
        lines = []

        # --- 1. Recent reports: what problems did they flag? ---
        try:
            reports = await self.memory.registers.get_json("report_history") or []
            if reports:
                recent = reports[-2:]  # Last 2 reports
                lines.append("### Recent Report Self-Assessment")
                for r in recent:
                    cycle = r.get("cycle", "?")
                    content = r.get("content", "")
                    # Extract coverage assessment and gaps sections
                    for section_header in ["## Coverage Assessment", "## Watch Items"]:
                        idx = content.find(section_header)
                        if idx >= 0:
                            # Grab up to next ## or 500 chars
                            end = content.find("\n## ", idx + len(section_header))
                            snippet = content[idx:end if end > 0 else idx + 500].strip()
                            lines.append(f"\n**Cycle {cycle} — {section_header.strip('# ')}:**")
                            lines.append(snippet[:500])
                lines.append("")
        except Exception:
            lines.append("(Could not load recent reports)")

        # --- 2. Recent reflections: recurring themes in self-assessment ---
        try:
            rf = await self.memory.registers.get_json("reflection_forward")
            if rf:
                pattern = rf.get("recent_work_pattern", "")
                if pattern:
                    lines.append("### Recent Work Pattern")
                    lines.append(pattern[:300])
                    lines.append("")
        except Exception:
            pass

        # --- 3. Source utilization ---
        try:
            async with self.memory.structured._pool.acquire() as conn:
                total_sources = await conn.fetchval(
                    "SELECT count(*) FROM sources WHERE status = 'active'"
                )
                fetched_sources = await conn.fetchval(
                    "SELECT count(*) FROM sources WHERE status = 'active' AND data->>'last_fetched_at' IS NOT NULL"
                )
                lines.append("### Source Utilization")
                lines.append(f"- Active sources: {total_sources}")
                lines.append(f"- Ever fetched: {fetched_sources}")
                pct = (fetched_sources / total_sources * 100) if total_sources > 0 else 0
                lines.append(f"- Utilization: {pct:.0f}%")

                # Sources not fetched in 30+ cycles
                stale_rows = await conn.fetch("""
                    SELECT name, data->>'last_fetched_at' as last_fetched
                    FROM sources
                    WHERE status = 'active'
                    AND (data->>'last_fetched_at' IS NULL
                         OR data->>'last_fetched_at' < NOW() - INTERVAL '7 days')
                    ORDER BY data->>'last_fetched_at' ASC NULLS FIRST
                    LIMIT 10
                """)
                if stale_rows:
                    lines.append(f"- Stale/unfetched sources: {len(stale_rows)}+")
                    for r in stale_rows[:5]:
                        lines.append(f"  - {r['name']}: last fetched {r['last_fetched'] or 'never'}")
                lines.append("")
        except Exception as e:
            lines.append(f"(Source stats unavailable: {e})")

        # --- 4. Entity freshness ---
        try:
            async with self.memory.structured._pool.acquire() as conn:
                total_entities = await conn.fetchval("SELECT count(*) FROM entity_profiles")
                # Entities with old or missing updated_at
                stale_entities = await conn.fetchval("""
                    SELECT count(*) FROM entity_profiles
                    WHERE updated_at < NOW() - INTERVAL '14 days'
                       OR updated_at IS NULL
                """)
                lines.append("### Entity Freshness")
                lines.append(f"- Total entities: {total_entities}")
                lines.append(f"- Stale (>14 days since update): {stale_entities}")

                # Leaders specifically — check for LeaderOf assertions
                leader_rows = await conn.fetch("""
                    SELECT ep.canonical_name, ep.updated_at
                    FROM entity_profiles ep
                    WHERE ep.entity_type = 'person'
                    AND ep.updated_at < NOW() - INTERVAL '14 days'
                    ORDER BY ep.updated_at ASC
                    LIMIT 10
                """)
                if leader_rows:
                    lines.append(f"- Stale person profiles (may have outdated leader info):")
                    for r in leader_rows[:5]:
                        lines.append(f"  - {r['canonical_name']}: updated {r['updated_at']}")
                lines.append("")
        except Exception as e:
            lines.append(f"(Entity stats unavailable: {e})")

        # --- 5. Coverage breadth ---
        try:
            async with self.memory.structured._pool.acquire() as conn:
                region_rows = await conn.fetch("""
                    SELECT DISTINCT jsonb_array_elements_text(
                        COALESCE(data->'locations', '[]'::jsonb)
                    ) as location
                    FROM signals
                    WHERE created_at > NOW() - INTERVAL '7 days'
                """)
                regions = [r['location'] for r in region_rows] if region_rows else []
                lines.append("### Coverage Breadth (last 7 days)")
                lines.append(f"- Distinct locations in recent events: {len(regions)}")
                if regions:
                    lines.append(f"- Locations: {', '.join(sorted(regions)[:20])}")
                lines.append("")
        except Exception as e:
            lines.append(f"(Coverage stats unavailable: {e})")

        # --- 6. Previous evolve log ---
        try:
            prev_log = await self.memory.registers.get_json(_EVOLVE_LOG_KEY)
            if prev_log:
                lines.append("### Previous Evolve Cycle")
                lines.append(f"- Cycle: {prev_log.get('cycle', '?')}")
                changes = prev_log.get("changes", [])
                if changes:
                    lines.append(f"- Changes made ({len(changes)}):")
                    for c in changes[:5]:
                        lines.append(f"  - {c}")
                assessment = prev_log.get("self_assessment", "")
                if assessment:
                    lines.append(f"- Assessment: {assessment[:300]}")
                lines.append("")
        except Exception:
            lines.append("(No previous evolve log)")

        return "\n".join(lines)

    async def _store_evolve_log(self: AgentCycle) -> None:
        """Store a structured log of what this evolve cycle did."""
        try:
            if not self.memory or not self.memory.registers:
                return

            # Extract changes from working memory / final response
            wm_summary = ""
            if self.llm and self.llm.working_memory:
                wm_summary = self.llm.working_memory.summary()

            log = {
                "cycle": self.state.cycle_number,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "actions_taken": self.state.actions_taken,
                "summary": self._final_response[:500] if self._final_response else "",
                "working_memory": wm_summary[:500] if wm_summary else "",
                "changes": [],  # Agent should use note_to_self to log specific changes
                "self_assessment": "",
            }

            # Try to extract structured info from notes
            if wm_summary:
                for line in wm_summary.split("\n"):
                    line = line.strip()
                    if line.startswith("- ") or line.startswith("* "):
                        log["changes"].append(line[2:])

            await self.memory.registers.set_json(_EVOLVE_LOG_KEY, log)
        except Exception:
            pass
