# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for :mod:`legba.runtime.substrate_singleton_factory` (#235).

Covers the lazy re-resolving holders for the qdrant client, the hosted
embedding service, and the composed :class:`PostgresQdrantSubstrateQueryPort`
— the fix for the 2026-07-23 ~18:54 outage (a deploy that recreated the
registry + runtime simultaneously raced the boot-time ONE-SHOT lookups,
pinning both clients — and therefore the substrate port — to ``None`` for
the rest of the process's lifetime).

No real network / no live registry: every test injects a fake at the
``build_*_from_stack_component`` layer (monkeypatched onto the factory
module each holder calls into) so the suite is fully hermetic, mirroring
:mod:`tests.runtime.test_nlp_client_factory`'s ``_ToggleRegistry`` pattern
one level up (the holder wraps the SAME factory functions those tests
already cover end-to-end against a mocked registry transport — duplicating
that here would just re-test the factory, not the holder).

Coverage map:

  * ``LazyQdrantClient`` — resolves on first use; caches success; does
    NOT cache failure (retries on the next ``get()``); backoff cooldown
    prevents a retry storm inside the window; concurrent first-use fans
    out exactly one build.
  * ``LazyEmbeddingService`` — same shape, mirrored.
  * ``LazySubstrateQueryPort`` — builds only once BOTH deps resolve;
    degrades gracefully (still builds) when only the embedder is
    unavailable; re-raises the qdrant holder's failure when qdrant itself
    is unavailable; caches the constructed port permanently.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from legba.runtime import substrate_singleton_factory as ssf
from legba.runtime.embedding_factory import EmbeddingFactoryError
from legba.runtime.qdrant_factory import QdrantFactoryError
from legba.runtime.substrate_singleton_factory import (
    DEFAULT_RETRY_COOLDOWN_SECONDS,
    LazyEmbeddingService,
    LazyQdrantClient,
    LazySubstrateQueryPort,
)


# ---------------------------------------------------------------------------
# Fake clock — deterministic backoff testing without real sleeps.
# ---------------------------------------------------------------------------


class _FakeClock:
    """Monotonic-shaped fake clock the tests advance explicitly."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


# ---------------------------------------------------------------------------
# LazyQdrantClient
# ---------------------------------------------------------------------------


_FAKE_QDRANT = object()  # stand-in for an AsyncQdrantClient instance
_FAKE_EMBEDDER = object()  # stand-in for a HostedEmbeddingClient instance


class _ToggleQdrantBuilder:
    """Patches ``build_qdrant_client_from_stack_component`` with a
    toggleable success/failure stand-in. Records call count."""

    def __init__(self) -> None:
        self.available = False
        self.calls = 0

    async def __call__(self, component_id: str, *, registry_client: Any) -> Any:
        self.calls += 1
        if not self.available:
            raise QdrantFactoryError(f"qdrant unreachable for {component_id!r}")
        return _FAKE_QDRANT


class _ToggleEmbeddingBuilder:
    """Mirrors :class:`_ToggleQdrantBuilder` for the embedding factory."""

    def __init__(self) -> None:
        self.available = False
        self.calls = 0

    async def __call__(
        self, component_id: str, *, registry_client: Any, secrets_resolve: Any,
    ) -> Any:
        self.calls += 1
        if not self.available:
            raise EmbeddingFactoryError(
                f"embedding endpoint unreachable for {component_id!r}"
            )
        return _FAKE_EMBEDDER


@pytest.mark.asyncio
async def test_lazy_qdrant_resolves_on_first_use(monkeypatch: pytest.MonkeyPatch) -> None:
    builder = _ToggleQdrantBuilder()
    builder.available = True
    monkeypatch.setattr(ssf, "build_qdrant_client_from_stack_component", builder)

    lazy = LazyQdrantClient(registry_client=object(), component_id="vec.test")
    assert lazy.cached is None
    assert builder.calls == 0

    client = await lazy.get()
    assert client is _FAKE_QDRANT
    assert lazy.cached is client
    assert builder.calls == 1
    assert lazy.attempt_count == 1


@pytest.mark.asyncio
async def test_lazy_qdrant_caches_success(monkeypatch: pytest.MonkeyPatch) -> None:
    builder = _ToggleQdrantBuilder()
    builder.available = True
    monkeypatch.setattr(ssf, "build_qdrant_client_from_stack_component", builder)

    lazy = LazyQdrantClient(registry_client=object(), component_id="vec.test")
    first = await lazy.get()
    second = await lazy.get()
    third = await lazy.get()

    assert first is second is third
    # Built exactly once despite three get() calls — the whole point of the
    # holder (a live client is reused, never rebuilt).
    assert builder.calls == 1


@pytest.mark.asyncio
async def test_lazy_qdrant_boot_failure_then_lazy_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE #235 regression guard: a boot-time failure must NOT stick.

    Simulates the outage exactly — the registry is unreachable at the
    first ``get()`` (the boot-time attempt inside
    ``bring_up_production_runtime``), then becomes reachable moments
    later (the registry container finishes coming up) and a SUBSEQUENT
    ``get()`` (a later analyst deps build) must heal — no restart.
    """
    builder = _ToggleQdrantBuilder()
    builder.available = False  # boot-before-registry-ready
    clock = _FakeClock()
    monkeypatch.setattr(ssf, "build_qdrant_client_from_stack_component", builder)

    lazy = LazyQdrantClient(
        registry_client=object(), component_id="vec.test", clock=clock,
    )

    # Boot-time attempt: fails loud (NOT a silently-cached None).
    with pytest.raises(QdrantFactoryError):
        await lazy.get()
    assert lazy.cached is None
    assert builder.calls == 1

    # Advance PAST the cooldown window so the next get() actually re-attempts
    # rather than short-circuiting on the cached failure.
    clock.advance(DEFAULT_RETRY_COOLDOWN_SECONDS + 1.0)

    # The registry finishes coming up — the NEXT get() (a later deps build,
    # e.g. the first consult_on_demand activation attempt after the race)
    # heals: builds + caches the client, with NO process restart involved.
    builder.available = True
    client = await lazy.get()
    assert client is _FAKE_QDRANT
    assert lazy.cached is client
    assert builder.calls == 2

    # And stays cached thereafter — a THIRD get() does not re-resolve.
    again = await lazy.get()
    assert again is client
    assert builder.calls == 2


@pytest.mark.asyncio
async def test_lazy_qdrant_backoff_prevents_retry_storm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated get() calls INSIDE the cooldown window must NOT re-hit the
    registry — a burst of callers during an outage (e.g. several consult
    requests seconds apart) must not turn into a retry storm."""
    builder = _ToggleQdrantBuilder()
    builder.available = False
    clock = _FakeClock()
    monkeypatch.setattr(ssf, "build_qdrant_client_from_stack_component", builder)

    lazy = LazyQdrantClient(
        registry_client=object(), component_id="vec.test", clock=clock,
    )

    with pytest.raises(QdrantFactoryError):
        await lazy.get()
    assert builder.calls == 1

    # Five more calls, all still inside the cooldown window (clock unmoved).
    for _ in range(5):
        with pytest.raises(QdrantFactoryError):
            await lazy.get()
    # NOT re-resolved — every call inside the window re-raised the SAME
    # cached failure without a new registry round-trip.
    assert builder.calls == 1

    # Advance to just BEFORE the cooldown elapses — still suppressed.
    clock.advance(DEFAULT_RETRY_COOLDOWN_SECONDS - 0.5)
    with pytest.raises(QdrantFactoryError):
        await lazy.get()
    assert builder.calls == 1

    # Advance past the cooldown — the NEXT call retries for real.
    clock.advance(1.0)
    with pytest.raises(QdrantFactoryError):
        await lazy.get()
    assert builder.calls == 2


@pytest.mark.asyncio
async def test_lazy_qdrant_success_first_try_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the registry answers on the FIRST try (the common / non-outage
    case), behaviour is byte-for-byte the old eager-build contract: one
    call, one build, client returned immediately."""
    builder = _ToggleQdrantBuilder()
    builder.available = True
    monkeypatch.setattr(ssf, "build_qdrant_client_from_stack_component", builder)

    lazy = LazyQdrantClient(registry_client=object(), component_id="vec.test")
    client = await lazy.get()

    assert client is _FAKE_QDRANT
    assert builder.calls == 1
    assert lazy.attempt_count == 1


@pytest.mark.asyncio
async def test_lazy_qdrant_concurrent_first_use_builds_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    builder = _ToggleQdrantBuilder()
    builder.available = True
    monkeypatch.setattr(ssf, "build_qdrant_client_from_stack_component", builder)

    lazy = LazyQdrantClient(registry_client=object(), component_id="vec.test")
    results = await asyncio.gather(*[lazy.get() for _ in range(8)])

    assert all(r is results[0] for r in results)
    assert builder.calls == 1


# ---------------------------------------------------------------------------
# LazyEmbeddingService — mirrors LazyQdrantClient's coverage (thinner: the
# resolution logic is identical, so we don't re-run every backoff variant).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lazy_embedding_resolves_on_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _ToggleEmbeddingBuilder()
    builder.available = True
    monkeypatch.setattr(ssf, "build_embedding_service_from_stack_component", builder)

    lazy = LazyEmbeddingService(
        registry_client=object(),
        secrets_resolve=AsyncMock(return_value=b"key"),
        component_id="embed.test",
    )
    assert lazy.cached is None

    client = await lazy.get()
    assert client is _FAKE_EMBEDDER
    assert lazy.cached is client
    assert builder.calls == 1


@pytest.mark.asyncio
async def test_lazy_embedding_boot_failure_then_lazy_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _ToggleEmbeddingBuilder()
    builder.available = False
    clock = _FakeClock()
    monkeypatch.setattr(ssf, "build_embedding_service_from_stack_component", builder)

    lazy = LazyEmbeddingService(
        registry_client=object(),
        secrets_resolve=AsyncMock(return_value=b"key"),
        component_id="embed.test",
        clock=clock,
    )

    with pytest.raises(EmbeddingFactoryError):
        await lazy.get()
    assert lazy.cached is None
    assert builder.calls == 1

    clock.advance(DEFAULT_RETRY_COOLDOWN_SECONDS + 1.0)
    builder.available = True
    client = await lazy.get()
    assert client is _FAKE_EMBEDDER
    assert lazy.cached is client
    assert builder.calls == 2


@pytest.mark.asyncio
async def test_lazy_embedding_caches_success_no_restorm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _ToggleEmbeddingBuilder()
    builder.available = True
    monkeypatch.setattr(ssf, "build_embedding_service_from_stack_component", builder)

    lazy = LazyEmbeddingService(
        registry_client=object(),
        secrets_resolve=AsyncMock(return_value=b"key"),
        component_id="embed.test",
    )
    first = await lazy.get()
    second = await lazy.get()
    assert first is second
    assert builder.calls == 1


# ---------------------------------------------------------------------------
# LazySubstrateQueryPort
# ---------------------------------------------------------------------------


class _FakePort:
    """Stand-in the test monkeypatches in place of PostgresQdrantSubstrateQueryPort
    so the holder's construction call is observable without touching the
    real port's asyncpg-typed constructor."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def _patch_real_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the real PostgresQdrantSubstrateQueryPort import target so
    LazySubstrateQueryPort.get() builds a _FakePort instead — the module
    under test does `from .substrate_query_port import
    PostgresQdrantSubstrateQueryPort` INSIDE the method (not at module
    top), so we patch it on the substrate_query_port module directly."""
    import legba.runtime.substrate_query_port as sqp_module

    monkeypatch.setattr(sqp_module, "PostgresQdrantSubstrateQueryPort", _FakePort)


def _qdrant_holder(*, available: bool, monkeypatch: pytest.MonkeyPatch) -> LazyQdrantClient:
    builder = _ToggleQdrantBuilder()
    builder.available = available
    monkeypatch.setattr(ssf, "build_qdrant_client_from_stack_component", builder)
    return LazyQdrantClient(registry_client=object(), component_id="vec.test")


def _embedding_holder(
    *, available: bool, monkeypatch: pytest.MonkeyPatch,
) -> LazyEmbeddingService:
    builder = _ToggleEmbeddingBuilder()
    builder.available = available
    monkeypatch.setattr(ssf, "build_embedding_service_from_stack_component", builder)
    return LazyEmbeddingService(
        registry_client=object(),
        secrets_resolve=AsyncMock(return_value=b"key"),
        component_id="embed.test",
    )


@pytest.mark.asyncio
async def test_substrate_port_builds_once_both_deps_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qdrant_holder = _qdrant_holder(available=True, monkeypatch=monkeypatch)
    # Separate attribute on the same `ssf` module namespace — patching it
    # here does not disturb the qdrant patch `_qdrant_holder` just installed.
    embed_builder = _ToggleEmbeddingBuilder()
    embed_builder.available = True
    monkeypatch.setattr(ssf, "build_embedding_service_from_stack_component", embed_builder)
    embedding_holder = LazyEmbeddingService(
        registry_client=object(),
        secrets_resolve=AsyncMock(return_value=b"key"),
        component_id="embed.test",
    )

    lazy_port = LazySubstrateQueryPort(
        pg_pool=object(),
        qdrant_holder=qdrant_holder,
        embedding_holder=embedding_holder,
        world_context_collection="world_context",
        tradecraft_collection="tradecraft",
    )
    assert lazy_port.cached is None

    port = await lazy_port.get()
    assert isinstance(port, _FakePort)
    assert port.kwargs["qdrant_client"] is _FAKE_QDRANT
    assert port.kwargs["embedder"] is _FAKE_EMBEDDER
    assert lazy_port.cached is port

    # A second get() does not rebuild.
    again = await lazy_port.get()
    assert again is port


@pytest.mark.asyncio
async def test_substrate_port_boot_failure_then_lazy_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE #235 regression guard at the port layer: qdrant unreachable at
    first get() → port stays unbuilt → qdrant recovers → the NEXT get()
    (a later consult_on_demand deps build) builds the port for real."""
    qdrant_builder = _ToggleQdrantBuilder()
    qdrant_builder.available = False
    clock = _FakeClock()
    monkeypatch.setattr(ssf, "build_qdrant_client_from_stack_component", qdrant_builder)
    qdrant_holder = LazyQdrantClient(
        registry_client=object(), component_id="vec.test", clock=clock,
    )

    lazy_port = LazySubstrateQueryPort(
        pg_pool=object(),
        qdrant_holder=qdrant_holder,
        embedding_holder=None,
        world_context_collection="world_context",
        tradecraft_collection="tradecraft",
    )

    with pytest.raises(QdrantFactoryError):
        await lazy_port.get()
    assert lazy_port.cached is None

    clock.advance(DEFAULT_RETRY_COOLDOWN_SECONDS + 1.0)
    qdrant_builder.available = True
    port = await lazy_port.get()
    assert isinstance(port, _FakePort)
    assert lazy_port.cached is port


@pytest.mark.asyncio
async def test_substrate_port_builds_without_embedder_when_embedder_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing/unreachable embedder must NOT withhold the whole port —
    the port degrades its OWN vector_search/search_context methods to
    no_embedder_wired (existing contract); qdrant alone is sufficient to
    build."""
    qdrant_holder = _qdrant_holder(available=True, monkeypatch=monkeypatch)
    embed_builder = _ToggleEmbeddingBuilder()
    embed_builder.available = False  # embedder stays unreachable
    monkeypatch.setattr(ssf, "build_embedding_service_from_stack_component", embed_builder)
    embedding_holder = LazyEmbeddingService(
        registry_client=object(),
        secrets_resolve=AsyncMock(return_value=b"key"),
        component_id="embed.test",
    )

    lazy_port = LazySubstrateQueryPort(
        pg_pool=object(),
        qdrant_holder=qdrant_holder,
        embedding_holder=embedding_holder,
        world_context_collection="world_context",
        tradecraft_collection="tradecraft",
    )
    port = await lazy_port.get()
    assert isinstance(port, _FakePort)
    assert port.kwargs["qdrant_client"] is _FAKE_QDRANT
    assert port.kwargs["embedder"] is None


@pytest.mark.asyncio
async def test_substrate_port_no_embedding_holder_builds_qdrant_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """embedding_holder=None (never even offered) is a distinct, equally
    valid path from "offered but unreachable" — both degrade the same way."""
    qdrant_holder = _qdrant_holder(available=True, monkeypatch=monkeypatch)
    lazy_port = LazySubstrateQueryPort(
        pg_pool=object(),
        qdrant_holder=qdrant_holder,
        embedding_holder=None,
        world_context_collection="world_context",
        tradecraft_collection="tradecraft",
    )
    port = await lazy_port.get()
    assert isinstance(port, _FakePort)
    assert port.kwargs["embedder"] is None


@pytest.mark.asyncio
async def test_substrate_port_threads_opensearch_and_collections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constructor kwargs (world_context/tradecraft collections + the
    opensearch store) ride through to the real port unchanged."""
    qdrant_holder = _qdrant_holder(available=True, monkeypatch=monkeypatch)
    fake_os_store = object()
    lazy_port = LazySubstrateQueryPort(
        pg_pool=object(),
        qdrant_holder=qdrant_holder,
        embedding_holder=None,
        world_context_collection="wc_custom",
        tradecraft_collection="tc_custom",
        opensearch_store=fake_os_store,
    )
    port = await lazy_port.get()
    assert port.kwargs["world_context_collection"] == "wc_custom"
    assert port.kwargs["tradecraft_collection"] == "tc_custom"
    assert port.kwargs["opensearch_store"] is fake_os_store
