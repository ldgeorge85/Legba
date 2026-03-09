"""
Core Agent Cycle

WAKE → ORIENT → PLAN → REASON+ACT → REFLECT → NARRATE → PERSIST

One cycle = one execution of this module. The supervisor manages the lifecycle:
it launches the agent for a single cycle, the agent runs through all phases,
emits a heartbeat, and exits. Changes take effect next cycle.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..shared.config import LegbaConfig
from ..shared.schemas.cycle import Challenge, CycleResponse, CycleState
from ..shared.schemas.comms import Inbox, InboxMessage, Outbox, OutboxMessage, MessagePriority
from ..shared.schemas.memory import Episode, EpisodeType, Entity, Fact
from .llm.client import LLMClient
from .llm.format import Message
from .memory.manager import MemoryManager
from .goals.manager import GoalManager
from .tools.registry import ToolRegistry
from .tools.executor import ToolExecutor
from .tools.subagent import run_subagent
from .comms.nats_client import LegbaNatsClient
from .comms.airflow_client import AirflowClient
from .memory.opensearch import OpenSearchStore
from .prompt.assembler import PromptAssembler
from .selfmod.engine import SelfModEngine
from .log import CycleLogger


# Reporting cadence: produce a status report every N cycles.
REPORT_INTERVAL = 5


class AgentCycle:
    """
    Executes a single agent cycle.

    Wires together: LLM client, memory manager, goal manager, tool executor,
    prompt assembler, self-mod engine, and the cycle logger.
    """

    def __init__(self, config: LegbaConfig):
        self.config = config
        self.state = CycleState()
        self.logger: CycleLogger | None = None
        self.llm: LLMClient | None = None
        self.memory: MemoryManager | None = None
        self.goals: GoalManager | None = None
        self.registry: ToolRegistry | None = None
        self.executor: ToolExecutor | None = None
        self.selfmod: SelfModEngine | None = None
        self.assembler: PromptAssembler | None = None
        self.nats: LegbaNatsClient | None = None
        self.opensearch: OpenSearchStore | None = None
        self.airflow: AirflowClient | None = None
        self._outbox_messages: list[OutboxMessage] = []
        self._cycle_plan: str = ""
        self._planned_tools: set[str] | None = None
        self._reflection_data: dict = {}

    async def run(self) -> CycleResponse:
        """Execute the full cycle. Returns the heartbeat response."""
        self.state.started_at = datetime.now(timezone.utc)

        try:
            await self._wake()
            await self._orient()

            # Introspection cycles replace the normal PLAN→REASON→REFLECT flow
            if self._is_introspection_cycle():
                await self._mission_review()
                await self._reflect()
                await self._narrate()
                await self._journal_consolidation()
                await self._generate_analysis_report()
            else:
                await self._plan()
                await self._reason_and_act()
                await self._reflect()
                await self._narrate()

            return await self._persist()
        except Exception as e:
            if self.logger:
                self.logger.log_error(f"Cycle failed: {e}")
            return CycleResponse(
                cycle_number=self.state.cycle_number,
                nonce=self.state.nonce,
                started_at=self.state.started_at or datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
                status="error",
                cycle_summary=f"Cycle failed: {e}",
                error=str(e),
            )
        finally:
            await self._cleanup()

    # -----------------------------------------------------------------------
    # WAKE
    # -----------------------------------------------------------------------

    async def _wake(self) -> None:
        """
        Phase 1: Initialize everything.

        - Read supervisor challenge
        - Load seed goal
        - Connect to memory services
        - Set up LLM client, tools, etc.
        - Check inbox
        """
        # Initialize logger
        self.logger = CycleLogger(self.config.paths.logs, cycle_number=0)
        self.logger.log_phase("wake")

        # Clean up stale signal files from previous cycles
        self._cleanup_signals()

        # Clear within-cycle HTTP cache
        from .tools.builtins.http import clear_http_cache
        clear_http_cache()

        # Read challenge
        challenge_path = Path(self.config.paths.challenge)
        if challenge_path.exists():
            data = json.loads(challenge_path.read_text())
            challenge = Challenge(**data)
            self.state.cycle_number = challenge.cycle_number
            self.state.nonce = challenge.nonce
            self._challenge = challenge
        else:
            self.state.cycle_number = 0
            self.state.nonce = str(uuid4())
            self._challenge = Challenge(
                cycle_number=0,
                nonce=self.state.nonce,
            )

        # Update logger with real cycle number
        self.logger.update_cycle_number(self.state.cycle_number)
        self.logger.log("wake_challenge",
                        cycle_number=self.state.cycle_number,
                        nonce=self.state.nonce[:8])

        # Load seed goal
        seed_path = Path(self.config.paths.seed_goal)
        if seed_path.exists():
            self.state.seed_goal = seed_path.read_text().strip()
        else:
            self.state.seed_goal = "No seed goal configured."

        # Load world briefing (for bootstrap cycles)
        briefing_path = seed_path.parent / "world_briefing.txt"
        self._world_briefing = ""
        if briefing_path.exists():
            self._world_briefing = briefing_path.read_text().strip()

        # Connect to memory
        self.memory = MemoryManager(self.config, self.logger)
        await self.memory.connect()

        # Get/increment cycle number from registers
        stored_cycle = await self.memory.get_cycle_number()
        if self.state.cycle_number == 0:
            self.state.cycle_number = stored_cycle + 1
        await self.memory.registers.set("cycle_number", str(self.state.cycle_number))

        # Initialize LLM client
        self.llm = LLMClient(self.config.llm, self.logger)

        # Initialize goal manager
        self.goals = GoalManager(self.memory.structured, self.logger)

        # Initialize self-mod engine
        self.selfmod = SelfModEngine(self.config.paths.agent_code, self.logger)
        await self.selfmod.initialize()

        # Connect to external services BEFORE tool registration
        # (tool handlers capture references via closures — must not be None)

        # Connect to NATS
        self.nats = LegbaNatsClient(
            url=self.config.nats.url,
            connect_timeout=self.config.nats.connect_timeout,
        )
        await self.nats.connect()
        self.logger.log("nats_connect", available=self.nats.available)

        # Connect to OpenSearch
        self.opensearch = OpenSearchStore(self.config.opensearch)
        await self.opensearch.connect()
        self.logger.log("opensearch_connect", available=self.opensearch.available)

        # Connect to Airflow
        self.airflow = AirflowClient(self.config.airflow)

        # Initialize tool registry and executor (after all connections)
        self.registry = ToolRegistry(self.config.paths.agent_tools)
        self._register_builtin_tools()
        self.registry.load_dynamic_tools()

        self.executor = ToolExecutor(self.registry, self.logger)

        # Initialize prompt assembler
        self.assembler = PromptAssembler(
            tool_data=self.registry.to_tool_data(),
            tool_summary=self.registry.to_tool_summary(),
            bootstrap_threshold=self.config.agent.bootstrap_threshold,
            max_context_tokens=self.config.agent.max_context_tokens,
            report_interval=REPORT_INTERVAL,
            world_briefing=self._world_briefing,
            airflow_available=self.airflow.available if self.airflow else False,
        )
        await self.airflow.connect()
        self.logger.log("airflow_connect", available=self.airflow.available)

        # Check inbox — NATS first, file fallback
        nats_messages = await self.nats.drain_human_inbound()
        if nats_messages:
            self.state.inbox_messages = [m.model_dump() for m in nats_messages]
            self.logger.log("inbox_read", source="nats", count=len(nats_messages))
        else:
            inbox_path = Path(self.config.paths.inbox)
            if inbox_path.exists():
                try:
                    data = json.loads(inbox_path.read_text())
                    inbox = Inbox(**data)
                    self.state.inbox_messages = [m.model_dump() for m in inbox.messages]
                    inbox_path.write_text(Inbox().model_dump_json(indent=2))
                    if inbox.messages:
                        self.logger.log("inbox_read", source="file", count=len(inbox.messages))
                except Exception:
                    pass

        self.state.phase = "wake"

    # -----------------------------------------------------------------------
    # ORIENT
    # -----------------------------------------------------------------------

    async def _orient(self) -> None:
        """
        Phase 2: Gather context.

        - Retrieve relevant memories
        - Load active goals
        - Build working context

        Query uses active goal focus (not just static seed goal) for better
        memory retrieval relevance.
        """
        self.logger.log_phase("orient")
        self.state.phase = "orient"

        # Build query from seed goal + active goal focus for better retrieval
        query_text = self.state.seed_goal[:300]

        # Load active goals first so we can use them in the query
        self._active_goals = await self.goals.get_active_goals()

        # Enrich query with the highest-priority active goal
        if self._active_goals:
            top_goal = self._active_goals[0]
            goal_desc = top_goal.description if hasattr(top_goal, 'description') else str(top_goal)
            query_text += f" Current focus: {str(goal_desc)[:200]}"

        try:
            query_embedding = await self.llm.generate_embedding(query_text)
        except Exception:
            query_embedding = None

        # Retrieve context from all memory layers
        self._memory_context = await self.memory.retrieve_context(
            query_embedding=query_embedding,
            limit=self.config.agent.memory_retrieval_limit,
            current_cycle=self.state.cycle_number,
        )

        # Get NATS queue summary for context
        self._queue_summary = await self.nats.queue_summary()

        # Build knowledge graph summary — entity type counts, relationship
        # counts, and entity profile health for SA context.
        self._graph_inventory = ""
        try:
            if self.memory.graph and self.memory.graph.available:
                lines = ["## Knowledge Graph Summary", ""]

                # Query 1: Entity type counts
                type_counts = await self.memory.graph.execute_cypher(
                    "MATCH (n) "
                    "RETURN label(n) AS lbl, count(n) AS cnt "
                    "ORDER BY cnt DESC"
                )
                if type_counts:
                    parts = []
                    total = 0
                    for row in type_counts:
                        lbl = str(row.get("lbl", "?")).strip('"')
                        cnt = row.get("cnt", 0)
                        total += cnt
                        parts.append(f"{lbl} ({cnt})")
                    lines.append(f"**Graph entities ({total}):** {', '.join(parts)}")
                    lines.append("")

                # Query 2: Relationship count
                rel_counts = await self.memory.graph.execute_cypher(
                    "MATCH ()-[r]->() "
                    "RETURN type(r) AS rtype, count(r) AS cnt "
                    "ORDER BY cnt DESC LIMIT 15"
                )
                if rel_counts:
                    parts = []
                    for row in rel_counts:
                        rtype = str(row.get("rtype", "?")).strip('"')
                        cnt = row.get("cnt", 0)
                        parts.append(f"{rtype} ({cnt})")
                    lines.append(f"**Top relationships:** {', '.join(parts)}")
                    lines.append("")

                # Query 3: Unknown entities warning
                unknowns = await self.memory.graph.execute_cypher(
                    "MATCH (n:Unknown) RETURN n.name AS name LIMIT 20"
                )
                if unknowns:
                    unames = [str(r.get("name", "?")).strip('"') for r in unknowns]
                    lines.append(
                        f"**Warning: {len(unames)} Unknown-type entities "
                        f"need classification:** {', '.join(unames[:10])}"
                        + ("..." if len(unames) > 10 else "")
                    )
                    lines.append("")

                if not type_counts:
                    lines.append("Graph is empty. Build it by storing entities and relationships from events and source documents.")
                    lines.append("")

                self._graph_inventory = "\n".join(lines)
        except Exception as e:
            self.logger.log_error(f"Graph inventory query failed: {e}")

        # Source health stats (injected into graph inventory for planning)
        self._source_health = ""
        try:
            if self.memory and self.memory.structured and self.memory.structured._available:
                import asyncpg
                from ..shared.config import PostgresConfig
                pg = PostgresConfig.from_env()
                conn = await asyncpg.connect(
                    host=pg.host, port=pg.port, user=pg.user,
                    password=pg.password, database=pg.database,
                )
                total_sources = await conn.fetchval("SELECT COUNT(*) FROM sources")
                sources_with_events = await conn.fetchval(
                    "SELECT COUNT(DISTINCT source_id) FROM events WHERE source_id IS NOT NULL"
                )
                total_events = await conn.fetchval("SELECT COUNT(*) FROM events")
                await conn.close()

                utilization = (sources_with_events / total_sources * 100) if total_sources else 0
                health_line = (
                    f"\n## Source Health\n"
                    f"**{total_sources} sources registered**, "
                    f"**{sources_with_events} have produced events** "
                    f"({utilization:.0f}% utilization), "
                    f"**{total_events} total events**"
                )
                if utilization < 50:
                    health_line += (
                        f"\n**WARNING: Source utilization is very low.** "
                        f"Focus on parsing existing sources rather than adding new ones."
                    )
                self._source_health = health_line
                # Append to graph inventory so the planner sees it
                if self._graph_inventory:
                    self._graph_inventory += "\n" + self._source_health
                else:
                    self._graph_inventory = self._source_health
        except Exception:
            pass

        # Retrieve previous cycle's reflection-forward data
        self._reflection_forward = ""
        try:
            rf = await self.memory.registers.get_json("reflection_forward")
            if rf:
                parts = []
                if rf.get("self_assessment"):
                    parts.append(f"**Self-assessment:** {rf['self_assessment']}")
                if rf.get("next_cycle_suggestion"):
                    parts.append(f"**Suggested next action:** {rf['next_cycle_suggestion']}")
                if rf.get("recent_work_pattern"):
                    parts.append(f"**Recent work pattern:** {rf['recent_work_pattern']}")
                if rf.get("stale_goal_count", 0) > 0:
                    parts.append(f"**Stale goals (no progress >1hr):** {rf['stale_goal_count']}")
                if parts:
                    self._reflection_forward = "## Previous Cycle Reflection\n" + "\n".join(parts)
        except Exception:
            pass

        # Retrieve per-goal work tracker (Phase O)
        self._goal_work_tracker: dict[str, dict] = {}
        try:
            tracker = await self.memory.registers.get_json("goal_work_tracker")
            if isinstance(tracker, dict):
                self._goal_work_tracker = tracker
        except Exception:
            pass

        # Retrieve journal context — latest consolidation + recent entries
        self._journal_context = ""
        self._journal_consolidation_text = ""
        try:
            if self.memory and self.memory.registers:
                journal_data = await self.memory.registers.get_json("journal")
                if journal_data:
                    consolidation = journal_data.get("consolidation", "")
                    entries = journal_data.get("entries", [])
                    self._journal_consolidation_text = consolidation

                    parts = []
                    if consolidation:
                        parts.append(f"### Last Consolidation\n{consolidation}")
                    if entries:
                        parts.append("### Recent Entries")
                        for e in entries[-10:]:  # Last 10 entries
                            cycle_n = e.get("cycle", "?")
                            for line in e.get("entries", []):
                                parts.append(f"- [cycle {cycle_n}] {line}")
                    self._journal_context = "\n".join(parts) if parts else ""
        except Exception:
            pass

        self.logger.log("orient_complete",
                        episodes=len(self._memory_context.get("episodes", [])),
                        goals=len(self._active_goals),
                        facts=len(self._memory_context.get("facts", [])),
                        graph_entities=len(self._graph_inventory) > 0,
                        nats_data_streams=len(self._queue_summary.data_streams),
                        has_journal=bool(self._journal_context))

    # -----------------------------------------------------------------------
    # INTROSPECTION / MISSION REVIEW (periodic)
    # -----------------------------------------------------------------------

    def _is_introspection_cycle(self) -> bool:
        """Check if this cycle should be a deep introspection instead of normal ops."""
        interval = self.config.agent.mission_review_interval
        return interval > 0 and self.state.cycle_number % interval == 0

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

    async def _mission_review(self) -> None:
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
        review_messages = self.assembler.assemble_introspection_prompt(
            cycle_number=self.state.cycle_number,
            seed_goal=self.state.seed_goal,
            active_goals=[g.model_dump() for g in self._active_goals],
            deferred_goals=deferred_goals_data,
            recent_work_pattern=recent_work_pattern,
            allowed_tools=self.INTROSPECTION_TOOLS,
        )

        # Create a filtered executor that only allows introspection tools
        async def introspection_executor(tool_name: str, arguments: dict) -> str:
            if tool_name not in self.INTROSPECTION_TOOLS:
                return (f"Tool '{tool_name}' is not available during introspection. "
                        f"This is an internal review cycle — only query and analysis tools are available: "
                        f"{', '.join(sorted(self.INTROSPECTION_TOOLS))}")
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
            # to prepend to the reflection_forward for the next PLAN phase
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

    def _parse_json_with_key(self, text: str, required_key: str) -> dict:
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

    # -----------------------------------------------------------------------
    # PLAN
    # -----------------------------------------------------------------------

    async def _plan(self) -> None:
        """
        Phase 3: Decide what to do this cycle.

        The model reviews context and produces a concrete plan before taking
        any actions. This prevents aimless tool calls and drift.
        """
        self.logger.log_phase("plan")
        self.state.phase = "plan"

        inbox_messages = [InboxMessage(**m) for m in self.state.inbox_messages]

        plan_messages = self.assembler.assemble_plan_prompt(
            cycle_number=self.state.cycle_number,
            seed_goal=self.state.seed_goal,
            active_goals=[g.model_dump() for g in self._active_goals],
            memory_context=self._memory_context,
            inbox_messages=inbox_messages,
            queue_summary=self._queue_summary,
            graph_inventory=self._graph_inventory,
            reflection_forward=self._reflection_forward,
            goal_work_tracker=self._goal_work_tracker,
            journal_context=self._journal_context,
        )

        try:
            response = await self.llm.complete(
                plan_messages,
                purpose="plan",
            )
            self._cycle_plan = response.content.strip()
            # Clean up any model stop tokens from the plan text
            for token in ["<|end|>", "<|return|>", "<|call|>"]:
                self._cycle_plan = self._cycle_plan.replace(token, "")
            # Extract tool names from "Tools: a, b, c" line in the plan
            self._planned_tools = self._parse_planned_tools(self._cycle_plan)
        except Exception as e:
            self._cycle_plan = f"Planning failed: {e}. Will proceed with highest-priority goal."
            self._planned_tools = None
            self.logger.log_error(f"Plan phase failed: {e}")

        self.logger.log("plan_complete",
                        plan_length=len(self._cycle_plan),
                        planned_tools=sorted(self._planned_tools) if self._planned_tools else None)

    @staticmethod
    def _parse_planned_tools(plan_text: str) -> set[str] | None:
        """Extract tool names from a 'Tools: a, b, c' line in the plan output.

        Returns None if no Tools line is found (falls back to full defs).
        Always includes explain_tool so the model can look up unexpected tools.
        """
        import re
        for line in reversed(plan_text.splitlines()):
            m = re.match(r'^tools\s*:\s*(.+)', line.strip(), re.IGNORECASE)
            if m:
                names = {t.strip() for t in m.group(1).split(",") if t.strip()}
                names.add("explain_tool")  # always available for on-demand lookup
                return names
        return None

    # -----------------------------------------------------------------------
    # Graceful shutdown support
    # -----------------------------------------------------------------------

    def _check_stop_flag(self) -> bool:
        """Check if the supervisor has requested graceful shutdown."""
        return os.path.exists(os.path.join(self.config.paths.shared, "stop_flag.json"))

    def _send_ping(self) -> None:
        """Acknowledge the stop flag so the supervisor extends the timeout."""
        ping = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cycle": self.state.cycle_number,
        }
        path = os.path.join(self.config.paths.shared, "stop_ping.json")
        with open(path, "w") as f:
            json.dump(ping, f)

    def _make_stop_checker(self):
        """Return a callable that checks for stop flag and pings back once."""
        pinged = False

        def check() -> bool:
            nonlocal pinged
            if self._check_stop_flag():
                if not pinged:
                    self._send_ping()
                    pinged = True
                    self.logger.log("graceful_shutdown", detail="stop_flag_detected")
                return True
            return False

        return check

    def _cleanup_signals(self) -> None:
        """Remove stale signal files from a previous cycle."""
        for name in ("stop_flag.json", "stop_ping.json"):
            path = os.path.join(self.config.paths.shared, name)
            if os.path.exists(path):
                os.remove(path)

    # -----------------------------------------------------------------------
    # REASON + ACT (interleaved)
    # -----------------------------------------------------------------------

    async def _reason_and_act(self) -> None:
        """
        Phase 4: LLM reasoning with tool execution.

        The LLM follows its cycle plan, calls tools, observes results,
        and continues until it produces a final response or exhausts
        its step budget.
        """
        self.logger.log_phase("reason")
        self.state.phase = "reason"

        inbox_messages = [InboxMessage(**m) for m in self.state.inbox_messages]

        # Assemble the full prompt with cycle plan and working memory.
        # planned_tools controls which tools get full parameter details in system;
        # all others are listed as name+description with explain_tool for on-demand lookup.
        messages = self.assembler.assemble_reason_prompt(
            cycle_number=self.state.cycle_number,
            seed_goal=self.state.seed_goal,
            active_goals=[g.model_dump() for g in self._active_goals],
            memory_context=self._memory_context,
            inbox_messages=inbox_messages,
            cycle_plan=self._cycle_plan,
            working_memory_summary=self.llm.working_memory.summary(),
            queue_summary=self._queue_summary,
            graph_inventory=self._graph_inventory,
            reflection_forward=self._reflection_forward,
            goal_work_tracker=self._goal_work_tracker,
            planned_tools=self._planned_tools,
        )

        # Run REASON→ACT loop with graceful shutdown support
        self._final_response, self._conversation = await self.llm.reason_with_tools(
            messages=messages,
            tool_executor=self.executor.execute,
            purpose="cycle_reason",
            max_steps=self.config.agent.max_reasoning_steps,
            stop_check=self._make_stop_checker(),
        )

        # Count actions taken (tool result messages in conversation)
        self.state.actions_taken = sum(
            1 for m in self._conversation
            if m.role == "user" and m.content.startswith("[Tool Result:")
        )

        self.logger.log("reason_complete",
                        response_length=len(self._final_response),
                        conversation_length=len(self._conversation),
                        actions_taken=self.state.actions_taken)

    # -----------------------------------------------------------------------
    # REFLECT
    # -----------------------------------------------------------------------

    async def _reflect(self) -> None:
        """
        Phase 5: Evaluate outcomes with structured extraction.

        Requests JSON output from the LLM to extract facts, entities,
        relationships, and self-assessment. Parsed results are stored
        via the memory manager.
        """
        self.logger.log_phase("reflect")
        self.state.phase = "reflect"

        # Build reflection context from working memory + final response
        working_memory_text = self.llm.working_memory.full_text()
        results_summary = self._final_response[:3000] if self._final_response else "(no response)"

        reflect_messages = self.assembler.assemble_reflect_prompt(
            cycle_plan=self._cycle_plan,
            working_memory=working_memory_text,
            results_summary=results_summary,
            seed_goal=self.state.seed_goal,
            cycle_number=self.state.cycle_number,
        )

        try:
            response = await self.llm.complete(
                reflect_messages,
                purpose="reflect",
            )
            self._reflection = response.content

            # Try to parse structured JSON from the reflection
            self._reflection_data = self._parse_reflection(self._reflection)

            # Store extracted facts
            await self._store_reflection_facts()

            # Store extracted entities and relationships in graph
            await self._store_reflection_graph()

        except Exception as e:
            self._reflection = f"Reflection failed: {e}"
            self._reflection_data = {}
            self.logger.log_error(f"Reflection failed: {e}")

        self.logger.log("reflect_complete",
                        reflection_length=len(self._reflection),
                        facts_extracted=len(self._reflection_data.get("facts_learned", [])),
                        entities_extracted=len(self._reflection_data.get("entities_discovered", [])))

    def _parse_reflection(self, text: str) -> dict:
        """Parse structured JSON from the reflection response.

        The model may include chain-of-thought reasoning before the JSON,
        which can contain small JSON snippets like {} or {"key": "val"}.
        We scan for all top-level JSON objects and return the one that
        looks like a real reflection (contains 'cycle_summary').
        """
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
                            if isinstance(candidate, dict) and "cycle_summary" in candidate:
                                return candidate
                        except (json.JSONDecodeError, ValueError):
                            pass
                        pos = i + 1
                        break
            else:
                break
        return {}

    async def _store_reflection_facts(self) -> None:
        """Store facts extracted from the reflection phase."""
        from .memory.fact_normalize import normalize_fact_predicate, normalize_fact_value

        facts = self._reflection_data.get("facts_learned", [])
        for fact_data in facts:
            try:
                subject = str(fact_data.get("subject", "")).strip()
                predicate = normalize_fact_predicate(str(fact_data.get("predicate", "")))
                value = normalize_fact_value(str(fact_data.get("value", "")))
                if not (subject and predicate and value):
                    continue

                fact = Fact(
                    subject=subject,
                    predicate=predicate,
                    value=value,
                    confidence=min(float(fact_data.get("confidence", 0.5)), 1.0),
                    source_cycle=self.state.cycle_number,
                )

                # Generate embedding for semantic search
                try:
                    embedding = await self.llm.generate_embedding(
                        f"{subject} {predicate} {value}"
                    )
                except Exception:
                    embedding = None

                await self.memory.store_fact(fact, embedding=embedding)

            except Exception as e:
                self.logger.log_error(f"Failed to store reflection fact: {e}")

    async def _store_reflection_graph(self) -> None:
        """Store entities and relationships from reflection in the graph."""
        from .tools.builtins.graph_tools import normalize_relationship_type, _find_similar_entity

        entities = self._reflection_data.get("entities_discovered", [])
        relationships = self._reflection_data.get("relationships", [])
        name_remap: dict[str, str] = {}

        for entity_data in entities:
            try:
                name = entity_data.get("name", "")
                etype = entity_data.get("type", "Entity")
                if not name:
                    continue
                # Fuzzy dedup: check for similar existing entity
                existing = await self.memory.graph.find_entity(name)
                if not existing:
                    similar = await _find_similar_entity(
                        self.memory.graph, name, etype,
                    )
                    if similar:
                        name_remap[name] = similar
                        name = similar
                props = entity_data.get("properties", {})
                props["discovered_cycle"] = self.state.cycle_number
                entity = Entity(name=name, entity_type=etype, properties=props)
                await self.memory.graph.upsert_entity(entity)
            except Exception as e:
                self.logger.log_error(f"Failed to store graph entity: {e}")

        for rel in relationships:
            try:
                from_e = rel.get("from_entity", "")
                to_e = rel.get("to_entity", "")
                rel_type = rel.get("relationship", "RELATED_TO")
                if not (from_e and to_e):
                    continue
                # Apply name remappings from dedup
                from_e = name_remap.get(from_e, from_e)
                to_e = name_remap.get(to_e, to_e)
                rel_type, _ = normalize_relationship_type(rel_type)
                props = rel.get("properties", {})
                props["discovered_cycle"] = self.state.cycle_number
                await self.memory.graph.add_relationship(from_e, to_e, rel_type, props)
            except Exception as e:
                self.logger.log_error(f"Failed to store graph relationship: {e}")

    # -----------------------------------------------------------------------
    # LIVENESS CHECK
    # -----------------------------------------------------------------------

    async def _validate_liveness(self) -> str:
        """
        Dedicated liveness check: ask the LLM to transform the nonce.

        The LLM outputs nonce:cycle_number — a simple separator-based
        format that avoids character-level precision issues with hex strings.
        Retries once if the first attempt is a partial/truncated match.
        """
        import re

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
                    max_tokens=512,
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

    # -----------------------------------------------------------------------
    # NARRATE — Legba's journal
    # -----------------------------------------------------------------------

    _JOURNAL_KEY = "journal"
    _JOURNAL_MAX_ENTRIES = 30  # Keep at most this many raw entries before trimming

    async def _narrate(self) -> None:
        """Write 1-3 journal entries reflecting on this cycle."""
        self.logger.log_phase("narrate")
        try:
            cycle_summary = self._reflection_data.get(
                "cycle_summary",
                self._final_response[:500] if self._final_response else "empty cycle",
            )

            narrate_messages = self.assembler.assemble_narrate_prompt(
                cycle_summary=cycle_summary,
                journal_context=self._journal_context,
            )

            response = await self.llm.complete(
                narrate_messages,
                purpose="narrate",
                max_tokens=512,
            )

            # Parse JSON array of strings
            raw = response.content.strip()
            # Find the JSON array in the response
            start = raw.find("[")
            end = raw.rfind("]")
            if start >= 0 and end > start:
                entries = json.loads(raw[start:end + 1])
                if isinstance(entries, list):
                    entries = [str(e) for e in entries if e][:3]
                else:
                    entries = []
            else:
                entries = []

            if entries:
                await self._store_journal_entries(entries)
                self.logger.log("narrate_complete", entries=len(entries))
            else:
                self.logger.log("narrate_complete", entries=0)

        except Exception as e:
            self.logger.log_error(f"Narrate failed: {e}")

    async def _store_journal_entries(self, entries: list[str]) -> None:
        """Append journal entries to Redis storage."""
        if not self.memory or not self.memory.registers:
            return

        journal_data = await self.memory.registers.get_json(self._JOURNAL_KEY) or {}
        raw_entries = journal_data.get("entries", [])
        raw_entries.append({
            "cycle": self.state.cycle_number,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "entries": entries,
        })

        # Trim old entries (keep last N)
        if len(raw_entries) > self._JOURNAL_MAX_ENTRIES:
            raw_entries = raw_entries[-self._JOURNAL_MAX_ENTRIES:]

        journal_data["entries"] = raw_entries
        await self.memory.registers.set_json(self._JOURNAL_KEY, journal_data)

    async def _journal_consolidation(self) -> None:
        """Consolidate recent journal entries into a narrative (introspection only)."""
        self.logger.log_phase("journal_consolidation")
        try:
            journal_data = await self.memory.registers.get_json(self._JOURNAL_KEY) or {}
            raw_entries = journal_data.get("entries", [])
            previous_consolidation = journal_data.get("consolidation", "")

            if not raw_entries:
                self.logger.log("journal_consolidation_skipped", reason="no entries")
                return

            # Format entries for the consolidation prompt
            entry_lines = []
            for e in raw_entries:
                cycle_n = e.get("cycle", "?")
                for line in e.get("entries", []):
                    entry_lines.append(f"[cycle {cycle_n}] {line}")

            entries_text = "\n".join(entry_lines)

            consolidation_messages = self.assembler.assemble_journal_consolidation_prompt(
                entries=entries_text,
                previous_consolidation=previous_consolidation,
            )

            response = await self.llm.complete(
                consolidation_messages,
                purpose="journal_consolidation",
            )

            new_consolidation = response.content.strip()
            # Clean any model artifacts
            for token in ["<|end|>", "<|return|>"]:
                new_consolidation = new_consolidation.replace(token, "")

            # Store consolidation, clear old entries
            journal_data["consolidation"] = new_consolidation
            journal_data["consolidation_cycle"] = self.state.cycle_number
            journal_data["consolidation_timestamp"] = datetime.now(timezone.utc).isoformat()
            journal_data["entries"] = []  # Clear raw entries after consolidation
            await self.memory.registers.set_json(self._JOURNAL_KEY, journal_data)

            self.logger.log("journal_consolidation_complete",
                            entries_consolidated=len(raw_entries),
                            consolidation_length=len(new_consolidation))

        except Exception as e:
            self.logger.log_error(f"Journal consolidation failed: {e}")

    async def _generate_analysis_report(self) -> None:
        """Generate a full Current World Assessment (introspection only).

        Queries actual data from all stores to ground the report in facts,
        preventing the LLM from hallucinating leaders, events, or programs.
        """
        self.logger.log_phase("analysis_report")
        try:
            # Gather context for the report
            graph_summary = self._graph_inventory or "(no graph data)"

            # --- Key relationships from graph (LeaderOf, HostileTo, AlliedWith, etc.) ---
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
                from ..shared.config import PostgresConfig
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
                    from ..shared.config import PostgresConfig
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

            report_messages = self.assembler.assemble_analysis_report_prompt(
                cycle_number=self.state.cycle_number,
                graph_summary=graph_summary,
                key_relationships=key_relationships,
                entity_profiles=entity_profiles_text,
                recent_events=recent_events,
                entity_count=entity_count,
                coverage_regions=coverage_regions,
                narrative=narrative,
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

    # -----------------------------------------------------------------------
    # PERSIST
    # -----------------------------------------------------------------------

    async def _persist(self) -> CycleResponse:
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
        # The real status report was already sent via nats_publish during REASON.
        # This uses the reflection summary (not the raw final LLM response which
        # may contain tool call JSON).
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

    # -----------------------------------------------------------------------
    # Cleanup
    # -----------------------------------------------------------------------

    async def _cleanup(self) -> None:
        """Close all connections."""
        if self.airflow:
            await self.airflow.close()
        if self.opensearch:
            await self.opensearch.close()
        if self.nats:
            await self.nats.close()
        if self.llm:
            await self.llm.close()
        if self.memory:
            await self.memory.close()
        if self.logger:
            self.logger.close()

    # -----------------------------------------------------------------------
    # Tool registration
    # -----------------------------------------------------------------------

    def _register_builtin_tools(self) -> None:
        """Register all built-in tools.

        Each builtin module exports register(registry, **deps). Dependencies
        are injected here — the modules own their handler logic, cycle.py
        just wires the deps.
        """
        from .tools.builtins import fs, shell, http, memory_tools, graph_tools, goal_tools, selfmod_tools, nats_tools, opensearch_tools, analytics_tools, orchestration_tools, feed_tools, source_tools, event_tools, entity_tools

        # Simple tools (config for timeout ceilings)
        shell.register(self.registry, agent_config=self.config.agent)
        http.register(self.registry, agent_config=self.config.agent)
        selfmod_tools.register(self.registry)

        # Filesystem tools — with selfmod interception for /agent writes
        fs.register(
            self.registry,
            selfmod=self.selfmod,
            agent_prefix=self.config.paths.agent_code,
            state=self.state,
        )

        # Memory tools — wired to live memory manager + LLM
        memory_tools.register(
            self.registry,
            memory=self.memory,
            llm=self.llm,
            state=self.state,
            logger=self.logger,
        )

        # Graph tools — wired to live AGE graph store
        graph_tools.register(self.registry, graph=self.memory.graph)

        # Goal tools — wired to live GoalManager
        goal_tools.register(self.registry, goals=self.goals, state=self.state)

        # NATS tools — wired to live NATS client
        nats_tools.register(self.registry, nats=self.nats)

        # OpenSearch tools — wired to live OpenSearch store
        opensearch_tools.register(self.registry, opensearch=self.opensearch)

        # Analytics tools — wired to OpenSearch + graph for reference-based data flow
        analytics_tools.register(
            self.registry,
            opensearch=self.opensearch,
            graph=self.memory.graph,
        )

        # Orchestration tools — wired to live Airflow client
        orchestration_tools.register(self.registry, airflow=self.airflow)

        # SA-1: Feed, source, and event tools
        feed_tools.register(self.registry, structured=self.memory.structured)
        source_tools.register(self.registry, structured=self.memory.structured)
        event_tools.register(
            self.registry,
            structured=self.memory.structured,
            opensearch=self.opensearch,
        )

        # Entity intelligence tools — structured profiles + event linking
        entity_tools.register(
            self.registry,
            structured=self.memory.structured,
            graph=self.memory.graph,
        )

        # note_to_self tool — working memory within this cycle
        self._register_note_to_self()

        # cycle_complete tool — clean exit from tool loop
        self._register_cycle_complete()

        # explain_tool — returns full definition for a tool not in the plan
        self._register_explain_tool()

        # Sub-agent tool
        self._register_subagent()

    def _register_note_to_self(self) -> None:
        """Register the note_to_self tool for within-cycle working memory."""
        from ..shared.schemas.tools import ToolDefinition, ToolParameter

        note_def = ToolDefinition(
            name="note_to_self",
            description="Record an observation or insight for this cycle's working memory. "
                        "Notes are visible in re-grounding prompts and fed to the reflection phase. "
                        "Use for: key findings, decisions made, things to remember later in this cycle.",
            parameters=[
                ToolParameter(name="note", type="string",
                              description="The observation or insight to record"),
            ],
        )

        llm_client = self.llm  # capture reference

        async def note_handler(args: dict) -> str:
            note = args.get("note", "")
            if not note:
                return "Error: note parameter is required"
            llm_client.working_memory.add_note(note)
            return f"Noted: {note[:100]}{'...' if len(note) > 100 else ''}"

        self.registry.register(note_def, note_handler)

    def _register_cycle_complete(self) -> None:
        """Register the cycle_complete pseudo-tool for clean early exit.

        When the agent calls this, reason_with_tools() detects the tool name
        and breaks out of the loop — proceeding directly to REFLECT and PERSIST.
        The tool is never actually executed; it's intercepted in client.py.
        """
        from ..shared.schemas.tools import ToolDefinition, ToolParameter

        complete_def = ToolDefinition(
            name="cycle_complete",
            description="Signal that you have finished your plan for this cycle. "
                        "Call this when you have completed all planned actions and "
                        "have no more useful tool calls to make. The cycle will "
                        "proceed to REFLECT and PERSIST.",
            parameters=[
                ToolParameter(name="reason", type="string",
                              description="Brief summary of what was accomplished"),
            ],
        )

        # Handler is a no-op — cycle_complete is intercepted in client.py
        # before execution. Registered here so it appears in the tool list.
        async def noop_handler(args: dict) -> str:
            return "cycle_complete acknowledged"

        self.registry.register(complete_def, noop_handler)

    def _register_explain_tool(self) -> None:
        """Register explain_tool — returns full parameter details for any tool."""
        from ..shared.schemas.tools import ToolDefinition, ToolParameter
        import json as _json

        explain_def = ToolDefinition(
            name="explain_tool",
            description="Get full parameter details for a tool. Use when you need a tool "
                        "that wasn't included in your plan's full definitions.",
            parameters=[
                ToolParameter(name="tool_name", type="string",
                              description="Name of the tool to look up"),
            ],
        )

        registry = self.registry  # capture reference

        async def explain_handler(args: dict) -> str:
            name = args.get("tool_name", "")
            if not name:
                return "Error: tool_name is required"
            defn = registry.get_definition(name)
            if defn is None:
                return f"Error: Unknown tool '{name}'. Use the tool summary to find valid names."
            return _json.dumps({
                "name": defn.name,
                "description": defn.description,
                "parameters": [
                    {"name": p.name, "type": p.type,
                     "description": p.description, "required": p.required}
                    for p in defn.parameters
                ],
            }, indent=2)

        self.registry.register(explain_def, explain_handler)

    def _register_subagent(self) -> None:
        """Register the spawn_subagent tool."""
        from ..shared.schemas.tools import ToolDefinition, ToolParameter

        subagent_def = ToolDefinition(
            name="spawn_subagent",
            description="Spawn a sub-agent with its own context window for a focused task. "
                        "Returns the sub-agent's summary. Use for complex research, large file analysis, "
                        "or multi-step tool chains that would consume too much context.",
            parameters=[
                ToolParameter(name="task", type="string",
                              description="What the sub-agent should accomplish"),
                ToolParameter(name="context", type="string",
                              description="Relevant context to pass to the sub-agent"),
                ToolParameter(name="tools", type="string",
                              description="Comma-separated list of tool names the sub-agent can use"),
                ToolParameter(name="max_steps", type="number",
                              description="Maximum tool calls the sub-agent can make (default 10)",
                              required=False),
            ],
        )

        async def subagent_handler(args: dict) -> str:
            task = args.get("task", "")
            if not task:
                # Fallback: check _raw for unparsed JSON
                raw = args.get("_raw", "")
                if raw:
                    return f"Error: spawn_subagent arguments could not be parsed. Raw: {raw[:200]}"
                return "Error: task parameter is required for spawn_subagent"

            tools_val = args.get("tools", "")
            # Handle both comma-separated string and JSON array
            if isinstance(tools_val, list):
                tool_list = [str(t).strip() for t in tools_val if str(t).strip()]
            else:
                tool_list = [t.strip() for t in str(tools_val).split(",") if t.strip()]

            max_steps = min(int(args.get("max_steps", 10)), self.config.max_subagent_steps)

            return await run_subagent(
                task=task,
                context=args.get("context", ""),
                allowed_tools=tool_list,
                max_steps=max_steps,
                llm_client=self.llm,
                registry=self.registry,
                logger=self.logger,
            )

        self.registry.register(subagent_def, subagent_handler)
