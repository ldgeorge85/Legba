"""
Core Agent Cycle

WAKE → ORIENT → [ACQUIRE|ANALYZE|RESEARCH|INTROSPECTION|PLAN→ACT] → REFLECT → NARRATE → PERSIST

One cycle = one execution of this module. The supervisor manages the lifecycle:
it launches the agent for a single cycle, the agent runs through all phases,
emits a heartbeat, and exits. Changes take effect next cycle.

Phase logic lives in phases/*.py as mixin classes. This module wires them
together into a single AgentCycle class and owns the top-level orchestration.

Cycle type routing (evaluated in priority order):
  - Every 15 cycles: INTROSPECTION (deep audit, reports, journal consolidation)
  - Every 10 cycles: ANALYSIS (analytics, pattern detection, graph mining)
  - Every 5 cycles:  RESEARCH (entity enrichment, gap-filling)
  - Every 3 cycles:  ACQUIRE (dedicated source fetching + event ingestion)
  - Otherwise:       NORMAL (goal-directed, mixed)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..shared.config import LegbaConfig
from ..shared.schemas.cycle import CycleResponse, CycleState
from ..shared.schemas.comms import OutboxMessage
from .llm.client import LLMClient
from .llm.format import Message
from .memory.manager import MemoryManager
from .goals.manager import GoalManager
from .tools.registry import ToolRegistry
from .tools.executor import ToolExecutor
from .comms.nats_client import LegbaNatsClient
from .comms.airflow_client import AirflowClient
from .memory.opensearch import OpenSearchStore
from .prompt.assembler import PromptAssembler
from .selfmod.engine import SelfModEngine
from .log import CycleLogger

# Phase mixins
from .phases.wake import WakeMixin
from .phases.orient import OrientMixin
from .phases.plan import PlanMixin
from .phases.act import ActMixin
from .phases.reflect import ReflectMixin
from .phases.narrate import NarrateMixin
from .phases.persist import PersistMixin
from .phases.introspect import IntrospectMixin
from .phases.research import ResearchMixin
from .phases.acquire import AcquireMixin
from .phases.analyze import AnalyzeMixin

# Re-export constants for backward compatibility
from .phases import REPORT_INTERVAL, RESEARCH_INTERVAL, ACQUIRE_INTERVAL, ANALYSIS_INTERVAL


class AgentCycle(
    WakeMixin,
    OrientMixin,
    PlanMixin,
    ActMixin,
    ReflectMixin,
    NarrateMixin,
    PersistMixin,
    IntrospectMixin,
    ResearchMixin,
    AcquireMixin,
    AnalyzeMixin,
):
    """
    Executes a single agent cycle.

    Wires together: LLM client, memory manager, goal manager, tool executor,
    prompt assembler, self-mod engine, and the cycle logger.

    Phase logic is implemented in mixin classes (phases/*.py).
    This class owns initialization, top-level orchestration, and shutdown helpers.
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

            # Cycle type routing — evaluated in priority order.
            # Higher-priority types take precedence when intervals overlap.
            if self._is_introspection_cycle():
                await self._mission_review()
                await self._reflect()
                await self._narrate()
                await self._journal_consolidation()
                await self._generate_analysis_report()
            elif self._is_analysis_cycle():
                await self._analyze()
                await self._reflect()
                await self._narrate()
            elif self._is_research_cycle():
                await self._research()
                await self._reflect()
                await self._narrate()
            elif self._is_acquire_cycle():
                await self._acquire()
                await self._reflect()
                await self._narrate()
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
