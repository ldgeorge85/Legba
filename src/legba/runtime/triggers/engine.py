# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""TriggerEngine — the NATS-driven wrapper around the coalescer (P-10).

Consumes the W2 subscription engine's matched-signal stream and the cadence
ticker, feeding both into the :class:`~.coalescer.Coalescer`. This is the thin
production loop; the testable mechanism is the coalescer (drivable directly
against the dev rig with no loop), mirroring the P-06 SourceCore/SourceActor
split.

Wiring (PIVOT §4.6 / §6.1):

  * A target's analysts subscribe to the SAME per-target aggregated JetStream
    consumer the W2 engine already binds (``legba_signals`` stream, subject-
    filtered to the target's coarse axes). The trigger engine binds a durable
    PULL subscription per (analyst, target) registration onto that consumer's
    subject filters and re-checks each delivered signal against the binding's
    FULL structured filter + Starlark residual (``SubscriptionEngine.delivers``
    / ``matches``) before marking the pair dirty — the coarse subject only
    narrows; the exact match is SQL/Starlark, never the subject (PIVOT §6.1).

  * A new upstream FINDING (a derived signal published by an analyst, event
    class ``derived``) is just another matching signal on the same stream — the
    same dirty→gate path handles "a new upstream finding" with no special case.

  * The cadence ticker drives :meth:`Coalescer.on_cadence_tick` on a fixed
    period so a quiet drip of sub-threshold signals (and any pair whose cooldown
    just lapsed) still fires.

Acks: a delivered signal is acked once its dirty-state is durably persisted (or
it fired). The accumulator is the durable record — restart-survives reads it
back — so acking a held signal never loses it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..subscription.subjects import ResolvedBinding
from .coalescer import Coalescer
from .dispatch import TriggerRunResult

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass
class TriggerRegistration:
    """One (analyst, target) watch over a set of resolved source bindings.

    The bindings come straight from the W2 ``TargetSubscription`` — the trigger
    engine re-uses the target's resolved+authorized bindings to re-check each
    delivered signal. ``subject_filters`` is the coarse set the engine binds its
    durable consumer onto.
    """

    analyst_id: str
    target_id: str
    tenant: str
    bindings: list[ResolvedBinding] = field(default_factory=list)
    subject_filters: list[str] = field(default_factory=list)

    def matches_signal(self, row: dict[str, Any]) -> bool:
        """True iff the delivered signal matches ANY of the target's bindings.

        Exact re-check (structured + residual) via the W2 matcher — the coarse
        subject only narrowed delivery; this confirms the real match before the
        pair is marked dirty.
        """
        from ..subscription.filter import matches

        for b in self.bindings:
            if matches(
                b.subscription,
                row,
                source_id=b.source_id,
                owner_tenant=b.owner_tenant,
            ):
                return True
        return False


class TriggerEngine:
    """Owns the NATS consumer loop + cadence ticker feeding the coalescer.

    Construct with a connected ``NatsStore`` + the coalescer. Register
    (analyst, target) watches, then ``run`` (continuous) or use
    :meth:`drain_once` + :meth:`tick_once` from a test harness.
    """

    def __init__(
        self,
        *,
        nats: Any,
        coalescer: Coalescer,
        stream: str = "legba_signals",
        durable: str | None = None,
        fetch_batch: int = 16,
        fetch_timeout: float = 1.0,
    ) -> None:
        self._nats = nats
        self._coalescer = coalescer
        self._stream = stream
        self._durable = durable or "legba-trigger-engine"
        self._fetch_batch = fetch_batch
        self._fetch_timeout = fetch_timeout
        self._regs: list[TriggerRegistration] = []
        self._psub = None
        self._stopped = asyncio.Event()
        self.delivered = 0
        self.matched = 0
        self.fired = 0

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, reg: TriggerRegistration) -> None:
        self._regs.append(reg)

    def unregister(self, analyst_id: str, target_id: str | None = None) -> int:
        """Remove (analyst, target) watches. ``target_id=None`` removes ALL of
        the analyst's registrations — the RETIRE case (§2.1). Returns the count
        removed.

        Stops the engine from marking the pair dirty on future signals, so a
        retired analyst's reactive fires cease at the source (the dispatch-time
        lifecycle gate in ``build_trigger_work`` is the belt-and-braces for any
        pair already dirty when retire lands). The durable consumer's subject
        filters are deliberately NOT re-narrowed here: a now-superset filter
        only over-DELIVERS, which each surviving reg's ``matches_signal``
        re-check discards — re-narrowing would churn the JetStream consumer on
        every retire for no correctness gain.
        """
        before = len(self._regs)
        self._regs = [
            r
            for r in self._regs
            if not (
                r.analyst_id == analyst_id
                and (target_id is None or r.target_id == target_id)
            )
        ]
        removed = before - len(self._regs)
        if removed:
            logger.info(
                "trigger.unregister analyst=%s target=%s removed=%d",
                analyst_id, target_id or "*", removed,
            )
        return removed

    def _all_subject_filters(self) -> list[str]:
        filters: set[str] = set()
        for r in self._regs:
            filters.update(r.subject_filters)
        return sorted(filters)

    async def ensure_consumer(self) -> None:
        """Bind one durable consumer onto the union of registered subject
        filters. Idempotent (recreates on filter-set change — the W2
        ``ensure_durable_consumer`` semantics)."""
        filters = self._all_subject_filters() or [f"{self._stream}.>"]
        await self._nats.ensure_durable_consumer(
            self._stream,
            self._durable,
            filter_subjects=filters,
            deliver_policy="new",  # only signals after the engine came up
        )

    async def bind(self) -> None:
        if self._psub is None:
            from ...data.nats import SIGNAL_SUBJECT_ROOT  # local import: optional dep

            self._psub = await self._nats.js.pull_subscribe(
                f"{SIGNAL_SUBJECT_ROOT}.>",
                durable=self._durable,
                stream=self._stream,
            )

    # ------------------------------------------------------------------
    # Per-message handling
    # ------------------------------------------------------------------

    async def _handle_msg(self, msg) -> list[TriggerRunResult]:
        self.delivered += 1
        try:
            row = json.loads(msg.data)
        except Exception as exc:
            logger.error("trigger.parse_failed (terming): %s", exc)
            await _term(msg)
            return []

        # The published Signal carries its id as ``signal_id``; normalise to the
        # ``id`` key the W2 matcher + coalescer read off a substrate row.
        if "id" not in row and "signal_id" in row:
            row["id"] = row["signal_id"]

        results: list[TriggerRunResult] = []
        now = _utcnow()
        for reg in self._regs:
            if not reg.matches_signal(row):
                continue
            self.matched += 1
            res = await self._coalescer.on_signal(
                analyst_id=reg.analyst_id,
                target_id=reg.target_id,
                tenant=reg.tenant,
                signal_row=row,
                now=now,
            )
            if res is not None:
                self.fired += 1
                results.append(res)
        await _ack(msg)
        return results

    # ------------------------------------------------------------------
    # Loops
    # ------------------------------------------------------------------

    async def drain_once(self) -> list[TriggerRunResult]:
        """Fetch + process one batch of delivered signals (test/operator helper)."""
        await self.bind()
        try:
            msgs = await self._psub.fetch(self._fetch_batch, timeout=self._fetch_timeout)
        except Exception as exc:
            if "timeout" not in str(exc).lower() and not isinstance(
                exc, asyncio.TimeoutError
            ):
                logger.debug("trigger.fetch error (treating as empty): %s", exc)
            return []
        out: list[TriggerRunResult] = []
        for msg in msgs:
            out.extend(await self._handle_msg(msg))
        return out

    async def tick_once(self, *, now: datetime | None = None) -> list[TriggerRunResult]:
        """Run one cadence tick over all dirty pairs."""
        res = await self._coalescer.on_cadence_tick(now=now)
        self.fired += len(res)
        return res

    async def run(self, *, cadence_period_seconds: float = 30.0) -> None:
        """Continuous loop: drain the NATS consumer + tick on cadence."""
        await self.bind()
        ticker = asyncio.create_task(self._tick_loop(cadence_period_seconds))
        try:
            while not self._stopped.is_set():
                await self.drain_once()
        finally:
            ticker.cancel()
            try:
                await ticker
            except (asyncio.CancelledError, Exception):
                pass

    async def _tick_loop(self, period: float) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=period)
            except asyncio.TimeoutError:
                await self.tick_once()

    def stop(self) -> None:
        self._stopped.set()


# --- NATS msg ack helpers (mirror the job-worker helpers) ------------------


async def _ack(msg) -> None:
    try:
        await msg.ack()
    except Exception:  # pragma: no cover
        logger.debug("trigger.ack failed")


async def _term(msg) -> None:
    try:
        await msg.term()
    except Exception:  # pragma: no cover
        logger.debug("trigger.term failed")


__all__ = ["TriggerEngine", "TriggerRegistration"]
