# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source-kind handler contract (L-102 §2) — minimal Protocol surface.

The full runtime context type lives in L-103 (not yet landed). Until then
this module declares the structural-typing surface that source handlers
depend on:

  * :class:`StateStore` — async per-instance key/value persistence; the
    runtime is expected to scope keys per (target_id, source_id).
  * :class:`SourceContext` — what a handler receives at ``pull`` /
    ``health_check`` / lifecycle hooks: identity + state_store + logger.
  * :class:`Signal` — one raw payload yielded by a source handler.
  * :class:`SourceHealth` — health-probe return.

The brief for L-130 specifies ``state_store`` as an accessor on
``target_ctx``; this module honors that literally — a single context type
satisfies both the descriptor identity AND the state-store accessor. When
the L-103 runtime lands and provides a richer split between
``ConfigureContext`` / ``RuntimeContext``, a future adapter can wrap them
into this :class:`SourceContext` shape so handlers remain unchanged.

All shapes are Pydantic / Protocol — no ABC inheritance — so third-party
handler packages don't need to import a Legba base class to satisfy the
contract.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Literal, Mapping, Protocol, runtime_checkable
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# StateStore protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class StateStore(Protocol):
    """Async per-instance key/value store.

    Runtime contract: keys are scoped per ``(target_id, source_id)`` by the
    host; handlers use short keys (e.g. ``"rss_cursor"``). Values are JSON-
    coercible Python objects. Crash-safe; survives actor evictions.
    """

    async def get(self, key: str) -> Any: ...
    async def set(self, key: str, value: Any) -> None: ...


class InMemoryStateStore:
    """Process-local :class:`StateStore` for tests and dev runs.

    Not crash-safe. The runtime substitutes a Postgres- or Redis-backed
    implementation in production (per topology v2 §7.1 — Dapr virtual-actor
    state).
    """

    def __init__(self, initial: Mapping[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = dict(initial or {})

    async def get(self, key: str) -> Any:
        return self._data.get(key)

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    # Test-only inspection helper; not part of the StateStore protocol.
    def snapshot(self) -> dict[str, Any]:
        return dict(self._data)


# ---------------------------------------------------------------------------
# SourceContext
# ---------------------------------------------------------------------------


class SourceContext(BaseModel):
    """Per-pull context handed to a source handler.

    Carries the descriptor identity (so the handler can stamp signals with
    ``target_id`` / ``target_version`` / ``source_id``), the parsed
    instance config, the :class:`StateStore` accessor for cursor
    persistence, an optional secrets resolver for SecretRef → live
    credential, and a bound logger.

    When L-103 runtime types land, an adapter will construct one of these
    from the runtime's :class:`RuntimeContext` so handlers don't need to
    care which executor they're running under.

    ``secrets_resolve`` is an async callable ``(vault_id) -> str``. It is
    optional so unit tests and bootstrap scripts can run without the full
    credentials package; handlers fall back to treating the config-side
    SecretRef value as the literal secret in that case.

    ``now_fn`` lets tests inject a deterministic clock via
    :meth:`utcnow`. The runtime supplies ``None`` in production.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    target_id: str
    target_version: str
    source_id: str
    config: BaseModel = Field(..., exclude=True)
    state_store: StateStore = Field(..., exclude=True)
    secrets_resolve: Any = Field(default=None, exclude=True)
    now_fn: Any = Field(default=None, exclude=True)
    logger: logging.Logger = Field(
        default_factory=lambda: logging.getLogger("legba.source"),
        exclude=True,
    )

    # Optional scope hints — present on the L-102 TargetContext; carried
    # here for handlers that want to filter by language / geo at pull time.
    scope_geo: list[str] = Field(default_factory=list)
    scope_languages: list[str] = Field(default_factory=list)

    def utcnow(self) -> datetime:
        """Time accessor — overridable for deterministic tests."""
        if self.now_fn is not None:
            return self.now_fn()
        return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------


class Signal(BaseModel):
    """One observation from a source — target-agnostic + modality-first.

    Source-first pivot contract (``docs/PIVOT_PROPOSAL.md`` §4.3). The
    observation is **source-owned**: ``target_id`` is gone — it lives only
    on derived analyst outputs (interpretation is target-owned; observation
    is shared). A target's "slice" is a predicate-filtered view over the
    shared raw pool, not a per-target copy.

    ``payload`` stays an open dict (KC-2: "stay open dict"); the per-source
    baseline pipeline populates enrichment fields; the substrate write
    validates against ``schema_uri``. ``extra='forbid'`` keeps new fields
    declared here rather than smuggled into payload.
    """

    model_config = ConfigDict(extra="forbid")

    signal_id: UUID = Field(default_factory=uuid4)

    # --- ownership / provenance (P-01) ---
    source_id: str                        # ORIGIN source = SourceDescriptor.id
    source_version: str = ""              # content-hash of the source descriptor
    produced_by_id: str | None = None     # job/analyst/actor that produced THIS row (null = raw source row)
    produced_by_kind: Literal[
        "source", "job", "analyst", "deterministic", "system"
    ] = "source"
    fetched_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    owner_tenant: str = "default"         # from SourceDescriptor.scope.owner_tenant; indexed (tenancy seam)

    # --- modality-first ---
    modality: Literal[
        "text", "image", "audio", "video", "structured", "binary"
    ] = "text"
    mime_type: str | None = None
    media_ref: str | None = None          # object-store URI / external URL — REFERENCE, not inlined
    embedding_ref: str | None = None      # qdrant point id (cross-modal retrieval)

    # --- media retention policy (schema home now; SeaweedFS handler post-core) ---
    retention_class: Literal[
        "reference_only", "retain_on_match", "retain_always", "evidence_hold"
    ] = "reference_only"
    media_ref_expires_at: datetime | None = None
    object_ref: str | None = None         # our retained copy when retention_class != reference_only

    # --- content + enrichment (populated once by the per-source baseline) ---
    payload: dict[str, Any] = Field(default_factory=dict)
    canonical_url: str | None = None
    language_hint: str | None = None      # source-provided hint (pre-detection)
    raw_provenance: dict[str, Any] = Field(default_factory=dict)
    # structured-filter columns — set by baseline enrichment, INDEXED on the
    # signals table for subscription SQL/NATS pushdown (PIVOT review §3.B).
    # A raw pre-enrichment signal has them empty; the per-source baseline fills
    # them (language_detect -> language, geocode -> geo, classify -> tags,
    # ner -> entity_classes).
    language: str | None = None
    geo: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    entity_classes: list[str] = Field(default_factory=list)
    # source-host credibility SCORE is a signal property (per-target floor
    # threshold is evaluated at subscription/read time, not stored here).
    source_credibility: float | None = None

    # --- dedup (P-02 — alias/canonical LINKS, never destructive collapse) ---
    content_hash: str = ""
    canonical_signal_id: UUID | None = None   # set by the dedup analyst; raw rows preserved + aliased

    # --- lineage ---
    derived_from: list[UUID] = Field(default_factory=list)
    schema_uri: str = "iglu:legba/signal/jsonschema/3-0-0"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class SourceHealth(BaseModel):
    """Source-handler health probe result. Same shape as L-102's
    ``SourceHealth``.

    ``detail`` is kind-specific (RSS records HTTP status / ETag; GDELT
    records BigQuery job state; etc.).
    """

    model_config = ConfigDict(extra="forbid")

    state: str = Field(default="healthy")     # healthy | degraded | unhealthy
    last_success_at: datetime | None = None
    last_error: str | None = None
    rows_pulled_24h: int = 0
    last_cursor: str | None = None
    rate_limit_remaining: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# SourceHandler protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SourceHandler(Protocol):
    """Structural-typing surface for source kinds. L-102 §2 contract.

    A concrete handler exposes:

      * ``kind``: ClassVar[str] — registered kind name.
      * ``schema_version``: ClassVar[str] — handler schema version.
      * ``config_schema``: ClassVar[type[BaseModel]] — pydantic config type.
      * ``pull(ctx, since) -> AsyncIterator[Signal]``.
      * ``health_check(ctx) -> SourceHealth``.

    Lifecycle hooks (``on_configure`` / ``on_activate`` / etc.) are runtime-
    optional; default no-op when omitted.
    """

    kind: str
    schema_version: str
    config_schema: type[BaseModel]

    def pull(
        self,
        ctx: SourceContext,
        since: datetime | None = None,
    ) -> AsyncIterator[Signal]: ...

    async def health_check(self, ctx: SourceContext) -> SourceHealth: ...


__all__ = [
    "InMemoryStateStore",
    "Signal",
    "SourceContext",
    "SourceHandler",
    "SourceHealth",
    "StateStore",
]
