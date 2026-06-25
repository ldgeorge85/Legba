# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""§2.1 — the trigger dispatch path must not fire retired/paused analysts.

A per-target reactive fire routes straight to a version-less WORKER actor
(``analyst::<id>::<target>``) that lazy-activates and gates ONLY on its own
record — it never consults head lifecycle. So a retired/paused analyst whose
cadence reminder is already gone would keep burning reactive LLM budget on
every matching signal. Two guards close this:

  * ``build_trigger_work`` NOOPs a fire when the analyst is not in the runtime's
    live-set (``_ANALYST_ACTOR_IDS``), BEFORE any actor proxy is created;
  * ``TriggerEngine.unregister`` tears down a retired analyst's registrations at
    the source so the engine stops marking its pairs dirty.

These are unit-level (the live-set is the runtime's authoritative lifecycle
view; the executor wiring is covered in test_lifecycle_propagation). The
positive dispatch path is exercised with a fake ActorProxy — the real daprd
round-trip is the cutover's live verify.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import legba.runtime.source_first_runtime as sfr
from legba.runtime.triggers.dispatch import TriggerFire
from legba.runtime.triggers.engine import TriggerEngine, TriggerRegistration
from legba.runtime.triggers.policy import TriggerReason


@pytest.fixture(autouse=True)
def _isolate_live_set():
    """Snapshot/restore the module-global dispatch live-set around each test."""
    saved = dict(sfr._ANALYST_ACTOR_IDS)
    sfr._ANALYST_ACTOR_IDS.clear()
    yield
    sfr._ANALYST_ACTOR_IDS.clear()
    sfr._ANALYST_ACTOR_IDS.update(saved)


def _fire(analyst_id: str, target_id: str | None) -> TriggerFire:
    return TriggerFire(
        analyst_id=analyst_id,
        target_id=target_id or "",
        tenant="shared",
        reason=TriggerReason.ACCUMULATION,
        pending_count=3,
        severity_wake=False,
        fired_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# _analyst_is_live — the in-memory lifecycle gate
# ---------------------------------------------------------------------------


def test_analyst_is_live_tracks_remember_and_forget():
    assert sfr._analyst_is_live("country_assessor") is False
    sfr.remember_analyst_actor_id("country_assessor", "analyst::country_assessor::abc")
    assert sfr._analyst_is_live("country_assessor") is True
    sfr.forget_analyst_actor_id("country_assessor", "analyst::country_assessor::abc")
    assert sfr._analyst_is_live("country_assessor") is False


# ---------------------------------------------------------------------------
# build_trigger_work — dispatch gate
# ---------------------------------------------------------------------------


async def test_fire_for_not_live_analyst_noops_without_dispatch(monkeypatch):
    # Only `live_one` is active; a fire for `retired_one` must NOOP and never
    # touch ActorProxy (which would activate a worker → LLM spend).
    sfr.remember_analyst_actor_id("live_one", "analyst::live_one::abc")

    import dapr.actor as da

    def _boom(*a, **k):
        raise AssertionError(
            "ActorProxy.create must NOT be called for a not-live analyst"
        )

    monkeypatch.setattr(da.ActorProxy, "create", staticmethod(_boom))

    work = sfr.build_trigger_work(None)
    out = await work(_fire("retired_one", "BR"))
    assert out == {"skipped": "analyst_not_live", "target_id": "BR"}


async def test_fire_for_live_analyst_dispatches_to_worker(monkeypatch):
    sfr.remember_analyst_actor_id("live_one", "analyst::live_one::abc")

    captured: dict = {}

    class _FakeProxy:
        async def run(self, payload):
            captured["payload"] = payload
            return {"ran": True}

    import dapr.actor as da

    monkeypatch.setattr(
        da.ActorProxy, "create", staticmethod(lambda *a, **k: _FakeProxy())
    )

    work = sfr.build_trigger_work(None)
    out = await work(_fire("live_one", "BR"))
    assert out["actor_run"] == {"ran": True}
    assert out["target_id"] == "BR"
    # Routed to the per-target worker via a coalesced fire.
    assert captured["payload"]["trigger_kind"] == "coalesced_fire"
    assert captured["payload"]["target_filter"] == "BR"


# ---------------------------------------------------------------------------
# TriggerEngine.unregister — source-level teardown on retire
# ---------------------------------------------------------------------------


def _reg(analyst: str, target: str) -> TriggerRegistration:
    return TriggerRegistration(analyst_id=analyst, target_id=target, tenant="shared")


def _engine() -> TriggerEngine:
    return TriggerEngine(nats=object(), coalescer=object())


def test_unregister_by_analyst_removes_all_its_targets():
    eng = _engine()
    eng.register(_reg("a1", "BR"))
    eng.register(_reg("a1", "US"))
    eng.register(_reg("a2", "BR"))
    assert eng.unregister("a1") == 2
    assert [(r.analyst_id, r.target_id) for r in eng._regs] == [("a2", "BR")]


def test_unregister_by_pair_removes_one():
    eng = _engine()
    eng.register(_reg("a1", "BR"))
    eng.register(_reg("a1", "US"))
    assert eng.unregister("a1", "US") == 1
    assert [(r.analyst_id, r.target_id) for r in eng._regs] == [("a1", "BR")]


def test_unregister_unknown_analyst_is_noop():
    eng = _engine()
    eng.register(_reg("a1", "BR"))
    assert eng.unregister("nope") == 0
    assert len(eng._regs) == 1
