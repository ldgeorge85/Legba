# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.discovery — discovery-kind handler contract + registry (L-180 / L-183).

The discovery family is the fifth leaf of the kind taxonomy. Per L-106
(authoritative spec) discovery kinds emit *candidate target descriptors*
that the registry expands into N materialized target instances via the
template's relabel chain. The handler never knows what gets scraped; the
target descriptor never knows how it was discovered. Static targets
(no ``discovery`` block) bypass the pipeline entirely — see
:mod:`legba.data.discovery.static`.

Public surface
~~~~~~~~~~~~~~

:class:`DiscoveryKind`
    Protocol every discovery handler implements
    (``kind`` / ``family`` / ``schema_version`` / ``config_schema`` /
    ``discover`` / ``healthcheck`` + lifecycle hooks).

:class:`CandidateTarget`
    Pydantic model the handler yields per row. Carries the stable
    ``natural_key``, raw ``label_set``, ``source_metadata``, and
    per-candidate ``evidence`` (provenance).

:class:`RelabelRule`
    Prometheus-style rewrite rule with 9 closed actions per L-106 §3
    (``set`` / ``set_list`` / ``format`` / ``lookup`` /
    ``lookup_languages`` / ``merge_list`` / ``keep`` / ``drop`` /
    ``hash_mod``). The registry — not the handler — applies the chain.

:class:`ResyncPolicy`
    Per-descriptor disappearance-ratio threshold policy. Default 0.30
    per L-106 §5.

:func:`evaluate_relabel_chain`
    The deterministic rule-chain evaluator. Given a candidate + ordered
    list of rules, produces a :class:`RelabelResult` (rewritten labels
    + drop decision).

:func:`evaluate_disappearance`
    Given prior and current ``natural_key`` sets + a
    :class:`ResyncPolicy`, returns a :class:`DisappearanceDecision`
    classifying retained / new / disappeared candidates and deciding
    whether to proceed or pause the discovery.

:func:`is_static_descriptor` + :func:`materialize_static`
    The L-183 static-target shortcut path. Descriptors without a
    ``discovery`` block route here and materialize as a single
    instance whose body equals the descriptor itself.

:func:`discover_discovery_kinds`
    Walks the package for first-party discovery kinds (L-181 / L-182)
    and returns a ``dict[kind_name, DiscoveryHandlerBundle]`` mirroring
    :func:`legba.data.analysts.discover_analyst_kinds`. Always includes
    the static-target sentinel.

Wave-B inheritance pattern
~~~~~~~~~~~~~~~~~~~~~~~~~~

L-181 ``country_list_discovery`` and L-182 ``file_sd_discovery`` will
land as new modules under this package. Each exposes:

  * ``KIND_NAME`` — module-level string.
  * ``CONFIG_SCHEMA`` — pydantic config type.
  * A class satisfying :class:`DiscoveryKind` (or module-level
    ``discover`` + ``healthcheck`` callables) — discovered by
    :func:`discover_discovery_kinds`.

They consume :class:`DiscoveryContext`, yield
:class:`CandidateTarget`. The registry-side materialization loop calls
:func:`evaluate_relabel_chain` per candidate and
:func:`evaluate_disappearance` per cycle. No edits to this package init
are required for the two reference kinds — the walker picks them up.
"""

from __future__ import annotations

from ._contract import (
    CandidateTarget,
    DEFAULT_DISAPPEARANCE_RATIO_THRESHOLD,
    DisappearanceAnomalyAction,
    DiscoveryContext,
    DiscoveryEvidence,
    DiscoveryHandler,
    DiscoveryHealth,
    DiscoveryKind,
    InMemoryStateStore,
    RELABEL_ACTIONS,
    RelabelAction,
    RelabelRule,
    ResyncPolicy,
    SecretResolverFn,
    StateStore,
)
from .disappearance import (
    DisappearanceDecision,
    DisappearanceVerdict,
    evaluate_disappearance,
)
from .registry import (
    DiscoveryHandlerBundle,
    discover_discovery_kinds,
)
from .relabel import (
    RELABEL_ACTION_HANDLERS,
    RelabelResult,
    apply_relabel_rule,
    evaluate_relabel_chain,
)
from .static import (
    STATIC_KIND_NAME,
    StaticMaterialization,
    is_static_descriptor,
    materialize_static,
)

# P-13 — polymorphic discovery: actor-resolved deps (G20 fix), source-discovery
# flavor, validate-before-register, selector auto-wire.
from .deps_resolver import (
    ResolvedDiscoveryDeps,
    load_country_rows,
    resolve_discovery_deps,
)
from .source_contract import (
    CandidateSource,
    SourceCandidateValidation,
    SourceDiscoveryHealth,
    SourceDiscoveryKind,
)
from .source_validate import validate_candidate_source
from .autowire import auto_wire_discovered_source
from .source_materializer import (
    MaterializeSourceOutcome,
    ReconcileSourceResult,
    materialize_discovered_source,
    reconcile_discovered_sources,
)
from .materializer import (
    run_source_discovery_cycle,
    run_target_discovery_cycle,
)

# Trigger the schemas-side forward-ref resolution for DiscoveryBlock +
# TargetDescriptor (per Wave D L-200 — they hold forward refs to
# RelabelRule + ResyncPolicy declared in ._contract). The schemas
# package can't eagerly import these at top level (schemas → sources →
# mediacloud → schemas cycle), so we drive the rebuild here after both
# packages have settled.
try:
    from ..schemas.target import _resolve_discovery_refs as _schemas_resolve_discovery_refs
    _schemas_resolve_discovery_refs()
except Exception:                                                # pragma: no cover
    pass

__all__ = [
    # Protocol + dataclasses
    "CandidateTarget",
    "DiscoveryContext",
    "DiscoveryEvidence",
    "DiscoveryHandler",
    "DiscoveryHealth",
    "DiscoveryKind",
    "InMemoryStateStore",
    "RelabelAction",
    "RelabelRule",
    "ResyncPolicy",
    "SecretResolverFn",
    "StateStore",
    # Relabel evaluator
    "RELABEL_ACTIONS",
    "RELABEL_ACTION_HANDLERS",
    "RelabelResult",
    "apply_relabel_rule",
    "evaluate_relabel_chain",
    # Disappearance
    "DEFAULT_DISAPPEARANCE_RATIO_THRESHOLD",
    "DisappearanceAnomalyAction",
    "DisappearanceDecision",
    "DisappearanceVerdict",
    "evaluate_disappearance",
    # Registry
    "DiscoveryHandlerBundle",
    "discover_discovery_kinds",
    # Static shortcut
    "STATIC_KIND_NAME",
    "StaticMaterialization",
    "is_static_descriptor",
    "materialize_static",
    # P-13 — actor-resolved deps (G20 fix)
    "ResolvedDiscoveryDeps",
    "resolve_discovery_deps",
    "load_country_rows",
    # P-13 — source-discovery flavor
    "CandidateSource",
    "SourceCandidateValidation",
    "SourceDiscoveryHealth",
    "SourceDiscoveryKind",
    "validate_candidate_source",
    "MaterializeSourceOutcome",
    "ReconcileSourceResult",
    "materialize_discovered_source",
    "reconcile_discovered_sources",
    # P-13 — selector auto-wire
    "auto_wire_discovered_source",
    # P-13 — polymorphic materialiser entry
    "run_target_discovery_cycle",
    "run_source_discovery_cycle",
]
