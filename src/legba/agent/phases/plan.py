"""PLAN phase — decide what to do this cycle."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ...shared.schemas.comms import InboxMessage

if TYPE_CHECKING:
    from ..cycle import AgentCycle


class PlanMixin:
    """Phase 3: Decide what to do this cycle."""

    async def _plan(self: AgentCycle) -> None:
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
        for line in reversed(plan_text.splitlines()):
            m = re.match(r'^tools\s*:\s*(.+)', line.strip(), re.IGNORECASE)
            if m:
                names = {t.strip() for t in m.group(1).split(",") if t.strip()}
                names.add("explain_tool")  # always available for on-demand lookup
                return names
        return None
