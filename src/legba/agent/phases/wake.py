"""WAKE phase — initialization, connections, tool registration, inbox drain."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from ...shared.schemas.cycle import Challenge
from ...shared.schemas.comms import Inbox
from ..comms.nats_client import LegbaNatsClient
from ..comms.airflow_client import AirflowClient
from ..memory.opensearch import OpenSearchStore
from ..memory.manager import MemoryManager
from ..llm.client import LLMClient
from ..goals.manager import GoalManager
from ..tools.registry import ToolRegistry
from ..tools.executor import ToolExecutor
from ..selfmod.engine import SelfModEngine
from ..prompt.assembler import PromptAssembler
from ..log import CycleLogger
from . import REPORT_INTERVAL

if TYPE_CHECKING:
    from ..cycle import AgentCycle


class WakeMixin:
    """Phase 1: Initialize everything."""

    async def _wake(self: AgentCycle) -> None:
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
        from ..tools.builtins.http import clear_http_cache
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

        # Initialize LLM client — check for cycle-type-specific provider override
        llm_config = self.config.llm
        forced_type = os.environ.get("CYCLE_TYPE", "").strip().upper()
        if not forced_type:
            # Detect cycle type from interval routing for provider selection
            cn = self.state.cycle_number
            from . import EVOLVE_INTERVAL, SYNTHESIZE_INTERVAL, ANALYSIS_INTERVAL, RESEARCH_INTERVAL, CURATE_INTERVAL
            if cn % EVOLVE_INTERVAL == 0:
                forced_type = "EVOLVE"
            elif cn % 15 == 0:
                forced_type = "INTROSPECTION"
            elif cn % SYNTHESIZE_INTERVAL == 0:
                forced_type = "SYNTHESIZE"
            elif cn % ANALYSIS_INTERVAL == 0:
                forced_type = "ANALYSIS"
            elif cn % RESEARCH_INTERVAL == 0:
                forced_type = "RESEARCH"
            elif cn % CURATE_INTERVAL == 0:
                forced_type = "CURATE"
            else:
                forced_type = "SURVEY"

        from ...shared.config import LLMConfig
        alt_config = LLMConfig.for_cycle_type(forced_type)
        if alt_config:
            self.logger.log("llm_provider_override",
                           cycle_type=forced_type,
                           provider=alt_config.provider,
                           model=alt_config.model)
            llm_config = alt_config

        self.llm = LLMClient(llm_config, self.logger)

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

    def _register_builtin_tools(self: AgentCycle) -> None:
        """Register all built-in tools.

        Each builtin module exports register(registry, **deps). Dependencies
        are injected here — the modules own their handler logic, cycle.py
        just wires the deps.
        """
        from ..tools.builtins import (
            fs, shell, http, memory_tools, graph_tools, goal_tools,
            selfmod_tools, nats_tools, opensearch_tools, analytics_tools,
            orchestration_tools, feed_tools, source_tools, event_tools,
            entity_tools, watchlist_tools, situation_tools, prediction_tools,
            derived_event_tools, hypothesis_tools, metrics_tools,
        )
        from ..tools.subagent import run_subagent
        from ...shared.schemas.tools import ToolDefinition, ToolParameter

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

        # Analytics tools — wired to OpenSearch + graph + structured for reference-based data flow
        analytics_tools.register(
            self.registry,
            opensearch=self.opensearch,
            graph=self.memory.graph,
            structured=self.memory.structured,
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

        # Derived event tools — create/update/query real-world occurrences
        derived_event_tools.register(
            self.registry,
            structured=self.memory.structured,
        )

        # Entity intelligence tools — structured profiles + event linking
        entity_tools.register(
            self.registry,
            structured=self.memory.structured,
            graph=self.memory.graph,
        )

        # Watchlist tools — persistent alerting patterns
        watchlist_tools.register(self.registry, structured=self.memory.structured)

        # Situation tracking tools — persistent tracked narratives
        situation_tools.register(self.registry, structured=self.memory.structured)

        # Prediction / hypothesis tracking tools
        prediction_tools.register(self.registry, structured=self.memory.structured, state=self.state)

        # Hypothesis (ACH) tools — competing hypothesis pairs with evidence tracking
        hypothesis_tools.register(self.registry, structured=self.memory.structured, state=self.state)

        # Metrics query tool — time-series baselines from TimescaleDB
        metrics_tools.register(self.registry)

        # note_to_self tool — working memory within this cycle
        self._register_note_to_self()

        # cycle_complete tool — clean exit from tool loop
        self._register_cycle_complete()

        # explain_tool — returns full definition for a tool not in the plan
        self._register_explain_tool()

        # Sub-agent tool
        self._register_subagent()

    def _register_note_to_self(self: AgentCycle) -> None:
        """Register the note_to_self tool for within-cycle working memory."""
        from ...shared.schemas.tools import ToolDefinition, ToolParameter

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

    def _register_cycle_complete(self: AgentCycle) -> None:
        """Register the cycle_complete pseudo-tool for clean early exit."""
        from ...shared.schemas.tools import ToolDefinition, ToolParameter

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

        async def noop_handler(args: dict) -> str:
            return "cycle_complete acknowledged"

        self.registry.register(complete_def, noop_handler)

    def _register_explain_tool(self: AgentCycle) -> None:
        """Register explain_tool — returns full parameter details for any tool."""
        from ...shared.schemas.tools import ToolDefinition, ToolParameter
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

    def _register_subagent(self: AgentCycle) -> None:
        """Register the spawn_subagent tool."""
        from ...shared.schemas.tools import ToolDefinition, ToolParameter
        from ..tools.subagent import run_subagent

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
                raw = args.get("_raw", "")
                if raw:
                    return f"Error: spawn_subagent arguments could not be parsed. Raw: {raw[:200]}"
                return "Error: task parameter is required for spawn_subagent"

            tools_val = args.get("tools", "")
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
