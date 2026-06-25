# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discovery-kind handler contract (L-106 / L-180) — minimal Protocol surface.

The discovery family is the *fifth* leaf of the kind taxonomy alongside
``sources`` (L-102 §2), ``filters`` (L-102 §3), ``outputs`` (L-197) and
``analysts`` (L-102 §5). Where sources/filters/outputs handle the
*watch-the-things* path of an already-materialized target, **discovery
handlers emit candidate targets** — they are the *find-the-things*
counterpart.

The split (Prometheus service-discovery → scrape config) means: a discovery
kind never knows what's scraped; a target descriptor never knows how it was
discovered. A target descriptor without a ``discovery`` block is **static**
— the descriptor *is* the instance, and the registry treats it as a
one-and-only materialized target (see :mod:`legba.data.discovery.static`
for the documented no-op path). A target descriptor *with* a ``discovery``
block is a **template** that materializes N instances per discovery cycle.

This module declares:

  * :class:`DiscoveryContext` — per-discover context handed to a handler:
    descriptor identity + parsed config + state_store accessor + secrets
    resolver + bound logger.
  * :class:`CandidateTarget` — one materialized seed yielded by a discovery
    handler. Pydantic model with ``natural_key``, ``label_set``,
    ``source_metadata``, ``evidence``. The registry diffs ``natural_key``
    across runs to classify as new / retained / disappeared.
  * :class:`DiscoveryEvidence` — per-candidate provenance: where the row
    came from, when, source version. Carried alongside the label_set so
    materialized targets can lineage back to the discovery cycle.
  * :class:`RelabelRule` — Prometheus-style rewrite rule with 9 actions
    per L-106 §3. The handler emits raw labels; the *registry* (not the
    handler) applies the rule chain at materialization time.
  * :class:`DiscoveryHealth` — health-probe return; mirrors the L-102 §3
    handler-health shape with discovery-specific fields.
  * :class:`DiscoveryHandler` — runtime-checkable Protocol for the kind.

All shapes are Pydantic / Protocol — no ABC inheritance — so third-party
discovery packages don't need to import a Legba base class to satisfy the
contract. Sister to :mod:`legba.data.sources._contract`,
:mod:`legba.data.filters._contract`, and
:mod:`legba.data.outputs._contract`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    ClassVar,
    Literal,
    Mapping,
    Protocol,
    runtime_checkable,
)

from pydantic import BaseModel, ConfigDict, Field

from ..sources._contract import InMemoryStateStore, StateStore


# ---------------------------------------------------------------------------
# Relabel actions — closed set per L-106 §3
# ---------------------------------------------------------------------------


RelabelAction = Literal[
    "set",                # Copy source_labels[0] to target_label
    "set_list",           # Copy, wrapping scalar into single-element list
    "format",             # Jinja-style replacement template
    "lookup",             # Side-table lookup against a named stack table
    "lookup_languages",   # Convenience alias: country_iso2 → list of locales
    "merge_list",         # Append a static list onto an existing list label
    "keep",               # Filter-in: drop candidate if predicate is false
    "drop",               # Filter-out: drop candidate if predicate is true
    "hash_mod",           # Shard via hash(source_labels) % n == k
]

RELABEL_ACTIONS: frozenset[str] = frozenset(
    [
        "set",
        "set_list",
        "format",
        "lookup",
        "lookup_languages",
        "merge_list",
        "keep",
        "drop",
        "hash_mod",
    ]
)
"""The closed action set per L-106 §3. Adding a new action is a registered
handler class (see :mod:`legba.data.discovery.relabel`), not a schema
change — the descriptor `action:` field is a free string validated
against this set + the registered-extension set at materialization time.
"""


# ---------------------------------------------------------------------------
# DiscoveryEvidence
# ---------------------------------------------------------------------------


class DiscoveryEvidence(BaseModel):
    """Per-candidate provenance carried alongside the raw label set.

    Recorded in the discovery_state table per L-106 §2 so materialized
    targets can lineage back to the discovery cycle that produced them.
    A diff-loop comparing ``label_set_hash`` across cycles uses this to
    classify retention/update transitions.
    """

    model_config = ConfigDict(extra="forbid")

    source_id: str = ""
    """Logical id of the source the candidate came from (e.g.
    ``stack.country_lists.global_active`` or the file path for file_sd)."""

    source_version: str = ""
    """Source-side version stamp (etag, schema_uri, file mtime, etc.)."""

    row_index: int | None = None
    """Position within the discovery's emitted set, if meaningful (CSV
    row index, list index). Optional — natural_key is the stable handle."""

    fetched_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    """When the underlying row was observed."""

    extra: dict[str, Any] = Field(default_factory=dict)
    """Free-form per-kind metadata (BQ job id, S3 ETag, NATS msg id, etc.)."""


# ---------------------------------------------------------------------------
# CandidateTarget
# ---------------------------------------------------------------------------


class CandidateTarget(BaseModel):
    """One materialized target seed yielded by a discovery handler.

    The handler's only job is to populate this; the *registry* (per L-181)
    applies relabel rules and expands the candidate into a full target
    descriptor. Re-emission of the same ``natural_key`` is normal — the
    diff loop in the registry treats it as "still seen" (no-op).

    Per L-106 §2:
      * ``natural_key`` is the stable per-candidate id; ISO 3166-1 alpha-2
        for country_list_discovery, filename-plus-block-index for
        file_sd_discovery. **Not** the resulting target id (that comes
        from the relabel chain).
      * ``label_set`` is the raw labels keyed however the source is
        shaped. The relabel chain rewrites these into descriptor
        template variables.
      * ``source_metadata`` is arbitrary per-discovery extras (region,
        sub-region, schema_uri lookups, etc.) — separate from labels so
        rules can choose to lift fields explicitly.

    The field name ``id`` is an alias for ``natural_key`` so the task
    brief's CandidateTarget(id, labels, source_metadata) shape parses
    cleanly. The two names share storage.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    natural_key: str = Field(..., alias="id", min_length=1)
    """Stable id of this candidate within the discovery (e.g. ``BR``)."""

    label_set: Mapping[str, Any] = Field(default_factory=dict, alias="labels")
    """Raw labels emitted by the discovery. Free-form. Relabel rules
    consume + rewrite these per L-106 §3."""

    source_metadata: Mapping[str, Any] = Field(default_factory=dict)
    """Arbitrary per-candidate extras (region, sub-region, list-source
    pointers). Separate from labels so rules don't accidentally consume
    them."""

    evidence: DiscoveryEvidence = Field(default_factory=DiscoveryEvidence)
    """Per-candidate provenance — see :class:`DiscoveryEvidence`."""

    seen_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    """When the discovery yielded this candidate. Distinct from
    ``evidence.fetched_at`` (the underlying row's observation timestamp);
    these match for cheap pull-once kinds but diverge for cached/replayed
    discoveries."""

    # --- convenience aliases -------------------------------------------

    @property
    def id(self) -> str:
        """Alias for :attr:`natural_key` to match the L-180 brief shape."""
        return self.natural_key

    @property
    def labels(self) -> Mapping[str, Any]:
        """Alias for :attr:`label_set`."""
        return self.label_set


# ---------------------------------------------------------------------------
# RelabelRule — Prometheus-style rewrite
# ---------------------------------------------------------------------------


class RelabelRule(BaseModel):
    """One relabel rule from a discovery descriptor's ``relabel:`` block.

    Per L-106 §3, rules apply in declared order, each rule's output
    feeding the next. Actions:

    +-------------------+--------------------------------------------------+
    | action            | semantics                                        |
    +===================+==================================================+
    | ``set``           | Copy ``source_labels[0]`` to ``target_label``.   |
    +-------------------+--------------------------------------------------+
    | ``set_list``      | Copy, wrapping scalar into a one-element list.   |
    +-------------------+--------------------------------------------------+
    | ``format``        | Jinja-style replacement template; filters        |
    |                   | ``lower / upper / slug / trim``.                 |
    +-------------------+--------------------------------------------------+
    | ``lookup``        | Side-table lookup against a named stack lookup   |
    |                   | table (``lookup_table.languages_by_country``).   |
    +-------------------+--------------------------------------------------+
    | ``lookup_languages``| Convenience alias: country_iso2 → locale list. |
    +-------------------+--------------------------------------------------+
    | ``merge_list``    | Append a static list (``extend_with``) onto an   |
    |                   | existing list label.                             |
    +-------------------+--------------------------------------------------+
    | ``keep``          | Filter-in: drop candidate if Starlark            |
    |                   | ``predicate`` evaluates false.                   |
    +-------------------+--------------------------------------------------+
    | ``drop``          | Filter-out: drop candidate if Starlark           |
    |                   | ``predicate`` evaluates true.                    |
    +-------------------+--------------------------------------------------+
    | ``hash_mod``      | Shard: ``hash(source_labels) % modulus == eq``.  |
    +-------------------+--------------------------------------------------+

    The ``action`` field is a *free string* validated against
    :data:`RELABEL_ACTIONS` + the runtime-extension set so new actions
    can land as registered handler classes without a schema bump.
    """

    model_config = ConfigDict(extra="forbid")

    source_labels: list[str] = Field(default_factory=list)
    """Labels read by this rule. For ``hash_mod`` / ``set`` / ``set_list``
    the rule typically reads exactly one label; ``lookup_languages`` reads
    two (country code + an optional fallback list); ``keep`` / ``drop``
    expose all named labels into the predicate ctx."""

    target_label: str | None = None
    """Label written by this rule. Omitted for ``keep``/``drop`` which
    only filter."""

    action: str = Field(
        ...,
        description=(
            "One of RELABEL_ACTIONS or a runtime-registered extension. "
            "Stored as bare string so extensions don't require a schema bump."
        ),
    )

    # --- per-action knobs (all optional; rule-evaluator validates which
    # are required per action) ----------------------------------------

    replacement: str | None = None
    """``format`` action: Jinja-style template with the standard filters
    (``lower / upper / slug / trim``). Other actions ignore this field."""

    extend_with: list[Any] = Field(default_factory=list)
    """``merge_list`` action: the static list appended onto the existing
    list label. Empty for other actions."""

    predicate: str | None = None
    """``keep`` / ``drop`` action: Starlark expression evaluated against
    the candidate's label set. ``value`` is bound to ``source_labels[0]``
    for one-shot predicates; the full label set is also accessible via
    ``labels`` (and the candidate's ``natural_key`` via ``natural_key``).
    """

    table: str | None = None
    """``lookup`` action: name of the stack lookup table to consult."""

    fallback: Any = None
    """``lookup`` / ``lookup_languages`` action: value to write when the
    lookup returns nothing. Defaults to ``None``."""

    modulus: int | None = None
    """``hash_mod`` action: divisor."""

    eq: int | None = None
    """``hash_mod`` action: required residue (``hash(...) % modulus == eq``)."""


# ---------------------------------------------------------------------------
# Disappearance-ratio config
# ---------------------------------------------------------------------------


DEFAULT_DISAPPEARANCE_RATIO_THRESHOLD: float = 0.30
"""Default per-cycle disappearance threshold per L-106 §5. If more than
30% of previously-active candidates vanish in a single cycle the discovery
routes to ``resync_review`` and alerts rather than retiring N targets —
prevents flaky list sources from cascading mass-retirements."""


class DisappearanceAnomalyAction(BaseModel):
    """How a discovery reacts when a cycle breaches the disappearance
    threshold. See :func:`legba.data.discovery.disappearance.evaluate`.

    Per L-106 §5 / §9 OQ-4 default lean: ``alert_and_pause`` — the
    discovery pauses (no retirement, no new materialization) until an
    operator clears the anomaly; running instances stay ``active``.
    """

    model_config = ConfigDict(extra="forbid")

    on_anomaly: Literal["alert_and_pause", "alert_only", "retire_anyway"] = (
        "alert_and_pause"
    )
    """``alert_and_pause`` (default): hold disappearance, route to
    resync_review, alert. ``alert_only``: alert but proceed with
    retirement. ``retire_anyway``: just retire (no alert). The last is
    intended for transient-tolerant discoveries (deferred for now)."""


class ResyncPolicy(BaseModel):
    """Per-descriptor resync policy. Pinned to the discovery block.

    The default values match L-106 §5 / §9 OQ-4 leans; descriptors may
    override per-discovery — e.g., a hand-curated 50-row country list may
    tolerate ``disappearance_ratio_threshold: 0.10`` because *any*
    disappearance is suspect.
    """

    model_config = ConfigDict(extra="forbid")

    disappearance_ratio_threshold: float = Field(
        default=DEFAULT_DISAPPEARANCE_RATIO_THRESHOLD,
        ge=0.0,
        le=1.0,
        description=(
            "Max fraction of previously-active candidates that may "
            "disappear in a single cycle before the discovery is routed "
            "to resync_review. 0.0 disables the check (always retire)."
        ),
    )

    on_anomaly: Literal["alert_and_pause", "alert_only", "retire_anyway"] = (
        "alert_and_pause"
    )
    """See :class:`DisappearanceAnomalyAction`."""

    min_prior_active: int = Field(
        default=10,
        ge=0,
        description=(
            "Skip the ratio check when there were fewer than "
            "min_prior_active candidates in the prior cycle. Avoids "
            "false positives on a 3-row hand-curated list where any "
            "disappearance is >30%."
        ),
    )


# ---------------------------------------------------------------------------
# DiscoveryContext
# ---------------------------------------------------------------------------


SecretResolverFn = Callable[[str], Awaitable[str]]


class DiscoveryContext(BaseModel):
    """Per-discover context handed to a discovery handler.

    Carries the descriptor identity (so the handler can stamp candidates
    with ``discovery_id`` and ``discovery_version``), the parsed
    discovery-instance config, the :class:`StateStore` accessor for
    cursor persistence, an optional secrets resolver, and a bound logger.

    When the L-103 runtime types land, an adapter will construct one of
    these from the runtime's :class:`RuntimeContext` so handlers don't
    need to care which executor they're running under.

    ``stack_resolve`` is an optional async callable that returns a live
    stack-registered component (typically a Postgres reader for a
    country list, an HTTP client for a remote JSON list, etc.). It is
    optional so unit tests can pass an inline list source instead of a
    stack reference.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    discovery_id: str
    discovery_version: str = ""
    config: BaseModel = Field(..., exclude=True)
    state_store: StateStore = Field(..., exclude=True)
    secrets_resolve: SecretResolverFn | None = Field(default=None, exclude=True)
    stack_resolve: Any = Field(default=None, exclude=True)
    now_fn: Any = Field(default=None, exclude=True)
    logger: logging.Logger = Field(
        default_factory=lambda: logging.getLogger("legba.discovery"),
        exclude=True,
    )

    def utcnow(self) -> datetime:
        """Time accessor — overridable for deterministic tests."""
        if self.now_fn is not None:
            return self.now_fn()
        return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class DiscoveryHealth(BaseModel):
    """Discovery-handler health probe result. Mirrors L-102 §3 shape."""

    model_config = ConfigDict(extra="forbid")

    state: Literal["healthy", "degraded", "unhealthy"] = "healthy"
    last_success_at: datetime | None = None
    last_error: str | None = None
    candidates_24h: int = 0
    materialized_targets: int = 0
    last_cycle_disappearance_ratio: float | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# DiscoveryKind protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class DiscoveryKind(Protocol):
    """Structural-typing surface for discovery kinds. L-106 §2 contract.

    A concrete handler exposes:

      * ``kind``: ClassVar[str] — registered kind name
        (``country_list_discovery``, ``file_sd_discovery``, etc.).
      * ``family``: ClassVar[Literal["discovery"]] = "discovery".
      * ``schema_version``: ClassVar[str] — iglu-style URI
        (``legba/discovery/country_list/1.0.0``).
      * ``config_schema``: ClassVar[type[BaseModel]] — pydantic config
        type validated at descriptor-registration time.
      * ``discover(ctx) -> AsyncIterator[CandidateTarget]`` — the async
        entry point the runtime calls per discovery cycle.
      * ``healthcheck(ctx) -> DiscoveryHealth``.

    Lifecycle hooks (``on_configure`` / ``on_activate`` / ``on_pause`` /
    ``on_resume`` / ``on_retire``) are runtime-optional; default no-op
    when omitted. The runtime invokes them per state transition.
    """

    kind: ClassVar[str]
    family: ClassVar[Literal["discovery"]]
    schema_version: ClassVar[str]
    config_schema: ClassVar[type[BaseModel]]

    def discover(
        self,
        ctx: DiscoveryContext,
    ) -> AsyncIterator[CandidateTarget]: ...

    async def healthcheck(self, ctx: DiscoveryContext) -> DiscoveryHealth: ...


# Convenience alias — some references in the kind_contracts spec call it
# DiscoveryHandler (the noun for "the handler class") vs DiscoveryKind
# (the noun for "the registered kind taxonomy member"). They are the
# same Protocol; both names are exported.
DiscoveryHandler = DiscoveryKind


__all__ = [
    "CandidateTarget",
    "DEFAULT_DISAPPEARANCE_RATIO_THRESHOLD",
    "DisappearanceAnomalyAction",
    "DiscoveryContext",
    "DiscoveryEvidence",
    "DiscoveryHandler",
    "DiscoveryHealth",
    "DiscoveryKind",
    "InMemoryStateStore",
    "RELABEL_ACTIONS",
    "RelabelAction",
    "RelabelRule",
    "ResyncPolicy",
    "SecretResolverFn",
    "StateStore",
]
