# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.runtime.triggers — coalescing trigger + event-driven analyst dispatch.

The W3 triggering plane on top of the W2 source-first runtime (PIVOT §4.6,
task P-10). An analyst over a target does NOT run per signal. A new matching
signal (from the W2 subscription engine) or a new upstream finding marks the
(analyst, target) pair **dirty**; the analyst fires on whichever of three gates
trips first — **cadence** tick, **accumulation** threshold (N new), or
**severity** gate (a critical signal wakes immediately) — all clamped by a
**cooldown** so a high-volume source can't thrash the analyst.

Layers (inner → outer):

  * :mod:`.policy`    — the pure deterministic decision kernel (no I/O):
                        ``TriggerPolicy`` (the gates + cooldown) + ``decide()``.
                        Enforces the hard rule "LLM analysts NEVER fire per
                        signal" (accumulation floored, severity-wake opt-in).
  * :mod:`.state`     — crash-safe ``trigger_state`` (Postgres) so the dirty
                        accumulator + cooldown anchor SURVIVE A RESTART. CAS on
                        the fire anchor prevents double-dispatch.
  * :mod:`.dispatch`  — ``AnalystTriggerRunner`` protocol + a deterministic
                        in-process runner (belt-and-braces LLM-method refusal).
  * :mod:`.coalescer` — the I/O-bearing mechanism (testable against the dev rig
                        with no loop): signal → dirty (dedup-aware) → decide →
                        CAS-claim → dispatch.
  * :mod:`.engine`    — the thin NATS-driven wrapper: per-target consumer +
                        cadence ticker feeding the coalescer.

The mechanism is the deliverable; the deterministic-handler library is minimal
(one working example wired through the runner). LLM analysts route to a
separate batched runner (out of P-10 scope), wired the same way.
"""

from __future__ import annotations

from .coalescer import Coalescer, PolicyResolver, canonical_id_of, severity_of
from .dispatch import (
    ActorTriggerRunner,
    AnalystTriggerRunner,
    DeterministicTriggerRunner,
    DeterministicWork,
    TriggerFire,
    TriggerRunResult,
    is_llm_method,
)
from .engine import TriggerEngine, TriggerRegistration
from .policy import (
    TriggerAccumulator,
    TriggerDecision,
    TriggerPolicy,
    TriggerReason,
    apply_dirty,
    apply_fire,
    decide,
    policy_from_descriptor,
    severity_rank,
)
from .state import TriggerStateStore

__all__ = [
    # policy
    "TriggerPolicy",
    "TriggerAccumulator",
    "TriggerDecision",
    "TriggerReason",
    "decide",
    "apply_dirty",
    "apply_fire",
    "policy_from_descriptor",
    "severity_rank",
    # state
    "TriggerStateStore",
    # dispatch
    "AnalystTriggerRunner",
    "DeterministicTriggerRunner",
    "ActorTriggerRunner",
    "DeterministicWork",
    "TriggerFire",
    "TriggerRunResult",
    "is_llm_method",
    # coalescer
    "Coalescer",
    "PolicyResolver",
    "canonical_id_of",
    "severity_of",
    # engine
    "TriggerEngine",
    "TriggerRegistration",
]
