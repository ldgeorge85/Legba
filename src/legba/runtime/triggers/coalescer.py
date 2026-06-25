# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Coalescer — the I/O-bearing core that turns matched signals into fires.

The :class:`Coalescer` is the testable mechanism (mirrors the SourceCore
pattern from P-06: a plain class drivable directly against the dev rig, no NATS
loop required). The :class:`~.engine.TriggerEngine` is the thin NATS-driven
wrapper that feeds it.

Responsibilities (PIVOT §4.6 / P-10):

  1. **on_signal** — a matching signal (already matched for the target by the
     W2 subscription engine) marks the (analyst, target) pair dirty. Reads the
     signal's CANONICAL id (``canonical_signal_id`` ?? ``id``) + severity, then
     :func:`policy.apply_dirty` (idempotent on canonical id — alias-no-double-
     wake) and persists. Then re-evaluates: a severity-gate signal fires NOW.
  2. **on_cadence_tick** — the periodic re-evaluation for every dirty pair so a
     quiet drip of sub-threshold signals eventually fires on cadence.
  3. **_maybe_fire** — read accumulator → :func:`policy.decide` → on a fire,
     CAS-claim the fire (no double-dispatch across workers) → dispatch the
     batch to the analyst runner → reset.

Severity is read from the signal row: top-level ``severity`` if a baseline set
it, else ``payload->>'severity'`` (the open-dict convention the
``severity_at_least`` predicate also uses). Unknown → rank -1 (never wakes the
severity gate).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from .dispatch import AnalystTriggerRunner, TriggerFire, TriggerRunResult
from .policy import (
    TriggerAccumulator,
    TriggerPolicy,
    apply_dirty,
    decide,
    severity_rank,
)
from .state import TriggerStateStore

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def canonical_id_of(row: dict[str, Any]) -> str:
    """The id a signal coalesces under — its canonical if dedup linked it.

    The P-09 dedup analyst sets ``canonical_signal_id`` on every member of a
    duplicate set (the canonical points at itself). Coalescing on the canonical
    id is what makes two deliveries of the SAME observation (via two sources, or
    a re-delivery) count ONCE — the alias-no-double-wake behaviour.
    """
    canon = row.get("canonical_signal_id")
    if canon:
        return str(canon)
    return str(row.get("id"))


def _payload_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise a signal row's ``payload`` to a dict.

    The pool-level JSONB codec (``data/postgres.py``) now delivers payload as
    a dict on every fetch path, and the published envelope is parsed JSON —
    so the str branch is NO-OP SAFETY for codec-less raw connections
    (scripts/tests) and malformed envelopes, kept so severity lookup is
    robust regardless of how the row arrived (G4 parity).
    """
    payload = row.get("payload")
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str) and payload:
        try:
            parsed = json.loads(payload)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def severity_of(row: dict[str, Any]) -> int:
    """Severity rank for a signal row (-1 when absent/unknown)."""
    # Prefer an explicit top-level severity (a baseline may promote it), then
    # the open payload dict (the platform convention).
    top = row.get("severity")
    if isinstance(top, str):
        r = severity_rank(top)
        if r >= 0:
            return r
    return severity_rank(_payload_dict(row).get("severity"))


class Coalescer:
    """Dirty-bit accumulation + gate evaluation for many (analyst, target) pairs.

    Construct once per process; share across the engine's consumer + ticker.
    Policies are resolved per pair via the injected ``policy_for`` callable
    (the engine builds these from analyst descriptors via
    :func:`policy.policy_from_descriptor`).
    """

    def __init__(
        self,
        *,
        state: TriggerStateStore,
        runner: AnalystTriggerRunner,
        policy_for: "PolicyResolver",
        seen_cap: int = 10_000,
    ) -> None:
        self._state = state
        self._runner = runner
        self._policy_for = policy_for
        self._seen_cap = seen_cap

    # ------------------------------------------------------------------
    # Signal ingress
    # ------------------------------------------------------------------

    async def on_signal(
        self,
        *,
        analyst_id: str,
        target_id: str,
        tenant: str,
        signal_row: dict[str, Any],
        now: datetime | None = None,
    ) -> TriggerRunResult | None:
        """Process one matching signal for a pair.

        Returns the :class:`TriggerRunResult` if the signal caused a fire,
        else ``None`` (dirty recorded, gate not tripped / cooldown held). The
        caller (engine) acks the NATS message regardless — a held signal is
        durably persisted in the accumulator, so an ack is safe (restart-
        survives reads it back).
        """
        now = now or _utcnow()
        policy = self._policy_for(analyst_id, target_id)
        canon = canonical_id_of(signal_row)
        sev = severity_of(signal_row)

        acc = await self._state.get(analyst_id, target_id, seen_cap=self._seen_cap)
        new_acc, counted = apply_dirty(
            acc, signal_id=canon, severity_rank_value=sev, now=now
        )
        if counted:
            await self._state.save_dirty(
                analyst_id, target_id, new_acc, tenant=tenant
            )
        else:
            # Duplicate / alias of an already-counted observation in this window
            # — no accumulation change, no re-evaluation needed (the prior
            # delivery already evaluated). Alias-no-double-wake.
            logger.debug(
                "trigger.dirty.duplicate analyst=%s target=%s canonical=%s",
                analyst_id, target_id, canon,
            )
            return None

        return await self._maybe_fire(
            analyst_id=analyst_id,
            target_id=target_id,
            tenant=tenant,
            policy=policy,
            acc=new_acc,
            now=now,
            seed_signal_id=canon,
        )

    # ------------------------------------------------------------------
    # Cadence tick
    # ------------------------------------------------------------------

    async def on_cadence_tick(
        self, *, now: datetime | None = None
    ) -> list[TriggerRunResult]:
        """Re-evaluate every dirty pair on the periodic tick.

        Picks up pairs whose accumulation never reached threshold but whose
        cadence period has elapsed (a slow drip), and any pair whose cooldown
        just expired with pending dirt waiting.
        """
        now = now or _utcnow()
        fired: list[TriggerRunResult] = []
        for analyst_id, target_id, tenant in await self._state.list_dirty():
            policy = self._policy_for(analyst_id, target_id)
            acc = await self._state.get(
                analyst_id, target_id, seen_cap=self._seen_cap
            )
            res = await self._maybe_fire(
                analyst_id=analyst_id,
                target_id=target_id,
                tenant=tenant,
                policy=policy,
                acc=acc,
                now=now,
                seed_signal_id=None,
            )
            if res is not None:
                fired.append(res)
        return fired

    # ------------------------------------------------------------------
    # Fire path (CAS-guarded)
    # ------------------------------------------------------------------

    async def _maybe_fire(
        self,
        *,
        analyst_id: str,
        target_id: str,
        tenant: str,
        policy: TriggerPolicy,
        acc: TriggerAccumulator,
        now: datetime,
        seed_signal_id: str | None,
    ) -> TriggerRunResult | None:
        decision = decide(acc, policy, now=now)
        if not decision.should_fire:
            return None

        # CAS-claim the fire on the read fire-anchor — exactly one worker wins,
        # so a pair that two paths (signal + tick) both decide to fire for is
        # dispatched once. The loser observes a changed anchor and backs off.
        won = await self._state.claim_fire(
            analyst_id,
            target_id,
            expected_last_fired_at=acc.last_fired_at,
            fired_at=now,
        )
        if not won:
            logger.debug(
                "trigger.fire.lost_cas analyst=%s target=%s", analyst_id, target_id
            )
            return None

        sig_ids = (seed_signal_id,) if seed_signal_id else ()
        fire = TriggerFire(
            analyst_id=analyst_id,
            target_id=target_id,
            tenant=tenant,
            reason=decision.reason,
            pending_count=decision.pending_count,
            severity_wake=decision.severity_wake,
            fired_at=now,
            method_kind="deterministic" if not policy.is_llm else "llm",
            signal_ids=tuple(s for s in sig_ids if s),
        )
        logger.info(
            "trigger.fire analyst=%s target=%s reason=%s pending=%d severity_wake=%s",
            analyst_id, target_id, decision.reason.value,
            decision.pending_count, decision.severity_wake,
        )
        result = await self._runner.run(fire)
        # NOTE: the accumulator was already reset inside claim_fire (the CAS
        # also zeroed pending + cleared the seen-set), so a handler crash does
        # NOT re-fire on the same batch — it is logged + counted as failed and
        # the next window starts fresh. (The dedup-analyst handler is itself
        # idempotent, so a re-run on the next window is harmless anyway.)
        return result


# A callable resolving the policy for a pair. Sync — policy is cheap to build
# and the engine caches descriptors.
PolicyResolver = Callable[[str, str], TriggerPolicy]


__all__ = ["Coalescer", "PolicyResolver", "canonical_id_of", "severity_of"]
