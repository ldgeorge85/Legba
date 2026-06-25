# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Analyst-trigger dispatch — the seam from a fire decision to an analyst run.

When :mod:`.coalescer` decides a (analyst, target) pair should fire, it hands a
:class:`TriggerFire` to an :class:`AnalystTriggerRunner`. The runner is the
boundary between the trigger plane (P-10) and the analyst-execution plane
(deterministic handlers now; the LLM analyst method runner later). P-10 owns
the *triggering*; it does NOT own analyst execution — so the runner is a
protocol with a thin in-process implementation for the deterministic case.

A fire carries the WHOLE batch (``pending_count`` signals accumulated since the
last fire), never one signal — that is the coalescing guarantee. The runner
reads the target's matched slice itself (via the W2 subscription engine /
substrate); the fire just says "this pair is ready, here's why and how big".

LLM SAFETY: the policy layer already forbids an LLM analyst from firing per
signal (its accumulation is floored, severity-wake is opt-in). As a second,
independent belt-and-braces guard, :class:`DeterministicTriggerRunner` refuses
to run anything whose ``method_kind`` is LLM-bearing — the deterministic runner
is for deterministic analysts only, full stop. An LLM analyst routes to a
separate (future) runner that batches; it is wired the same way but is out of
P-10's "prove with deterministic analysts FIRST" scope.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from .policy import TriggerReason

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TriggerFire:
    """A fully-coalesced fire event for one (analyst, target) pair."""

    analyst_id: str
    target_id: str
    tenant: str
    reason: TriggerReason
    pending_count: int        # how many NEW matching signals accumulated
    severity_wake: bool       # True iff this fire was the immediate severity gate
    fired_at: datetime
    method_kind: str = "deterministic"
    # The canonical ids that drove this fire (best-effort, may be capped). Lets
    # a runner scope its batch read; empty means "read the whole matched slice".
    signal_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TriggerRunResult:
    """A runner's outcome — what the coalescer logs / counts."""

    analyst_id: str
    target_id: str
    status: str               # "ran" | "skipped" | "failed"
    reason: TriggerReason
    pending_count: int
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@runtime_checkable
class AnalystTriggerRunner(Protocol):
    """The boundary the coalescer dispatches a fire to."""

    async def run(self, fire: TriggerFire) -> TriggerRunResult: ...


# Method kinds that are pure-code (no LLM in the critical path). Anything else
# is LLM-bearing and must NOT reach the deterministic runner.
_DETERMINISTIC_KINDS = frozenset({"deterministic", "stat_forecaster", "dspy_compile"})


def is_llm_method(method_kind: str) -> bool:
    return method_kind not in _DETERMINISTIC_KINDS


# A thin callable a test / the registry supplies: given a fire, do the analyst's
# deterministic work and return an opaque detail dict. Keeps the runner
# decoupled from the deterministic-handler dispatcher (which the analyst-method
# runtime owns) — P-10 only needs the SEAM to be complete + one working
# example.
DeterministicWork = Callable[[TriggerFire], Awaitable[dict[str, Any]]]


class DeterministicTriggerRunner:
    """Runs a deterministic analyst when its trigger fires.

    Belt-and-braces LLM guard: refuses any LLM-bearing ``method_kind`` (raises
    at dispatch — that fire should never have been routed here). The actual
    analyst work is the injected :class:`DeterministicWork` callable; the P-10
    deliverable is the complete trigger→run seam, with the deterministic
    handler library minimal (one working example), exactly per the task.
    """

    def __init__(self, work: DeterministicWork) -> None:
        self._work = work
        self.runs = 0
        self.fires: list[TriggerFire] = []

    async def run(self, fire: TriggerFire) -> TriggerRunResult:
        if is_llm_method(fire.method_kind):
            # This is the hard rule — an LLM analyst must NEVER fire per signal
            # and must never be dispatched through the per-signal-driven
            # deterministic runner. The policy layer should have prevented this;
            # if it didn't, fail loud rather than fan out an LLM call.
            raise ValueError(
                f"LLM-bearing analyst {fire.analyst_id!r} (method_kind="
                f"{fire.method_kind!r}) routed to the deterministic trigger "
                "runner — LLM analysts never fire per-signal"
            )
        self.fires.append(fire)
        self.runs += 1
        try:
            detail = await self._work(fire)
        except Exception as exc:  # a handler crash is non-fatal to the engine
            logger.exception(
                "trigger.run.failed analyst=%s target=%s: %s",
                fire.analyst_id, fire.target_id, exc,
            )
            return TriggerRunResult(
                analyst_id=fire.analyst_id,
                target_id=fire.target_id,
                status="failed",
                reason=fire.reason,
                pending_count=fire.pending_count,
                error=str(exc),
            )
        return TriggerRunResult(
            analyst_id=fire.analyst_id,
            target_id=fire.target_id,
            status="ran",
            reason=fire.reason,
            pending_count=fire.pending_count,
            detail=detail or {},
        )


class ActorTriggerRunner:
    """Dispatches a fire to the analyst's actor run — for ANY method kind.

    The injected ``work`` routes the (analyst, target) fire to its
    :class:`AnalystActor` via an ActorProxy ``run`` (see
    ``source_first_runtime.build_trigger_work``). The actor is the correct
    execution path for deterministic AND LLM-bearing analysts alike — it reads
    the matched slice, runs its method, and writes provenance + receipts.

    Unlike :class:`DeterministicTriggerRunner`, this does NOT refuse LLM fires.
    The "LLM analysts never fire per-signal" rule is enforced UPSTREAM in the
    policy kernel (``effective_accumulation`` floored to ``min_llm_batch``, the
    severity gate opt-in) — so by the time a fire reaches here it is already a
    coalesced batch, safe to run an LLM analyst on. The runner only owns the
    seam to execution; the batching guarantee lives in :mod:`.policy`.

    A handler crash is non-fatal to the engine: it is logged and counted as a
    failed run (the window already reset on the CAS fire-claim, so the next
    window starts fresh and the actor's own per-(analyst, target) cooldown
    dedups against a near-simultaneous cadence run).
    """

    def __init__(self, work: DeterministicWork) -> None:
        self._work = work
        self.runs = 0
        self.fires: list[TriggerFire] = []

    async def run(self, fire: TriggerFire) -> TriggerRunResult:
        self.fires.append(fire)
        self.runs += 1
        try:
            detail = await self._work(fire)
        except Exception as exc:  # a handler/actor crash is non-fatal to the loop
            logger.exception(
                "trigger.run.failed analyst=%s target=%s: %s",
                fire.analyst_id, fire.target_id, exc,
            )
            return TriggerRunResult(
                analyst_id=fire.analyst_id,
                target_id=fire.target_id,
                status="failed",
                reason=fire.reason,
                pending_count=fire.pending_count,
                error=str(exc),
            )
        return TriggerRunResult(
            analyst_id=fire.analyst_id,
            target_id=fire.target_id,
            status="ran",
            reason=fire.reason,
            pending_count=fire.pending_count,
            detail=detail or {},
        )


__all__ = [
    "TriggerFire",
    "TriggerRunResult",
    "AnalystTriggerRunner",
    "DeterministicTriggerRunner",
    "ActorTriggerRunner",
    "DeterministicWork",
    "is_llm_method",
]
