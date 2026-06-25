# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pipeline runner — chains filter handlers from legba.data.filters.

Per legba_runtime_spec.md §3 the target actor's run() pulls signals from
each source, feeds them through the registered pipeline filters in order,
and writes the survivors to substrate with provenance tags.

The runner here is the thin shim that turns a list of (kind, config)
``PipelineStage`` entries into a chain of filter handlers that conform
to :class:`legba.data.filters.StreamHandler`.

Supported filter kinds (full Phase 4 set, per L-150..L-155):

  * ``language_detect``     -> :class:`LanguageDetectHandler` (L-150)
  * ``dedupe_tier_1``       -> URL-canonical dedupe (Dedupe4Tier, tier 1 only)
  * ``dedupe_tier_2``       -> SHA-256 content-hash dedupe (tier 2 only)
  * ``dedupe_tier_3``       -> Qdrant semantic dedupe (tier 3 only)
  * ``dedupe_tier_4``       -> Temporal-window dedupe (tier 4 only)
  * ``source_credibility``  -> :class:`SourceCredibilityHandler` (L-152)
  * ``geocode``             -> :class:`GeocodeHandler` (L-151)
  * ``ner_multilingual``    -> :class:`NERMultilingualHandler` (L-154)
  * ``classify``            -> :class:`ClassifyHandler` (L-155)
  * ``slm_entity_resolve``  -> :class:`SLMEntityResolveHandler` (L-202) —
    OFF by default; a descriptor must name this kind to enable the SLM
    disambiguator. When unnamed, substrate identity rides the lexical
    composite-key path (``entity_resolution`` sub-handler + migration 0035).

The Phase 5a spike's [tier_1, tier_2] choice translates into one
``Dedupe4TierHandler`` instance per stage with the non-selected tiers
disabled via ``Dedupe4TierConfig.tiers``. We preserve the stage name in
the descriptor for operator readability — the handler does the gating
internally.

L-248: the descriptor-level ``tiers`` opt-out on ``Dedupe4TierConfig``
is preserved. A descriptor that names ``dedupe_tier_3`` without supplying
an embedding service / qdrant client receives an explicit ValueError at
construction — we never return a stub handler that silently no-ops
(Lewis's no-stubs rule).

Dependency injection: ``build_filter_handler`` accepts a wide set of
optional ``**deps`` keyword arguments. Each builder only consumes the
deps it actually needs; missing required deps raise ``ValueError`` with
a clear message naming the missing dep and the filter kind that needs it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, TYPE_CHECKING

from ..data.filters import (
    Dedupe4TierConfig,
    Dedupe4TierHandler,
    FilterContext,
    FilterHealth,
    LanguageDetectConfig,
    LanguageDetectHandler,
    StreamHandler,
    Tier3Config,
    Tier4Config,
    TierToggle,
)
from ..data.sources._contract import Signal

if TYPE_CHECKING:  # pragma: no cover — type-only
    from ..data.stack.nlp_service.client import NlpServiceClient


@dataclass
class PipelineResult:
    """Per-pull pipeline result — what happened on one batch."""

    signals_in: int = 0
    signals_out: int = 0
    signals_dropped: int = 0
    drop_reasons: dict[str, int] = field(default_factory=dict)


class PipelineRunner:
    """Runs a chain of filter handlers against an async stream of signals.

    Handlers are constructed once per runner. Each ``filter`` call takes a
    Signal and returns either the (possibly mutated) Signal or None to
    drop it. The runner records drop counts per stage for observability.
    """

    def __init__(
        self,
        stages: list[tuple[str, StreamHandler]],
        *,
        ctx_factory,  # callable filter_id -> FilterContext
    ) -> None:
        self._stages = stages
        self._ctx_factory = ctx_factory

    async def run(self, signals: AsyncIterator[Signal]) -> AsyncIterator[Signal]:
        """Async-generator that yields surviving signals."""
        async for sig in signals:
            current: Signal | None = sig
            for filter_id, handler in self._stages:
                if current is None:
                    break
                ctx = self._ctx_factory(filter_id)
                try:
                    current = await handler.transform(current, ctx)
                except Exception as exc:  # pragma: no cover — handler-level errors
                    ctx.logger.exception(
                        "pipeline.filter.error filter_id=%s err=%s",
                        filter_id,
                        exc,
                    )
                    current = None
                    break
            if current is not None:
                yield current

    async def health(self, ctx_factory) -> dict[str, FilterHealth]:
        """Probe each filter's health. Returns kind -> FilterHealth."""
        out: dict[str, FilterHealth] = {}
        for filter_id, handler in self._stages:
            ctx = ctx_factory(filter_id)
            try:
                out[filter_id] = await handler.health_check(ctx)
            except Exception as exc:  # pragma: no cover
                out[filter_id] = FilterHealth(
                    state="unhealthy",
                    last_error=str(exc),
                )
        return out


# ---------------------------------------------------------------------------
# Filter construction — descriptor PipelineStage -> handler instance
# ---------------------------------------------------------------------------


# Each dedupe_tier_N stage maps to the same Dedupe4TierHandler with the
# config's `tiers` list narrowed to just that tier. We use TierToggle
# instances to leave the per-tier `enabled` flag intact for non-selected
# tiers (they're gated out by `tiers` membership anyway).
_DEDUPE_TIER_MAP: dict[str, int] = {
    "dedupe_tier_1": 1,
    "dedupe_tier_2": 2,
    "dedupe_tier_3": 3,
    "dedupe_tier_4": 4,
}


# Sentinel kinds the spike already supported; the registry-style dispatch
# below routes them through their original helpers.
_KNOWN_KINDS: frozenset[str] = frozenset({
    "language_detect",
    "dedupe_tier_1",
    "dedupe_tier_2",
    "dedupe_tier_3",
    "dedupe_tier_4",
    "source_credibility",
    "geocode",
    "ner_multilingual",
    "classify",
    "fact_extractor",
    # OFF by default: only present in the pipeline when a descriptor names it.
    "slm_entity_resolve",
})


def build_filter_handler(
    *,
    kind: str,
    config: Mapping[str, Any],
    redis_client: Any | None = None,
    pg_pool: Any | None = None,
    nlp_client_factory: Callable[[], "NlpServiceClient"] | None = None,
    qdrant_client: Any | None = None,
    embedding_service: Any | None = None,
    secrets_resolve: Callable[[str], Awaitable[bytes]] | None = None,
    llm_handler_factory: Callable[[str], Awaitable[Any]] | None = None,
    graph_store: Any | None = None,
) -> StreamHandler:
    """Construct one filter handler from a descriptor PipelineStage.

    Supports the full Phase-4 filter set (see module docstring). Each
    kind consumes only the deps it needs; missing required deps raise
    ``ValueError`` naming the dep and the kind.

    Parameters
    ----------
    kind:
        Filter kind name as it appears in the descriptor's
        ``PipelineStage.kind``.
    config:
        The stage's ``config`` mapping (already factory-unwrapped).
    redis_client:
        Required for all four dedupe tiers (set/zset storage) and the
        geocode handler (CacheStore-shaped).
    pg_pool:
        Required for ``source_credibility`` (reads the registry table
        populated by migration 0014).
    nlp_client_factory:
        Deferred-construction callable returning an
        :class:`NlpServiceClient`. Required for ``ner_multilingual`` and
        ``classify``. Deferred so we don't pay httpx import / connection
        cost for descriptors that don't include NLP filters.
    qdrant_client:
        Required for ``dedupe_tier_3`` (semantic vector lookup). When
        absent the tier-3 builder raises ValueError — the no-stubs rule
        means we don't accept "skip and pretend".
    embedding_service:
        Required for ``dedupe_tier_3``. Must satisfy the
        :class:`legba.data.filters.dedupe.EmbeddingService` protocol.
    secrets_resolve:
        Async callable secret_id -> bytes. Required only for
        ``geocode`` when ``backend == "google"`` (to resolve the
        Google Maps API key from the credentials vault).
    llm_handler_factory:
        Async callable ``component_id -> LLMProviderHandler``. Optional;
        required for ``fact_extractor`` with ``backend == "llm"`` (the
        8B provider-plane path) and for ``slm_entity_resolve`` (the SLM
        disambiguator). Default ``None`` so every existing caller works
        unchanged — the default ``relation`` backend never needs it, and
        ``slm_entity_resolve`` is OFF unless a descriptor names it.
    graph_store:
        AGE-capable store (``PostgresStore``-shaped, exposes ``cypher()``).
        Optional; used only by ``fact_extractor`` when ``emit_graph_edges``
        is set. Facts-first: a missing store just skips the edge leg.

    Raises
    ------
    ValueError
        On unknown kind, or when a required dep for the named kind
        wasn't passed.
    """
    if kind not in _KNOWN_KINDS:
        known = ", ".join(sorted(_KNOWN_KINDS))
        raise ValueError(
            f"pipeline kind {kind!r} not supported; known kinds: {known}"
        )

    if kind == "language_detect":
        return _build_language_detect(config)

    if kind in ("dedupe_tier_1", "dedupe_tier_2", "dedupe_tier_4"):
        if redis_client is None:
            raise ValueError(
                f"pipeline kind {kind!r} requires a redis client; pass redis_client="
            )
        return _build_dedupe_single_tier(
            kind,
            config,
            redis_client=redis_client,
        )

    if kind == "dedupe_tier_3":
        if redis_client is None:
            raise ValueError(
                "pipeline kind 'dedupe_tier_3' requires a redis client; pass redis_client="
            )
        if qdrant_client is None:
            raise ValueError(
                "pipeline kind 'dedupe_tier_3' requires a qdrant_client; "
                "pass qdrant_client= (semantic dedupe cannot run without it — "
                "no stubs)"
            )
        if embedding_service is None:
            raise ValueError(
                "pipeline kind 'dedupe_tier_3' requires an embedding_service; "
                "pass embedding_service= (semantic dedupe needs vector "
                "embeddings — no stubs)"
            )
        return _build_dedupe_single_tier(
            kind,
            config,
            redis_client=redis_client,
            qdrant_client=qdrant_client,
            embedding_service=embedding_service,
        )

    if kind == "source_credibility":
        if pg_pool is None:
            raise ValueError(
                "pipeline kind 'source_credibility' requires a pg_pool; "
                "pass pg_pool= (the source_credibility table lookup needs it)"
            )
        return _build_source_credibility(config, pg_pool=pg_pool)

    if kind == "geocode":
        return _build_geocode(
            config,
            redis_client=redis_client,
            secrets_resolve=secrets_resolve,
        )

    if kind == "ner_multilingual":
        if nlp_client_factory is None:
            raise ValueError(
                "pipeline kind 'ner_multilingual' requires an nlp_client_factory; "
                "pass nlp_client_factory= (HTTP variant calls the hosted "
                "/extract endpoint — no stubs)"
            )
        return _build_ner_multilingual(
            config,
            nlp_client_factory=nlp_client_factory,
        )

    if kind == "classify":
        if nlp_client_factory is None:
            raise ValueError(
                "pipeline kind 'classify' requires an nlp_client_factory; "
                "pass nlp_client_factory= (HTTP variant calls the hosted "
                "/classify endpoint — no stubs)"
            )
        return _build_classify(
            config,
            nlp_client_factory=nlp_client_factory,
        )

    if kind == "fact_extractor":
        if pg_pool is None:
            raise ValueError(
                "pipeline kind 'fact_extractor' requires a pg_pool; "
                "pass pg_pool= (the facts table is the write target — no stub)"
            )
        return _build_fact_extractor(
            config,
            pg_pool=pg_pool,
            nlp_client_factory=nlp_client_factory,
            llm_handler_factory=llm_handler_factory,
            graph_store=graph_store,
        )

    if kind == "slm_entity_resolve":
        if pg_pool is None:
            raise ValueError(
                "pipeline kind 'slm_entity_resolve' requires a pg_pool; the "
                "trigram candidate lookup against entity_profiles needs it "
                "(no stub)"
            )
        if llm_handler_factory is None:
            raise ValueError(
                "pipeline kind 'slm_entity_resolve' requires an "
                "llm_handler_factory; the SLM disambiguator routes through the "
                "provider plane (never litellm) — pass llm_handler_factory= "
                "(no stub)"
            )
        return _build_slm_entity_resolve(
            config,
            pg_pool=pg_pool,
            llm_handler_factory=llm_handler_factory,
        )

    # Unreachable — _KNOWN_KINDS guard at top covers this.
    raise ValueError(f"pipeline kind {kind!r} not supported")  # pragma: no cover


# ---------------------------------------------------------------------------
# Per-kind builders
# ---------------------------------------------------------------------------


def _build_language_detect(config: Mapping[str, Any]) -> LanguageDetectHandler:
    """Construct a LanguageDetectHandler. Default config when unspecified."""
    cfg = LanguageDetectConfig(**dict(config)) if config else LanguageDetectConfig()
    return LanguageDetectHandler(config=cfg)


def _build_dedupe_single_tier(
    kind: str,
    config: Mapping[str, Any],
    *,
    redis_client: Any,
    qdrant_client: Any | None = None,
    embedding_service: Any | None = None,
) -> Dedupe4TierHandler:
    """Construct a Dedupe4TierHandler narrowed to a single tier.

    Each ``dedupe_tier_N`` descriptor stage builds one handler with
    ``tiers=[N]`` and the other three tiers disabled (both via
    ``Dedupe4TierConfig.tiers`` selector AND the per-tier ``enabled``
    toggle, so any downstream code that reads either gate agrees).

    L-248 contract: when the descriptor's stage config carries an
    explicit ``tiers`` list, it wins (operator override). Otherwise we
    derive ``tiers=[N]`` from the stage name. The per-tier ``enabled``
    flags in the stage config are honored.
    """
    raw = dict(config) if config else {}
    target_tier = _DEDUPE_TIER_MAP[kind]

    # L-248: if the operator passed an explicit ``tiers`` list, honor it
    # in full — that's the descriptor's opt-out contract. We do NOT then
    # additionally clamp per-tier ``enabled`` flags; the operator picked
    # the active set deliberately.
    if "tiers" in raw:
        cfg = Dedupe4TierConfig(**raw)
        return Dedupe4TierHandler(
            config=cfg,
            redis=redis_client,
            qdrant=qdrant_client,
            embedder=embedding_service,
        )

    # No explicit operator override → narrow to the named tier only.
    # We set both ``tiers=[N]`` AND per-tier ``enabled=False`` for the
    # other three so the AND gate in Dedupe4TierHandler agrees with the
    # selector (any single read on either gate gives the same answer).
    raw["tiers"] = [target_tier]
    if target_tier != 1 and "tier1" not in raw:
        raw["tier1"] = TierToggle(enabled=False)
    if target_tier != 2 and "tier2" not in raw:
        raw["tier2"] = TierToggle(enabled=False)
    if target_tier != 3 and "tier3" not in raw:
        raw["tier3"] = Tier3Config(enabled=False)
    if target_tier != 4 and "tier4" not in raw:
        raw["tier4"] = Tier4Config(enabled=False)

    cfg = Dedupe4TierConfig(**raw)
    return Dedupe4TierHandler(
        config=cfg,
        redis=redis_client,
        qdrant=qdrant_client,
        embedder=embedding_service,
    )


def _build_source_credibility(
    config: Mapping[str, Any],
    *,
    pg_pool: Any,
) -> StreamHandler:
    """Construct a SourceCredibilityHandler. Imports defensively."""
    try:
        from ..data.filters.source_credibility import (
            SourceCredibilityConfig,
            SourceCredibilityHandler,
        )
    except Exception as exc:  # pragma: no cover — import-shape regression
        raise ValueError(
            f"source_credibility handler module failed to import: {exc!r}"
        ) from exc

    cfg = (
        SourceCredibilityConfig(**dict(config))
        if config
        else SourceCredibilityConfig()
    )
    return SourceCredibilityHandler(config=cfg, pool=pg_pool)


def _build_geocode(
    config: Mapping[str, Any],
    *,
    redis_client: Any | None,
    secrets_resolve: Callable[[str], Awaitable[bytes]] | None,
) -> StreamHandler:
    """Construct a GeocodeHandler. Defers backend construction.

    Backend selection:

      * ``nominatim`` (default): no creds needed; redis_client may be
        passed as the CacheStore. When redis_client is None the handler
        falls back to its in-process cache (intentional — Nominatim is
        operator-friendly even without shared cache).
      * ``google``: requires ``secrets_resolve`` and a
        ``google_api_key_secret_ref`` in the config. We resolve the
        secret eagerly (synchronously via asyncio.run is wrong — we
        instead defer via a synchronous wrapper that the runtime calls
        from on_configure). For now we raise ValueError if secrets_resolve
        isn't passed: production wiring (dapr_host) supplies it; tests
        that build a google-backed handler must supply it.
    """
    try:
        from ..data.filters.geocode import (
            GeocodeConfig,
            GeocodeHandler,
            GoogleBackend,
            NominatimBackend,
            resolve_user_agent,
        )
    except Exception as exc:  # pragma: no cover — import-shape regression
        raise ValueError(
            f"geocode handler module failed to import: {exc!r}"
        ) from exc

    cfg = GeocodeConfig(**dict(config)) if config else GeocodeConfig()

    backend: Any
    if cfg.backend == "nominatim":
        # resolve_user_agent fails loud (RuntimeError) when the PUBLIC
        # Nominatim endpoint would be hit without an operator contact
        # (LEGBA_GEOCODER_CONTACT_EMAIL) — per the OSM usage policy the
        # filter refuses activation rather than degrade etiquette.
        backend = NominatimBackend(
            base_url=cfg.nominatim_url,
            user_agent=resolve_user_agent(
                cfg.user_agent, nominatim_url=cfg.nominatim_url
            ),
            timeout_seconds=cfg.timeout_seconds,
        )
    elif cfg.backend == "google":
        if secrets_resolve is None:
            raise ValueError(
                "pipeline kind 'geocode' with backend='google' requires "
                "secrets_resolve= (the Google API key is resolved from the "
                "credentials vault, never inlined in the descriptor)"
            )
        if not cfg.google_api_key_secret_ref:
            raise ValueError(
                "pipeline kind 'geocode' with backend='google' requires "
                "config.google_api_key_secret_ref to be set (vault path of "
                "the Google Maps API key)"
            )
        # Defer the actual secret resolution to a synchronous attribute
        # the runtime resolves at on_configure-time. To keep build_filter
        # synchronous (we're called from pipeline_factory which is sync
        # itself), we stash a coroutine factory on the handler. The
        # runtime's on_configure path then awaits it before activate.
        #
        # For now: build the handler with a placeholder backend that
        # carries the secret_ref + resolver. The runtime invokes the
        # deferred resolution in on_configure.
        backend = _DeferredGoogleBackend(
            secret_ref=cfg.google_api_key_secret_ref,
            secrets_resolve=secrets_resolve,
            timeout_seconds=cfg.timeout_seconds,
        )
    else:  # pragma: no cover — pydantic enforces Literal["nominatim","google"]
        raise ValueError(f"unsupported geocode backend: {cfg.backend!r}")

    return GeocodeHandler(config=cfg, backend=backend, cache=redis_client)


class _DeferredGoogleBackend:
    """Lazy GoogleBackend wrapper that resolves its API key on first call.

    The geocode handler builder is synchronous, but the credentials vault
    is async. This shim implements the :class:`GeocodeBackend` protocol
    and resolves the secret on the first ``geocode()`` call, then delegates
    every subsequent call to a real :class:`GoogleBackend` instance.

    A real backend is constructed inside an ``asyncio.Lock`` so concurrent
    transforms can't double-resolve.
    """

    name = "google"

    def __init__(
        self,
        *,
        secret_ref: str,
        secrets_resolve: Callable[[str], Awaitable[bytes]],
        timeout_seconds: int,
    ) -> None:
        self._secret_ref = secret_ref
        self._secrets_resolve = secrets_resolve
        self._timeout_seconds = timeout_seconds
        self._real_backend: Any | None = None
        # Lock built lazily — at construction we may not be in an event
        # loop yet.
        self._lock: Any | None = None

    async def _ensure_backend(self) -> Any:
        if self._real_backend is not None:
            return self._real_backend
        import asyncio
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._real_backend is not None:
                return self._real_backend
            raw = await self._secrets_resolve(self._secret_ref)
            api_key = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
            from ..data.filters.geocode import GoogleBackend
            self._real_backend = GoogleBackend(
                api_key=api_key,
                timeout_seconds=self._timeout_seconds,
            )
        return self._real_backend

    async def geocode(self, query: str) -> Any:
        backend = await self._ensure_backend()
        return await backend.geocode(query)

    async def reachable(self) -> bool:
        try:
            backend = await self._ensure_backend()
        except Exception:
            return False
        return await backend.reachable()


def _build_ner_multilingual(
    config: Mapping[str, Any],
    *,
    nlp_client_factory: Callable[[], "NlpServiceClient"],
) -> StreamHandler:
    """Construct an NERMultilingualHandler against the hosted /extract endpoint.

    ``nlp_client_factory`` is deferred so the httpx-based client isn't
    built unless this filter is actually wired into a descriptor.
    """
    try:
        from ..data.filters.ner import (
            NERMultilingualConfig,
            NERMultilingualHandler,
        )
    except Exception as exc:  # pragma: no cover — import-shape regression
        raise ValueError(
            f"ner_multilingual handler module failed to import: {exc!r}"
        ) from exc

    cfg = (
        NERMultilingualConfig(**dict(config))
        if config
        else NERMultilingualConfig()
    )
    # Build the client now (factory call is cheap — it's a constructor).
    # The httpx.AsyncClient inside is built lazily on first request.
    client = nlp_client_factory()
    return NERMultilingualHandler(config=cfg, nlp_client=client)


def _build_classify(
    config: Mapping[str, Any],
    *,
    nlp_client_factory: Callable[[], "NlpServiceClient"],
) -> StreamHandler:
    """Construct a ClassifyHandler against the hosted /classify endpoint."""
    try:
        from ..data.filters.classify import (
            ClassifyConfig,
            ClassifyHandler,
        )
    except Exception as exc:  # pragma: no cover — import-shape regression
        raise ValueError(
            f"classify handler module failed to import: {exc!r}"
        ) from exc

    cfg = ClassifyConfig(**dict(config))
    client = nlp_client_factory()
    return ClassifyHandler(config=cfg, nlp_client=client)


def _build_fact_extractor(
    config: Mapping[str, Any],
    *,
    pg_pool: Any,
    nlp_client_factory: Callable[[], "NlpServiceClient"] | None = None,
    llm_handler_factory: Callable[[str], Awaitable[Any]] | None = None,
    graph_store: Any | None = None,
) -> StreamHandler:
    """Construct a FactExtractorHandler (anchor §5 PIECE 2).

    The default ``relation`` backend reuses the GLiREL relation triples on the
    signal (or calls ``/extract`` itself when ``nlp_client_factory`` is wired);
    ``backend="llm"`` routes through ``llm_handler_factory`` (the 8B path) and
    raises loud at construction if that factory is missing (no stub).
    """
    try:
        from ..data.filters.fact_extractor import (
            FactExtractorConfig,
            FactExtractorHandler,
        )
    except Exception as exc:  # pragma: no cover — import-shape regression
        raise ValueError(
            f"fact_extractor handler module failed to import: {exc!r}"
        ) from exc

    cfg = (
        FactExtractorConfig(**dict(config))
        if config
        else FactExtractorConfig()
    )
    # The /extract fallback (and the relation backend) want an NLP client when
    # one is available; construct it lazily only if the factory was threaded.
    nlp_client = nlp_client_factory() if nlp_client_factory is not None else None

    # Opt-in SLM relationship-validation stage (W3). OFF by default — built
    # only when the descriptor sets ``slm_validate_relations``. Routes the
    # extracted triples through the provider plane (never litellm) before they
    # become facts; requires the same llm_handler_factory the 8B path uses.
    relationship_validator = None
    if cfg.slm_validate_relations:
        if llm_handler_factory is None:
            raise ValueError(
                "fact_extractor slm_validate_relations=True requires an "
                "llm_handler_factory; the SLM relationship-validator routes "
                "through the provider plane (never litellm) — pass "
                "llm_handler_factory= (no stub)"
            )
        relationship_validator = _build_relationship_validator(
            cfg,
            llm_handler_factory=llm_handler_factory,
        )

    return FactExtractorHandler(
        cfg,
        pg_pool=pg_pool,
        nlp_client=nlp_client,
        llm_handler_factory=llm_handler_factory,
        graph_store=graph_store,
        relationship_validator=relationship_validator,
    )


def _build_relationship_validator(
    fact_cfg: Any,
    *,
    llm_handler_factory: Callable[[str], Awaitable[Any]],
) -> Any:
    """Construct an SLMRelationshipValidateHandler for the fact_extractor wire.

    The SLM port is the lazy provider-plane adapter (:class:`_FactorySLMPort`,
    never litellm), shared with ``slm_entity_resolve``. The target component is
    the descriptor's ``slm_validate_component_id`` if set, else the
    ``LEGBA_SLM_RELATIONSHIP_VALIDATE_COMPONENT`` env var, else the shared
    default SLM component id. The validator's per-signal cap mirrors the
    fact_extractor's ``slm_validate_max_triples`` knob.
    """
    import os

    try:
        from ..data.filters.slm_relationship_validate import (
            SLMRelationshipValidateConfig,
            SLMRelationshipValidateHandler,
        )
    except Exception as exc:  # pragma: no cover — import-shape regression
        raise ValueError(
            f"slm_relationship_validate handler module failed to import: {exc!r}"
        ) from exc

    component_id = (
        fact_cfg.slm_validate_component_id
        or os.environ.get("LEGBA_SLM_RELATIONSHIP_VALIDATE_COMPONENT")
        or os.environ.get("LEGBA_SLM_COMPONENT_ID")
        or "slm"
    )
    # The validator's source-text window mirrors the fact_extractor's, clamped
    # to the validator config bounds (100..32768).
    source_window = max(100, min(int(fact_cfg.max_text_chars), 32_768))
    validate_cfg = SLMRelationshipValidateConfig(
        max_source_chars=source_window,
        max_triples_per_signal=fact_cfg.slm_validate_max_triples,
    )
    slm_port = _FactorySLMPort(llm_handler_factory, str(component_id))
    return SLMRelationshipValidateHandler(validate_cfg, slm=slm_port)


class _FactorySLMPort:
    """``ChatSLMPort`` adapter over an ``llm_handler_factory``.

    Resolves the provider-plane :class:`LLMProviderHandler` lazily, per call,
    via ``llm_handler_factory(component_id)`` — the same pattern
    ``FactExtractorHandler`` uses for its 8B path. The factory targets the
    hosted/remote provider stack (``legba.data.stack.llm``); litellm is never
    on this path (the never-litellm runtime rule). Exposes ``chat_complete`` so
    the :class:`SLMEntityResolveHandler` treats it as a :class:`ChatSLMPort`.
    """

    def __init__(
        self,
        llm_handler_factory: Callable[[str], Awaitable[Any]],
        component_id: str,
    ) -> None:
        self._factory = llm_handler_factory
        self._component_id = component_id

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        handler = await self._factory(self._component_id)
        return await handler.chat_complete(messages, system=system, **kwargs)


def _build_slm_entity_resolve(
    config: Mapping[str, Any],
    *,
    pg_pool: Any,
    llm_handler_factory: Callable[[str], Awaitable[Any]],
) -> StreamHandler:
    """Construct an SLMEntityResolveHandler (L-202), descriptor-gated.

    OFF by default — only built when a descriptor names the
    ``slm_entity_resolve`` kind. The SLM port is a lazy provider-plane adapter
    (never litellm); the candidate port is the Postgres trigram lookup against
    ``entity_profiles``. The descriptor's ``config`` may carry an
    ``llm_component_id`` naming the provider-stack component to target; absent
    that we fall back to ``LEGBA_SLM_ENTITY_RESOLVE_COMPONENT`` then the
    shared default SLM component id.
    """
    import os

    try:
        from ..data.filters.entity_candidate_port import (
            PostgresEntityCandidatePort,
        )
        from ..data.filters.slm_entity_resolve import (
            SLMEntityResolveConfig,
            SLMEntityResolveHandler,
        )
    except Exception as exc:  # pragma: no cover — import-shape regression
        raise ValueError(
            f"slm_entity_resolve handler module failed to import: {exc!r}"
        ) from exc

    cfg_dict = dict(config) if config else {}
    # ``llm_component_id`` is a builder-level knob (the handler config schema
    # forbids extras), so pop it before constructing the pydantic config.
    component_id = (
        cfg_dict.pop("llm_component_id", None)
        or os.environ.get("LEGBA_SLM_ENTITY_RESOLVE_COMPONENT")
        or os.environ.get("LEGBA_SLM_COMPONENT_ID")
        or "slm"
    )
    cfg = (
        SLMEntityResolveConfig(**cfg_dict)
        if cfg_dict
        else SLMEntityResolveConfig()
    )

    slm_port = _FactorySLMPort(llm_handler_factory, str(component_id))
    candidate_port = PostgresEntityCandidatePort(pg_pool)
    return SLMEntityResolveHandler(
        cfg,
        slm=slm_port,
        candidates=candidate_port,
    )


__all__ = [
    "PipelineResult",
    "PipelineRunner",
    "build_filter_handler",
]
