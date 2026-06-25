# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-10 coalescing-trigger — live integration tests (real Postgres + NATS).

No mocks. Drives the :class:`Coalescer` against the dev rig (fresh migrated
``signals``/``signal_aliases`` + the P-10 ``trigger_state`` ledger) to prove the
FIVE deterministic acceptance behaviours, then one full NATS-driven
:class:`TriggerEngine` end-to-end run:

  1. severity-wake       — a critical signal fires immediately, ahead of batch.
  2. batch               — N sub-threshold signals fire once at threshold.
  3. cooldown-cap        — a burst can't thrash; fires are cooldown-bounded.
  4. restart-survives    — a NEW Coalescer over the SAME DB resumes the pending
                           accumulator + cooldown anchor (durable state).
  5. alias-no-double-wake— two deliveries of the SAME canonical observation
                           (a dedup alias) count ONCE.

All against DETERMINISTIC analysts (the task: prove with deterministic FIRST).
An extra test asserts the LLM analyst NEVER fires per-signal.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from legba.data.sources._contract import Signal
from legba.runtime.triggers.coalescer import Coalescer
from legba.runtime.triggers.dispatch import (
    ActorTriggerRunner,
    DeterministicTriggerRunner,
    TriggerFire,
)
from legba.runtime.triggers.policy import TriggerPolicy, TriggerReason

T0 = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_signal(
    pg,
    *,
    source_id: str = "source.test.feed",
    tenant: str = "default",
    severity: str | None = None,
    canonical_signal_id=None,
) -> dict:
    """Insert a real signals row; return the row dict (the coalescer's input)."""
    payload = {"severity": severity} if severity else {}
    sig = Signal(
        source_id=source_id, owner_tenant=tenant, modality="text",
        payload=payload, tags=["news"],
    )
    async with pg.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO signals
                (id, source_id, owner_tenant, modality, payload, tags,
                 canonical_signal_id)
            VALUES ($1,$2,$3,$4,$5::jsonb,$6::text[],$7)
            """,
            sig.signal_id, source_id, tenant, "text",
            __import__("json").dumps(payload), ["news"], canonical_signal_id,
        )
        row = await conn.fetchrow("SELECT * FROM signals WHERE id = $1", sig.signal_id)
    return dict(row)


def _runner():
    seen: list[TriggerFire] = []

    async def _work(fire: TriggerFire) -> dict:
        # The "deterministic analyst" — minimal one working example: it records
        # the fire + its batch size. The SEAM is complete; the handler library
        # is intentionally minimal per the task.
        seen.append(fire)
        return {"batch": fire.pending_count, "reason": fire.reason.value}

    r = DeterministicTriggerRunner(_work)
    return r, seen


def _coalescer(state, runner, policy: TriggerPolicy) -> Coalescer:
    return Coalescer(state=state, runner=runner, policy_for=lambda a, t: policy)


# ===========================================================================
# 1. severity-wake
# ===========================================================================


async def test_severity_wake_fires_immediately(trig_pg, trig_state):
    runner, fires = _runner()
    policy = TriggerPolicy(accumulation_threshold=100, severity_gate="critical")
    co = _coalescer(trig_state, runner, policy)
    a, t = "analyst.det", f"target.{uuid4().hex[:8]}"

    # A medium signal holds (1 << 100, no severity wake).
    med = await _insert_signal(trig_pg, severity="medium")
    assert await co.on_signal(analyst_id=a, target_id=t, tenant="default", signal_row=med, now=T0) is None

    # A critical signal wakes NOW despite pending 2 << 100.
    crit = await _insert_signal(trig_pg, severity="critical")
    res = await co.on_signal(analyst_id=a, target_id=t, tenant="default", signal_row=crit, now=T0)
    assert res is not None and res.status == "ran"
    assert res.reason.value == "severity"
    assert fires and fires[-1].severity_wake is True
    # The accumulator reset on fire.
    acc = await trig_state.get(a, t)
    assert acc.pending_count == 0
    assert await trig_state.fire_count(a, t) == 1


# ===========================================================================
# 2. batch (accumulation)
# ===========================================================================


async def test_batch_fires_once_at_threshold(trig_pg, trig_state):
    runner, fires = _runner()
    policy = TriggerPolicy(accumulation_threshold=3)  # no severity, no cooldown
    co = _coalescer(trig_state, runner, policy)
    a, t = "analyst.det", f"target.{uuid4().hex[:8]}"

    results = []
    for _ in range(3):
        row = await _insert_signal(trig_pg)
        results.append(await co.on_signal(
            analyst_id=a, target_id=t, tenant="default", signal_row=row, now=T0,
        ))
    # First two held, third fired ONCE on the whole batch of 3.
    assert results[0] is None and results[1] is None
    assert results[2] is not None and results[2].status == "ran"
    assert results[2].pending_count == 3 and results[2].reason.value == "accumulation"
    assert len(fires) == 1 and await trig_state.fire_count(a, t) == 1


# ===========================================================================
# 3. cooldown-cap (a burst can't thrash the analyst)
# ===========================================================================


async def test_cooldown_caps_fires_over_a_burst(trig_pg, trig_state):
    runner, fires = _runner()
    # accumulation=1 → every signal *wants* to fire; cooldown bounds it.
    policy = TriggerPolicy(accumulation_threshold=1, cooldown_seconds=10)
    co = _coalescer(trig_state, runner, policy)
    a, t = "analyst.det", f"target.{uuid4().hex[:8]}"

    # 20 signals over 19s of simulated time, one every ~1s.
    for i in range(20):
        row = await _insert_signal(trig_pg)
        await co.on_signal(
            analyst_id=a, target_id=t, tenant="default", signal_row=row,
            now=T0 + timedelta(seconds=i),
        )
    # 19s span / 10s cooldown ⇒ at most 3 fires (t=0, 10, +1). Crucially << 20.
    fc = await trig_state.fire_count(a, t)
    assert 1 <= fc <= 3, fc
    assert len(fires) == fc
    # Dirt accumulated between fires is preserved (not lost) — pending > 0 since
    # the last signal arrived inside the final cooldown window.
    acc = await trig_state.get(a, t)
    assert acc.pending_count >= 1


# ===========================================================================
# 4. restart-survives (durable accumulator + cooldown anchor)
# ===========================================================================


async def test_restart_survives_pending_and_cooldown(trig_pg, trig_state):
    from legba.runtime.triggers.state import TriggerStateStore

    policy = TriggerPolicy(accumulation_threshold=3, cooldown_seconds=60)
    a, t = "analyst.det", f"target.{uuid4().hex[:8]}"

    # --- "process 1": accumulate 2 (below threshold of 3), then crash. ---
    runner1, fires1 = _runner()
    co1 = _coalescer(trig_state, runner1, policy)
    for _ in range(2):
        row = await _insert_signal(trig_pg)
        assert await co1.on_signal(
            analyst_id=a, target_id=t, tenant="default", signal_row=row, now=T0,
        ) is None
    assert not fires1  # nothing fired yet
    del co1, runner1

    # --- "process 2": a brand-new store + coalescer over the SAME DB. ---
    state2 = TriggerStateStore(trig_pg.pool)
    runner2, fires2 = _runner()
    co2 = _coalescer(state2, runner2, policy)

    # The pending 2 survived the restart — the 3rd signal now fires.
    acc = await state2.get(a, t)
    assert acc.pending_count == 2  # durable!
    row = await _insert_signal(trig_pg)
    res = await co2.on_signal(
        analyst_id=a, target_id=t, tenant="default", signal_row=row, now=T0,
    )
    assert res is not None and res.pending_count == 3
    assert len(fires2) == 1

    # And the cooldown anchor it just set ALSO survives — a 3rd process sees it.
    state3 = TriggerStateStore(trig_pg.pool)
    runner3, fires3 = _runner()
    co3 = _coalescer(state3, runner3, policy)
    row2 = await _insert_signal(trig_pg)
    held = await co3.on_signal(
        analyst_id=a, target_id=t, tenant="default", signal_row=row2,
        now=T0 + timedelta(seconds=5),  # inside the 60s cooldown
    )
    assert held is None and not fires3  # cooldown (durable) clamped it


# ===========================================================================
# 5. alias-no-double-wake
# ===========================================================================


async def test_alias_does_not_double_wake(trig_pg, trig_state):
    runner, fires = _runner()
    policy = TriggerPolicy(accumulation_threshold=2)
    co = _coalescer(trig_state, runner, policy)
    a, t = "analyst.det", f"target.{uuid4().hex[:8]}"

    # A canonical signal + an ALIAS pointing at it (the dedup analyst's link).
    canonical = await _insert_signal(trig_pg)
    canon_id = canonical["id"]
    alias = await _insert_signal(trig_pg, canonical_signal_id=canon_id)

    # Deliver the canonical, then its alias. Both coalesce under the canonical
    # id ⇒ pending stays 1, no fire (threshold 2 not reached by ONE real obs).
    r1 = await co.on_signal(analyst_id=a, target_id=t, tenant="default", signal_row=canonical, now=T0)
    r2 = await co.on_signal(analyst_id=a, target_id=t, tenant="default", signal_row=alias, now=T0)
    assert r1 is None and r2 is None
    acc = await trig_state.get(a, t)
    assert acc.pending_count == 1, "alias must NOT double-count"
    assert not fires

    # A genuinely distinct observation now reaches the threshold of 2.
    other = await _insert_signal(trig_pg)
    r3 = await co.on_signal(analyst_id=a, target_id=t, tenant="default", signal_row=other, now=T0)
    assert r3 is not None and r3.pending_count == 2 and len(fires) == 1


# ===========================================================================
# cadence tick — a slow sub-threshold drip eventually fires (live)
# ===========================================================================


async def test_cadence_tick_fires_quiet_pair(trig_pg, trig_state):
    runner, fires = _runner()
    # High accumulation so the drip never trips the batch gate; cadence 60s is
    # the floor that eventually fires it. Tenant is non-default to exercise the
    # tenancy seam through the ticker.
    policy = TriggerPolicy(accumulation_threshold=100, cadence_seconds=60)
    co = _coalescer(trig_state, runner, policy)
    a, t = "analyst.det", f"target.{uuid4().hex[:8]}"

    # One sub-threshold signal, tenant 'tenant_x'.
    row = await _insert_signal(trig_pg, tenant="tenant_x")
    assert await co.on_signal(
        analyst_id=a, target_id=t, tenant="tenant_x", signal_row=row, now=T0,
    ) is None
    assert not fires

    # A tick BEFORE the cadence period: still held.
    assert await co.on_cadence_tick(now=T0 + timedelta(seconds=30)) == []
    assert not fires

    # A tick AFTER the period: the quiet pair fires on cadence, with the
    # correct tenant threaded through from the durable row.
    fired = await co.on_cadence_tick(now=T0 + timedelta(seconds=61))
    assert len(fired) == 1 and fired[0].reason.value == "cadence"
    assert fires[0].tenant == "tenant_x"
    assert await trig_state.fire_count(a, t) == 1


# ===========================================================================
# LLM guard — never fires per signal (the hard rule), live
# ===========================================================================


async def test_llm_analyst_never_fires_per_signal(trig_pg, trig_state):
    runner, fires = _runner()
    # An LLM analyst, even (mis)configured per-signal + critical, must not fire
    # on one signal. The runner would also REFUSE an LLM fire, so the proof is
    # that no fire is even attempted.
    policy = TriggerPolicy(
        accumulation_threshold=1, severity_gate="critical", is_llm=True,
    )
    co = _coalescer(trig_state, runner, policy)
    a, t = "analyst.llm", f"target.{uuid4().hex[:8]}"

    crit = await _insert_signal(trig_pg, severity="critical")
    res = await co.on_signal(analyst_id=a, target_id=t, tenant="default", signal_row=crit, now=T0)
    assert res is None and not fires
    # It accumulated (dirty), it just didn't FIRE — the batch gate (floored to
    # min_llm_batch) hasn't been reached and severity-wake is disabled.
    acc = await trig_state.get(a, t)
    assert acc.pending_count == 1
    assert await trig_state.fire_count(a, t) == 0


async def test_actor_runner_dispatches_coalesced_llm_fire():
    """ActorTriggerRunner runs a coalesced LLM fire — the reactive LLM path.

    The per-signal-LLM guard lives in the policy (floored accumulation), so by
    the time a fire reaches the runner it is already a coalesced batch. The
    actor runner dispatches it (status="ran"); the deterministic runner still
    REFUSES the same LLM fire (the belt-and-braces boundary stays intact).
    """
    seen: list[TriggerFire] = []

    async def _work(fire: TriggerFire) -> dict:
        seen.append(fire)
        return {"ok": True, "batch": fire.pending_count}

    fire = TriggerFire(
        analyst_id="analyst.llm", target_id="country_g20_br", tenant="default",
        reason=TriggerReason.ACCUMULATION, pending_count=3,
        severity_wake=False, fired_at=T0, method_kind="llm",
    )

    res = await ActorTriggerRunner(_work).run(fire)
    assert res.status == "ran"
    assert len(seen) == 1 and seen[0].method_kind == "llm"

    with pytest.raises(ValueError):
        await DeterministicTriggerRunner(_work).run(fire)
