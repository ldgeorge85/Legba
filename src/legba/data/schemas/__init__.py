# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.schemas — vendored pydantic descriptor schemas.

A working subset of the descriptor pydantic shapes, vendored into the package
so the descriptor and stack registries can import them directly without a
separate `legba-schemas` distribution.

Coverage:
  * Property-factory catalog (13 factories)
  * Target descriptor schema
  * Analyst descriptor schema
  * Stack-component descriptor schemas
  * Lifecycle state machine
  * Versioning + content-hashing
  * Vocabulary registry

Out of scope:
  * The injected runtime validator that calls VocabularyRegistry.values()
    at registration time — needs the registry connection wired.
  * Starlark predicate compilation.
  * The conversion-webhook walk + apply.
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

from .collection import (
    CollectionDescriptor,
    CollectionIdentity,
    CollectionState,
    COLLECTION_SCHEMA_URI,
    COLLECTION_TRANSITIONS,
    FENCED_SURFACES,
    FirewallBlock,
    LoaderBlock,
    ManifestBlock,
    OPT_IN_READERS,
    SeriesEntry,
    SubjectEntry,
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
# ResyncPolicy. Resolving them eagerly at target.py
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
    # Collection (bounded holdings of the past)
    "CollectionDescriptor", "CollectionIdentity", "CollectionState",
    "COLLECTION_SCHEMA_URI", "COLLECTION_TRANSITIONS",
    "FENCED_SURFACES", "OPT_IN_READERS",
    "FirewallBlock", "LoaderBlock", "ManifestBlock", "SeriesEntry",
    "SubjectEntry",
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
