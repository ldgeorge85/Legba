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

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from ..source_classification import (
    MAX_DECLARED_CLASSIFICATIONS,
    TAG_MAX_LENGTH,
    TAG_PATTERN,
    classification_shape_error,
    tag_shape_error,
)
from .lifecycle import AbstractionLevel, LifecycleState, WireEnumCoercion
from .properties import Cron
from .source_claim import ClaimDeclaration
from .source_follow import SourceFollow
from .source_series_api import SERIES_API_KIND, validate_series_api_descriptor
from .source_sink import SourceSink
from .source_subscribe import SourceSubscribe, validate_subscribe_descriptor

# Source ids follow the stack-component naming convention `source.<provider>.<purpose>`
# (dots allowed, unlike target/analyst ids).
SourceId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$", max_length=128)]
GeoCode = Annotated[str, Field(pattern=r"^[A-Z]{2,3}$")]


def _checked(rule):
    def _check(v: str) -> str:
        err = rule(v)
        if err is not None:
            raise ValueError(err)
        return v
    return _check


# W10 TAX-2 (W8-F1): a tag is a bare ``[a-z][a-z0-9_]*`` value OR a namespaced
# ``<family>:<value>`` whose prefix is family-shaped (``src``, a platform family,
# ``<profile>.<axis>``) — one regex (``source_classification.TAG_PATTERN``), the
# family-shape check beside it. Every bare tag that validated before still does.
Tag = Annotated[
    str,
    Field(pattern=TAG_PATTERN, max_length=TAG_MAX_LENGTH),
    AfterValidator(_checked(tag_shape_error)),
]
# A DECLARED classification: always ``<family>:<value>``, never bare, never the
# reserved ``src``. Its family and value are validated against the registered
# vocabulary at registration (the registry refuses an unloaded family by name).
ClassificationTag = Annotated[
    str,
    Field(pattern=TAG_PATTERN, max_length=TAG_MAX_LENGTH),
    AfterValidator(_checked(classification_shape_error)),
]

# S1-T8 — editorial CLASS of a source, for analysis-side source tiering. A fixed,
# auditable taxonomy (kept in lock-step with the collection-doctrine map in
# ``legba.data.analysts.deterministic_handlers.collection_gap``):
#   * ``reporting``    — wire services / news outlets (the conservative default);
#   * ``analysis``     — think-tanks / research / OSINT collectives;
#   * ``official``     — government / IGO / primary-source publishers;
#   * ``state_media``  — state-controlled outlets (read as FRAMING evidence and
#                        an official-position signal, LOW-tier for establishing
#                        facts — see the narrative_coordination unit prompt);
#   * ``claim``        — C-1: a party's OWN claim (a group saying it did
#                        something; a tracker listing what groups posted).
#                        Supports only "<claimant> claimed/posted/listed Y",
#                        attributed and dated — never Y as fact, never a
#                        corroboration, never a credibility input. Requires a
#                        ``scope.claim`` declaration (source_claim.py); the
#                        support rules are code (legba.data.claim_support).
SourceClass = Literal["reporting", "analysis", "official", "state_media", "claim"]

# The license
# class a source's content carries, stamped SourceScope → every signal's
# ``payload.license_class`` at ingest (source_actor) and read by the OpenSearch
# corpus facet + the P2-2 evidence-archiver retention gate. The closed
# enum; ``None`` (the field default) = unset/unreviewed — honest absence, NOT
# ``unknown`` (which is a REVIEWED "we looked and could not determine" verdict).
# The orthogonal flags + the derived public_ledger_ok firewall bit are not
# yet fielded.
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

# WHO may read what this source produces, once it is in
# our own substrate. Orthogonal to LicenseClass (what we may KEEP of a fetched
# page) — see legba.data.provenance.access for the full axis writeup and the
# closed ACCESS_CLASSES vocabulary this Literal mirrors. Defaults FAIL CLOSED
# to "restricted" (never "public"): every committed source_*.yaml sets this
# explicitly per the taxonomy in tests/data_pkg/test_access_class_taxonomy.py,
# and a descriptor that omits it is presumed NOT reviewed rather than open.
AccessClass = Literal[
    "public",
    "licensed_commercial",
    "licensed_noncommercial",
    "restricted",
    "internal",
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
    # W10 TAX-2: what this source's stream IS — ``<family>:<value>`` tags of
    # registered vocabulary families, stamped on every signal it writes
    # (``signals.tags`` gets the keys, ``signals.data.classifications`` the
    # bound entries; ``legba.data.source_classification``). OPTIONAL and
    # HASH-NEUTRAL: an empty list is left out of the dump, so every existing
    # descriptor's body and content hash are byte-identical.
    classifications: list[ClassificationTag] = Field(
        default_factory=list,
        max_length=MAX_DECLARED_CLASSIFICATIONS,
        exclude_if=lambda v: not v,
    )
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
    # Wave-E access-class stamp (see :data:`AccessClass`). Defaults to
    # "restricted" — FAIL CLOSED, never "public" — so a descriptor nobody has
    # classified yet reads as unreviewed rather than open. The ingest path
    # (source_actor.write_canonical_signal) copies it onto every signal's
    # ``access_class`` column (migration 0216); the read routes' opt-in
    # ``access_class_in`` filter reads that column. Enforcement is SEAMS #59.
    access_class: AccessClass = "restricted"
    # N-R: the provider's required attribution line, verbatim (e.g. "Source:
    # IMF PortWatch (portwatch.imf.org)"). OPTIONAL and HASH-NEUTRAL: unset is
    # left out of the dump, so every existing body and content hash is
    # byte-identical. The observation sink copies it (with license_class) onto
    # each row's provenance; data/observation_attribution.py reads it back.
    attribution: str | None = Field(
        default=None, min_length=1, max_length=256, exclude_if=lambda v: v is None,
    )
    # C-1: the claimant + sign-off record of a ``source_class: claim`` source
    # (data/schemas/source_claim.py). Required for that class, refused for any
    # other. HASH-NEUTRAL: absent is left out of the dump.
    claim: ClaimDeclaration | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def _claim_declared_iff_claim_class(self) -> "SourceScope":
        if self.source_class == "claim" and self.claim is None:
            raise ValueError(
                "scope.source_class 'claim' needs a scope.claim block naming the "
                "declared claimant (claim.claimant): a claim row is always "
                "attributed to a named claimant"
            )
        if self.source_class != "claim" and self.claim is not None:
            raise ValueError(
                f"scope.claim is only for source_class 'claim' (this source is "
                f"{self.source_class!r})"
            )
        return self


# ---------------------------------------------------------------------------
# Acquisition: cadence (poll) + provision (upstream registration)
# ---------------------------------------------------------------------------


class CadenceBlock(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schedule: Cron | None = None          # poll sources only; None for push
    cooldown_seconds: int = Field(default=0, ge=0)
    jitter_seconds: int = Field(default=0, ge=0)
    # A feed that is QUIET BY NATURE (USGS significant quakes: days
    # with nothing new are the norm). The liveness watchdog's empty-poll streak
    # page skips it; the cadence-stall and error paths still watch it. Left out
    # of the dump when False, so every existing body and hash is unchanged.
    expect_quiet: bool = Field(default=False, exclude_if=lambda v: v is False)
    # How long (hours, since the last ingested signal) an honestly
    # quiet empty-poll streak must last before it pages. Unset = the platform
    # floor (``runtime.source_quiet_evidence.QUIET_PAGE_MIN_ELAPSED_S``, 72 h:
    # longer than a weekend); 0 = the poll count alone decides. Left out of the
    # dump when unset, so every existing body and hash is unchanged.
    quiet_page_after_hours: int | None = Field(
        default=None, ge=0, exclude_if=lambda v: v is None,
    )


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

    @model_validator(mode="after")
    def _keyword_gate_is_an_ingestion_filter(self) -> "SourcePipeline":
        # W3 SRC-3: ``keyword_gate`` DROPS. SourceCore runs it from
        # ``ingestion_filters`` before enrichment; the ``enrichment`` chain is
        # annotate-only (a None falls back to the unenriched signal), so a gate
        # declared there would silently drop nothing. Refuse it at validation.
        if any(st.kind == "keyword_gate" for st in self.enrichment):
            raise ValueError(
                "pipeline.enrichment may not contain 'keyword_gate' — it is a "
                "dropping filter; declare it under pipeline.ingestion_filters "
                "(it runs there before enrichment)"
            )
        return self


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


class SourceFetch(BaseModel):
    """How a poll source's fetches are made (N-L) — protocol only, never the
    client identity. ``http_version``: ``auto`` (the normal client — HTTP/2
    offered — with ONE HTTP/1.1 retry on a CDN/WAF protocol block, remembered
    per host for a week), ``h2`` (never fall back), ``http1.1`` (never offer
    HTTP/2). See ``legba.data.sources._http_version``."""

    model_config = ConfigDict(strict=True, extra="forbid")

    http_version: Literal["auto", "h2", "http1.1"] = "auto"

    def is_default(self) -> bool:
        return self.http_version == "auto"


# ---------------------------------------------------------------------------
# SourceDescriptor
# ---------------------------------------------------------------------------


class SourceDescriptor(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    identity: SourceIdentity
    scope: SourceScope = Field(default_factory=SourceScope)
    # "subscribe" (I-4): Legba holds a connection open to the upstream and
    # receives records as it emits them; the `subscribe` block names the
    # transport (built: sse; websocket / nats are refused at save, SEAMS #78).
    acquisition: Literal["poll", "push", "subscribe"] = "poll"
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
    # N-L: the pipeline FAMILY its records land in (data/schemas/source_sink.py).
    # Default `document` = today's signals path; the default block is left out
    # of the dump so every existing body and content hash is byte-identical.
    sink: SourceSink = Field(default_factory=SourceSink, exclude_if=lambda v: v.is_default())
    # N-L: fetch protocol posture (hash-neutral at its default, like `sink`).
    fetch: SourceFetch = Field(default_factory=SourceFetch, exclude_if=lambda v: v.is_default())
    # I-1: which outbound references the ingestion intake may follow from this
    # source's documents (data/schemas/source_follow.py). Absent = none; left
    # out of the dump when absent, so every existing hash is unchanged.
    follow: SourceFollow | None = Field(default=None, exclude_if=lambda v: v is None)
    # I-4: how a SUBSCRIBED source's connection is held (transport, url,
    # bounds). Required with acquisition='subscribe', refused otherwise; left
    # out of the dump when absent, so every existing hash is unchanged.
    subscribe: SourceSubscribe | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def _constraints(self) -> "SourceDescriptor":
        # I-4: the subscribe mode's save-time rules (the block, a built
        # transport — the block's own validator refuses an unbuilt one naming
        # SEAMS #78 — and a declared, non-refusing licence class).
        validate_subscribe_descriptor(
            acquisition=self.acquisition, subscribe=self.subscribe,
            license_class=self.scope.license_class,
        )
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
        if self.scope.source_class == "claim":
            _claim_source_constraints(self)
        elif "claims" in self.config:
            _claim_channel_constraints(self)
        if self.identity.kind == SERIES_API_KIND:
            # S-1: the generic series kind's config is checked AT SAVE, against
            # the series registry the observation sink enforces.
            validate_series_api_descriptor(self.config, self.sink)
        return self


#: C-1 — pipeline stages a CLAIM source may not run: each turns the claim's
#: text into something the platform reads as established (a fact row, a
#: fact-bearing graph edge). Refused at save, with the reason.
CLAIM_FORBIDDEN_STAGES: dict[str, str] = {
    "fact_extractor": "it mines the claimed event into facts; a claim supports "
                      "only '<claimant> claimed Y', never Y",
}


def _claim_source_constraints(d: "SourceDescriptor") -> None:
    """A claim source is TEXT ONLY and never a fact source (C-1)."""
    for stage in list(d.pipeline.ingestion_filters) + list(d.pipeline.enrichment):
        why = CLAIM_FORBIDDEN_STAGES.get(stage.kind)
        if why is not None:
            raise ValueError(f"a claim source may not run pipeline stage {stage.kind!r}: {why}")
    if d.pipeline.media != "reference":
        raise ValueError(
            "a claim source is text only: pipeline.media must be 'reference' "
            "(no media is fetched or processed at ingest)"
        )
    if d.deps.object_store:
        raise ValueError("a claim source is text only: deps.object_store (media retain) is refused")
    if d.follow is not None:
        raise ValueError(
            "a claim source may not declare a follow policy: links in a claim are "
            "never followed (no archive fetch, no linked files)"
        )
    if d.sink.family != "document":
        raise ValueError("a claim source lands documents only (sink.family 'document')")
    if d.identity.state == LifecycleState.ACTIVE and (d.scope.claim is None or d.scope.claim.sign_off is None):
        raise ValueError(
            "claim_sign_off_required: a claim source cannot be active without a "
            "recorded scope.claim.sign_off {by, at, note}"
        )


def _claim_channel_constraints(d: "SourceDescriptor") -> None:
    """C-2 — a descriptor that marks a CHANNEL ``claim`` (Telegram's
    ``config.classes`` + ``config.claims``) holds that channel to a claim
    source's rules: no follow policy, no media retain, documents only, and
    no ``active`` state while any claim channel lacks its own sign-off. The
    descriptor's other channels are unchanged; the per-row rules (stamp,
    text hygiene, the fact extractor's pass-through) key on the channel."""
    from ..claim_support import channel_claims, unsigned_claim_channels  # noqa: PLC0415

    if not channel_claims(d.config):
        return
    if d.follow is not None:
        raise ValueError("a descriptor with a claim channel may not declare a follow policy: "
                         "links in a claim are never followed")
    if d.deps.object_store or d.pipeline.media != "reference":
        raise ValueError("a descriptor with a claim channel is text only "
                         "(pipeline.media 'reference', no deps.object_store)")
    if d.sink.family != "document":
        raise ValueError("a descriptor with a claim channel lands documents only")
    unsigned = unsigned_claim_channels(d.config)
    if d.identity.state == LifecycleState.ACTIVE and unsigned:
        raise ValueError(
            f"claim_sign_off_required: claim channel(s) {unsigned} have no recorded "
            "config.claims.<channel>.sign_off {by, at, note}; the descriptor cannot be active"
        )


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
    "AccessClass",
    "ClassificationTag",
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
    "SourceFetch",
    "FilterStage",
    "Subscription",
    "SourceSelector",
    "SourceRef",
    "Tag",
]
