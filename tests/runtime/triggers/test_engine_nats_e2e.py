# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-10 TriggerEngine — full NATS-driven end-to-end (real Postgres + NATS).

Proves the PRODUCTION seam, not just the coalescer mechanism: signals are
published onto the SAME ``legba_signals`` stream the W2 subscription engine uses
(via ``SubscriptionEngine.publish_signal``); the :class:`TriggerEngine` binds a
durable consumer onto the target's coarse subject filters, re-checks each
delivered signal against the binding's structured filter + residual, and feeds
matches to the coalescer — firing on the severity gate and the accumulation
gate, while a NON-matching signal (wrong tag) is delivered-but-ignored.

This ties P-10 to the W2 fan-out exactly as designed (PIVOT §6.1): the coarse
subject narrows delivery; the exact match is SQL/Starlark, never the subject.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from legba.data.schemas.source import Subscription
from legba.data.sources._contract import Signal
from legba.runtime.subscription.engine import SubscriptionEngine
from legba.runtime.subscription.subjects import ResolvedBinding, subject_filters_for
from legba.runtime.triggers.coalescer import Coalescer
from legba.runtime.triggers.dispatch import DeterministicTriggerRunner, TriggerFire
from legba.runtime.triggers.engine import TriggerEngine, TriggerRegistration
from legba.runtime.triggers.policy import TriggerPolicy
from legba.runtime.triggers.state import TriggerStateStore


def _runner():
    fires: list[TriggerFire] = []

    async def _work(fire: TriggerFire) -> dict:
        fires.append(fire)
        return {"batch": fire.pending_count}

    return DeterministicTriggerRunner(_work), fires


async def test_trigger_engine_nats_end_to_end(trig_pg, trig_nats):
    source_id = f"source.trig.{uuid4().hex[:8]}"
    tenant = "default"
    target_id = f"target.{uuid4().hex[:8]}"
    analyst_id = "analyst.det.e2e"

    # One binding: this source, tag=news. (We feed bindings directly — the W2
    # registration/policy path is covered by the subscription-engine suite;
    # here we prove the TRIGGER consumption + match + coalesce.)
    binding = ResolvedBinding(
        source_id=source_id, owner_tenant=tenant,
        subscription=Subscription(tags=["news"]), via_selector=False,
    )
    reg = TriggerRegistration(
        analyst_id=analyst_id, target_id=target_id, tenant=tenant,
        bindings=[binding], subject_filters=subject_filters_for([binding]),
    )

    state = TriggerStateStore(trig_pg.pool)
    runner, fires = _runner()
    policy = TriggerPolicy(accumulation_threshold=3, severity_gate="critical")
    coalescer = Coalescer(state=state, runner=runner, policy_for=lambda a, t: policy)

    engine = TriggerEngine(
        nats=trig_nats,
        coalescer=coalescer,
        durable=f"legba-trigger-test-{uuid4().hex[:8]}",
        fetch_timeout=1.0,
    )
    engine.register(reg)

    pub = SubscriptionEngine(trig_pg, nats=trig_nats)
    await pub.ensure_signal_stream()
    # Bind the consumer with deliver_policy=new BEFORE publishing so the engine
    # only sees signals published after it came up.
    await engine.ensure_consumer()
    await engine.bind()

    try:
        # 1) A critical matching signal → severity-wake (fires immediately).
        crit = Signal(
            source_id=source_id, owner_tenant=tenant, modality="text",
            tags=["news"], payload={"severity": "critical"},
        )
        await pub.publish_signal(signal=crit)

        # 2) A non-matching signal (wrong tag) — delivered on the coarse subject
        #    (same source/modality) but the structured re-check drops it.
        noise = Signal(
            source_id=source_id, owner_tenant=tenant, modality="text",
            tags=["sports"],
        )
        await pub.publish_signal(signal=noise)

        # Drain until the engine has processed both (a few rounds for JetStream).
        for _ in range(10):
            await engine.drain_once()
            if engine.fired >= 1 and engine.delivered >= 2:
                break

        assert engine.delivered >= 2, "both signals should be delivered"
        assert engine.matched == 1, "only the news signal matches the binding"
        assert engine.fired == 1, "the critical news signal fires once (severity)"
        assert fires[0].reason.value == "severity" and fires[0].severity_wake
        assert await state.fire_count(analyst_id, target_id) == 1

        # 3) Accumulation gate over the SAME pair: publish 3 medium news signals
        #    → fires once on the batch of 3 (cooldown is 0 here).
        for _ in range(3):
            s = Signal(
                source_id=source_id, owner_tenant=tenant, modality="text",
                tags=["news"], payload={"severity": "low"},
            )
            await pub.publish_signal(signal=s)
        for _ in range(10):
            await engine.drain_once()
            if engine.fired >= 2:
                break
        assert engine.fired == 2, "the 3-signal batch fires exactly once more"
        assert fires[-1].reason.value == "accumulation" and fires[-1].pending_count == 3
        assert await state.fire_count(analyst_id, target_id) == 2
    finally:
        try:
            await trig_nats.js.delete_consumer(engine._stream, engine._durable)
        except Exception:
            pass
