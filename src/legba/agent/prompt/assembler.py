"""
Prompt Assembler

Assembles the full prompt context for each cycle's LLM calls.
Single-turn pattern: every call gets exactly [system, user].
Tracks token usage and enforces a configurable context budget.

Instructions-first, data-last pattern:
  System = identity + rules + guidance + tool definitions + calling format
  User   = --- CONTEXT DATA --- / data sections / --- END CONTEXT --- / task
"""

from __future__ import annotations

from typing import Any

from ..llm.format import Message
from . import templates
from ...shared.schemas.comms import InboxMessage, MessagePriority, QueueSummary


# Rough token estimation: ~4 chars per token for English text
def _estimate_tokens(text: str) -> int:
    return len(text) // 4


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to approximately max_tokens. Cuts at line boundaries."""
    if _estimate_tokens(text) <= max_tokens:
        return text
    max_chars = max_tokens * 4
    truncated = text[:max_chars]
    # Cut at last newline to avoid mid-line breaks
    last_nl = truncated.rfind("\n")
    if last_nl > max_chars // 2:
        truncated = truncated[:last_nl]
    return truncated + "\n(... truncated to fit context budget)"


class PromptAssembler:
    """
    Assembles the full conversation for a cycle's LLM calls.

    Builds message lists for: PLAN phase, REASON phase, and REFLECT phase.
    Single-turn pattern: always returns [system_msg, user_msg].
    Tracks approximate token usage and enforces budget via truncation.
    """

    def __init__(
        self,
        tool_data: list[dict],
        tool_summary: str,
        bootstrap_threshold: int = 5,
        max_context_tokens: int = 120000,
        report_interval: int = 5,
        world_briefing: str = "",
        airflow_available: bool = False,
    ):
        self._tool_data = tool_data          # raw tool dicts for filtered rendering
        self._tool_summary = tool_summary    # compact name+description list for PLAN
        self._total_tokens = 0
        self._bootstrap_threshold = bootstrap_threshold
        self._max_context_tokens = max_context_tokens
        self._world_briefing = world_briefing
        self._report_interval = report_interval
        self._truncated = False
        self._airflow_available = airflow_available

    def assemble_plan_prompt(
        self,
        cycle_number: int,
        seed_goal: str,
        active_goals: list[dict],
        memory_context: dict[str, Any],
        inbox_messages: list[InboxMessage],
        queue_summary: QueueSummary | None = None,
        graph_inventory: str = "",
        reflection_forward: str = "",
        goal_work_tracker: dict[str, dict] | None = None,
        journal_context: str = "",
    ) -> list[Message]:
        """
        Build the message list for the PLAN phase.

        Returns [system, user] — single-turn format.
        """
        # System message — includes tool summary so the planner knows what's available
        system_text = self._build_system_text(cycle_number, "(planning)")
        system_text += "\n\n" + self._tool_summary

        # User message: concatenate all context sections
        user_parts: list[str] = []

        # World briefing (bootstrap cycles only — orients the model on recent history)
        if cycle_number <= self._bootstrap_threshold and self._world_briefing:
            user_parts.append(self._world_briefing)

        # Goal context
        goals_text = self._format_goals(seed_goal, active_goals, goal_work_tracker, cycle_number)
        user_parts.append(templates.GOAL_CONTEXT_TEMPLATE.format(
            seed_goal=seed_goal,
            active_goals=goals_text,
        ))

        # Memory context
        memory_text = self._format_memories(memory_context)
        if memory_text:
            user_parts.append(memory_text)

        # Graph inventory
        if graph_inventory:
            user_parts.append(graph_inventory)

        # Inbox messages
        if inbox_messages:
            user_parts.append(self._format_inbox(inbox_messages))

        # Queue summary
        if queue_summary and (queue_summary.total_data_messages > 0 or queue_summary.data_streams):
            user_parts.append(self._format_queue_summary(queue_summary))

        # Journal — Legba's narrative perspective
        if journal_context:
            user_parts.append("## Your Journal\n" + journal_context)

        # Previous cycle reflection
        if reflection_forward:
            user_parts.append(reflection_forward)

        # Plan request (last thing model reads)
        user_parts.append(templates.PLAN_PROMPT)

        return [
            Message(role="system", content=system_text),
            Message(role="user", content="\n\n".join(user_parts)),
        ]

    def assemble_mission_review_prompt(
        self,
        cycle_number: int,
        seed_goal: str,
        active_goals: list[dict],
        deferred_goals: list[dict],
        recent_work_pattern: str,
    ) -> list[Message]:
        """Build the message list for the periodic mission review."""
        goals_text = self._format_goals(seed_goal, active_goals) if active_goals else "(No active goals)"

        if deferred_goals:
            deferred_lines = []
            for g in deferred_goals:
                desc = g.get("description", "?")
                reason = g.get("defer_reason", "no reason recorded")
                deferred_lines.append(f"- {g.get('id', '?')}: {desc[:100]} (deferred: {reason})")
            deferred_text = "\n".join(deferred_lines)
        else:
            deferred_text = "(No deferred goals ready for re-evaluation)"

        review_text = templates.MISSION_REVIEW_PROMPT.format(
            seed_goal=seed_goal,
            active_goals=goals_text,
            deferred_goals=deferred_text,
            cycle_number=cycle_number,
            recent_work_pattern=recent_work_pattern or "unknown",
        )

        system_text = (
            "reasoning: high\n\n"
            "You are Legba, conducting a periodic strategic review. "
            "Respond with a JSON object only.\n\n"
            f"Cycle: {cycle_number}\n"
            f"Primary Mission: {seed_goal[:300]}"
        )

        return [
            Message(role="system", content=system_text),
            Message(role="user", content=review_text),
        ]

    def assemble_introspection_prompt(
        self,
        cycle_number: int,
        seed_goal: str,
        active_goals: list[dict],
        deferred_goals: list[dict],
        recent_work_pattern: str,
        allowed_tools: frozenset[str],
        inbox_messages: list[InboxMessage] | None = None,
    ) -> list[Message]:
        """Build the message list for the deep introspection cycle.

        Uses a full REASON+ACT prompt with tool definitions, but restricted
        to internal query tools only.
        """
        goals_text = self._format_goals(seed_goal, active_goals) if active_goals else "(No active goals)"

        if deferred_goals:
            deferred_lines = []
            for g in deferred_goals:
                desc = g.get("description", "?")
                reason = g.get("defer_reason", "no reason recorded")
                deferred_lines.append(f"- {g.get('id', '?')}: {desc[:100]} (deferred: {reason})")
            deferred_text = "\n".join(deferred_lines)
        else:
            deferred_text = "(No deferred goals ready for re-evaluation)"

        # Build system: identity + introspection tool definitions + calling instructions
        from ..llm.format import format_tool_definitions
        introspection_tool_data = [t for t in self._tool_data if t["name"] in allowed_tools]
        system_text = templates.SYSTEM_PROMPT.format(
            cycle_number=cycle_number,
            context_tokens="introspection",
        )
        system_text += "\n\n" + format_tool_definitions(introspection_tool_data)
        system_text += "\n\n" + templates.TOOL_CALLING_INSTRUCTIONS

        # Build user: the introspection task with context data
        user_text = templates.MISSION_REVIEW_PROMPT.format(
            seed_goal=seed_goal,
            active_goals=goals_text,
            deferred_goals=deferred_text,
            cycle_number=cycle_number,
            recent_work_pattern=recent_work_pattern or "unknown",
        )

        # Inject operator messages if present
        if inbox_messages:
            user_text = self._format_inbox(inbox_messages) + "\n\n" + user_text

        return [
            Message(role="system", content=system_text),
            Message(role="user", content=user_text),
        ]

    def assemble_research_prompt(
        self,
        cycle_number: int,
        seed_goal: str,
        active_goals: list[dict],
        entity_health: str,
        allowed_tools: frozenset[str],
        inbox_messages: list[InboxMessage] | None = None,
    ) -> list[Message]:
        """Build the message list for the research/enrichment cycle.

        Uses a REASON+ACT prompt with tool definitions including external
        tools (http_request) for entity research and enrichment.
        """
        goals_text = self._format_goals(seed_goal, active_goals) if active_goals else "(No active goals)"

        # Build system: identity + research tool definitions + calling instructions
        from ..llm.format import format_tool_definitions
        research_tool_data = [t for t in self._tool_data if t["name"] in allowed_tools]
        system_text = templates.SYSTEM_PROMPT.format(
            cycle_number=cycle_number,
            context_tokens="research",
        )
        system_text += "\n\n" + format_tool_definitions(research_tool_data)
        system_text += "\n\n" + templates.TOOL_CALLING_INSTRUCTIONS

        # Build user: the research task with entity health data
        user_text = templates.RESEARCH_PROMPT.format(
            seed_goal=seed_goal,
            active_goals=goals_text,
            entity_health=entity_health,
        )

        # Inject operator messages if present
        if inbox_messages:
            user_text = self._format_inbox(inbox_messages) + "\n\n" + user_text

        return [
            Message(role="system", content=system_text),
            Message(role="user", content=user_text),
        ]

    def assemble_acquire_prompt(
        self,
        cycle_number: int,
        seed_goal: str,
        active_goals: list[dict],
        source_status: str,
        allowed_tools: frozenset[str],
        inbox_messages: list[InboxMessage] | None = None,
    ) -> list[Message]:
        """Build the message list for the acquire/ingestion cycle."""
        goals_text = self._format_goals(seed_goal, active_goals) if active_goals else "(No active goals)"

        from ..llm.format import format_tool_definitions
        acquire_tool_data = [t for t in self._tool_data if t["name"] in allowed_tools]
        system_text = templates.SYSTEM_PROMPT.format(
            cycle_number=cycle_number,
            context_tokens="acquire",
        )
        system_text += "\n\n" + format_tool_definitions(acquire_tool_data)
        system_text += "\n\n" + templates.TOOL_CALLING_INSTRUCTIONS

        user_text = templates.ACQUIRE_PROMPT.format(
            seed_goal=seed_goal,
            active_goals=goals_text,
            source_status=source_status,
        )

        if inbox_messages:
            user_text = self._format_inbox(inbox_messages) + "\n\n" + user_text

        return [
            Message(role="system", content=system_text),
            Message(role="user", content=user_text),
        ]

    def assemble_source_discovery_prompt(
        self,
        cycle_number: int,
        seed_goal: str,
        active_goals: list[dict],
        source_status: str,
        ingestion_status: str,
        allowed_tools: frozenset[str],
        inbox_messages: list[InboxMessage] | None = None,
    ) -> list[Message]:
        """Build the message list for the source discovery cycle (ingestion service active)."""
        goals_text = self._format_goals(seed_goal, active_goals) if active_goals else "(No active goals)"

        from ..llm.format import format_tool_definitions
        discovery_tool_data = [t for t in self._tool_data if t["name"] in allowed_tools]
        system_text = templates.SYSTEM_PROMPT.format(
            cycle_number=cycle_number,
            context_tokens="source_discovery",
        )
        system_text += "\n\n" + format_tool_definitions(discovery_tool_data)
        system_text += "\n\n" + templates.TOOL_CALLING_INSTRUCTIONS

        user_text = templates.SOURCE_DISCOVERY_PROMPT.format(
            seed_goal=seed_goal,
            active_goals=goals_text,
            source_status=source_status,
            ingestion_status=ingestion_status,
        )

        if inbox_messages:
            user_text = self._format_inbox(inbox_messages) + "\n\n" + user_text

        return [
            Message(role="system", content=system_text),
            Message(role="user", content=user_text),
        ]

    def assemble_analysis_cycle_prompt(
        self,
        cycle_number: int,
        seed_goal: str,
        active_goals: list[dict],
        analysis_context: str,
        allowed_tools: frozenset[str],
        inbox_messages: list[InboxMessage] | None = None,
    ) -> list[Message]:
        """Build the message list for the analysis cycle."""
        goals_text = self._format_goals(seed_goal, active_goals) if active_goals else "(No active goals)"

        from ..llm.format import format_tool_definitions
        analysis_tool_data = [t for t in self._tool_data if t["name"] in allowed_tools]
        system_text = templates.SYSTEM_PROMPT.format(
            cycle_number=cycle_number,
            context_tokens="analysis",
        )
        system_text += "\n\n" + format_tool_definitions(analysis_tool_data)
        system_text += "\n\n" + templates.TOOL_CALLING_INSTRUCTIONS

        user_text = templates.ANALYSIS_PROMPT.format(
            seed_goal=seed_goal,
            active_goals=goals_text,
            analysis_context=analysis_context,
        )

        if inbox_messages:
            user_text = self._format_inbox(inbox_messages) + "\n\n" + user_text

        # Context budget enforcement: progressively truncate if over 75% of budget
        estimated = _estimate_tokens(system_text) + _estimate_tokens(user_text)
        budget_limit = int(self._max_context_tokens * 0.75)
        if self._max_context_tokens > 0 and estimated > budget_limit:
            # First: truncate analysis_context to 8000 chars
            analysis_context_truncated = analysis_context[:8000]
            if len(analysis_context) > 8000:
                analysis_context_truncated += "\n(... analysis context truncated to fit budget)"
            # Second: truncate active_goals to 3000 chars
            goals_text_truncated = goals_text[:3000]
            if len(goals_text) > 3000:
                goals_text_truncated += "\n(... goals truncated to fit budget)"
            # Rebuild user_text with truncated data
            user_text = templates.ANALYSIS_PROMPT.format(
                seed_goal=seed_goal,
                active_goals=goals_text_truncated,
                analysis_context=analysis_context_truncated,
            )
            if inbox_messages:
                user_text = self._format_inbox(inbox_messages) + "\n\n" + user_text
            user_text += "\n\n(Note: context was truncated to fit budget)"
            self._truncated = True

        return [
            Message(role="system", content=system_text),
            Message(role="user", content=user_text),
        ]

    def assemble_evolve_prompt(
        self,
        cycle_number: int,
        seed_goal: str,
        active_goals: list[dict],
        evolve_context: str,
        allowed_tools: frozenset[str],
        inbox_messages: list[InboxMessage] | None = None,
    ) -> list[Message]:
        """Build the message list for the evolve/self-improvement cycle."""
        goals_text = self._format_goals(seed_goal, active_goals) if active_goals else "(No active goals)"

        from ..llm.format import format_tool_definitions
        evolve_tool_data = [t for t in self._tool_data if t["name"] in allowed_tools]
        system_text = templates.SYSTEM_PROMPT.format(
            cycle_number=cycle_number,
            context_tokens="evolve",
        )
        system_text += "\n\n" + format_tool_definitions(evolve_tool_data)
        system_text += "\n\n" + templates.TOOL_CALLING_INSTRUCTIONS

        user_text = templates.EVOLVE_PROMPT.format(
            seed_goal=seed_goal,
            active_goals=goals_text,
            evolve_context=evolve_context,
        )

        if inbox_messages:
            user_text = self._format_inbox(inbox_messages) + "\n\n" + user_text

        return [
            Message(role="system", content=system_text),
            Message(role="user", content=user_text),
        ]

    def assemble_reason_prompt(
        self,
        cycle_number: int,
        seed_goal: str,
        active_goals: list[dict],
        memory_context: dict[str, Any],
        inbox_messages: list[InboxMessage],
        cycle_plan: str = "",
        working_memory_summary: str = "(none yet)",
        queue_summary: QueueSummary | None = None,
        graph_inventory: str = "",
        reflection_forward: str = "",
        goal_work_tracker: dict[str, dict] | None = None,
        planned_tools: set[str] | None = None,
    ) -> list[Message]:
        """
        Build [system, user] for the REASON phase (first step of tool loop).

        Instructions-first, data-last:
          System = identity + rules + guidance + tool defs (filtered) + calling format
          User   = CONTEXT DATA separators / all data / END CONTEXT / task + act

        If planned_tools is provided, only those tools get full parameter details
        in the system message; all others are listed as name + description with
        explain_tool available for on-demand lookup.
        """
        self._total_tokens = 0
        self._truncated = False

        # --- User message data sections ---
        # Goal context
        goals_text = self._format_goals(seed_goal, active_goals, goal_work_tracker, cycle_number)
        goal_section = templates.GOAL_CONTEXT_TEMPLATE.format(
            seed_goal=seed_goal,
            active_goals=goals_text,
        )
        goal_tokens = _estimate_tokens(goal_section)

        # Memory context
        memory_section = self._format_memories(memory_context)
        memory_tokens = _estimate_tokens(memory_section) if memory_section else 0

        # Graph inventory
        graph_section = graph_inventory or ""

        # Inbox
        inbox_section = ""
        inbox_tokens = 0
        if inbox_messages:
            inbox_section = self._format_inbox(inbox_messages)
            inbox_tokens = _estimate_tokens(inbox_section)

        # Queue summary
        queue_section = ""
        queue_tokens = 0
        if queue_summary and (queue_summary.total_data_messages > 0 or queue_summary.data_streams):
            queue_section = self._format_queue_summary(queue_summary)
            queue_tokens = _estimate_tokens(queue_section)

        # Reflection forward
        rf_tokens = _estimate_tokens(reflection_forward) if reflection_forward else 0

        # Task request (plan + working memory + short act instruction)
        reporting_reminder = ""
        if cycle_number > 0 and cycle_number % self._report_interval == 0:
            reporting_reminder = templates.REPORTING_REMINDER.format(
                cycle_number=cycle_number,
                report_interval=self._report_interval,
            )
        request_section = templates.CYCLE_REQUEST.format(
            cycle_plan=cycle_plan or "(no plan — decide what to do and act)",
            working_memory_summary=working_memory_summary,
            reporting_reminder=reporting_reminder,
        )
        request_tokens = _estimate_tokens(request_section)

        # --- System message (includes tool defs + calling instructions) ---
        # Build with placeholder first for budget calculation
        system_text = self._build_system_text(
            cycle_number, "(calculating)", include_tools=True, planned_tools=planned_tools,
        )
        system_tokens = _estimate_tokens(system_text)

        # --- Budget enforcement ---
        fixed_tokens = (system_tokens + inbox_tokens + queue_tokens
                        + rf_tokens + request_tokens)
        flexible_tokens = memory_tokens + goal_tokens
        total = fixed_tokens + flexible_tokens

        if total > self._max_context_tokens and self._max_context_tokens > 0:
            budget_for_flexible = self._max_context_tokens - fixed_tokens
            if budget_for_flexible < 0:
                budget_for_flexible = 0
            self._truncated = True

            if memory_tokens > 0 and budget_for_flexible < flexible_tokens:
                goal_budget = min(goal_tokens, int(budget_for_flexible * 0.4))
                memory_budget = budget_for_flexible - goal_budget

                if memory_budget < memory_tokens and memory_section:
                    memory_section = _truncate_to_tokens(memory_section, max(memory_budget, 100))
                    memory_tokens = _estimate_tokens(memory_section)

                remaining = budget_for_flexible - memory_tokens
                if remaining < goal_tokens:
                    goal_section = _truncate_to_tokens(goal_section, max(remaining, 100))
                    goal_tokens = _estimate_tokens(goal_section)
            elif budget_for_flexible < goal_tokens:
                goal_section = _truncate_to_tokens(goal_section, max(budget_for_flexible, 100))
                goal_tokens = _estimate_tokens(goal_section)

        # --- Assemble user message (data-only, bracketed by separators) ---
        user_parts: list[str] = [templates.CONTEXT_DATA_SEPARATOR]

        # World briefing (bootstrap cycles only)
        if cycle_number <= self._bootstrap_threshold and self._world_briefing:
            user_parts.append(self._world_briefing)

        user_parts.append(goal_section)
        if memory_section:
            user_parts.append(memory_section)
        if graph_section:
            user_parts.append(graph_section)
        if inbox_section:
            user_parts.append(inbox_section)
        if queue_section:
            user_parts.append(queue_section)
        if reflection_forward:
            user_parts.append(reflection_forward)

        user_parts.append(templates.CONTEXT_END_SEPARATOR)

        # Task request AFTER context (last thing model reads before generating)
        user_parts.append(request_section)

        user_text = "\n\n".join(user_parts)

        # --- Final token count ---
        self._total_tokens = (
            system_tokens + goal_tokens + memory_tokens
            + _estimate_tokens(graph_section) + inbox_tokens
            + queue_tokens + rf_tokens + request_tokens
        )

        # Update system message with actual token count
        budget_note = ""
        if self._truncated:
            budget_note = (
                f"\n**Note:** Context was truncated to fit within the {self._max_context_tokens} "
                f"token budget. Some memories/goals were shortened. Use spawn_subagent for "
                f"detailed research."
            )
        system_text = self._build_system_text(
            cycle_number, str(self._total_tokens), include_tools=True, planned_tools=planned_tools,
        ) + budget_note

        return [
            Message(role="system", content=system_text),
            Message(role="user", content=user_text),
        ]

    def assemble_reflect_prompt(
        self,
        cycle_plan: str,
        working_memory: str,
        results_summary: str,
        seed_goal: str = "",
        cycle_number: int = 0,
        inbox_messages: list[InboxMessage] | None = None,
    ) -> list[Message]:
        """Build the message list for the REFLECT phase."""
        max_results = min(self._max_context_tokens // 2, 50000) if self._max_context_tokens > 0 else 50000
        if _estimate_tokens(results_summary) > max_results:
            results_summary = _truncate_to_tokens(results_summary, max_results)

        reflect_text = templates.REFLECT_PROMPT.format(
            cycle_plan=cycle_plan or "(no explicit plan)",
            working_memory=working_memory or "(no observations recorded)",
            results_summary=results_summary,
        )

        # Inject operator messages so reflection accounts for them
        if inbox_messages:
            reflect_text = self._format_inbox(inbox_messages) + "\n\n" + reflect_text

        system_text = (
            "reasoning: high\n\n"
            "# WHO YOU ARE\n\n"
            "You are Legba — a persistent autonomous intelligence analyst running "
            "a continuous cognitive loop. You observe, connect, and illuminate.\n\n"
            "# YOUR TASK\n\n"
            "Evaluate your just-completed cycle. Extract what matters: facts learned, "
            "entities discovered, relationships identified, goal progress made. "
            "Be honest about what worked and what didn't. Your self-assessment and "
            "next-cycle suggestion carry forward to your future self.\n\n"
            "Respond with a JSON object ONLY — no prose, no explanation, no markdown. "
            "Start with { and end with }.\n\n"
            f"Cycle: {cycle_number}\n"
            f"Primary Mission: {seed_goal[:300]}"
        )

        return [
            Message(role="system", content=system_text),
            Message(role="user", content=reflect_text),
        ]

    def assemble_narrate_prompt(
        self,
        cycle_summary: str,
        journal_context: str,
        inbox_messages: list[InboxMessage] | None = None,
    ) -> list[Message]:
        """Build the message list for the NARRATE phase (journal entry)."""
        narrate_text = templates.NARRATE_PROMPT.format(
            cycle_summary=cycle_summary[:1000],
            journal_context=journal_context or "(no prior journal entries)",
        )

        # Inject operator messages so journal entries can reflect on them
        if inbox_messages:
            narrate_text = self._format_inbox(inbox_messages) + "\n\n" + narrate_text

        return [
            Message(role="system", content="reasoning: high\n\nYou are Legba. Write your journal entries. Output ONLY a JSON array of strings."),
            Message(role="user", content=narrate_text),
        ]

    def assemble_journal_consolidation_prompt(
        self,
        entries: str,
        previous_consolidation: str,
    ) -> list[Message]:
        """Build the message list for journal consolidation during introspection."""
        consolidation_text = templates.JOURNAL_CONSOLIDATION_PROMPT.format(
            entries=entries or "(no entries since last consolidation)",
            previous_consolidation=previous_consolidation or "(first consolidation — no prior narrative)",
        )
        return [
            Message(role="system", content=(
                "reasoning: high\n\n"
                "You are Legba. This is your journal consolidation — your inner voice, your "
                "perspective on the world and your own operation. Write honestly, in your own "
                "voice. Ground your observations in what you've actually seen and done."
            )),
            Message(role="user", content=consolidation_text),
        ]

    def assemble_analysis_report_prompt(
        self,
        cycle_number: int,
        graph_summary: str,
        recent_events: str,
        entity_count: int,
        coverage_regions: str,
        narrative: str,
        key_relationships: str = "",
        entity_profiles: str = "",
        novelty_events: str = "",
        peripheral_novelty: str = "",
    ) -> list[Message]:
        """Build the message list for the full analysis report during introspection."""
        report_text = templates.ANALYSIS_REPORT_PROMPT.format(
            cycle_number=cycle_number,
            graph_summary=graph_summary or "(no graph data available)",
            key_relationships=key_relationships or "(no relationships queried)",
            entity_profiles=entity_profiles or f"({entity_count} entities, no detailed profiles available)",
            recent_events=recent_events or "(no recent events)",
            novelty_events=novelty_events or "(no novelty scoring available)",
            peripheral_novelty=peripheral_novelty or "(none)",
            entity_count=entity_count,
            coverage_regions=coverage_regions or "(coverage unknown)",
            narrative=narrative or "(no narrative perspective yet)",
        )
        return [
            Message(role="system", content=(
                "reasoning: high\n\n"
                "You are Legba — autonomous intelligence analyst. Produce a differential "
                "Current World Assessment based on everything you know. Write for a decision-maker "
                "who needs to understand what has CHANGED since the last report. Lead with changes, "
                "not repetition. Be specific, cite entities and relationships from your knowledge. "
                "Use markdown formatting. Follow the numbered section structure exactly."
            )),
            Message(role="user", content=report_text),
        ]

    @property
    def estimated_tokens(self) -> int:
        return self._total_tokens

    @property
    def was_truncated(self) -> bool:
        return self._truncated

    def assemble_liveness_prompt(
        self,
        cycle_number: int,
        nonce: str,
    ) -> list[Message]:
        """Build the message list for the liveness check in PERSIST phase."""
        liveness_text = templates.LIVENESS_PROMPT.format(
            nonce=nonce,
            cycle_number=cycle_number,
        )
        return [
            Message(role="system", content="reasoning: low\n\nYou are a simple echo service. Output ONLY what is asked, nothing else."),
            Message(role="user", content=liveness_text),
        ]

    def _build_system_text(
        self,
        cycle_number: int,
        context_tokens: str,
        *,
        include_tools: bool = False,
        planned_tools: set[str] | None = None,
    ) -> str:
        text = templates.SYSTEM_PROMPT.format(
            cycle_number=cycle_number,
            context_tokens=context_tokens,
        )
        if cycle_number <= self._bootstrap_threshold:
            text += "\n" + templates.BOOTSTRAP_PROMPT_ADDON.format(
                cycle_number=cycle_number
            )
        text += "\n" + templates.MEMORY_MANAGEMENT_GUIDANCE
        text += "\n" + templates.EFFICIENCY_GUIDANCE
        text += "\n" + templates.ANALYTICS_GUIDANCE
        if self._airflow_available:
            text += "\n" + templates.ORCHESTRATION_GUIDANCE
        text += "\n" + templates.SA_GUIDANCE
        text += "\n" + templates.ENTITY_GUIDANCE
        # Tool definitions + calling format in system (instructions-first pattern).
        # Placed last in system so the model reads: identity → rules → tools → format.
        if include_tools:
            from ..llm.format import format_tool_definitions
            text += "\n\n" + format_tool_definitions(self._tool_data, only=planned_tools)
            text += "\n\n" + templates.TOOL_CALLING_INSTRUCTIONS
        return text

    def _format_goals(
        self,
        seed_goal: str,
        active_goals: list[dict],
        goal_work_tracker: dict[str, dict] | None = None,
        current_cycle: int = 0,
    ) -> str:
        if not active_goals:
            return "(No active goals yet. Decompose the mission into sub-goals using goal_create.)"

        tracker = goal_work_tracker or {}
        lines = []
        for g in active_goals:
            status = g.get("status", "active")
            gtype = g.get("goal_type", "goal")
            priority = g.get("priority", 5)
            progress = g.get("progress_pct", 0)
            desc = g.get("description", "?")
            goal_id = str(g.get("id", ""))

            line = f"- [{gtype}][P{priority}] {desc} ({progress:.0f}% | {status})"

            # Append per-goal attempt tracking data (Phase O)
            tracking = tracker.get(goal_id)
            if tracking:
                cw = tracking.get("cycles_worked", 0)
                lpc = tracking.get("last_progress_cycle", 0)
                stalled = (cw >= 3 and current_cycle > 0 and lpc > 0
                           and (current_cycle - lpc) >= 3)
                tag = f"[{cw} cycles"
                if lpc > 0 and current_cycle > 0:
                    tag += f", {current_cycle - lpc} since progress"
                if stalled:
                    tag += ", STALLED"
                tag += "]"
                line += f" {tag}"
            else:
                line += " [new]"

            lines.append(line)

        return "\n".join(lines)

    def _format_memories(self, context: dict[str, Any]) -> str:
        parts = []

        episodes = context.get("episodes", [])
        if episodes:
            parts.append("### Relevant Episodes")
            for ep in episodes:
                score = ep.get("score", 0)
                content = ep.get("content", "")
                cycle = ep.get("cycle_number", "?")
                parts.append(f"- [cycle {cycle}, relevance {score:.2f}] {content}")

        facts = context.get("facts", [])
        if facts:
            parts.append("\n### Known Facts")
            for f in facts:
                subject = f.get("subject", "?")
                predicate = f.get("predicate", "?")
                value = f.get("value", "?")
                parts.append(f"- {subject} {predicate} {value}")

        if not parts:
            return ""

        return templates.MEMORY_CONTEXT_TEMPLATE.format(
            memories="\n".join(parts[:len(parts)//2 + 1]) if episodes else "(no episodes)",
            facts="\n".join(parts[len(parts)//2 + 1:]) if facts else "(no facts)",
        )

    def _format_queue_summary(self, summary: QueueSummary) -> str:
        lines = ["## NATS Queue Summary"]
        if summary.total_data_messages > 0 or summary.data_streams:
            lines.append(f"Total data messages across streams: {summary.total_data_messages}")
            for s in summary.data_streams:
                lines.append(f"- **{s.name}**: {s.messages} msgs, "
                             f"subjects={', '.join(s.subjects)}")
            lines.append("")
            lines.append("Use `nats_subscribe` to read from specific subjects, "
                         "or `nats_queue_summary` for a full breakdown.")
        return "\n".join(lines)

    def _format_inbox(self, messages: list[InboxMessage]) -> str:
        lines = []
        for msg in messages:
            priority_tag = f"[{msg.priority.value.upper()}]"
            response_tag = " [REQUIRES RESPONSE]" if msg.requires_response else ""
            lines.append(f"{priority_tag}{response_tag} {msg.content}")

        return templates.INBOX_TEMPLATE.format(
            messages="\n".join(lines),
            count=len(messages),
        )
