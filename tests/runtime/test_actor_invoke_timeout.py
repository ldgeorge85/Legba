# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Actor-invoke round-trip timeout budget for the analyst-run hot paths.

The dapr-python ``ActorProxy.run`` (``invoke_method``) defaults to the SDK
global ``DAPR_HTTP_TIMEOUT_SECONDS`` (60s). On the busiest G20 targets the
heaviest deterministic analyst — then ``cross_source_dedup`` — held its queued
``run`` turn past 60s while it swept a large finding pool, so the round-trip
threw ``asyncio.TimeoutError`` (surfaced as ``trigger.run.failed``) on a tail of
fires even though the analyst completed. The fix passes an explicit
``ActorProxyFactory`` carrying a larger, env-overridable budget on both the
reactive trigger-dispatch path and the cadence fan-out.

(2026-08-02: ``cross_source_dedup`` is no longer that analyst. It declared a
predicate-less ``subscription.targets`` block, which the runtime fans out to
every active target, so 44 copies each ran the same full-pool scan; it is now a
singleton at ~0.85s a run. The budget these tests pin is unchanged and still
correct — it now serves the rest of the fleet rather than dedup.)

These tests pin the resolver semantics + the factory wiring (no daprd needed).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import legba.runtime.source_first_runtime as sfr
from legba.runtime.triggers.dispatch import TriggerFire
from legba.runtime.triggers.policy import TriggerReason


# ---------------------------------------------------------------------------
# actor_invoke_timeout_seconds — env resolver
# ---------------------------------------------------------------------------


def test_default_is_180(monkeypatch):
    monkeypatch.delenv(sfr.ACTOR_INVOKE_TIMEOUT_ENV, raising=False)
    assert sfr.actor_invoke_timeout_seconds() == 180
    assert sfr.ACTOR_INVOKE_TIMEOUT_DEFAULT_S == 180


def test_env_override_is_honoured(monkeypatch):
    monkeypatch.setenv(sfr.ACTOR_INVOKE_TIMEOUT_ENV, "240")
    assert sfr.actor_invoke_timeout_seconds() == 240


def test_malformed_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(sfr.ACTOR_INVOKE_TIMEOUT_ENV, "not-an-int")
    assert sfr.actor_invoke_timeout_seconds() == 180


def test_non_positive_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(sfr.ACTOR_INVOKE_TIMEOUT_ENV, "0")
    assert sfr.actor_invoke_timeout_seconds() == 180
    monkeypatch.setenv(sfr.ACTOR_INVOKE_TIMEOUT_ENV, "-5")
    assert sfr.actor_invoke_timeout_seconds() == 180


# ---------------------------------------------------------------------------
# _actor_proxy_factory — threads the budget into the Dapr actor http client
# ---------------------------------------------------------------------------


def test_factory_threads_explicit_timeout_into_dapr_client():
    factory = sfr._actor_proxy_factory(123)
    # ActorProxyFactory -> DaprActorHttpClient -> DaprHttpClient(_timeout=total)
    inner = factory._dapr_client._client
    assert inner._timeout.total == 123


def test_factory_uses_env_resolver_when_unset(monkeypatch):
    monkeypatch.delenv(sfr.ACTOR_INVOKE_TIMEOUT_ENV, raising=False)
    factory = sfr._actor_proxy_factory()
    assert factory._dapr_client._client._timeout.total == 180


# ---------------------------------------------------------------------------
# build_trigger_work — dispatch passes the budgeted factory to ActorProxy
# ---------------------------------------------------------------------------


def _fire(analyst: str, target: str) -> TriggerFire:
    return TriggerFire(
        analyst_id=analyst,
        target_id=target,
        tenant="shared",
        reason=TriggerReason.CADENCE,
        pending_count=1,
        severity_wake=False,
        fired_at=datetime.now(tz=timezone.utc),
    )


@pytest.mark.asyncio
async def test_dispatch_passes_budgeted_factory_to_actor_proxy(monkeypatch):
    saved = dict(sfr._ANALYST_ACTOR_IDS)
    try:
        sfr.remember_analyst_actor_id("live_one", "analyst::live_one::abc")

        captured: dict = {}

        class _FakeProxy:
            async def run(self, payload):
                return {"ran": True}

        import dapr.actor as da

        def _capture_create(*args, **kwargs):
            captured["factory"] = kwargs.get("actor_proxy_factory")
            return _FakeProxy()

        monkeypatch.setattr(da.ActorProxy, "create", staticmethod(_capture_create))
        monkeypatch.setenv(sfr.ACTOR_INVOKE_TIMEOUT_ENV, "200")

        work = sfr.build_trigger_work(None)
        out = await work(_fire("live_one", "BR"))
        assert out["actor_run"] == {"ran": True}

        # The fire dispatched through our explicit factory, not the SDK default.
        factory = captured["factory"]
        assert factory is not None
        assert factory._dapr_client._client._timeout.total == 200
    finally:
        sfr._ANALYST_ACTOR_IDS.clear()
        sfr._ANALYST_ACTOR_IDS.update(saved)
