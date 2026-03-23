"""
Core Agent Cycle

WAKE → ORIENT → [EVOLVE|INTROSPECTION|SYNTHESIZE|ANALYSIS|RESEARCH|CURATE|SURVEY] → REFLECT → NARRATE → PERSIST

One cycle = one execution of this module. The supervisor manages the lifecycle:
it launches the agent for a single cycle, the agent runs through all phases,
emits a heartbeat, and exits. Changes take effect next cycle.

Phase logic lives in phases/*.py as mixin classes. This module wires them
together into a single AgentCycle class and owns the top-level orchestration.

3-tier cycle type routing:
  Tier 1 — Scheduled outputs (fixed intervals):
    EVOLVE(30) > INTROSPECTION(15) > SYNTHESIZE(10)
  Tier 2 — Guaranteed work (coprime modulo intervals):
    ANALYSIS(4) > RESEARCH(7) > CURATE(9)
  Tier 3 — Dynamic fill (state-scored):
    CURATE (capped 0.45, recent 24h backlog) vs SURVEY (0.50 default)

CURATE runs _curate() when ingestion active, _acquire() as legacy fallback.
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
from .phases.curate import CurateMixin
from .phases.evolve import EvolveMixin
from .phases.survey import SurveyMixin
from .phases.synthesize import SynthesizeMixin

# Re-export constants for backward compatibility
from .phases import (REPORT_INTERVAL, RESEARCH_INTERVAL, ACQUIRE_INTERVAL,
                     CURATE_INTERVAL, ANALYSIS_INTERVAL, EVOLVE_INTERVAL,
                     SYNTHESIZE_INTERVAL)


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
    CurateMixin,
    AnalyzeMixin,
    EvolveMixin,
    SurveyMixin,
    SynthesizeMixin,
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

            # Worker mode: if CYCLE_TYPE env var is set, bypass interval routing
            # and run only that cycle type. Enables parallel workers.
            forced_type = os.environ.get("CYCLE_TYPE", "").strip().lower()
            if forced_type:
                await self._run_forced_cycle_type(forced_type)
                return await self._persist()

            # Hybrid cycle routing:
            #   Scheduled outputs: EVOLVE(30), INTROSPECTION(15), SYNTHESIZE(10)
            #   Dynamic work: state-scored selection from SURVEY/ANALYSIS/RESEARCH/CURATE
            cycle_type = self._select_cycle_type()
            self._selected_cycle_type = cycle_type
            self._write_cycle_type(cycle_type)
            self.logger.log("cycle_type_selected",
                           cycle_type=cycle_type,
                           cycle_number=self.state.cycle_number)

            if cycle_type == "EVOLVE":
                await self._evolve()
                await self._reflect()
                await self._narrate()
                await self._journal_consolidation()
                await self._generate_analysis_report()
            elif cycle_type == "INTROSPECTION":
                await self._mission_review()
                await self._reflect()
                await self._narrate()
                await self._journal_consolidation()
                await self._generate_analysis_report()
            elif cycle_type == "SYNTHESIZE":
                await self._synthesize()
                await self._reflect()
                await self._narrate()
            elif cycle_type == "ANALYSIS":
                await self._analyze()
                await self._reflect()
                await self._narrate()
            elif cycle_type == "RESEARCH":
                await self._research()
                await self._reflect()
                await self._narrate()
            elif cycle_type == "CURATE":
                if self._ingestion_service_active():
                    await self._curate()
                else:
                    await self._acquire()
                await self._reflect()
                await self._narrate()
            else:  # SURVEY
                await self._survey()
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
    # Hybrid cycle type selection
    # -----------------------------------------------------------------------

    def _select_cycle_type(self) -> str:
        """Select cycle type via 3-tier routing.

        Tier 1 — Scheduled outputs (fixed intervals, highest priority):
          EVOLVE(30) > INTROSPECTION(15) > SYNTHESIZE(10)

        Tier 2 — Guaranteed work (modulo floor, ensures all types fire):
          ANALYSIS(5) > RESEARCH(7) > CURATE(9)
          These fire on their interval unless a Tier 1 type already claimed it.

        Tier 3 — Dynamic fill (state-scored, remaining cycles):
          Scores CURATE vs SURVEY based on recent uncurated backlog.
          CURATE score capped at 0.45 to prevent monopolization.
          Dedicated CURATE workers (CYCLE_TYPE=CURATE) handle overflow.
        """
        cn = self.state.cycle_number

        # --- Tier 1: Scheduled outputs (non-negotiable) ---
        if cn > 0 and cn % EVOLVE_INTERVAL == 0:
            return "EVOLVE"
        if cn > 0 and cn % 15 == 0:
            return "INTROSPECTION"
        if cn > 0 and cn % SYNTHESIZE_INTERVAL == 0:
            return "SYNTHESIZE"

        # --- Tier 2: Guaranteed work (modulo floor) ---
        if cn > 0 and cn % ANALYSIS_INTERVAL == 0:
            return "ANALYSIS"
        if cn > 0 and cn % RESEARCH_INTERVAL == 0:
            return "RESEARCH"
        if cn > 0 and cn % CURATE_INTERVAL == 0:
            return "CURATE"

        # --- Tier 3: Dynamic fill (CURATE promotion vs SURVEY) ---
        scores = {}

        # CURATE: recent uncurated backlog (last 24h signals without event links).
        # Capped at config value (default 0.45) — SURVEY should win unless
        # there is a genuine backlog needing attention.
        uncurated = getattr(self, '_uncurated_count', 0)
        curate_cap = self.config.agent.curate_score_cap
        scores["CURATE"] = min(uncurated / 80.0, curate_cap) if uncurated > 30 else 0.0

        # SURVEY: analytical desk work — must run regularly for hypothesis eval,
        # situation linking, and graph maintenance.  Base score (default 0.50)
        # ensures it wins most Tier-3 slots unless backlog is high.
        scores["SURVEY"] = self.config.agent.survey_base_score

        # Cooldown: don't repeat the same dynamic type back-to-back
        last_types = getattr(self, '_recent_cycle_types', [])
        if last_types and last_types[-1] in scores:
            scores[last_types[-1]] *= 0.5

        selected = max(scores, key=scores.get)

        # Track for cooldown
        if not hasattr(self, '_recent_cycle_types'):
            self._recent_cycle_types = []
        self._recent_cycle_types.append(selected)
        if len(self._recent_cycle_types) > 5:
            self._recent_cycle_types = self._recent_cycle_types[-5:]

        # Log scores for debugging
        self.logger.log("cycle_scores",
                       scores={k: round(v, 2) for k, v in scores.items()},
                       selected=selected,
                       uncurated=uncurated)

        return selected

    # -----------------------------------------------------------------------
    # Worker mode: forced cycle type via CYCLE_TYPE env var
    # -----------------------------------------------------------------------

    async def _run_forced_cycle_type(self, cycle_type: str) -> None:
        """Run a specific cycle type, bypassing interval routing.

        Used when CYCLE_TYPE env var is set for parallel worker mode.
        """
        _CYCLE_MAP = {
            "evolve": self._run_evolve_sequence,
            "introspection": self._run_introspection_sequence,
            "synthesize": self._run_synthesize_sequence,
            "analysis": self._run_analysis_sequence,
            "research": self._run_research_sequence,
            "curate": self._run_curate_sequence,
            "survey": self._run_survey_sequence,
        }

        runner = _CYCLE_MAP.get(cycle_type)
        if not runner:
            self.logger.log_error(
                f"Unknown CYCLE_TYPE '{cycle_type}'. "
                f"Valid: {', '.join(sorted(_CYCLE_MAP))}"
            )
            return

        self._selected_cycle_type = cycle_type.upper()
        self._write_cycle_type(cycle_type.upper())
        self.logger.log("forced_cycle_type", cycle_type=cycle_type)
        await runner()

    async def _run_evolve_sequence(self):
        await self._evolve()
        await self._reflect()
        await self._narrate()
        await self._journal_consolidation()
        await self._generate_analysis_report()

    async def _run_introspection_sequence(self):
        await self._mission_review()
        await self._reflect()
        await self._narrate()
        await self._journal_consolidation()
        await self._generate_analysis_report()

    async def _run_synthesize_sequence(self):
        await self._synthesize()
        await self._reflect()
        await self._narrate()

    async def _run_analysis_sequence(self):
        await self._analyze()
        await self._reflect()
        await self._narrate()

    async def _run_research_sequence(self):
        await self._research()
        await self._reflect()
        await self._narrate()

    async def _run_curate_sequence(self):
        if self._ingestion_service_active():
            await self._curate()
        else:
            await self._acquire()
        await self._reflect()
        await self._narrate()

    async def _run_survey_sequence(self):
        if self._should_promote_to_curate():
            self.logger.log("curate_promotion",
                           uncurated=getattr(self, '_uncurated_count', 0))
            await self._curate()
        else:
            await self._survey()
        await self._reflect()
        await self._narrate()

    # -----------------------------------------------------------------------
    # Curate scheduling helpers
    # -----------------------------------------------------------------------

    def _is_curate_cycle(self: AgentCycle) -> bool:
        """Check if this is a curate cycle (replaces old _is_acquire_cycle in routing).

        Runs every CURATE_INTERVAL cycles, but yields to all higher-priority types.
        """
        cn = self.state.cycle_number
        return (CURATE_INTERVAL > 0
                and cn > 0
                and cn % CURATE_INTERVAL == 0
                and not self._is_evolve_cycle()
                and not self._is_introspection_cycle()
                and not self._is_synthesize_cycle()
                and not self._is_analysis_cycle()
                and not self._is_research_cycle())

    def _should_promote_to_curate(self) -> bool:
        """Check if uncurated backlog warrants promoting SURVEY to CURATE."""
        return getattr(self, '_uncurated_count', 0) > 100

    # -----------------------------------------------------------------------
    # Graceful shutdown support
    # -----------------------------------------------------------------------

    def _write_cycle_type(self, cycle_type: str) -> None:
        """Write cycle type to shared volume so the supervisor can read it."""
        path = os.path.join(self.config.paths.shared, "cycle_type.json")
        try:
            with open(path, "w") as f:
                json.dump({"cycle_type": cycle_type, "cycle": self.state.cycle_number}, f)
        except OSError:
            pass

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
        for name in ("stop_flag.json", "stop_ping.json", "cycle_type.json"):
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
