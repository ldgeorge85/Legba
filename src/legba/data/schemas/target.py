# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Target descriptor schema (per L-101 §3)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .lifecycle import AbstractionLevel, LifecycleState, WireEnumCoercion
from .properties import Cron, FactoryValue
from .source import SourceRef
from .action_pack import ActionPackRef

if TYPE_CHECKING:                                                # pragma: no cover
    # Used purely for static-type readers. The runtime import happens at
    # the bottom of this module via :func:`_resolve_discovery_refs` so we
    # avoid the schemas ↔ discovery._contract import cycle (sources init
    # eagerly imports mediacloud which imports schemas).
    from ..discovery._contract import RelabelRule, ResyncPolicy


# Vocabulary-typed strings (registry-validated at registration; see L-101 §8).
EntityClass = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
RelationshipType = Annotated[str, Field(pattern=r"^[A-Z][A-Za-z0-9]*$")]
TargetId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=128)]
AnalystId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=128)]


class TargetIdentity(WireEnumCoercion):
    # strict=True + enum fields ⇒ the wire's bare strings need the mixin's
    # ``mode='before'`` coercers; see :class:`WireEnumCoercion`.
    model_config = ConfigDict(strict=True, extra="forbid")

    id: TargetId
    name: str
    schema_uri: str = Field(pattern=r"^legba/target/\d+\.\d+\.\d+$")
    version: str = Field(pattern=r"^[a-f0-9]{16,64}$")  # content hash
    abstraction_level: AbstractionLevel = AbstractionLevel.L1
    inherits: list[TargetId] = Field(default_factory=list, max_length=8)
    state: LifecycleState = LifecycleState.DRAFT
    owner: str
    created: datetime
    retire_after: datetime | None = None


# Scope item types (shared across domains).
_GeoCode = Annotated[str, Field(pattern=r"^[A-Z]{2,3}$")]
_Lang = Annotated[str, Field(pattern=r"^[a-z]{2}(-[A-Z]{2})?$")]
_ScopeTag = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]


class _ScopeBase(BaseModel):
    """Shared scope fields across all target domains.

    Pivot: ``TargetScope`` is now **polymorphic by domain** (PIVOT_PROPOSAL
    §9) — a target watching a single person or a customer estate no longer
    fakes ``geo: ["XX"]``. ``geo``/``languages`` are no longer universally
    required; they live on the ``geo`` domain variant only.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    entity_classes: list[EntityClass] = Field(default_factory=list)
    relationship_types: list[RelationshipType] = Field(default_factory=list)
    time_horizon_days: int = Field(default=90, ge=1, le=3650)
    predicate: str | None = None  # Starlark, compiled at registration per L-104
    tags: list[_ScopeTag] = Field(
        default_factory=list,
        max_length=32,
        description=(
            "Free-form domain / surface tags consumed by the subscription "
            "router and the UI panel filter; also the match keys for a "
            "target's source-selector SourceRefs. snake_case identifiers."
        ),
    )

    @field_validator("predicate", mode="after")
    @classmethod
    def _compile_predicate(cls, v: str | None) -> str | None:
        """Compile the predicate at registration (compile-only, no eval)."""
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
            raise ValueError(
                f"target.scope.predicate failed to compile: {exc}"
            ) from exc
        return v


class GeoScope(_ScopeBase):
    """Geopolitical / OSINT scope — watch places in languages (founding case)."""

    domain: Literal["geo"] = "geo"
    geo: list[_GeoCode] = Field(default_factory=list)
    languages: list[_Lang] = Field(default_factory=list)


class EstateScope(_ScopeBase):
    """Asset-estate scope (ASM / customer estates)."""

    domain: Literal["estate"] = "estate"
    customer_id: str
    asset_tags: list[str] = Field(default_factory=list)
    cloud_accounts: list[str] = Field(default_factory=list)


class EntityScope(_ScopeBase):
    """Single-entity scope (a person / org / asset of interest)."""

    domain: Literal["entity"] = "entity"
    entity_ref: str
    geo: list[_GeoCode] = Field(default_factory=list)


class ThematicScope(_ScopeBase):
    """Thematic / situational scope — a non-geo FRAME (5c).

    The operator's "a target predicate CAN be 'iran war'" case: a target that
    watches a SITUATION (a cross-country theme) rather than a place. The frame
    is carried by the inherited ``scope.predicate`` (e.g.
    ``contains_any(["iran"]) and contains_any(["war","strike"])``), which the
    analyst-slice reader now applies to focus the slice (the cadence slice is no
    longer geo-only). ``themes`` is a human/semantic label set; the predicate
    does the matching. ``geo`` is optional historical context, usually empty.
    """

    domain: Literal["thematic"] = "thematic"
    themes: list[_ScopeTag] = Field(default_factory=list, max_length=32)
    geo: list[_GeoCode] = Field(default_factory=list)


# Discriminated union — the descriptor's ``scope`` field. ``domain`` selects.
TargetScope = Annotated[
    GeoScope | EstateScope | EntityScope | ThematicScope,
    Field(discriminator="domain"),
]


class DiscoveryBlock(BaseModel):
    """Per-target ``discovery`` sub-block — declared on L2/L3 templates only.

    Per L-106 § 2 + L-180 contract: a target descriptor with a ``discovery``
    block is a *template* that materialises N L1 instances per cycle. The
    L2 template's body defines what to inherit; the discovery block defines
    *how to seed candidates* and *how to rewrite their label_set into
    template variables*.

    The ``relabel`` field is the chain of :class:`RelabelRule` instances
    the registry-side discovery materialiser
    (:mod:`legba.data.registry.discovered_materializer`) walks per
    candidate. The ``resync_policy`` field carries the disappearance-ratio
    threshold + on_anomaly action; defaults match L-106 §5 (30%, pause).

    Schema constraint: a descriptor with ``discovery is not None`` must
    have ``identity.abstraction_level`` in ``{L2, L3}`` — enforced by
    :class:`TargetDescriptor._state_constraints`.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    kind: str
    list_source: str = ""
    emit_per_match: bool = True

    # Typed relabel rules per L-180 contract — the dict-of-dict form is
    # accepted by pydantic's coercion and rewritten into RelabelRule on
    # load. Avoids re-defining the rule schema here.
    relabel: list["RelabelRule"] = Field(default_factory=list)

    # Per-discovery resync policy. None → handler defaults (L-106 §5:
    # threshold=0.30, on_anomaly=alert_and_pause, min_prior_active=10).
    resync_policy: "ResyncPolicy | None" = None

    # Free-form per-kind config (mediacloud collection id, file_sd
    # directory, etc.). Surface-level extras are accepted so descriptors
    # don't have to update this schema for every new discovery kind.
    config: dict[str, Any] = Field(default_factory=dict)


class SourceBinding(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")

    id: str
    kind: str
    config: dict[str, Any] = Field(default_factory=dict)  # values are FactoryValue subclasses
    schedule: Cron | None = None
    enabled: bool = True


class PipelineStage(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")

    kind: str
    config: dict[str, Any] = Field(default_factory=dict)


class TargetPipeline(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    ingestion_filters: list[PipelineStage] = Field(default_factory=list)
    enrichment: list[PipelineStage] = Field(default_factory=list)
    routing: list[PipelineStage] = Field(default_factory=list)


class InlineAnalystBlock(BaseModel):
    """Default analyst declared inline on the target. Optional."""

    model_config = ConfigDict(strict=True, extra="allow")

    use: Literal["inline_target", "deterministic"] = "inline_target"
    analyst_ref: AnalystId | None = None
    cycle_types: list[str] = Field(default_factory=list)
    cadence: dict[str, str] = Field(default_factory=dict)
    method: dict[str, Any] = Field(default_factory=dict)
    # inline analyst inherits target.allowed_action_packs (the target is its
    # owner + context); no separate grant. (Pivot — retired tools_whitelist.)
    rubric: str | None = None


class OutputBinding(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")

    kind: Literal[
        "ui_panel", "a2a_skill", "mcp_tool", "stix_bundle",
        "webhook", "nats_stream", "alert",
    ]
    config: dict[str, Any] = Field(default_factory=dict)


class CoordinationBlock(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    subscribes_to: list[dict[str, str]] = Field(default_factory=list)
    publishes: list[str] = Field(default_factory=list)
    allow_cycles: bool = False
    cycle_hop_limit: int = Field(default=0, ge=0, le=8)


class TargetDescriptor(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    identity: TargetIdentity
    scope: TargetScope
    discovery: DiscoveryBlock | None = None
    # pivot: targets reference shared sources (explicit id or selector) +
    # subscribe by predicate — they no longer own inline SourceBindings.
    sources: list[SourceRef] = Field(default_factory=list, max_length=64)
    # action packs this target's analysts are permitted to use (PIVOT §4.8).
    allowed_action_packs: list[ActionPackRef] = Field(default_factory=list)
    pipeline: TargetPipeline = Field(default_factory=TargetPipeline)
    analyst: InlineAnalystBlock | None = None
    outputs: list[OutputBinding] = Field(default_factory=list)
    coordination: CoordinationBlock = Field(default_factory=CoordinationBlock)

    @model_validator(mode="after")
    def _state_constraints(self) -> "TargetDescriptor":
        # Discovery descriptors (L2/L3 templates that materialise N L1
        # instances) are active-but-sourceless by design — the source
        # bindings live on the materialised L1 children, not on the
        # discovery descriptor itself.  Exempt them from the sources
        # requirement.  Non-discovery active targets still need at
        # least one source.
        if (
            self.identity.state == LifecycleState.ACTIVE
            and not self.sources
            and self.discovery is None
        ):
            raise ValueError("active target must declare at least one source")
        if (
            self.discovery is not None
            and self.identity.abstraction_level == AbstractionLevel.L1
        ):
            raise ValueError("discovery block requires abstraction_level L2 or L3")
        return self


# ---------------------------------------------------------------------------
# Forward-ref resolution
# ---------------------------------------------------------------------------
#
# DiscoveryBlock.relabel + resync_policy reference RelabelRule + ResyncPolicy
# from ``legba.data.discovery._contract``. Importing those eagerly at the top
# of this module would create a cycle (sources/__init__.py eagerly imports
# mediacloud which imports schemas.properties). The forward-ref strings let
# us declare the field types at class-body time and resolve them lazily here
# — after both modules have completed top-level execution.
#
# Failure modes:
#   * If the discovery package is not importable in this process (test
#     isolation, vendored substrate package without runtime extras), the
#     forward refs stay unresolved. pydantic v2 still accepts dict values
#     for the fields and validates as Any in that case; callers wanting
#     typed access invoke :func:`legba.data.discovery.import_for_schemas`.
#
# Successful resolution is the normal path — the descriptor registry and
# the materialiser both expect typed RelabelRule / ResyncPolicy on read.


def _resolve_discovery_refs() -> None:
    """Resolve DiscoveryBlock's forward refs against the discovery contract.

    Public hook called from :mod:`legba.data.schemas.__init__` after the
    full schemas package has finished importing (so the discovery
    package — which can't be imported earlier without cycling through
    schemas → sources → mediacloud → schemas) is allowed to be loaded
    before we attempt the rebuild.

    Failure is swallowed so a partial-substrate environment without the
    discovery package can still parse legacy / static descriptors. In
    that case DiscoveryBlock stays loosely-typed (forward refs unresolved)
    and ``descriptor.discovery`` reads as a dict — callers needing the
    typed shape import this function and re-run after wiring up.
    """
    try:
        from ..discovery._contract import RelabelRule as _RR, ResyncPolicy as _RP
    except Exception:                                            # pragma: no cover
        return
    globals()["RelabelRule"] = _RR
    globals()["ResyncPolicy"] = _RP
    DiscoveryBlock.model_rebuild(force=True)
    TargetDescriptor.model_rebuild(force=True)


# Attempt the resolve at import time — works when target.py is imported
# *after* legba.data.discovery has finished loading. The schemas package
# __init__ calls _resolve_discovery_refs again post-init to cover the
# import-before-discovery path (see schemas/__init__.py).
_resolve_discovery_refs()
