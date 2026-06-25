# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.sources — first-party source-kind handler implementations.

Phase 3 (L-130 .. L-140). Each module here registers one handler against
the L-102 source-kind contract. Handlers are pure Python; runtime wiring
(L-160 / L-103) is responsible for actor scheduling, credential injection,
state persistence, and NATS publication.

Public surface (populated incrementally as each Phase 3 task lands):

  * ``rss``        — :class:`RSSSourceHandler`,        kind ``"rss"``         (L-130).
  * ``geojson``    — :class:`GeoJSONSourceHandler`,    kind ``"geojson"``     — model-free structured/GIS modality. Emits ``modality="structured"`` + ``mime_type="application/geo+json"`` Signals from a configurable GeoJSON (RFC 7946) document URL; the UI renders them via the ``application/geo+json`` modality renderer (no ML model in the loop).
  * ``json_api``   — :class:`JsonApiSourceHandler`,    kind ``"json_api"``    (S-3) — generic polled JSON/CSV HTTP API. Cursor-driven URL-template windows, JSONPath-lite item extraction, configurable field mappings, optional vault-backed header/query auth (fail-loud when unresolvable).
  * ``gdelt``      — :class:`GDELTBigQuerySourceHandler`, kind ``"gdelt_query"`` (L-131). Highest-leverage source — free, 100+ languages, 15-min refresh, CAMEO/FIPS coding via BigQuery.
  * ``acled``      — :class:`ACLEDSourceHandler`,      kind ``"acled"``       (L-132).
  * ``mediacloud`` — :class:`MediaCloudSourceHandler`, kind ``"mediacloud"``  (L-133).
  * ``opensanctions`` — :class:`OpenSanctionsSourceHandler`, kind ``"opensanctions"`` (L-134). PEPs + sanctions + criminal lists. Three modes (api / bulk_csv / self_hosted). FollowTheMoney schema for entity normalization.
  * ``intelmq``    — :class:`IntelMQCollectorBridge`,  kind ``"intelmq_collector_bridge"`` (L-140). Wraps IntelMQ collector bots (subprocess or redis_pipe) so Legba reuses the CERT-grade 200+ bot catalog. Per LB-12 decision (Lewis, 2026-05-16).

A common type module ``_contract`` declares the minimal Protocol shapes the
handlers depend on — :class:`SourceContext`, :class:`StateStore`,
:class:`Signal`, :class:`SourceHealth` — so this package can be imported
before the runtime executor (L-103) lands. When L-103 arrives it will
provide concrete implementations that satisfy the same Protocol surfaces.

Sibling handler modules are imported lazily-defensive so a Phase 3 task
landing later (or earlier) doesn't break the package-level import for
already-merged handlers.
"""

from __future__ import annotations

from ._contract import (
    InMemoryStateStore,
    Signal,
    SourceContext,
    SourceHandler,
    SourceHealth,
    StateStore,
)
from ._protocols import (
    PollSource,
    ProvisioningSource,
    PushSource,
    acquisition_protocol_for,
)
from .baseline import (
    MEDIA_MODALITIES,
    MediaExtractor,
    PassthroughTextExtractor,
    default_extractor_registry,
    run_baseline,
)
from .generic_webhook import (
    GenericWebhookConfig,
    GenericWebhookSourceHandler,
)
from .provision import (
    HttpUpstreamClient,
    ReconcileResult,
    UpstreamClient,
    deprovision_all,
    desired_watch_set,
    reconcile_provision,
)
from .rss import RSSConfig, RSSSourceHandler
from .geojson import (
    GEOJSON_MIME_TYPE,
    GeoJSONConfig,
    GeoJSONSourceHandler,
)
from .json_api import (
    JsonApiAuth,
    JsonApiAuthNotConfigured,
    JsonApiConfig,
    JsonApiSourceHandler,
)
from .mediacloud import (
    MediaCloudConfig,
    MediaCloudHttpError,
    MediaCloudRateLimited,
    MediaCloudSourceHandler,
)

__all__ = [
    "InMemoryStateStore",
    "Signal",
    "SourceContext",
    "SourceHandler",
    "SourceHealth",
    "StateStore",
    # acquisition protocols (P-06)
    "PollSource",
    "PushSource",
    "ProvisioningSource",
    "acquisition_protocol_for",
    # baseline pipeline (P-06 §4.6)
    "MEDIA_MODALITIES",
    "MediaExtractor",
    "PassthroughTextExtractor",
    "default_extractor_registry",
    "run_baseline",
    # provisioning (P-06 §4.2.1)
    "UpstreamClient",
    "HttpUpstreamClient",
    "ReconcileResult",
    "reconcile_provision",
    "deprovision_all",
    "desired_watch_set",
    # push reference kind (P-06)
    "GenericWebhookConfig",
    "GenericWebhookSourceHandler",
    "RSSConfig",
    "RSSSourceHandler",
    "GEOJSON_MIME_TYPE",
    "GeoJSONConfig",
    "GeoJSONSourceHandler",
    "JsonApiAuth",
    "JsonApiAuthNotConfigured",
    "JsonApiConfig",
    "JsonApiSourceHandler",
    "MediaCloudConfig",
    "MediaCloudHttpError",
    "MediaCloudRateLimited",
    "MediaCloudSourceHandler",
]

# ACLED handler (L-132) lands in parallel. Re-export when present so callers
# can `from legba.data.sources import ACLEDSourceHandler` without caring
# about merge ordering, but tolerate its absence to keep this package
# importable during the parallel Phase 3 wave.
try:                                                        # pragma: no cover
    from .acled import ACLEDConfig, ACLEDSourceHandler      # noqa: F401
except Exception:                                           # pragma: no cover
    pass
else:                                                       # pragma: no cover
    __all__.extend(["ACLEDConfig", "ACLEDSourceHandler"])

# GDELT BigQuery handler (L-131). Defensive import — the runtime BigQuery
# client (google-cloud-bigquery) is an optional dep; the handler module is
# pure Python and imports the client lazily, so importing the module here
# should never fail. The try/except mirrors the ACLED pattern for safety
# across parallel Phase 3 merges.
try:                                                        # pragma: no cover
    from .gdelt import (                                    # noqa: F401
        CostCapExceeded,
        GDELTBigQuerySourceHandler,
        GDELTConfig,
        build_gdelt_sql,
    )
except Exception:                                           # pragma: no cover
    pass
else:
    __all__.extend([
        "CostCapExceeded",
        "GDELTBigQuerySourceHandler",
        "GDELTConfig",
        "build_gdelt_sql",
    ])

# IntelMQ collector bridge (L-140). The IntelMQ package itself is an
# optional extra (legba[intelmq]); the bridge module is pure Python and
# imports IntelMQ lazily inside on_configure / health_check, so the module
# import here is always safe. Defensive try/except matches the pattern.
try:                                                        # pragma: no cover
    from .intelmq import (                                  # noqa: F401
        IntelMQBridgeConfig,
        IntelMQCollectorBridge,
        IntelMQNotInstalled,
        translate_idf_event,
    )
except Exception:                                           # pragma: no cover
    pass
else:
    __all__.extend([
        "IntelMQBridgeConfig",
        "IntelMQCollectorBridge",
        "IntelMQNotInstalled",
        "translate_idf_event",
    ])

# Discord webhook handler (L-137). Push (inbound) source: events arrive
# via the shared inbound-webhook router rather than via polling. Defensive
# import for the same parallel-merge-safety reason as ACLED / GDELT.
try:                                                        # pragma: no cover
    from .discord import (                                  # noqa: F401
        DiscordSignatureError,
        DiscordWebhookConfig,
        DiscordWebhookSourceHandler,
        EmitSignal,
        ParsedDiscordEvent,
        parse_discord_payload,
        verify_discord_signature,
    )
    from .webhook_router import (                           # noqa: F401
        InboundWebhookHandler,
        InboundWebhookRouter,
        WEBHOOK_PREFIX,
        default_router,
        reset_default_router,
    )
except Exception:                                           # pragma: no cover
    pass
else:
    __all__.extend([
        "DiscordSignatureError",
        "DiscordWebhookConfig",
        "DiscordWebhookSourceHandler",
        "EmitSignal",
        "ParsedDiscordEvent",
        "parse_discord_payload",
        "verify_discord_signature",
        "InboundWebhookHandler",
        "InboundWebhookRouter",
        "WEBHOOK_PREFIX",
        "default_router",
        "reset_default_router",
    ])

# Firecrawl handler (L-139). AI-friendly markdown extraction of arbitrary
# URLs; distinct from the generic scraper kind (L-135) in that the cleaned,
# LLM-consumable markdown *is* the product. Uses httpx against the
# api.firecrawl.dev REST API. Defensive import per the Phase 3 pattern.
try:                                                        # pragma: no cover
    from .firecrawl import (                                # noqa: F401
        CreditUsageRecord,
        FirecrawlAPIError,
        FirecrawlAuthError,
        FirecrawlConfig,
        FirecrawlRateLimited,
        FirecrawlSourceHandler,
    )
except Exception:                                           # pragma: no cover
    pass
else:
    __all__.extend([
        "CreditUsageRecord",
        "FirecrawlAPIError",
        "FirecrawlAuthError",
        "FirecrawlConfig",
        "FirecrawlRateLimited",
        "FirecrawlSourceHandler",
    ])

# Generic scraper handler (L-135). Pure-Python; depends only on httpx +
# trafilatura + feedparser, all already in `pyproject.toml`. Wraps an
# `impl`-pointed scraper-impl module (see `legba.data.sources.scrapers/`)
# with a shared crawler that owns rate-limit, robots.txt, BFS, proxy via
# StackRef. Defensive try/except matches the Phase 3 parallel-merge pattern.
try:                                                        # pragma: no cover
    from .scraper import (                                  # noqa: F401
        SCRAPER_KIND,
        SCRAPER_SCHEMA_VERSION,
        ScraperConfig,
        ScraperImpl,
        ScraperSourceHandler,
        load_impl,
    )
except Exception:                                           # pragma: no cover
    pass
else:
    __all__.extend([
        "SCRAPER_KIND",
        "SCRAPER_SCHEMA_VERSION",
        "ScraperConfig",
        "ScraperImpl",
        "ScraperSourceHandler",
        "load_impl",
    ])

# OpenSanctions handler (L-134). Pure-Python; depends only on httpx (already
# in pyproject.toml). The `followthemoney` package is an optional dep — the
# handler module imports it lazily and falls back to pass-through if absent.
# Defensive try/except matches the Phase 3 parallel-merge pattern.
try:                                                        # pragma: no cover
    from .opensanctions import (                            # noqa: F401
        OpenSanctionsConfig,
        OpenSanctionsSourceHandler,
    )
except Exception:                                           # pragma: no cover
    pass
else:
    __all__.extend([
        "OpenSanctionsConfig",
        "OpenSanctionsSourceHandler",
    ])
