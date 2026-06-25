# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Coalescing-trigger policy — the deterministic decision kernel (P-10).

PIVOT §4.6 / the P-10 task: an analyst over a target does NOT run per signal.
A new matching signal (from the W2 subscription engine) or a new upstream
finding marks the (analyst, target) pair **dirty**; the analyst fires on
whichever of three gates trips first, bounded by a cooldown:

  * **cadence**       — a periodic tick (``cadence_seconds``). The floor — even
                        a quiet target gets re-evaluated on a schedule so a
                        slow drip of sub-threshold signals eventually fires.
  * **accumulation**  — ``N`` new matching signals have piled up since the last
                        fire (``accumulation_threshold``). The batch gate — a
                        busy target fires as soon as it has enough to chew on,
                        without waiting for the cadence tick.
  * **severity**      — a single signal at/above ``severity_gate`` wakes the
                        analyst IMMEDIATELY (a critical signal can't wait for a
                        batch to fill or a tick to land). The escape hatch.

…all clamped by **cooldown**: after a fire the pair is muted for
``cooldown_seconds``. A high-volume source therefore can't thrash the analyst
— between fires, dirt accumulates but no fire happens until the cooldown ends.

This module is a PURE function over the persisted accumulator + the policy.
No I/O, no clock reads beyond the ``now`` passed in — so the five P-10
deterministic behaviours (severity-wake, batch, cooldown-cap, restart-survives,
alias-no-double-wake) are exercised by feeding it explicit states + clocks.
The coalescer (:mod:`.coalescer`) owns the I/O and calls in here to decide.

LLM SAFETY (hard rule): an LLM-bearing analyst NEVER fires per signal. The
severity gate is the only sub-cadence wake, and even it is forbidden for LLM
analysts unless the operator explicitly opts in via
``allow_llm_severity_wake``. The default keeps LLM analysts on the cadence /
accumulation gates only — :func:`policy_from_descriptor` enforces this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any


# Severity ladder — mirrors legba.data.predicates.helpers._SEVERITY_ORDER and
# AlertPayload.severity. Duplicated (not imported) so the kernel stays pure +
# import-light; the values are a frozen platform convention.
SEVERITY_ORDER: dict[str, int] = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


def severity_rank(value: object) -> int:
    """Rank a severity label; unknown / non-string → ``-1`` (below ``info``)."""
    if not isinstance(value, str):
        return -1
    return SEVERITY_ORDER.get(value.lower(), -1)


class TriggerReason(str, Enum):
    """Why a fire decision was reached (or why it was held)."""

    SEVERITY = "severity"          # a signal at/above the gate woke it now
    ACCUMULATION = "accumulation"  # N new matching signals piled up
    CADENCE = "cadence"            # the periodic tick landed
    COOLDOWN = "cooldown"          # held — still inside the cooldown window
    NOT_DIRTY = "not_dirty"        # held — nothing new + no tick due
    BELOW_THRESHOLD = "below_threshold"  # held — dirty but no gate tripped yet


# Reasons that mean "fire now".
_FIRE_REASONS = frozenset(
    {TriggerReason.SEVERITY, TriggerReason.ACCUMULATION, TriggerReason.CADENCE}
)


@dataclass(frozen=True)
class TriggerPolicy:
    """The three gates + the cooldown clamp for one (analyst, target) pair.

    Derived from the analyst descriptor's ``cadence`` block
    (:func:`policy_from_descriptor`), but a plain value object so the kernel is
    testable without a descriptor.

    * ``cadence_seconds``        — periodic-tick floor; ``0`` disables cadence.
    * ``accumulation_threshold`` — fire when this many NEW matching signals have
                                   accumulated since the last fire; ``0``/``1``
                                   means "every new signal is enough" (only for
                                   deterministic analysts — see the LLM guard).
    * ``severity_gate``          — minimum severity label that wakes immediately
                                   (``None`` disables the severity gate).
    * ``cooldown_seconds``       — minimum spacing between fires; ``0`` allows
                                   back-to-back fires (no thrash protection).
    * ``is_llm``                 — True for an LLM-bearing analyst. Guards: an
                                   LLM analyst NEVER fires per-signal, so its
                                   effective accumulation threshold is floored
                                   (``min_llm_batch``) and the severity gate is
                                   disabled unless ``allow_llm_severity_wake``.
    """

    cadence_seconds: int = 0
    accumulation_threshold: int = 1
    severity_gate: str | None = None
    cooldown_seconds: int = 0
    is_llm: bool = False
    allow_llm_severity_wake: bool = False
    # The smallest batch an LLM analyst may fire on. Even accumulation=1 on an
    # LLM analyst is clamped up to this so an LLM never fans out per signal.
    min_llm_batch: int = 2

    @property
    def effective_accumulation(self) -> int:
        """Accumulation threshold after the LLM per-signal guard.

        A deterministic analyst honours the declared threshold (min 1). An LLM
        analyst is floored to ``min_llm_batch`` so a per-signal accumulation
        (``1``) can never make an LLM fan out one call per signal.
        """
        base = max(1, self.accumulation_threshold)
        if self.is_llm:
            return max(base, self.min_llm_batch)
        return base

    @property
    def severity_gate_rank(self) -> int:
        """The rank a signal must reach to wake immediately; ``99`` = disabled.

        The severity gate is disabled (``99`` — unreachable) when no gate is
        declared OR when an LLM analyst hasn't explicitly opted into severity
        wakes (LLM analysts never fire per-signal by default).
        """
        if self.severity_gate is None:
            return 99
        if self.is_llm and not self.allow_llm_severity_wake:
            return 99
        rank = severity_rank(self.severity_gate)
        return rank if rank >= 0 else 99


@dataclass
class TriggerAccumulator:
    """The persisted dirty-state for one (analyst, target) pair.

    Lives in Postgres (:mod:`.state`) so it SURVIVES A RESTART — the whole
    point of the "restart-survives" acceptance behaviour. The coalescer reads
    it, hands it + the policy to :func:`decide`, then writes back the mutated
    accumulator returned by :func:`apply_fire` / :func:`apply_dirty`.

    * ``pending_count``       — NEW matching signals seen since the last fire.
    * ``max_pending_rank``    — highest severity rank among the pending signals
                                (drives the severity gate; reset on fire).
    * ``last_fired_at``       — when the analyst last fired (cooldown anchor +
                                cadence anchor). ``None`` = never fired.
    * ``first_dirty_at``      — when THIS window first went dirty (the first
                                matching signal since the last fire). Anchors
                                the cadence gate for a never-fired pair so the
                                first signal does NOT fire on cadence — only a
                                full ``cadence_seconds`` after the window opened.
                                Reset on fire; set on the first dirty of a clean
                                accumulator.
    * ``seen_signal_ids``     — canonical ids already counted this window, so a
                                duplicate / alias delivery does NOT double-count
                                (the "alias-no-double-wake" behaviour). Bounded.
    """

    pending_count: int = 0
    max_pending_rank: int = -1
    last_fired_at: datetime | None = None
    first_dirty_at: datetime | None = None
    seen_signal_ids: set[str] = field(default_factory=set)
    # Cap the seen-set so a long window can't grow it without bound. When the
    # cap is hit the oldest insertions are dropped (insertion order via list).
    seen_cap: int = 10_000
    _seen_order: list[str] = field(default_factory=list)

    def cooldown_until(self, policy: TriggerPolicy) -> datetime | None:
        if self.last_fired_at is None or policy.cooldown_seconds <= 0:
            return None
        return self.last_fired_at + timedelta(seconds=policy.cooldown_seconds)

    def cadence_due_at(self, policy: TriggerPolicy) -> datetime | None:
        """When the next cadence tick is due (``None`` if cadence disabled).

        Anchored on the LAST FIRE if the pair has fired, else on
        ``first_dirty_at`` (when this window opened). A never-fired, never-dirty
        pair has no anchor → not due. This makes the cadence a true PERIOD: the
        first signal of a fresh window does not fire on cadence; the tick fires
        a full ``cadence_seconds`` after the window opened.
        """
        if policy.cadence_seconds <= 0:
            return None
        anchor = self.last_fired_at or self.first_dirty_at
        if anchor is None:
            return None
        return anchor + timedelta(seconds=policy.cadence_seconds)


@dataclass(frozen=True)
class TriggerDecision:
    """The kernel's verdict for one evaluation."""

    should_fire: bool
    reason: TriggerReason
    pending_count: int
    severity_wake: bool = False  # True iff fired specifically on the severity gate

    @property
    def is_fire(self) -> bool:
        return self.should_fire


# ---------------------------------------------------------------------------
# Accumulator mutation — pure functions returning the new state. The coalescer
# persists the returned accumulator; these never touch I/O.
# ---------------------------------------------------------------------------


def apply_dirty(
    acc: TriggerAccumulator,
    *,
    signal_id: str,
    severity_rank_value: int,
    now: datetime | None = None,
) -> tuple[TriggerAccumulator, bool]:
    """Mark the pair dirty for one delivered matching signal.

    Returns ``(new_accumulator, counted)``. ``counted`` is False when the
    ``signal_id`` was already seen this window — the **alias-no-double-wake**
    guard: the dedup analyst (P-09) routes duplicate observations onto one
    canonical id, and the coalescer passes the CANONICAL id here, so two
    deliveries of the same canonical bump the counter once.

    ``now`` stamps ``first_dirty_at`` when a CLEAN window goes dirty (the
    cadence anchor for a never-fired pair). When omitted the timestamp is left
    unchanged (the kernel tests that don't care about cadence pass nothing).
    """
    if signal_id in acc.seen_signal_ids:
        return acc, False

    seen = set(acc.seen_signal_ids)
    order = list(acc._seen_order)
    seen.add(signal_id)
    order.append(signal_id)
    # Bound the seen-set (drop oldest) so a long, busy window stays memory-safe.
    while len(order) > acc.seen_cap:
        evicted = order.pop(0)
        seen.discard(evicted)

    # Anchor the window on the FIRST dirty signal (when none recorded yet).
    first_dirty = acc.first_dirty_at
    if first_dirty is None and acc.pending_count == 0:
        first_dirty = now

    new = TriggerAccumulator(
        pending_count=acc.pending_count + 1,
        max_pending_rank=max(acc.max_pending_rank, severity_rank_value),
        last_fired_at=acc.last_fired_at,
        first_dirty_at=first_dirty,
        seen_signal_ids=seen,
        seen_cap=acc.seen_cap,
        _seen_order=order,
    )
    return new, True


def apply_fire(acc: TriggerAccumulator, *, fired_at: datetime) -> TriggerAccumulator:
    """Reset the accumulator after a fire — clears pending + opens the cooldown.

    ``seen_signal_ids`` is cleared too: a NEW window starts after a fire, so a
    signal counted in the previous batch can legitimately re-contribute if it
    is re-delivered post-fire (it won't double-count WITHIN a window, which is
    what alias-no-double-wake protects). ``first_dirty_at`` resets — the next
    dirty signal opens the next window's cadence anchor.
    """
    return TriggerAccumulator(
        pending_count=0,
        max_pending_rank=-1,
        last_fired_at=fired_at,
        first_dirty_at=None,
        seen_signal_ids=set(),
        seen_cap=acc.seen_cap,
        _seen_order=[],
    )


# ---------------------------------------------------------------------------
# The decision kernel.
# ---------------------------------------------------------------------------


def decide(
    acc: TriggerAccumulator,
    policy: TriggerPolicy,
    *,
    now: datetime,
) -> TriggerDecision:
    """Decide whether the (analyst, target) pair should fire NOW.

    Evaluation order (first match wins):

      1. **Cooldown clamp.** Inside the cooldown window → never fire, regardless
         of how much dirt or how severe. (Thrash protection — the cooldown-cap
         behaviour.) The ONE exception is that we still report the held reason
         so the caller can re-arm a deferred wake.
      2. **Severity gate.** A pending signal at/above the gate fires immediately
         (severity-wake). Checked before accumulation/cadence so a single
         critical signal beats a half-full batch.
      3. **Accumulation gate.** ``pending_count >= effective_accumulation`` →
         fire (batch).
      4. **Cadence gate.** The periodic tick is due AND there is at least one
         pending signal (a tick over an empty window is a no-op — nothing to
         analyse). ``last_fired_at is None`` makes the first tick due once any
         dirt arrives.
      5. Otherwise hold (dirty-but-below-threshold, or not dirty at all).
    """
    # 1) Cooldown clamp — the hard ceiling on fire frequency.
    cd_until = acc.cooldown_until(policy)
    if cd_until is not None and now < cd_until:
        return TriggerDecision(
            should_fire=False,
            reason=TriggerReason.COOLDOWN,
            pending_count=acc.pending_count,
        )

    has_pending = acc.pending_count > 0

    # 2) Severity gate — a single critical signal wakes immediately.
    if has_pending and acc.max_pending_rank >= policy.severity_gate_rank:
        return TriggerDecision(
            should_fire=True,
            reason=TriggerReason.SEVERITY,
            pending_count=acc.pending_count,
            severity_wake=True,
        )

    # 3) Accumulation gate — N new matching signals piled up.
    if acc.pending_count >= policy.effective_accumulation:
        return TriggerDecision(
            should_fire=True,
            reason=TriggerReason.ACCUMULATION,
            pending_count=acc.pending_count,
        )

    # 4) Cadence gate — periodic tick, but only with something to analyse.
    cadence_due = acc.cadence_due_at(policy)
    if has_pending and cadence_due is not None and now >= cadence_due:
        return TriggerDecision(
            should_fire=True,
            reason=TriggerReason.CADENCE,
            pending_count=acc.pending_count,
        )

    # 5) Hold.
    return TriggerDecision(
        should_fire=False,
        reason=(
            TriggerReason.BELOW_THRESHOLD if has_pending else TriggerReason.NOT_DIRTY
        ),
        pending_count=acc.pending_count,
    )


# ---------------------------------------------------------------------------
# Descriptor → policy mapping.
# ---------------------------------------------------------------------------


# Defaults when the analyst's cadence block under-specifies. A deterministic
# analyst with no explicit trigger config fires on every matching signal
# (accumulation=1) with no cooldown — the coalescer is still in the path
# (dirty-bit + dedup), just permissive. Operators tune via the cadence block.
_DEFAULT_DET_ACCUMULATION = 1
_DEFAULT_LLM_MIN_BATCH = 2


def policy_from_descriptor(
    *,
    cadence: Any,
    method_kind: str,
    accumulation_threshold: int | None = None,
    severity_gate: str | None = None,
    allow_llm_severity_wake: bool = False,
) -> TriggerPolicy:
    """Build a :class:`TriggerPolicy` from an analyst descriptor's cadence block.

    ``cadence`` is the analyst ``CadenceBlock`` (frozen contract): it carries
    ``cooldown_seconds`` and ``fallback_schedule`` (a cron we coarsen to a
    cadence-seconds floor) and the ``trigger`` predicate. The accumulation
    threshold + severity gate are P-10 extensions passed alongside (they live
    in operator config / the trigger registry, NOT in the frozen schema).

    ``method_kind`` selects the LLM guard: any kind other than ``deterministic``
    /``stat_forecaster``/``dspy_compile`` is treated as LLM-bearing, so its
    accumulation is floored to ``min_llm_batch`` and the severity gate is
    disabled unless explicitly allowed. This is the hard rule "LLM analysts
    must NEVER fire per-signal" enforced in code, not just by convention.
    """
    is_llm = method_kind not in ("deterministic", "stat_forecaster", "dspy_compile")

    cooldown = int(getattr(cadence, "cooldown_seconds", 0) or 0)
    cadence_seconds = _coarse_cadence_seconds(
        getattr(cadence, "fallback_schedule", None)
    )

    if accumulation_threshold is None:
        accumulation_threshold = (
            _DEFAULT_LLM_MIN_BATCH if is_llm else _DEFAULT_DET_ACCUMULATION
        )

    return TriggerPolicy(
        cadence_seconds=cadence_seconds,
        accumulation_threshold=accumulation_threshold,
        severity_gate=severity_gate,
        cooldown_seconds=cooldown,
        is_llm=is_llm,
        allow_llm_severity_wake=allow_llm_severity_wake,
        min_llm_batch=_DEFAULT_LLM_MIN_BATCH,
    )


# Map a few common cron cadences to a seconds floor. P-10 does not own a cron
# evaluator (runtime/dapr_cron.py does, for poll sources); for the analyst
# cadence FLOOR we only need a coarse period. An unrecognised cron leaves
# cadence disabled (0) — the accumulation/severity gates still fire.
_CRON_TO_SECONDS: dict[str, int] = {
    "* * * * *": 60,
    "*/5 * * * *": 300,
    "*/10 * * * *": 600,
    "*/15 * * * *": 900,
    "*/30 * * * *": 1800,
    "0 * * * *": 3600,
    "0 */6 * * *": 21_600,
    "0 0 * * *": 86_400,
}


def _coarse_cadence_seconds(fallback_schedule: Any) -> int:
    if fallback_schedule is None:
        return 0
    raw = getattr(fallback_schedule, "raw", None) or str(fallback_schedule)
    return _CRON_TO_SECONDS.get(raw.strip(), 0)


__all__ = [
    "SEVERITY_ORDER",
    "severity_rank",
    "TriggerReason",
    "TriggerPolicy",
    "TriggerAccumulator",
    "TriggerDecision",
    "apply_dirty",
    "apply_fire",
    "decide",
    "policy_from_descriptor",
]
