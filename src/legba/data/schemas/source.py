# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source descriptor schema — the source-first pivot's first-class acquisition unit.

Per ``docs/PIVOT_PROPOSAL.md`` §4.1–§4.4 + ``docs/PIVOT_BUILD_PLAN.md`` P-04.
A ``SourceDescriptor`` is a peer of ``TargetDescriptor`` / ``AnalystDescriptor``:
content-hashed, lifecycle-FSM'd, audited, registry-managed. It owns ingestion
(one pull/connection per source, regardless of how many targets consume it);
targets reference sources via :class:`SourceRef` + :class:`Subscription` and
match the shared raw pool by predicate.

Mirrors the conventions in :mod:`legba.data.schemas.target` (strict,
``extra="forbid"``, property factories, lifecycle states).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .lifecycle import AbstractionLevel, LifecycleState, WireEnumCoercion
from .properties import Cron

# Source ids follow the stack-component naming convention `source.<provider>.<purpose>`
# (dots allowed, unlike target/analyst ids).
SourceId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$", max_length=128)]
GeoCode = Annotated[str, Field(pattern=r"^[A-Z]{2,3}$")]
Tag = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]

# S1-T8 — editorial CLASS of a source, for analysis-side source tiering. A fixed,
# auditable taxonomy (kept in lock-step with the collection-doctrine map in
# ``legba.data.analysts.deterministic_handlers.collection_gap``):
#   * ``reporting``    — wire services / news outlets (the conservative default);
#   * ``analysis``     — think-tanks / research / OSINT collectives;
#   * ``official``     — government / IGO / primary-source publishers;
#   * ``state_media``  — state-controlled outlets (read as FRAMING evidence and
#                        an official-position signal, LOW-tier for establishing
#                        facts — see the narrative_coordination unit prompt).
SourceClass = Literal["reporting", "analysis", "official", "state_media"]

# LIC-2 (planning/SOURCE_LICENSING_LEDGER_2026-07-10.md §E.4) — the license
# class a source's content carries, stamped SourceScope → every signal's
# ``payload.license_class`` at ingest (source_actor) and read by the OpenSearch
# corpus facet + the P2-2 evidence-archiver retention gate. The closed §E.4
# enum; ``None`` (the field default) = unset/unreviewed — honest absence, NOT
# ``unknown`` (which is a REVIEWED "we looked and could not determine" verdict).
# The orthogonal flags + the derived public_ledger_ok firewall bit remain the
# rest of the LIC-2 build (not yet fielded).
LicenseClass = Literal[
    "public_domain",             # US-gov / CC0
    "open_gov_attribution",      # OGL / Etalab / EU / IGO-reuse
    "cc_by",
    "cc_by_sa",
    "cc_nc",                     # any NC variant
    "open_data_sharealike",      # ODbL
    "permissive_feed_unreviewed",  # the honest default for public RSS
    "tos_restrictive",           # reviewed-restrictive publisher terms
    "personal_use_only",
    "api_terms",
    "anti_ai_walled",            # TollBit/402 or anti-inference EULA
    "unknown",
]


# ---------------------------------------------------------------------------
# Identity + scope
# ---------------------------------------------------------------------------


class SourceIdentity(WireEnumCoercion):
    # strict=True + enum fields ⇒ the wire's bare strings need the mixin's
    # ``mode='before'`` coercers; see :class:`WireEnumCoercion`.
    model_config = ConfigDict(strict=True, extra="forbid")

    id: SourceId
    name: str
    kind: str                       # source-handler kind: rss / qualys_vmdr / crowdstrike_falcon / ...
    schema_uri: str = Field(pattern=r"^legba/source/\d+\.\d+\.\d+$")
    version: str = Field(pattern=r"^[a-f0-9]{16,64}$")     # content hash
    abstraction_level: AbstractionLevel = AbstractionLevel.L1
    inherits: list[SourceId] = Field(default_factory=list, max_length=8)
    state: LifecycleState = LifecycleState.DRAFT
    owner: str
    created: datetime
    retire_after: datetime | None = None


class SourceScope(BaseModel):
    """Scope metadata a source advertises — what target source-selectors match against."""

    model_config = ConfigDict(strict=True, extra="forbid")

    owner_tenant: str = "default"   # tenancy seam (indexed); single-tenant-first enforcement
    geo: list[GeoCode] = Field(default_factory=list)  # OPTIONAL now (pivot scope relax)
    languages: list[str] = Field(default_factory=list)
    tags: list[Tag] = Field(default_factory=list)
    # S1-T8: editorial class of the source (see :data:`SourceClass`). OPTIONAL /
    # defaulted to ``reporting`` (the conservative "when unsure" bucket) so every
    # pre-S1-T8 descriptor still validates unchanged; the analysis side reads it
    # (e.g. narrative_coordination weighs ``state_media`` as framing, not fact).
    source_class: SourceClass = "reporting"
    # LIC-2 stamp (see :data:`LicenseClass`). OPTIONAL / defaulted to ``None``
    # (unset — every pre-LIC-2 descriptor validates unchanged). When set, the
    # ingest path copies it onto every signal's ``payload.license_class``
    # (source_actor), where the corpus facet + the P2-2 evidence-archiver
    # retention gate read it.
    license_class: LicenseClass | None = None


# ---------------------------------------------------------------------------
# Acquisition: cadence (poll) + provision (upstream registration)
# ---------------------------------------------------------------------------


class CadenceBlock(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schedule: Cron | None = None          # poll sources only; None for push
    cooldown_seconds: int = Field(default=0, ge=0)
    jitter_seconds: int = Field(default=0, ge=0)


class ProvisionBlock(BaseModel):
    """Outbound upstream registration for sources that subscribe upstream first.

    The full reconciliation/idempotency spec is P-06 (PIVOT §4.2.1); this is the
    declarative shape. ``register`` / ``deregister`` describe the upstream call;
    ``credential_secret`` is a vault ref for that call. ``watch_param_field``
    names the per-subscription parameter folded into the upstream watch
    (subscriber-driven dynamic watchlist).
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    enabled: bool = False
    register_call: dict[str, Any] = Field(default_factory=dict)
    deregister_call: dict[str, Any] = Field(default_factory=dict)
    credential_secret: str | None = None
    watch_param_field: str | None = None
    idempotency_key_field: str | None = None


# ---------------------------------------------------------------------------
# Baseline pipeline + deps + output
# ---------------------------------------------------------------------------


class FilterStage(BaseModel):
    """One baseline pipeline stage — {kind, config}. (Local to avoid a
    source<->target import cycle; mirrors target.PipelineStage.)"""

    model_config = ConfigDict(strict=True, extra="allow")

    kind: str
    config: dict[str, Any] = Field(default_factory=dict)


class SourcePipeline(BaseModel):
    """Baseline enrichment that runs ONCE per signal at the source (not per target)."""

    model_config = ConfigDict(strict=True, extra="forbid")

    ingestion_filters: list[FilterStage] = Field(default_factory=list)
    enrichment: list[FilterStage] = Field(default_factory=list)
    # media: reference (default — hold a media_ref pointer) | eager (fetch+process at ingest)
    media: Literal["reference", "eager"] = "reference"


class SourceDeps(BaseModel):
    """Stack components the SourceActor must resolve at activation.

    The fix for the G20 ``ctx.stack_resolve('postgres')`` blocker: a source
    declares its substrate dependencies once, here, instead of every consuming
    target plumbing them through.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    postgres: bool = False          # e.g. iso_countries-style discovery lookups
    qdrant: bool = False            # semantic dedupe
    embedding: bool = False
    object_store: bool = False      # media retain
    vault_secrets: list[str] = Field(default_factory=list)


class SourceOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    # subject defaults to `source.<id>.signals`; subjects are COARSE
    # (tenant/source/modality/event-class) — exact matching is SQL + Starlark.
    subject_prefix: str = ""
    retention: Literal["interest", "workqueue", "limits"] = "interest"
    max_age_seconds: int = Field(default=86_400, ge=0)       # 24h default
    max_msgs: int = Field(default=1_000_000, ge=0)
    delivery: Literal["lossy", "lossless"] = "lossy"         # per-source policy


# ---------------------------------------------------------------------------
# Source discovery (source-flavored — emits CandidateSource → source_descriptors)
# ---------------------------------------------------------------------------


class SourceDiscoveryBlock(BaseModel):
    """Declared on L2/L3 source templates that materialise N source instances.

    Mirrors the target ``DiscoveryBlock``; the source-discovery materialiser
    (P-13) walks ``relabel`` per candidate. ``validate_before_register`` is the
    P-13 default (liveness + trial pull/parse before a candidate becomes a
    source descriptor).
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    kind: str                       # country_list / firecrawl / dns_zone_walk / ct / shodan_org / web_search
    list_source: str = ""
    emit_per_match: bool = True
    relabel: list[dict[str, Any]] = Field(default_factory=list)
    resync_policy: dict[str, Any] | None = None
    validate_before_register: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# SourceDescriptor
# ---------------------------------------------------------------------------


class SourceDescriptor(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    identity: SourceIdentity
    scope: SourceScope = Field(default_factory=SourceScope)
    acquisition: Literal["poll", "push"] = "poll"   # "stream" is a documented future seam
    config: dict[str, Any] = Field(default_factory=dict)   # per-kind; values are FactoryValue
    cadence: CadenceBlock | None = None
    provision: ProvisionBlock | None = None
    pipeline: SourcePipeline = Field(default_factory=SourcePipeline)
    deps: SourceDeps = Field(default_factory=SourceDeps)
    output: SourceOutput = Field(default_factory=SourceOutput)
    # who may subscribe (enforced at subscription registration; PIVOT §4.4.1)
    subscription_policy: Literal["open", "allowlist", "grant"] = "open"
    allowed_targets: list[str] = Field(default_factory=list)
    allowed_tenants: list[str] = Field(default_factory=list)
    discovery: SourceDiscoveryBlock | None = None

    @model_validator(mode="after")
    def _constraints(self) -> "SourceDescriptor":
        # An active, non-discovery POLL source needs a schedule; PUSH sources
        # don't (they're inbound-driven); discovery templates don't (their
        # materialised children pull).
        if (
            self.identity.state == LifecycleState.ACTIVE
            and self.acquisition == "poll"
            and self.discovery is None
            and (self.cadence is None or self.cadence.schedule is None)
        ):
            raise ValueError("active poll source must declare a cadence.schedule")
        if self.discovery is not None and self.identity.abstraction_level == AbstractionLevel.L1:
            raise ValueError("a source with a discovery block must be L2/L3 (a template)")
        return self


# ---------------------------------------------------------------------------
# Subscription + SourceRef (the target side of the contract)
# ---------------------------------------------------------------------------


class Subscription(BaseModel):
    """How a target slices a source's stream.

    Structured filter (indexed; pushes to coarse NATS subject + SQL WHERE) +
    optional Starlark residual (compiled once, evaluated on the narrowed set).
    ``canonical_only`` makes delivery dedup-aware (P-02): receive canonical
    signals only, not their aliases.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    # structured filter
    geo: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    tags: list[Tag] = Field(default_factory=list)
    entity_classes: list[str] = Field(default_factory=list)
    modalities: list[str] = Field(default_factory=list)
    # Starlark residual (the long tail: mentions(), severity_at_least(), host_ip in cidr(), ...)
    predicate: str | None = None
    canonical_only: bool = True

    @field_validator("predicate", mode="after")
    @classmethod
    def _compile_predicate(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from ..predicates import (
            PredicateCompilationError,
            PredicateSurface,
            compile_predicate,
        )
        try:
            compile_predicate(v, PredicateSurface.TARGET_SCOPE)
        except PredicateCompilationError as exc:
            raise ValueError(f"subscription.predicate failed to compile: {exc}") from exc
        return v


class SourceSelector(BaseModel):
    """Matches SOURCE SCOPE (not signals) — which sources a target auto-wires.

    Distinct from :class:`Subscription` (which filters signals): source selection
    is a coarse query over source-descriptor scope and decides whether a
    *discovered* source joins the target. Only ``open`` sources auto-wire;
    ``allowlist``/``grant`` sources need explicit opt-in. ``owner_tenant``
    enforces the multi-tenant boundary (a target auto-wires sources in its tenant
    or ``shared`` only).
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    tags: list[Tag] = Field(default_factory=list)
    geo: list[GeoCode] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    owner_tenant: str | None = None
    kinds: list[str] = Field(default_factory=list)   # match SourceIdentity.kind
    predicate: str | None = None                     # Starlark residual over source metadata

    @field_validator("predicate", mode="after")
    @classmethod
    def _compile_predicate(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from ..predicates import (
            PredicateCompilationError,
            PredicateSurface,
            compile_predicate,
        )
        try:
            # G4: validate on the surface this predicate is actually
            # EVALUATED on — runtime/subscription/sourceref._residual_match
            # compiles it as ANALYST_SUBSCRIPTION (descriptor-scoped helpers
            # over the source's scope). Validating as TARGET_SCOPE here let
            # signal-helper selectors register and then fail/never-match at
            # resolution time.
            compile_predicate(v, PredicateSurface.ANALYST_SUBSCRIPTION)
        except PredicateCompilationError as exc:
            raise ValueError(f"source_selector.predicate failed to compile: {exc}") from exc
        return v


class SourceRef(BaseModel):
    """A target's reference to source(s): exactly one of explicit id OR selector."""

    model_config = ConfigDict(strict=True, extra="forbid")

    source_id: SourceId | None = None                # explicit: subscribe to a named source
    source_selector: SourceSelector | None = None    # selector: any source whose SCOPE matches
    subscription: Subscription = Field(default_factory=Subscription)  # signal-level slice

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "SourceRef":
        if (self.source_id is None) == (self.source_selector is None):
            raise ValueError("SourceRef must set exactly one of source_id / source_selector")
        return self


__all__ = [
    "LicenseClass",
    "SourceClass",
    "SourceIdentity",
    "SourceScope",
    "CadenceBlock",
    "ProvisionBlock",
    "SourcePipeline",
    "SourceDeps",
    "SourceOutput",
    "SourceDiscoveryBlock",
    "SourceDescriptor",
    "FilterStage",
    "Subscription",
    "SourceSelector",
    "SourceRef",
]
