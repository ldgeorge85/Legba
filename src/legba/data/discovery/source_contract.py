# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source-discovery contract (P-13) — the *find-the-sources* counterpart.

The target-discovery family (``_contract.py`` / :class:`CandidateTarget`) emits
candidate *targets* the registry materialises into ``target_descriptors``. The
source-discovery family emits candidate *sources* the registry materialises into
``source_descriptors`` — the same Prometheus service-discovery → scrape-config
split, one leaf up the stack.

Where it fits
-------------

  * A static source descriptor (``discovery is None``) *is* its own source
    instance (one RSS feed, one webhook).
  * An L2/L3 source *template* with a
    :class:`~legba.data.schemas.source.SourceDiscoveryBlock` materialises N
    source instances per cycle — e.g. "every RSS feed listed in this OPML / on
    this index page", "every host in this CT-log stream", "every collection in
    this MediaCloud directory".

Per ``SourceDiscoveryBlock.validate_before_register`` (the P-13 default), a
candidate source does NOT become a registered descriptor until it passes a
**liveness + trial pull/parse** probe (see
:mod:`legba.data.discovery.source_validate`). A feed that 404s, or returns a
body the handler can't parse, is rejected — never registered — so the source
pool stays clean and the selector auto-wire (PIVOT §4.4) only ever attracts
working sources.

This module declares:

  * :class:`CandidateSource` — one materialised source seed yielded by a
    source-discovery handler. Mirrors :class:`CandidateTarget`: stable
    ``natural_key`` + raw ``label_set`` + ``source_metadata`` + ``evidence``.
    The relabel chain (reused from :mod:`legba.data.discovery.relabel`) rewrites
    the labels into a :class:`~legba.data.schemas.source.SourceDescriptor`
    template's variables.
  * :class:`SourceCandidateValidation` — the validate-before-register verdict
    (liveness + trial-pull). Carried alongside the candidate so the registry
    knows whether to register or reject.
  * :class:`SourceDiscoveryHealth` — health-probe shape (mirrors the
    target-side :class:`DiscoveryHealth`).
  * :class:`SourceDiscoveryKind` — runtime-checkable Protocol for a
    source-discovery kind: ``discover(ctx) -> AsyncIterator[CandidateSource]``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import (
    Any,
    AsyncIterator,
    ClassVar,
    Literal,
    Mapping,
    Protocol,
    runtime_checkable,
)

from pydantic import BaseModel, ConfigDict, Field

from ._contract import DiscoveryContext, DiscoveryEvidence


# ---------------------------------------------------------------------------
# CandidateSource
# ---------------------------------------------------------------------------


class CandidateSource(BaseModel):
    """One materialised source seed yielded by a source-discovery handler.

    The source-side twin of :class:`legba.data.discovery._contract.CandidateTarget`.
    The handler populates this; the registry-side source materialiser
    (:mod:`legba.data.discovery.materializer`) applies the relabel chain and
    expands the candidate into a full
    :class:`~legba.data.schemas.source.SourceDescriptor`.

      * ``natural_key`` is the stable per-candidate id (the feed URL for a
        crawl/query source-discovery, the CT-log host, the org id, ...).
        **Not** the resulting source id (that comes from the relabel chain).
      * ``label_set`` is the raw labels the relabel chain rewrites into
        source-descriptor template variables.
      * ``source_kind`` is the kind the materialised *source instance* will be
        (``rss`` / ``firecrawl`` / ...). Carried explicitly so the validate-
        before-register probe can build the right trial handler.
      * ``probe_config`` is the per-candidate config the trial-pull probe needs
        (typically just ``{"url": ...}``) — separate from ``label_set`` so the
        probe doesn't depend on relabel having run yet.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    natural_key: str = Field(..., alias="id", min_length=1)
    """Stable id of this candidate within the discovery (e.g. a feed URL)."""

    source_kind: str = Field(..., min_length=1)
    """The source-handler kind the materialised instance will run as
    (``rss`` / ``firecrawl`` / ``gdelt_query`` / ...). Drives the trial probe."""

    label_set: Mapping[str, Any] = Field(default_factory=dict, alias="labels")
    """Raw labels the relabel chain consumes + rewrites."""

    source_metadata: Mapping[str, Any] = Field(default_factory=dict)
    """Arbitrary per-candidate extras (index page, OPML group, discovered-at).
    Separate from labels so rules don't accidentally consume them."""

    probe_config: Mapping[str, Any] = Field(default_factory=dict)
    """Config the validate-before-register trial probe needs to build a handler
    (typically ``{"url": "..."}``). Distinct from the relabel output."""

    evidence: DiscoveryEvidence = Field(default_factory=DiscoveryEvidence)
    """Per-candidate provenance — reused from the target-discovery contract."""

    seen_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    """When the discovery yielded this candidate."""

    @property
    def id(self) -> str:
        return self.natural_key

    @property
    def labels(self) -> Mapping[str, Any]:
        return self.label_set


# ---------------------------------------------------------------------------
# SourceCandidateValidation — validate-before-register verdict
# ---------------------------------------------------------------------------


class SourceCandidateValidation(BaseModel):
    """Verdict of the liveness + trial-pull probe for one candidate source.

    Per ``SourceDiscoveryBlock.validate_before_register``: a candidate that
    fails this probe is *rejected* — never written to ``source_descriptors`` —
    and routed to the DLQ with the reason. A candidate that passes proceeds to
    relabel + register.
    """

    model_config = ConfigDict(extra="forbid")

    natural_key: str
    valid: bool
    """True iff the candidate passed liveness AND trial pull/parse."""

    live: bool = False
    """True iff the liveness probe succeeded (handler.health_check healthy or
    the trial pull returned at least one signal without error)."""

    trial_signals: int = 0
    """Number of signals the trial pull produced (0 is allowed for a live-but-
    empty feed unless ``require_nonempty`` was set on the probe)."""

    reason: str = ""
    """Human-readable rejection reason when ``valid`` is False. Empty on pass."""

    detail: dict[str, Any] = Field(default_factory=dict)
    """Probe-specific extras (HTTP status, parse error class, sample title)."""


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class SourceDiscoveryHealth(BaseModel):
    """Source-discovery-handler health probe result."""

    model_config = ConfigDict(extra="forbid")

    state: Literal["healthy", "degraded", "unhealthy"] = "healthy"
    last_success_at: datetime | None = None
    last_error: str | None = None
    candidates_24h: int = 0
    validated_sources: int = 0
    rejected_sources: int = 0
    detail: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# SourceDiscoveryKind protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SourceDiscoveryKind(Protocol):
    """Structural-typing surface for source-discovery kinds.

    A concrete handler exposes:

      * ``kind``: ClassVar[str] — registered kind name
        (``query_source_discovery``, ``crawl_source_discovery``, ...).
      * ``family``: ClassVar[Literal["source_discovery"]].
      * ``schema_version``: ClassVar[str].
      * ``config_schema``: ClassVar[type[BaseModel]].
      * ``discover(ctx) -> AsyncIterator[CandidateSource]``.
      * ``healthcheck(ctx) -> SourceDiscoveryHealth``.

    Re-uses the target-side :class:`DiscoveryContext` so the actor-resolved
    dep bundle + state store + secrets resolver thread through unchanged.
    """

    kind: ClassVar[str]
    family: ClassVar[Literal["source_discovery"]]
    schema_version: ClassVar[str]
    config_schema: ClassVar[type[BaseModel]]

    def discover(
        self,
        ctx: DiscoveryContext,
    ) -> AsyncIterator[CandidateSource]: ...

    async def healthcheck(
        self, ctx: DiscoveryContext
    ) -> SourceDiscoveryHealth: ...


__all__ = [
    "CandidateSource",
    "SourceCandidateValidation",
    "SourceDiscoveryHealth",
    "SourceDiscoveryKind",
]
