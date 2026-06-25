# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dependency-registry fallback (Phase 5 hardening item 6).

When the runtime restarts but the actor host hasn't re-registered deps
for every active descriptor, an inbound ActorProxy invocation would
previously log a warning and hard-fail. The fallback path lets the
actor reconstruct deps via a resolver closure (which typically wraps a
registry HTTP lookup + the host's standard build-deps-from-descriptor).

This test exercises the cache-miss path at the function level:

  * ``_resolve_target_deps(actor_id)`` returns the resolver's deps on
    cache miss + caches the result for subsequent invocations.
  * Same for ``_resolve_analyst_deps``.
  * Setting ``LEGBA_DEPS_FALLBACK_ENABLED=0`` disables the resolver.
  * When the resolver returns ``None`` (descriptor not found / registry
    unreachable), the helper returns ``None`` — caller surfaces this
    as a loud hard_fail.
  * When the resolver raises (e.g. ``RegistryClientError`` for a 5xx
    or transport failure), the helper propagates — caller surfaces it
    as hard_fail rather than silently bypassing.

The :class:`RegistryHTTPClient` is exercised against the live
legba-registry through a focused test that proves the wire works without
needing the full descriptor-creation dance — we just GET a known
descriptor id and assert 404 (the descriptor doesn't exist; the
client should NOT raise).
"""

from __future__ import annotations

import os
import uuid

import pytest

from legba.runtime import dapr_actors
from legba.runtime.registry_client import (
    RegistryClientError,
    RegistryHTTPClient,
)


pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_deps_registry():
    """Wipe the deps registry before + after each test so previous tests
    can't pre-populate the cache we're checking."""
    dapr_actors.clear_deps_registry()
    yield
    dapr_actors.clear_deps_registry()


@pytest.fixture(autouse=True)
def _reset_fallback_env():
    """Restore the env flag after each test (test_disabled_fallback flips it)."""
    saved = os.environ.get("LEGBA_DEPS_FALLBACK_ENABLED")
    yield
    if saved is None:
        os.environ.pop("LEGBA_DEPS_FALLBACK_ENABLED", None)
    else:
        os.environ["LEGBA_DEPS_FALLBACK_ENABLED"] = saved


# A minimal sentinel "deps" object — the resolver layer is type-erased
# (``Any``) so the helper doesn't actually care that this is a real
# ``_TargetDeps``. The full deps shape is exercised by the integration
# test elsewhere.
class _SentinelDeps:
    def __init__(self, tag: str) -> None:
        self.tag = tag


# ---------------------------------------------------------------------------
# Resolver round-trips
# ---------------------------------------------------------------------------


async def test_cache_miss_calls_resolver_and_caches_target():
    actor_id = "target::brazil::deadbeef00000000"
    call_count = 0

    async def resolver(aid: str):
        nonlocal call_count
        call_count += 1
        assert aid == actor_id
        return _SentinelDeps(tag="resolved")

    dapr_actors.register_target_deps_resolver(resolver)

    # Cache miss → resolver runs.
    deps = await dapr_actors._resolve_target_deps(actor_id)
    assert isinstance(deps, _SentinelDeps)
    assert deps.tag == "resolved"
    assert call_count == 1

    # Second call hits the cache — resolver is NOT called again.
    deps2 = await dapr_actors._resolve_target_deps(actor_id)
    assert deps2 is deps
    assert call_count == 1


async def test_cache_miss_calls_resolver_and_caches_analyst():
    actor_id = "analyst::weather::feedface00000000"
    sentinel = _SentinelDeps(tag="analyst_resolved")

    async def resolver(aid: str):
        assert aid == actor_id
        return sentinel

    dapr_actors.register_analyst_deps_resolver(resolver)

    out = await dapr_actors._resolve_analyst_deps(actor_id)
    assert out is sentinel

    # Cache hit.
    out2 = await dapr_actors._resolve_analyst_deps(actor_id)
    assert out2 is sentinel


async def test_resolver_returning_none_yields_none():
    """Registry says 404 (descriptor not found) — helper returns None so
    caller can surface as hard_fail."""
    async def resolver(aid: str):
        return None

    dapr_actors.register_target_deps_resolver(resolver)

    out = await dapr_actors._resolve_target_deps("target::missing::00000000aaaaaaaa")
    assert out is None


async def test_resolver_raising_propagates_to_caller():
    """Registry is unreachable / 5xx — the helper does NOT swallow the
    error. Caller (actor run method) sees the exception and surfaces it."""
    async def resolver(aid: str):
        raise RegistryClientError("registry GET ... failed: ConnectError: ...")

    dapr_actors.register_target_deps_resolver(resolver)

    with pytest.raises(RegistryClientError):
        await dapr_actors._resolve_target_deps("target::x::aaaaaaaa00000000")


async def test_fallback_disabled_returns_none():
    """LEGBA_DEPS_FALLBACK_ENABLED=0 — resolver is registered but not consulted."""
    os.environ["LEGBA_DEPS_FALLBACK_ENABLED"] = "0"

    async def resolver(aid: str):
        pytest.fail("resolver should not be called when fallback disabled")
        return _SentinelDeps(tag="never")

    dapr_actors.register_target_deps_resolver(resolver)
    out = await dapr_actors._resolve_target_deps("target::x::11111111ffffffff")
    assert out is None


async def test_no_resolver_returns_none_silently():
    """Fallback enabled (default) but no resolver registered — return
    None, do not raise. The actor's downstream hard_fail surfaces the gap."""
    out = await dapr_actors._resolve_target_deps("target::nobody::22222222aaaaaaaa")
    assert out is None


async def test_clear_deps_registry_removes_resolvers():
    """The test hook also clears installed resolvers — so one test can't
    bleed configuration into the next."""
    async def resolver(aid: str):
        return _SentinelDeps(tag="should_not_persist")

    dapr_actors.register_target_deps_resolver(resolver)
    dapr_actors.register_analyst_deps_resolver(resolver)

    dapr_actors.clear_deps_registry()

    assert (
        await dapr_actors._resolve_target_deps("target::after::33333333aaaaaaaa")
    ) is None
    assert (
        await dapr_actors._resolve_analyst_deps("analyst::after::33333333aaaaaaaa")
    ) is None


# ---------------------------------------------------------------------------
# RegistryHTTPClient — sanity check against the live registry surface.
# ---------------------------------------------------------------------------


async def test_registry_client_returns_none_on_404():
    """Proves the HTTP client wires correctly: a GET for a non-existent
    descriptor returns ``None`` rather than raising. This is the path the
    resolver uses when a freshly-arriving actor_id has no descriptor."""
    client = RegistryHTTPClient()
    try:
        # A descriptor that almost certainly doesn't exist.
        result = await client.get_descriptor(
            f"nonexistent_{uuid.uuid4().hex}",
            family="target",
        )
        assert result is None
    finally:
        await client.close()


async def test_registry_client_fails_loud_on_unreachable_host():
    """When the registry is unreachable, the client raises
    RegistryClientError rather than silently returning None. Per Lewis's
    "fail loud, not silent" guidance for substrate-coordination paths."""
    client = RegistryHTTPClient(
        base_url="http://127.0.0.1:1",  # nothing listens on port 1
        timeout_seconds=1.0,
    )
    try:
        with pytest.raises(RegistryClientError):
            await client.get_descriptor("any_id", family="target")
    finally:
        await client.close()
