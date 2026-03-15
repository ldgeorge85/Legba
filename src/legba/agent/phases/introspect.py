"""INTROSPECTION phase — deep self-assessment, analysis reports."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from ...shared.schemas.comms import InboxMessage, OutboxMessage

if TYPE_CHECKING:
    from ..cycle import AgentCycle


# Tools allowed during introspection cycles (internal queries + graph building)
INTROSPECTION_TOOLS: frozenset[str] = frozenset({
    "graph_query", "graph_store", "graph_analyze",
    "memory_query", "memory_store", "memory_promote", "memory_supersede",
    "entity_inspect", "entity_profile",
    "os_search",
    "note_to_self", "explain_tool",
    "goal_update", "goal_create",
    "cycle_complete",
})


class IntrospectMixin:
    """Introspection cycle: deep review of knowledge base."""

    _REPORT_INDEX = "legba-reports"

    def _is_introspection_cycle(self: AgentCycle) -> bool:
        """Check if this cycle should be a deep introspection instead of normal ops."""
        interval = self.config.agent.mission_review_interval
        return interval > 0 and self.state.cycle_number % interval == 0

    async def _mission_review(self: AgentCycle) -> None:
        """Deep introspection cycle.

        Runs a full REASON+ACT loop but restricts tools to internal queries
        only. The agent surveys its knowledge base, finds gaps, discovers
        cross-domain connections, and strengthens the graph — then feeds
        its findings back into the next normal cycles.
        """
        self.logger.log_phase("mission_review")

        # Gather performance data
        recent_work_pattern = "unknown"
        try:
            rf = await self.memory.registers.get_json("reflection_forward")
            if rf:
                recent_work_pattern = rf.get("recent_work_pattern", "unknown")
        except Exception:
            pass

        # Load deferred goals
        deferred_goals_data = []
        try:
            deferred_goals = await self.goals.get_deferred_goals(self.state.cycle_number)
            deferred_goals_data = [g.model_dump() for g in deferred_goals]
        except Exception:
            pass

        # Build introspection prompt with tool definitions for allowed tools only
        inbox_messages = [InboxMessage(**m) for m in self.state.inbox_messages]
        review_messages = self.assembler.assemble_introspection_prompt(
            cycle_number=self.state.cycle_number,
            seed_goal=self.state.seed_goal,
            active_goals=[g.model_dump() for g in self._active_goals],
            deferred_goals=deferred_goals_data,
            recent_work_pattern=recent_work_pattern,
            allowed_tools=INTROSPECTION_TOOLS,
            inbox_messages=inbox_messages if inbox_messages else None,
        )

        # Create a filtered executor that only allows introspection tools
        async def introspection_executor(tool_name: str, arguments: dict) -> str:
            if tool_name not in INTROSPECTION_TOOLS:
                return (f"Tool '{tool_name}' is not available during introspection. "
                        f"This is an internal review cycle — only query and analysis tools are available: "
                        f"{', '.join(sorted(INTROSPECTION_TOOLS))}")
            return await self.executor.execute(tool_name, arguments)

        # Set cycle plan so reflect phase knows this was introspection
        self._cycle_plan = "INTROSPECTION CYCLE: Deep review of knowledge base, graph audit, cross-domain analysis, gap identification."

        try:
            self._final_response, self._conversation = await self.llm.reason_with_tools(
                messages=review_messages,
                tool_executor=introspection_executor,
                purpose="introspection",
                max_steps=self.config.agent.max_reasoning_steps,
                stop_check=self._make_stop_checker(),
            )

            # Count introspection actions
            self.state.actions_taken = sum(
                1 for m in self._conversation
                if m.role == "user" and m.content.startswith("[Tool Result:")
            )

            # Extract key findings from the final response and working memory
            parts = ["## Introspection Findings (cycle {})".format(self.state.cycle_number)]
            wm_summary = self.llm.working_memory.summary()
            if wm_summary:
                parts.append(wm_summary)
            elif self._final_response:
                parts.append(self._final_response[:1000])

            self._reflection_forward = "\n".join(parts) + "\n\n" + self._reflection_forward

            self.logger.log("mission_review_complete",
                            mode="introspection",
                            actions=self.state.actions_taken,
                            response_length=len(self._final_response))

        except Exception as e:
            self._final_response = f"Introspection failed: {e}"
            self._conversation = []
            self.logger.log_error(f"Introspection cycle failed: {e}")

    async def _generate_analysis_report(self: AgentCycle) -> None:
        """Generate a full Current World Assessment (introspection only).

        Queries actual data from all stores to ground the report in facts,
        preventing the LLM from hallucinating leaders, events, or programs.
        """
        self.logger.log_phase("analysis_report")
        try:
            # Gather context for the report
            graph_summary = self._graph_inventory or "(no graph data)"

            # --- Key relationships from graph ---
            key_relationships = ""
            try:
                if self.memory.graph and self.memory.graph.available:
                    rel_lines = []
                    for rel_type in ["LeaderOf", "HostileTo", "AlliedWith", "SuppliesWeaponsTo",
                                     "SanctionedBy", "MemberOf", "OperatesIn", "OccupiedBy",
                                     "SignatoryTo", "TradesWith"]:
                        rels = await self.memory.graph.execute_cypher(
                            f"MATCH (a)-[r:{rel_type}]->(b) "
                            f"RETURN a.name AS from, b.name AS to"
                        )
                        if rels:
                            for r in rels:
                                fn = str(r.get("from", "?")).strip('"')
                                tn = str(r.get("to", "?")).strip('"')
                                rel_lines.append(f"- {fn} --[{rel_type}]--> {tn}")
                    key_relationships = "\n".join(rel_lines) if rel_lines else "(no relationships found)"
            except Exception:
                key_relationships = "(could not query relationships)"

            # --- Entity profiles with summaries ---
            entity_profiles_text = ""
            entity_count = 0
            try:
                if self.memory.graph and self.memory.graph.available:
                    result = await self.memory.graph.execute_cypher(
                        "MATCH (n) RETURN count(n) AS cnt"
                    )
                    if result:
                        entity_count = result[0].get("cnt", 0)
            except Exception:
                pass

            try:
                from ...shared.config import PostgresConfig
                import asyncpg
                pg = PostgresConfig.from_env()
                conn = await asyncpg.connect(
                    host=pg.host, port=pg.port, user=pg.user,
                    password=pg.password, database=pg.database,
                )
                rows = await conn.fetch(
                    "SELECT data->>'name' AS name, data->>'type' AS type, "
                    "data->>'summary' AS summary "
                    "FROM entity_profiles "
                    "WHERE data->>'summary' IS NOT NULL AND data->>'summary' != '' "
                    "ORDER BY updated_at DESC LIMIT 60"
                )
                await conn.close()
                if rows:
                    ep_lines = []
                    for r in rows:
                        ep_lines.append(f"- [{r['type']}] {r['name']}: {r['summary']}")
                    entity_profiles_text = "\n".join(ep_lines)
            except Exception:
                pass
            if not entity_profiles_text:
                entity_profiles_text = f"({entity_count} entities in graph, but no detailed profiles with summaries available)"

            # --- Get previous report for evolution/comparison ---
            last_report = await self.memory.registers.get_json("latest_report") or {}
            last_report_content = last_report.get("content", "")
            last_report_cycle = last_report.get("cycle", 0)
            # Extract executive summary (first ~1500 chars after "## Executive Summary")
            previous_assessment = ""
            if last_report_content:
                idx = last_report_content.find("## Executive Summary")
                if idx >= 0:
                    end_idx = last_report_content.find("\n## ", idx + 20)
                    previous_assessment = last_report_content[idx:end_idx if end_idx > 0 else idx + 1500].strip()
                if not previous_assessment:
                    previous_assessment = last_report_content[:1500]

            # --- Recent events with full detail ---
            recent_events = ""
            try:
                if self.opensearch:
                    events = await self.opensearch.search(
                        index="legba-events",
                        query={"match_all": {}},
                        size=50,
                        sort=[{"timestamp": "desc"}],
                    )
                    if events:
                        lines = []
                        if last_report_cycle > 0:
                            lines.append(f"(Events since last report at cycle {last_report_cycle})")
                        for ev in events:
                            src = ev.get("_source", ev)
                            title = src.get("title", "untitled")
                            cat = src.get("category", "?")
                            ts = src.get("event_date", src.get("timestamp", "?"))
                            summary = src.get("summary", "")
                            actors = src.get("actors", [])
                            locations = src.get("locations", [])
                            actor_str = f" | Actors: {', '.join(actors)}" if actors else ""
                            loc_str = f" | Location: {', '.join(locations)}" if locations else ""
                            line = f"- [{cat}] {title} ({ts}){actor_str}{loc_str}"
                            if summary:
                                line += f"\n  Summary: {summary[:200]}"
                            lines.append(line)
                        recent_events = "\n".join(lines)
            except Exception:
                pass

            # Fallback: get events from Postgres JSONB
            if not recent_events:
                try:
                    from ...shared.config import PostgresConfig
                    import asyncpg
                    pg = PostgresConfig.from_env()
                    conn = await asyncpg.connect(
                        host=pg.host, port=pg.port, user=pg.user,
                        password=pg.password, database=pg.database,
                    )
                    rows = await conn.fetch(
                        "SELECT data->>'title' AS title, data->>'category' AS category, "
                        "data->>'event_date' AS event_date, data->>'summary' AS summary, "
                        "data->>'actors' AS actors, data->>'locations' AS locations "
                        "FROM events ORDER BY created_at DESC LIMIT 50"
                    )
                    await conn.close()
                    lines = []
                    for r in rows:
                        actors = r['actors'] or ""
                        locations = r['locations'] or ""
                        line = f"- [{r['category']}] {r['title']} ({r['event_date']})"
                        if actors:
                            line += f" | Actors: {actors}"
                        if locations:
                            line += f" | Location: {locations}"
                        if r['summary']:
                            line += f"\n  Summary: {r['summary'][:200]}"
                        lines.append(line)
                    recent_events = "\n".join(lines)
                except Exception:
                    recent_events = "(could not retrieve events)"

            # Coverage regions from graph
            coverage_regions = ""
            try:
                if self.memory.graph and self.memory.graph.available:
                    regions = await self.memory.graph.execute_cypher(
                        "MATCH (n:Country) RETURN n.name AS name ORDER BY name"
                    )
                    if regions:
                        names = [str(r.get("name", "?")).strip('"') for r in regions]
                        coverage_regions = ", ".join(names)
            except Exception:
                pass

            # Get current narrative (for voice/continuity only)
            journal_data = await self.memory.registers.get_json(self._JOURNAL_KEY) or {}
            narrative = journal_data.get("consolidation", "")

            # Append previous assessment for report evolution/comparison
            narrative_with_prev = narrative
            if previous_assessment:
                narrative_with_prev += f"\n\n## YOUR PREVIOUS ASSESSMENT (cycle {last_report_cycle})\n{previous_assessment}\n\nCRITICAL: Focus on what has CHANGED since cycle {last_report_cycle}. Do not repeat the same analysis."

            report_messages = self.assembler.assemble_analysis_report_prompt(
                cycle_number=self.state.cycle_number,
                graph_summary=graph_summary,
                key_relationships=key_relationships,
                entity_profiles=entity_profiles_text,
                recent_events=recent_events,
                entity_count=entity_count,
                coverage_regions=coverage_regions,
                narrative=narrative_with_prev,
            )

            response = await self.llm.complete(
                report_messages,
                purpose="analysis_report",
            )

            report_content = response.content.strip()
            for token in ["<|end|>", "<|return|>"]:
                report_content = report_content.replace(token, "")

            # Store report in Redis
            report_data = {
                "cycle": self.state.cycle_number,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "content": report_content,
            }
            await self.memory.registers.set_json("latest_report", report_data)

            # Append to report history
            report_history = await self.memory.registers.get_json("report_history") or []
            report_history.append(report_data)
            # Keep last 20 reports
            if len(report_history) > 20:
                report_history = report_history[-20:]
            await self.memory.registers.set_json("report_history", report_history)

            # Archive to OpenSearch for permanent storage and search
            await self._archive_report_to_opensearch(
                cycle=self.state.cycle_number,
                timestamp=report_data["timestamp"],
                content=report_content,
                report_type="world_assessment",
            )

            # Also publish to outbound so it shows in messages
            self._outbox_messages.append(OutboxMessage(
                id=str(uuid4()),
                content=f"[ANALYSIS REPORT — Cycle {self.state.cycle_number}]\n\n{report_content}",
                cycle_number=self.state.cycle_number,
                metadata={"type": "analysis_report"},
            ))

            self.logger.log("analysis_report_complete",
                            report_length=len(report_content))

        except Exception as e:
            self.logger.log_error(f"Analysis report generation failed: {e}")

    async def _archive_report_to_opensearch(self: AgentCycle, *, cycle: int,
                                             timestamp: str, content: str,
                                             report_type: str) -> None:
        """Archive a report document to OpenSearch for permanent storage."""
        if not self.opensearch or not self.opensearch.available:
            return
        try:
            doc = {
                "type": report_type,
                "cycle_number": cycle,
                "timestamp": timestamp,
                "content": content,
            }
            await self.opensearch.index_document(
                index=self._REPORT_INDEX,
                document=doc,
                doc_id=f"{report_type}-{cycle}",
            )
        except Exception:
            pass  # Best-effort — don't break the cycle if archiving fails
