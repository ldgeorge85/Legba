# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.filters — first-party filter/enrichment-kind handler implementations.

Phase 4 (L-150 .. L-155). Each module here registers one handler against the
L-102 filter/enrichment kind contract (§3 of
`design/legba_kind_contracts.md`). Handlers are stream-resident: they sit
inline between source emission and substrate write, transforming each in-
flight :class:`legba.data.sources.Signal` and either returning a mutated
copy or returning ``None`` to drop the signal from the stream.

Public surface (populated incrementally as each Phase 4 task lands):

  * ``language_detect`` — :class:`LanguageDetectHandler`, kind
    ``"language_detect"`` (L-150).

A common type module ``_contract`` declares the minimal Protocol shapes the
handlers depend on — :class:`FilterContext`, :class:`StreamHandler`,
:class:`FilterHealth` — mirroring the sibling
:mod:`legba.data.sources._contract` pattern so this package can be imported
before the runtime executor (L-103) lands. When L-103 arrives it will
provide concrete implementations that satisfy the same Protocol surfaces.

Sibling handler modules are imported lazily-defensively so a Phase 4 task
landing later (or earlier) doesn't break the package-level import for
already-merged handlers.
"""

from __future__ import annotations

from ._contract import (
    FilterContext,
    FilterHealth,
    StreamHandler,
)
from .dedupe import (
    Dedupe4TierConfig,
    Dedupe4TierHandler,
    EmbeddingService,
    QdrantLike,
    RedisLike,
    Tier3Config,
    Tier4Config,
    TierToggle,
)
from .ingest_dedupe import (
    IngestDedupe,
    IngestDedupeResult,
    ingest_dedupe_from_stages,
)

__all__ = [
    "Dedupe4TierConfig",
    "Dedupe4TierHandler",
    "EmbeddingService",
    "FilterContext",
    "FilterHealth",
    "IngestDedupe",
    "IngestDedupeResult",
    "QdrantLike",
    "RedisLike",
    "StreamHandler",
    "Tier3Config",
    "Tier4Config",
    "TierToggle",
    "ingest_dedupe_from_stages",
]

# Phase 4 handlers (L-150..L-155) are landing in parallel. Re-export each as
# it lands so callers can `from legba.data.filters import GeocodeHandler`
# without caring about merge ordering — but tolerate absence so a missing
# sibling doesn't break this package's import.
try:                                                        # pragma: no cover
    from .language_detect import (                           # noqa: F401
        LanguageDetectConfig,
        LanguageDetectHandler,
    )
except Exception:                                           # pragma: no cover
    pass
else:                                                       # pragma: no cover
    __all__.extend(["LanguageDetectConfig", "LanguageDetectHandler"])

try:
    from .geocode import (                                   # noqa: F401
        GeocodeBackend,
        GeocodeConfig,
        GeocodeHandler,
        GeocodeResult,
    )
except Exception:                                           # pragma: no cover
    pass
else:
    __all__.extend([
        "GeocodeBackend",
        "GeocodeConfig",
        "GeocodeHandler",
        "GeocodeResult",
    ])

try:                                                        # L-152
    from .source_credibility import (                        # noqa: F401
        SourceCredibilityConfig,
        SourceCredibilityHandler,
        extract_lookup_hosts,
        normalize_host,
    )
except Exception:                                           # pragma: no cover
    pass
else:
    __all__.extend([
        "SourceCredibilityConfig",
        "SourceCredibilityHandler",
        "extract_lookup_hosts",
        "normalize_host",
    ])

try:                                                        # L-154
    from .ner import (                                       # noqa: F401
        NERMultilingualConfig,
        NERMultilingualHandler,
        NERModelMissing,
    )
except Exception:                                           # pragma: no cover
    pass
else:
    __all__.extend([
        "NERMultilingualConfig",
        "NERMultilingualHandler",
        "NERModelMissing",
    ])

try:                                                        # L-155
    from .classify import (                                  # noqa: F401
        CLASSIFY_KIND,
        CLASSIFY_SCHEMA_VERSION,
        ClassifyConfig,
        ClassifyHandler,
        Label as ClassifyLabel,
        Rule as ClassifyRule,
    )
except Exception:                                           # pragma: no cover
    pass
else:
    __all__.extend([
        "CLASSIFY_KIND",
        "CLASSIFY_SCHEMA_VERSION",
        "ClassifyConfig",
        "ClassifyHandler",
        "ClassifyLabel",
        "ClassifyRule",
    ])

try:                                                        # anchor §5 PIECE 2
    from .fact_extractor import (                            # noqa: F401
        FactExtractorConfig,
        FactExtractorHandler,
        FactExtractorUnconfigured,
    )
except Exception:                                           # pragma: no cover
    pass
else:
    __all__.extend([
        "FactExtractorConfig",
        "FactExtractorHandler",
        "FactExtractorUnconfigured",
    ])

# L-202 — subconscious → stream-resident migration. Three SLM-bearing
# filters share a structural SLM port; sibling defensive imports so a
# missing optional dep on one doesn't sink the others.
try:                                                        # L-202 (a)
    from .slm_classification_refine import (                 # noqa: F401
        SLM_CLASSIFY_KIND,
        SLM_CLASSIFY_SCHEMA_VERSION,
        ChatSLMPort,
        ClassificationVerdict as SLMClassificationVerdict,
        SLMClassificationRefineConfig,
        SLMClassificationRefineHandler,
        SLMPort,
    )
except Exception:                                           # pragma: no cover
    pass
else:
    __all__.extend([
        "ChatSLMPort",
        "SLMClassificationRefineConfig",
        "SLMClassificationRefineHandler",
        "SLMClassificationVerdict",
        "SLMPort",
        "SLM_CLASSIFY_KIND",
        "SLM_CLASSIFY_SCHEMA_VERSION",
    ])

try:                                                        # L-202 (b)
    from .slm_entity_resolve import (                        # noqa: F401
        SLM_ENTITY_RESOLVE_KIND,
        SLM_ENTITY_RESOLVE_SCHEMA_VERSION,
        EntityCandidatePort,
        EntityResolutionVerdict as SLMEntityResolutionVerdict,
        InMemoryCandidatePort,
        SLMEntityResolveConfig,
        SLMEntityResolveHandler,
    )
except Exception:                                           # pragma: no cover
    pass
else:
    __all__.extend([
        "EntityCandidatePort",
        "InMemoryCandidatePort",
        "SLMEntityResolutionVerdict",
        "SLMEntityResolveConfig",
        "SLMEntityResolveHandler",
        "SLM_ENTITY_RESOLVE_KIND",
        "SLM_ENTITY_RESOLVE_SCHEMA_VERSION",
    ])

try:                                                        # L-202 (c)
    from .slm_relationship_validate import (                 # noqa: F401
        SLM_RELATIONSHIP_VALIDATE_KIND,
        SLM_RELATIONSHIP_VALIDATE_SCHEMA_VERSION,
        RelationshipVerdict as SLMRelationshipVerdict,
        SLMRelationshipValidateConfig,
        SLMRelationshipValidateHandler,
    )
except Exception:                                           # pragma: no cover
    pass
else:
    __all__.extend([
        "SLMRelationshipValidateConfig",
        "SLMRelationshipValidateHandler",
        "SLMRelationshipVerdict",
        "SLM_RELATIONSHIP_VALIDATE_KIND",
        "SLM_RELATIONSHIP_VALIDATE_SCHEMA_VERSION",
    ])
