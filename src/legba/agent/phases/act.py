"""ACT phase — LLM reasoning with tool execution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...shared.schemas.comms import InboxMessage

if TYPE_CHECKING:
    from ..cycle import AgentCycle


class ActMixin:
    """Phase 4: LLM reasoning with tool execution."""

    async def _reason_and_act(self: AgentCycle) -> None:
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
