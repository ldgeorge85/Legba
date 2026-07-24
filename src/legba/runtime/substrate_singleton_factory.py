# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lazy, re-resolving holders for the boot-time qdrant/embedding/substrate
singletons (#235).

Background — the 2026-07-23 ~18:54 silent outage
--------------------------------------------------

A deploy recreated the ``legba-registry`` and ``legba-runtime`` containers
simultaneously. The runtime's boot (:func:`legba.runtime.dapr_host.
bring_up_production_runtime`) raced the registry's readiness: its ONE-SHOT
``qdrant_client`` / ``embedding_service`` stack-component lookups hit
``ConnectError`` and were caught, logged, and pinned to ``None`` — for the
rest of the process's lifetime. Downstream, ``substrate_query_port`` was
never built (it required a non-``None`` ``qdrant_client``), so every
``consult_on_demand`` activation failed with "no deps registered for
actor_id" (502), and the embedder was unwired (tier-3 semantic dedupe +
``vector_search`` degraded). All silent — the containers reported
"healthy" the whole time. Nothing about the outage window itself required
a restart to fix; the registry was reachable again within seconds. The
boot-time snapshot simply never got a second try.

This mirrors EXACTLY the #91 finding that motivated
:class:`legba.runtime.nlp_client_factory.LazyNlpClient` — see that
module's docstring. This module carries the analogous fix for the
Qdrant client, the hosted embedding service, and the
:class:`~legba.runtime.substrate_query_port.PostgresQdrantSubstrateQueryPort`
built on top of them.

Design
------

:class:`LazyQdrantClient` and :class:`LazyEmbeddingService` are thin
holders — same shape as :class:`LazyNlpClient`: resolve on first
:meth:`get`, cache a SUCCESS permanently, NEVER cache a FAILURE (the next
:meth:`get` retries), and serialize concurrent first-use builds behind an
``asyncio.Lock`` so a burst of resolutions doesn't fan out N registry
round-trips.

:class:`LazySubstrateQueryPort` composes the two: it holds a reference to
the (already-lazy) qdrant + embedding holders and lazily constructs the
production :class:`PostgresQdrantSubstrateQueryPort` the first time BOTH
of its dependencies resolve. Once built it is cached permanently (same
contract as the other two) — a substrate port, once live, never needs to
be rebuilt; only the CONSTRUCTION needs to survive a boot-time race.

Cheap backoff (§1 of #235's build note): each holder additionally tracks
the wall-clock time of its last FAILED attempt and refuses to re-attempt
inside a short cooldown window (default 30s) — cheap insurance against a
caller hammering :meth:`get` in a tight loop (e.g. a burst of consult
requests arriving seconds apart during an outage) turning into a retry
storm against a registry that is still down. A caller inside the cooldown
window gets the SAME exception the last attempt raised (re-raised, not
re-fetched) rather than a fresh registry round-trip.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .embedding_factory import (
    EmbeddingFactoryError,
    build_embedding_service_from_stack_component,
)
from .qdrant_factory import QdrantFactoryError, build_qdrant_client_from_stack_component
from .registry_client import RegistryHTTPClient

if TYPE_CHECKING:  # pragma: no cover - typing only
    from qdrant_client import AsyncQdrantClient

    from .embedding_factory import HostedEmbeddingClient
    from .substrate_query_port import PostgresQdrantSubstrateQueryPort


logger = logging.getLogger(__name__)


__all__ = [
    "DEFAULT_RETRY_COOLDOWN_SECONDS",
    "LazyEmbeddingService",
    "LazyQdrantClient",
    "LazySubstrateQueryPort",
]


#: Minimum spacing between re-resolution ATTEMPTS while unresolved. Not a
#: TTL on a cached success (a success is cached forever) — a floor on how
#: often a FAILED holder will hit the registry again. Matches the build
#: note's "not more than once per 30-60s" guard.
DEFAULT_RETRY_COOLDOWN_SECONDS = 30.0


class LazyQdrantClient:
    """Lazy, re-resolving holder for the boot-time ``AsyncQdrantClient``.

    #235: the client used to be built ONCE at host bootstrap
    (``dapr_host.bring_up_production_runtime``) and cached as a possibly-
    permanent ``None`` when the registry lookup raced the runtime's own
    boot. This holder resolves on FIRST use and RE-resolves on every
    subsequent call while no client is cached, so a registry that becomes
    reachable moments later heals the vector plane on the next USE rather
    than requiring a restart.

    A successful build is cached (one client reused across calls). A
    failed build is NEVER cached as a sticky ``None`` — the next
    :meth:`get` retries (subject to the cooldown backoff) — and the
    underlying :class:`QdrantFactoryError` is RAISED so the caller decides
    whether to degrade or fail loud, exactly mirroring
    :class:`legba.runtime.nlp_client_factory.LazyNlpClient`.
    """

    def __init__(
        self,
        *,
        registry_client: RegistryHTTPClient,
        component_id: str,
        retry_cooldown_seconds: float = DEFAULT_RETRY_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry_client = registry_client
        self._component_id = component_id
        self._retry_cooldown_seconds = retry_cooldown_seconds
        self._clock = clock
        self._client: "AsyncQdrantClient | None" = None
        self._lock = asyncio.Lock()
        self._last_failure: BaseException | None = None
        self._last_attempt_at: float | None = None
        self._attempt_count = 0

    @property
    def cached(self) -> "AsyncQdrantClient | None":
        """The currently-cached client, or ``None`` if not yet resolved."""
        return self._client

    @property
    def attempt_count(self) -> int:
        """Total number of build attempts (success + failure) issued."""
        return self._attempt_count

    async def get(self) -> "AsyncQdrantClient":
        """Return the cached client, building (or re-building) it on demand.

        Raises :class:`QdrantFactoryError` on a build failure WITHOUT
        caching the failure — the next call (outside the cooldown window)
        retries. A call landing INSIDE the cooldown window re-raises the
        last failure without a new registry round-trip.
        """
        cached = self._client
        if cached is not None:
            return cached
        async with self._lock:
            # Double-check under the lock — a concurrent waiter may have
            # built it (or may have just recorded the failure we're about
            # to check the cooldown against).
            cached = self._client
            if cached is not None:
                return cached
            if self._in_cooldown():
                assert self._last_failure is not None  # _in_cooldown implies this
                raise self._last_failure
            self._attempt_count += 1
            self._last_attempt_at = self._clock()
            try:
                client = await build_qdrant_client_from_stack_component(
                    self._component_id, registry_client=self._registry_client,
                )
            except QdrantFactoryError as exc:
                self._last_failure = exc
                raise
            self._client = client
            self._last_failure = None
            logger.info(
                "substrate_singleton_factory.qdrant.lazy_built "
                "component_id=%s attempt=%d",
                self._component_id, self._attempt_count,
            )
            return client

    def _in_cooldown(self) -> bool:
        if self._last_failure is None or self._last_attempt_at is None:
            return False
        return (self._clock() - self._last_attempt_at) < self._retry_cooldown_seconds


class LazyEmbeddingService:
    """Lazy, re-resolving holder for the boot-time hosted embedding client.

    Mirrors :class:`LazyQdrantClient` exactly — see its docstring. Wraps
    :func:`legba.runtime.embedding_factory.
    build_embedding_service_from_stack_component`.
    """

    def __init__(
        self,
        *,
        registry_client: RegistryHTTPClient,
        secrets_resolve: Callable[[str], Awaitable[bytes]],
        component_id: str,
        retry_cooldown_seconds: float = DEFAULT_RETRY_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry_client = registry_client
        self._secrets_resolve = secrets_resolve
        self._component_id = component_id
        self._retry_cooldown_seconds = retry_cooldown_seconds
        self._clock = clock
        self._client: "HostedEmbeddingClient | None" = None
        self._lock = asyncio.Lock()
        self._last_failure: BaseException | None = None
        self._last_attempt_at: float | None = None
        self._attempt_count = 0

    @property
    def cached(self) -> "HostedEmbeddingClient | None":
        """The currently-cached client, or ``None`` if not yet resolved."""
        return self._client

    @property
    def attempt_count(self) -> int:
        """Total number of build attempts (success + failure) issued."""
        return self._attempt_count

    async def get(self) -> "HostedEmbeddingClient":
        """Return the cached client, building (or re-building) it on demand.

        Raises :class:`EmbeddingFactoryError` on a build failure WITHOUT
        caching the failure — see :meth:`LazyQdrantClient.get`.
        """
        cached = self._client
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._client
            if cached is not None:
                return cached
            if self._in_cooldown():
                assert self._last_failure is not None
                raise self._last_failure
            self._attempt_count += 1
            self._last_attempt_at = self._clock()
            try:
                client = await build_embedding_service_from_stack_component(
                    self._component_id,
                    registry_client=self._registry_client,
                    secrets_resolve=self._secrets_resolve,
                )
            except EmbeddingFactoryError as exc:
                self._last_failure = exc
                raise
            self._client = client
            self._last_failure = None
            logger.info(
                "substrate_singleton_factory.embedding.lazy_built "
                "component_id=%s attempt=%d",
                self._component_id, self._attempt_count,
            )
            return client

    def _in_cooldown(self) -> bool:
        if self._last_failure is None or self._last_attempt_at is None:
            return False
        return (self._clock() - self._last_attempt_at) < self._retry_cooldown_seconds


class LazySubstrateQueryPort:
    """Lazy holder for the ``consult_on_demand``-backing
    :class:`~legba.runtime.substrate_query_port.PostgresQdrantSubstrateQueryPort`.

    The production port needs a LIVE qdrant client at construction time
    (``PostgresQdrantSubstrateQueryPort.__init__`` just stores the
    reference, but a ``None`` qdrant client makes every vector-backed
    method degrade to the honest ``unavailable`` shape — the pre-#235
    boot-time contract was "build the port only if qdrant_client is
    already non-None", which is exactly the one-shot snapshot problem).
    This holder instead re-attempts qdrant resolution (via the supplied
    :class:`LazyQdrantClient`) on every :meth:`get` until it succeeds, and
    only THEN constructs the port — once. The embedder is threaded
    best-effort: if it isn't resolved yet, the port still builds (search_
    context / vector_search degrade to their own ``no_embedder_wired``
    shape, exactly as they do today when ``embedding_service`` is absent)
    — a missing embedder is not a reason to withhold the whole port.

    Once built, the port is cached permanently — a live port never needs
    rebuilding.
    """

    def __init__(
        self,
        *,
        pg_pool: Any,
        qdrant_holder: LazyQdrantClient,
        embedding_holder: "LazyEmbeddingService | None" = None,
        world_context_collection: str,
        tradecraft_collection: str,
        opensearch_store: Any | None = None,
    ) -> None:
        self._pg_pool = pg_pool
        self._qdrant_holder = qdrant_holder
        self._embedding_holder = embedding_holder
        self._world_context_collection = world_context_collection
        self._tradecraft_collection = tradecraft_collection
        self._opensearch_store = opensearch_store
        self._port: "PostgresQdrantSubstrateQueryPort | None" = None
        self._lock = asyncio.Lock()
        self._attempt_count = 0

    @property
    def cached(self) -> "PostgresQdrantSubstrateQueryPort | None":
        """The currently-cached port, or ``None`` if not yet resolved."""
        return self._port

    @property
    def attempt_count(self) -> int:
        """Total number of build attempts (success + failure) issued."""
        return self._attempt_count

    async def get(self) -> "PostgresQdrantSubstrateQueryPort":
        """Return the cached port, building it on demand.

        Raises whatever the underlying ``qdrant_holder.get()`` raises
        (:class:`QdrantFactoryError`) when qdrant is still unreachable —
        the port cannot be built without it. Never caches that failure
        (the qdrant holder itself owns the cooldown/backoff).
        """
        cached = self._port
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._port
            if cached is not None:
                return cached
            self._attempt_count += 1
            qdrant_client = await self._qdrant_holder.get()  # raises if unavailable
            embedder: Any | None = None
            if self._embedding_holder is not None:
                try:
                    embedder = await self._embedding_holder.get()
                except EmbeddingFactoryError as exc:
                    logger.warning(
                        "substrate_singleton_factory.substrate_port."
                        "embedder_unavailable err=%s — building the port "
                        "WITHOUT an embedder (vector_search/search_context "
                        "degrade to no_embedder_wired)",
                        exc,
                    )
            from .substrate_query_port import PostgresQdrantSubstrateQueryPort

            port = PostgresQdrantSubstrateQueryPort(
                pg_pool=self._pg_pool,
                qdrant_client=qdrant_client,
                embedder=embedder,
                world_context_collection=self._world_context_collection,
                tradecraft_collection=self._tradecraft_collection,
                opensearch_store=self._opensearch_store,
            )
            self._port = port
            logger.info(
                "substrate_singleton_factory.substrate_port.lazy_built "
                "attempt=%d embedder=%s corpus=%s",
                self._attempt_count,
                "wired" if embedder is not None else "absent",
                "wired" if self._opensearch_store is not None else "absent",
            )
            return port
