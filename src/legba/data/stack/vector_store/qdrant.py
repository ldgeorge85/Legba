# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""QdrantVectorStoreHandler — L-121 Phase-2 stack component handler.

Implements the L-102 `KindHandler` shape for the Qdrant vector store. Wraps
the L-001 `legba.data.qdrant.QdrantStore` connection wrapper and exposes a
typed operations surface (upsert / search / delete / collection CRUD) plus
the lifecycle + healthcheck hooks the runtime (L-103) will call.

Naming convention (per task brief):

  * `legba_signals`               — shared default collection (survives
                                    per L-091 §2.2; the only signal collection).
  * `legba_target__<target_id>`    — optional per-target collection.

Collection dim / distance defaults come from `VectorStoreConfig` (L-101 §5)
when the handler is configured from a stack descriptor; the convenience
constructor `from_legba_data_qdrant_config` reads the L-001 bootstrap
config instead, for in-tree callers that don't need the registry path yet.

Filter syntax (passed to `search`):

  * native Qdrant `Filter` model from `qdrant_client.http.models`, or
  * a plain dict that mirrors that shape. The handler normalises the dict
    into a `Filter` (see `_normalise_filter`).

Supported clauses (documented as part of the public surface):

  * **match**       — `{"match": {"value": <val>}}`, `MatchAny`, `MatchText`,
                      etc. — i.e. anything Qdrant's `Match` discriminated
                      union accepts. We pass through unchanged.
  * **range**       — `{"range": {"gt": ..., "gte": ..., "lt": ..., "lte": ...}}`.
  * **has_id**      — `{"has_id": [<id>, ...]}` becomes a `HasIdCondition`.
  * **geo**         — `{"geo_bounding_box": {...}}`, `{"geo_radius": {...}}`,
                      `{"geo_polygon": {...}}` — all pass through to the
                      corresponding Qdrant condition model.

Mixing `must` / `should` / `must_not` clauses is supported.

This module does NOT mock anything. Tests run against the real Qdrant
container per `tests/data_pkg/conftest.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any,
    Iterable,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

from pydantic import BaseModel, ConfigDict, Field

try:
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.http import models as qmodels
except Exception:  # pragma: no cover — qdrant-client must be installed
    AsyncQdrantClient = None  # type: ignore[assignment]
    qmodels = None  # type: ignore[assignment]

from ...config import QdrantConfig
from ...qdrant import QdrantStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — naming convention per task brief.
# ---------------------------------------------------------------------------

#: Shared default collection (the one collection that survives L-091's audit).
SHARED_SIGNALS_COLLECTION = "legba_signals"

#: Per-target collection prefix. Naming: `legba_target__<target_id>`.
#: Double-underscore separator matches the L-001 `data/qdrant.py` helper
#: and the L-151 dedupe collection naming (`legba_dedup__<target_id>`).
TARGET_COLLECTION_PREFIX = "legba_target__"


def collection_name_for_target(target_id: str) -> str:
    """Return the canonical per-target collection name for a target id.

    Matches the project-wide `legba_<purpose>__<target_id>` convention. The handler
    refuses target ids containing `/` or whitespace; Qdrant collection
    names accept a broad character set but we keep ours conservative.
    """
    if not target_id:
        raise ValueError("target_id must be non-empty")
    bad = {ch for ch in target_id if ch.isspace() or ch in "/\\"}
    if bad:
        raise ValueError(f"target_id contains forbidden characters: {sorted(bad)}")
    return f"{TARGET_COLLECTION_PREFIX}{target_id}"


# ---------------------------------------------------------------------------
# Typed payload models — surface for callers (no Qdrant types in the API).
# ---------------------------------------------------------------------------


class VectorPoint(BaseModel):
    """One vector to upsert. Mirrors `qmodels.PointStruct` but is a stable
    surface owned by the handler; callers don't import Qdrant client types."""

    model_config = ConfigDict(extra="forbid")

    id: str | int
    vector: list[float]
    payload: dict[str, Any] = Field(default_factory=dict)


class ScoredPoint(BaseModel):
    """One search hit. Mirrors `qmodels.ScoredPoint` (id + score + payload)."""

    model_config = ConfigDict(extra="ignore")

    id: str | int
    score: float
    payload: dict[str, Any] = Field(default_factory=dict)
    vector: list[float] | None = None


# Filter type accepted by `search()` — either a native Qdrant model or a dict.
VectorStoreFilter = Any  # qmodels.Filter | dict[str, Any] | None
# Note: kept loose at the type-system level so callers aren't forced to
# import `qdrant_client.http.models`. Runtime normalisation is in
# `_normalise_filter`.


# ---------------------------------------------------------------------------
# Handler config — pydantic model.
# ---------------------------------------------------------------------------


class QdrantVectorStoreConfig(BaseModel):
    """Handler-local config. Independent of `legba.data.schemas.stack.
    VectorStoreConfig` (the descriptor-side schema, L-101) — this is the
    parsed, post-credential-resolution view the handler operates on.

    Constructable from either the descriptor-side `VectorStoreConfig` (via
    `from_descriptor`) or from the L-001 bootstrap `QdrantConfig`
    (via `from_legba_data_qdrant_config`)."""

    model_config = ConfigDict(extra="forbid")

    host: str = "localhost"
    port: int = 6333
    grpc_port: int = 6334
    https: bool = False
    api_key: str | None = None

    default_dim: int = 1024
    default_distance: Literal["Cosine", "Dot", "Euclid", "Manhattan"] = "Cosine"
    master_collection: str = SHARED_SIGNALS_COLLECTION

    @classmethod
    def from_legba_data_qdrant_config(
        cls,
        cfg: QdrantConfig,
        *,
        default_distance: str = "Cosine",
    ) -> "QdrantVectorStoreConfig":
        return cls(
            host=cfg.host,
            port=cfg.port,
            grpc_port=cfg.grpc_port,
            https=cfg.https,
            api_key=cfg.api_key,
            default_dim=cfg.signals_dim,
            default_distance=default_distance,  # type: ignore[arg-type]
            master_collection=cfg.signals_collection,
        )


# ---------------------------------------------------------------------------
# Minimal context shape (forward-compatible with L-103 RuntimeContext).
# Defined locally so this module doesn't depend on Phase-5 runtime code.
# The runtime spec (L-103) will subsume these once it lands; the field
# names match the L-102 §1 sketch so downcasting is straightforward.
# ---------------------------------------------------------------------------


@dataclass
class ConfigureContext:
    """Per-instance configure context. Forward-compatible slice of L-102
    `ConfigureContext` — the runtime will inject the same field names and
    additional ones (stack resolver, secrets, NATS, etc.)."""

    instance_id: str
    instance_version: str = "0.1.0"
    logger: logging.Logger = field(default_factory=lambda: logger)


@dataclass
class RuntimeContext(ConfigureContext):
    """Lifecycle context. Carries the configured logger forward; richer
    fields land with L-103."""


@dataclass
class HandlerHealth:
    """Mirrors L-102 §1 `HandlerHealth`."""

    state: Literal["healthy", "degraded", "unhealthy"]
    last_success_at: datetime | None
    last_error: str | None
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# KindHandler base protocol slice (for type-checking only — full protocol
# lives in L-102 §1; runtime_checkable so handlers satisfy it structurally).
# ---------------------------------------------------------------------------


@runtime_checkable
class _KindHandlerLike(Protocol):
    kind: str
    family: str
    schema_version: str
    handler_version: str

    async def on_configure(self, ctx: ConfigureContext) -> None: ...
    async def on_activate(self, ctx: RuntimeContext) -> None: ...
    async def on_pause(self, ctx: RuntimeContext) -> None: ...
    async def on_retire(self, ctx: RuntimeContext) -> None: ...
    async def health_check(self, ctx: RuntimeContext) -> HandlerHealth: ...


# ---------------------------------------------------------------------------
# Distance mapping
# ---------------------------------------------------------------------------


_DISTANCE_MAP = {
    "Cosine": "COSINE",
    "Dot": "DOT",
    "Euclid": "EUCLID",
    "Manhattan": "MANHATTAN",
}


def _distance(name: str) -> Any:
    if qmodels is None:  # pragma: no cover
        raise RuntimeError("qdrant-client not installed")
    key = _DISTANCE_MAP.get(name)
    if key is None:
        raise ValueError(f"unsupported distance metric: {name!r}")
    return getattr(qmodels.Distance, key)


# ---------------------------------------------------------------------------
# Filter normalisation — accept dict, return qmodels.Filter (or None).
# ---------------------------------------------------------------------------


def _normalise_filter(value: VectorStoreFilter) -> Any | None:
    """Accept either a `qmodels.Filter`, a plain dict matching its shape,
    or None. Return a `qmodels.Filter` instance or None."""
    if value is None:
        return None
    if qmodels is None:  # pragma: no cover
        raise RuntimeError("qdrant-client not installed")
    if isinstance(value, qmodels.Filter):
        return value
    if isinstance(value, dict):
        # Qdrant's pydantic `Filter` model parses dict input verbatim, including
        # nested discriminated conditions (match / range / has_id / geo).
        return qmodels.Filter.model_validate(value)
    raise TypeError(
        f"filter must be qmodels.Filter, dict, or None; got {type(value).__name__}"
    )


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


class QdrantVectorStoreHandler:
    """Phase-2 vector-store stack-component handler.

    Conforms to the L-102 `KindHandler` shape (`kind = "vector_store"`,
    `family = "stack"` — stack components are not in the source/filter/
    output/analyst/discovery families of §1; we tag with `"stack"` so the
    registry can disambiguate, consistent with L-102 §7).

    Reuses the L-001 `QdrantStore` connection wrapper rather than holding
    a separate `AsyncQdrantClient` — single connection-lifecycle owner.
    """

    # ---- KindHandler ClassVars (L-102 §1) ----
    kind: str = "vector_store"
    family: str = "stack"
    schema_version: str = "legba/stack/vector_store/1-0-0"
    handler_version: str = "0.1.0"
    config_schema: type[BaseModel] = QdrantVectorStoreConfig

    def __init__(self, cfg: QdrantVectorStoreConfig):
        self._cfg = cfg
        self._store: QdrantStore | None = None
        self._configured = False
        self._activated = False
        self._last_success: datetime | None = None
        self._last_error: str | None = None
        self._log = logger

    # ------------------------------------------------------------------
    # Convenience constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "QdrantVectorStoreHandler":
        """Build the handler from the L-001 bootstrap env config."""
        cfg = QdrantVectorStoreConfig.from_legba_data_qdrant_config(
            QdrantConfig.from_env()
        )
        return cls(cfg)

    @property
    def cfg(self) -> QdrantVectorStoreConfig:
        return self._cfg

    @property
    def store(self) -> QdrantStore:
        if self._store is None:
            raise RuntimeError("QdrantVectorStoreHandler is not configured")
        return self._store

    # ------------------------------------------------------------------
    # Lifecycle hooks (L-102 §1)
    # ------------------------------------------------------------------

    async def on_configure(self, ctx: ConfigureContext) -> None:
        """Verify connectivity and ensure the master collection exists.

        The master collection (`legba_signals` by default) is the substrate
        anchor — per L-091 §2.2 it's the only signals collection that
        survives. If it doesn't exist, create it with `default_dim` /
        `default_distance` from the handler config.
        """
        # Build a `QdrantStore` that points at the same endpoint as our config.
        qdrant_cfg = QdrantConfig(
            host=self._cfg.host,
            port=self._cfg.port,
            grpc_port=self._cfg.grpc_port,
            api_key=self._cfg.api_key,
            https=self._cfg.https,
            signals_collection=self._cfg.master_collection,
            signals_dim=self._cfg.default_dim,
        )
        store = QdrantStore(qdrant_cfg)
        await store.connect()
        try:
            # Connectivity probe — non-empty get_collections is not required
            # at configure time, but the call MUST succeed.
            collections = await store.list_collections()
            if self._cfg.master_collection not in collections:
                await store.client.create_collection(
                    collection_name=self._cfg.master_collection,
                    vectors_config=qmodels.VectorParams(
                        size=self._cfg.default_dim,
                        distance=_distance(self._cfg.default_distance),
                    ),
                )
                self._log.info(
                    "qdrant master collection created: %s (dim=%d, distance=%s)",
                    self._cfg.master_collection,
                    self._cfg.default_dim,
                    self._cfg.default_distance,
                )
        except Exception:
            await store.close()
            raise
        self._store = store
        self._configured = True
        self._last_success = _utcnow()
        self._last_error = None
        self._log.info(
            "QdrantVectorStoreHandler configured: instance=%s endpoint=%s:%d master=%s",
            ctx.instance_id, self._cfg.host, self._cfg.port,
            self._cfg.master_collection,
        )

    async def on_activate(self, ctx: RuntimeContext) -> None:
        """Mark the handler ready to serve traffic.

        Phase-2 has no admission control yet (L-160 owns scheduling); this
        is a logical state transition + a re-verify of the master collection.
        """
        if not self._configured:
            raise RuntimeError(
                "on_activate called before on_configure for instance "
                f"{ctx.instance_id!r}"
            )
        collections = await self.store.list_collections()
        if self._cfg.master_collection not in collections:
            raise RuntimeError(
                f"master collection {self._cfg.master_collection!r} missing at activate"
            )
        self._activated = True
        self._last_success = _utcnow()

    async def on_pause(self, ctx: RuntimeContext) -> None:
        """Pause: stop serving traffic but keep the connection warm.

        Phase-2 minimal implementation: a flag flip. The runtime is expected
        to fence external callers; the handler doesn't enforce read/write
        gating until L-160 owns that.
        """
        self._activated = False

    async def on_retire(self, ctx: RuntimeContext) -> None:
        """Retire: close the connection. Does NOT drop collections — that
        is data-destructive and only happens via explicit operator action."""
        if self._store is not None:
            try:
                await self._store.close()
            finally:
                self._store = None
        self._configured = False
        self._activated = False

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health_check(self, ctx: RuntimeContext | None = None) -> HandlerHealth:
        """Per L-102 §1 + brief item 5: `get_collections()` returning a
        non-empty list is the healthy signal. An empty list is *degraded*
        (Qdrant reachable but no collections — including ours — exist)."""
        if self._store is None:
            return HandlerHealth(
                state="unhealthy",
                last_success_at=self._last_success,
                last_error="not configured",
            )
        try:
            collections = await self._store.list_collections()
        except Exception as exc:
            self._last_error = str(exc)
            return HandlerHealth(
                state="unhealthy",
                last_success_at=self._last_success,
                last_error=str(exc),
            )

        self._last_success = _utcnow()
        self._last_error = None

        master_present = self._cfg.master_collection in collections
        state: Literal["healthy", "degraded", "unhealthy"]
        if not collections:
            state = "degraded"
        elif not master_present:
            state = "degraded"
        else:
            state = "healthy"
        return HandlerHealth(
            state=state,
            last_success_at=self._last_success,
            last_error=None,
            detail={
                "collections": list(collections),
                "master_collection_present": master_present,
                "master_collection": self._cfg.master_collection,
            },
        )

    # ------------------------------------------------------------------
    # Operations surface — brief item 2
    # ------------------------------------------------------------------

    async def upsert(
        self,
        collection: str,
        vectors: Sequence[VectorPoint],
        wait: bool = True,
    ) -> int:
        """Upsert points into `collection`. Returns the number written."""
        if not vectors:
            return 0
        points = [
            qmodels.PointStruct(id=p.id, vector=list(p.vector), payload=dict(p.payload))
            for p in vectors
        ]
        await self.store.client.upsert(
            collection_name=collection,
            points=points,
            wait=wait,
        )
        return len(points)

    async def search(
        self,
        collection: str,
        vector: Sequence[float],
        top_k: int = 10,
        filter: VectorStoreFilter = None,
        with_payload: bool = True,
        with_vectors: bool = False,
    ) -> list[ScoredPoint]:
        """Cosine / dot / euclid search depending on the collection's metric.

        `filter` accepts a `qmodels.Filter`, a dict in the same shape, or
        None (see module docstring for supported clauses).

        Uses `query_points` under the hood (qdrant-client 1.10+ deprecated
        `search` in favour of the unified query API). The signature this
        method exposes is intentionally stable so callers don't need to
        track Qdrant API churn.
        """
        q_filter = _normalise_filter(filter)
        resp = await self.store.client.query_points(
            collection_name=collection,
            query=list(vector),
            query_filter=q_filter,
            limit=int(top_k),
            with_payload=with_payload,
            with_vectors=with_vectors,
        )
        out: list[ScoredPoint] = []
        for r in resp.points:
            payload = dict(r.payload) if r.payload is not None else {}
            vec = list(r.vector) if (with_vectors and r.vector is not None) else None
            out.append(
                ScoredPoint(id=r.id, score=float(r.score), payload=payload, vector=vec)
            )
        return out

    async def delete(
        self,
        collection: str,
        ids: Sequence[str | int],
    ) -> int:
        """Delete by point id. Returns the count requested (Qdrant doesn't
        return per-id success — count of input ids is the contract)."""
        if not ids:
            return 0
        await self.store.client.delete(
            collection_name=collection,
            points_selector=qmodels.PointIdsList(points=list(ids)),
            wait=True,
        )
        return len(ids)

    async def create_collection(
        self,
        name: str,
        dim: int | None = None,
        distance: str = "Cosine",
    ) -> bool:
        """Create a collection. Returns True if newly created, False if it
        already existed. Idempotent: re-create with the same params is a no-op."""
        size = int(dim if dim is not None else self._cfg.default_dim)
        existing = await self.store.list_collections()
        if name in existing:
            return False
        await self.store.client.create_collection(
            collection_name=name,
            vectors_config=qmodels.VectorParams(
                size=size,
                distance=_distance(distance),
            ),
        )
        return True

    async def delete_collection(self, name: str) -> bool:
        """Delete a collection. Refuses to delete the master collection
        unless the caller passes the exact name — gentle guardrail, no
        soft-delete. Returns True if a collection was dropped."""
        existing = set(await self.store.list_collections())
        if name not in existing:
            return False
        await self.store.client.delete_collection(collection_name=name)
        return True

    async def list_collections(self) -> list[str]:
        """List all collections currently in the Qdrant instance."""
        return await self.store.list_collections()

    # ------------------------------------------------------------------
    # Per-target convenience — brief item 3
    # ------------------------------------------------------------------

    async def ensure_target_collection(
        self,
        target_id: str,
        dim: int | None = None,
        distance: str = "Cosine",
    ) -> str:
        """Create a per-target collection on demand. Returns the canonical
        collection name (`legba_target__<target_id>`)."""
        name = collection_name_for_target(target_id)
        await self.create_collection(name, dim=dim, distance=distance)
        return name

    async def delete_target_collection(self, target_id: str) -> bool:
        """Delete a per-target collection. Returns True if dropped."""
        name = collection_name_for_target(target_id)
        return await self.delete_collection(name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)
