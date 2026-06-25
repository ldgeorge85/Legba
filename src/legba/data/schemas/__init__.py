# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.schemas — vendored pydantic descriptor schemas (L-101).

Decision per L-001 brief item 5: vendor a working subset of the L-101 pydantic
shapes into the package now, so L-110 (descriptor registry CRUD) and L-111
(stack registry CRUD) can import them directly without a separate
`legba-schemas` distribution.

Coverage in this drop:
  * Property-factory catalog (13 factories per L-101 §2)
  * Target descriptor schema (L-101 §3)
  * Analyst descriptor schema (L-101 §4)
  * Stack-component descriptor schemas (L-101 §5)
  * Lifecycle state machine (L-101 §6)
  * Versioning + content-hashing (L-101 §7)
  * Vocabulary registry (L-101 §8)

Out of scope for this drop (will land with L-110 / L-111 / L-104):
  * The injected runtime validator that calls VocabularyRegistry.values()
    at registration time — needs the registry connection wired (L-110).
  * Starlark predicate compilation (L-104).
  * The conversion-webhook walk + apply (L-112).

The interface lock matches L-101 verbatim so consumers don't need to chase
divergence between the doc and the code.
"""

from __future__ import annotations

from .properties import (
    FactoryValue,
    Secret,
    OAuth2,
    Text,
    Number,
    Cron,
    RateLimit,
    DropdownStatic,
    DropdownRefreshable,
    TypedList,
    TypedDict,
    StackRef,
    DynamicSchema,
    Free,
    Property,
)

from .versioning import (
    canonical_json_bytes,
    content_hash,
    ConversionWebhook,
)

from .lifecycle import (
    LifecycleState,
    LifecycleTransition,
    AbstractionLevel,
    ALLOWED_TRANSITIONS,
)

from .target import (
    TargetDescriptor,
    TargetIdentity,
    TargetScope,
    GeoScope,
    EstateScope,
    EntityScope,
    DiscoveryBlock,
    SourceBinding,
    PipelineStage,
    TargetPipeline,
    InlineAnalystBlock,
    OutputBinding,
    CoordinationBlock,
)

from .source import (
    SourceDescriptor,
    SourceIdentity,
    SourceScope,
    SourceDeps,
    SourceOutput,
    SourceDiscoveryBlock,
    ProvisionBlock,
    SourceRef,
    SourceSelector,
    Subscription,
)

from .action_pack import (
    ActionPack,
    ActionPackIdentity,
    ActionPackRef,
    ToolSpec,
    Channel,
    PackGovernor,
)

from .analyst import (
    AnalystDescriptor,
    AnalystIdentity,
    AnalystKind,
    AnalystKindRegistry,
    ANALYST_KIND_REGISTRY,
    register_analyst_kind,
    is_known_analyst_kind,
    TypeSignature,
    SubscriptionBlock,
    SubscriptionTargets,
    SubscriptionAnalyst,
    MappingBlock,
    FieldMapping,
    MethodBlock,
    CadenceBlock,
    EvalBlock,
    GroundingBlock,
)

from .stack import (
    StackComponentBase,
    LLMProvider,
    LLMProviderConfig,
    VectorStore,
    VectorStoreConfig,
    EmbeddingService,
    EmbeddingServiceConfig,
    NLPService,
    NLPServiceConfig,
    NATSCluster,
    NATSClusterConfig,
    PostgresCluster,
    PostgresClusterConfig,
    RedisCluster,
    RedisClusterConfig,
    ProxyPool,
    ProxyPoolConfig,
)

from .vocabulary import (
    VocabularyEntry,
    VocabularyRegistry,
)

# DiscoveryBlock + TargetDescriptor carry forward refs to RelabelRule +
# ResyncPolicy (per Wave D L-200). Resolving them eagerly at target.py
# import time hits a cycle (schemas → sources → mediacloud → schemas),
# so target.py only attempts the resolve and stays defensive on failure.
# This re-attempt runs post-init when both packages have settled.
try:
    from .target import _resolve_discovery_refs as _target_resolve_discovery_refs
    _target_resolve_discovery_refs()
except Exception:                                                # pragma: no cover
    pass

__all__ = [
    # Properties
    "FactoryValue", "Secret", "OAuth2", "Text", "Number", "Cron", "RateLimit",
    "DropdownStatic", "DropdownRefreshable", "TypedList", "TypedDict",
    "StackRef", "DynamicSchema", "Free", "Property",
    # Versioning
    "canonical_json_bytes", "content_hash", "ConversionWebhook",
    # Lifecycle
    "LifecycleState", "LifecycleTransition", "AbstractionLevel",
    "ALLOWED_TRANSITIONS",
    # Target
    "TargetDescriptor", "TargetIdentity", "TargetScope",
    "GeoScope", "EstateScope", "EntityScope", "DiscoveryBlock",
    "SourceBinding", "PipelineStage", "TargetPipeline", "InlineAnalystBlock",
    "OutputBinding", "CoordinationBlock",
    # Source (pivot)
    "SourceDescriptor", "SourceIdentity", "SourceScope", "SourceDeps",
    "SourceOutput", "SourceDiscoveryBlock", "ProvisionBlock",
    "SourceRef", "SourceSelector", "Subscription",
    # Action packs (pivot)
    "ActionPack", "ActionPackIdentity", "ActionPackRef",
    "ToolSpec", "Channel", "PackGovernor",
    # Analyst
    "AnalystDescriptor", "AnalystIdentity", "AnalystKind",
    "AnalystKindRegistry", "ANALYST_KIND_REGISTRY",
    "register_analyst_kind", "is_known_analyst_kind",
    "TypeSignature",
    "SubscriptionBlock", "SubscriptionTargets", "SubscriptionAnalyst",
    "MappingBlock", "FieldMapping", "MethodBlock", "CadenceBlock", "EvalBlock",
    "GroundingBlock",
    # Stack
    "StackComponentBase", "LLMProvider", "LLMProviderConfig",
    "VectorStore", "VectorStoreConfig",
    "EmbeddingService", "EmbeddingServiceConfig",
    "NLPService", "NLPServiceConfig",
    "NATSCluster", "NATSClusterConfig",
    "PostgresCluster", "PostgresClusterConfig",
    "RedisCluster", "RedisClusterConfig",
    "ProxyPool", "ProxyPoolConfig",
    # Vocabulary
    "VocabularyEntry", "VocabularyRegistry",
]
