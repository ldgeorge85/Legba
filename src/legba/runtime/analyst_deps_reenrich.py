# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RE-ENRICHMENT sweeps' deps wiring, in a leaf beside ``analyst_deps_builder``.

Three deterministic sub-handlers re-run ingest-time enrichment over rows that
are already persisted, and each needs the process-lifetime hosted-NLP source
threaded onto its ``deps.extras``:

  * ``reenrich_ner`` — the one-time NER backfill (:func:`_wire_reenrich_ner`);
  * ``reenrich_translation`` — the translation backfill
    (:func:`_wire_reenrich_translation`);
  * ``reenrich_pending`` — W11-F118, the standing sweep over the rows the
    source actor wrote UNENRICHED (``payload.enrichment_pending``) when the NLP
    plane was down (:func:`_wire_reenrich_pending`). Besides the NLP client it
    gets the registry client and the HOST's source-enrichment factory, so it can
    rebuild a row's source descriptor and run the very chain the ingest path
    runs.

A fourth, ``verify_backfill``, is not a re-enrichment but rides the
same dispatch for the same reason (one call in the builder, which sits at its
ceiling): it re-runs the faithfulness verify leg over cited findings left with no
critique, and needs only the registry client (the secrets resolver is already on
``deps.secrets_resolve``) to wire each analyzed analyst's judge chain
(:func:`_wire_verify_backfill`).

The first two moved here verbatim from ``analyst_deps_builder`` (module-size
gate: that file sat at its ceiling) and are re-exported there under the same
names; :func:`wire_reenrich_kind_deps` is the one dispatch the builder calls.
"""

from __future__ import annotations

import logging
from typing import Any

from ..data.schemas.analyst import AnalystDescriptor
from .deps import StandardDeps

logger = logging.getLogger(__name__)

__all__ = [
    "_wire_reenrich_ner",
    "_wire_reenrich_pending",
    "_wire_reenrich_translation",
    "_wire_verify_backfill",
    "wire_reenrich_kind_deps",
]


async def wire_reenrich_kind_deps(
    descriptor: AnalystDescriptor,
    deps: StandardDeps,
    *,
    sub_handler: str | None,
    nlp_client: Any | None,
    registry_client: Any | None = None,
    source_enrichment_factory: Any | None = None,
) -> StandardDeps:
    """Wire the bound re-enrichment sweep's deps; any other sub-handler (or a
    non-deterministic kind, ``sub_handler=None``) gets ``deps`` back unchanged,
    so no other analyst pays for a client it never uses."""
    if sub_handler == "reenrich_ner":
        return await _wire_reenrich_ner(descriptor, deps, nlp_client=nlp_client)
    if sub_handler == "reenrich_translation":
        return await _wire_reenrich_translation(descriptor, deps, nlp_client=nlp_client)
    if sub_handler == "reenrich_pending":
        return await _wire_reenrich_pending(
            descriptor, deps, nlp_client=nlp_client,
            registry_client=registry_client,
            source_enrichment_factory=source_enrichment_factory,
        )
    if sub_handler == "verify_backfill":
        return _wire_verify_backfill(descriptor, deps, registry_client=registry_client)
    return deps


def _wire_verify_backfill(
    descriptor: AnalystDescriptor,
    deps: StandardDeps,
    *,
    registry_client: Any | None,
) -> StandardDeps:
    """Thread the registry client onto ``deps.extras`` for the verify-backfill
    sweep — it resolves each analyzed analyst's CURRENT judge chain
    from its typed descriptor, as ``dapr_host`` does. Degrade-not-break: no
    client → ``deps`` unchanged and the sweep no-ops (``skipped_no_judge``)."""
    from dataclasses import replace as _dc_replace

    from ..data.analysts.deterministic_handlers.verify_backfill import (
        REGISTRY_DEPS_EXTRA_KEY as _REGISTRY_KEY,
    )

    if registry_client is None:
        logger.warning(
            "analyst_deps_builder.verify_backfill.no_registry analyst=%r — the "
            "verify-backfill sweep no-ops until a registry client is wired",
            descriptor.identity.id,
        )
        return deps
    return _dc_replace(deps, extras={**dict(deps.extras), _REGISTRY_KEY: registry_client})


async def _wire_reenrich_pending(
    descriptor: AnalystDescriptor,
    deps: StandardDeps,
    *,
    nlp_client: Any | None,
    registry_client: Any | None,
    source_enrichment_factory: Any | None,
) -> StandardDeps:
    """Thread the pending sweep's two chains onto ``deps.extras``.

    * The DESCRIPTOR chain: the registry client + the host's
      ``_source_enrichment_factory`` — the sweep fetches each row's source
      descriptor and builds the same ``pipeline.enrichment`` stage the
      SourceActor would have run. Wired only when both are present.
    * The FALLBACK chain: the resolved hosted NLP client, for rows whose source
      descriptor cannot be resolved (retired source, registry miss) — the
      language_detect + translate-then-NER chain ``reenrich_ner`` already runs.

    Degrade-not-break: whatever cannot be wired is left out (with a warning);
    with neither chain the sweep no-ops that tick (``skipped_no_chain``)."""
    from dataclasses import replace as _dc_replace

    from ..data.analysts.deterministic_handlers.reenrich_pending import (
        NLP_DEPS_EXTRA_KEY as _NLP_KEY,
        REGISTRY_DEPS_EXTRA_KEY as _REGISTRY_KEY,
        STAGE_FACTORY_DEPS_EXTRA_KEY as _FACTORY_KEY,
    )

    added: dict[str, Any] = {}
    if registry_client is not None and source_enrichment_factory is not None:
        added[_REGISTRY_KEY] = registry_client
        added[_FACTORY_KEY] = source_enrichment_factory
    resolved: Any | None = nlp_client
    getter = getattr(nlp_client, "get", None)
    if callable(getter):
        try:
            resolved = await getter()
        except Exception as exc:
            logger.warning(
                "analyst_deps_builder.reenrich_pending.nlp_resolve_failed analyst=%r "
                "err=%s — the fallback chain is not wired this build",
                descriptor.identity.id, exc,
            )
            resolved = None
    if resolved is not None:
        added[_NLP_KEY] = resolved
    if not added:
        logger.warning(
            "analyst_deps_builder.reenrich_pending.no_chain analyst=%r — neither the "
            "source-enrichment factory nor an NLP client is available; the pending "
            "sweep no-ops until one is wired",
            descriptor.identity.id,
        )
        return deps
    logger.info(
        "analyst_deps_builder.reenrich_pending.wired analyst=%r descriptor_chain=%s "
        "fallback_chain=%s",
        descriptor.identity.id, _FACTORY_KEY in added, _NLP_KEY in added,
    )
    return _dc_replace(deps, extras={**dict(deps.extras), **added})


async def _wire_reenrich_ner(
    descriptor: AnalystDescriptor,
    deps: StandardDeps,
    *,
    nlp_client: Any | None,
) -> StandardDeps:
    """Resolve the hosted :class:`NlpServiceClient` and merge it into
    ``deps.extras`` for the reenrich_ner NER-backfill sweep, returning the
    (replaced) deps.

    NLP-WIRING CHOICE (mirrors ``_wire_signal_embedder``'s embedder reuse): rather
    than re-plumb the registry + secrets here, we reuse the process-lifetime
    hosted-NLP source the host already built at bring-up — the
    :class:`~legba.runtime.nlp_client_factory.LazyNlpClient` holder for the
    ``nlp.local.legba_models`` stack component (the SAME source the source-enrichment
    pipeline's ner_multilingual filter binds to). It arrives via
    ``build_analyst_run_method(..., nlp_client=...)``. The holder resolves LAZILY —
    we call it HERE (only for this bound sub-handler) so no other deterministic
    analyst triggers a registry round-trip for a client it never uses.

    Degrade-not-break (mirrors ``_wire_signal_embedder`` / ``_wire_corpus_indexer_os``):
    a ``None`` source, or any resolution failure (models-host unreachable / stack row
    not seeded at deps-build), logs a warning and returns ``deps`` UNMODIFIED — the
    sweep then no-ops that tick (its own no-nlp guard sets ``skipped_no_nlp``). The
    holder does NOT cache a failure, so a later deps (re)build retries once the plane
    heals."""
    from dataclasses import replace as _dc_replace

    from ..data.analysts.deterministic_handlers.reenrich_ner import (
        NLP_DEPS_EXTRA_KEY as _NLP_KEY,
    )

    if nlp_client is None:
        logger.warning(
            "analyst_deps_builder.reenrich_ner.no_nlp_source analyst=%r — no hosted "
            "NLP client source was provisioned at bring-up; the NER-backfill sweep "
            "no-ops until one is wired",
            descriptor.identity.id,
        )
        return deps

    # The source is the LazyNlpClient holder (async .get()) in production; accept an
    # already-resolved client too (tests / a future eager wiring) via duck-typing on
    # the async getter.
    resolved: Any | None = nlp_client
    getter = getattr(nlp_client, "get", None)
    if callable(getter):
        try:
            resolved = await getter()
        except Exception as exc:
            logger.warning(
                "analyst_deps_builder.reenrich_ner.nlp_resolve_failed analyst=%r "
                "err=%s — degrading to the no-nlp path (the sweep no-ops this tick; "
                "a later deps rebuild retries once the models-host heals)",
                descriptor.identity.id, exc,
            )
            return deps

    if resolved is None or not getattr(resolved, "supports_translate", True):
        logger.warning(  # W11-F107/F109: a gateway client with no translate_model
            "analyst_deps_builder.reenrich_ner.nlp_unresolved analyst=%r — the NLP "
            "source is None or cannot translate; the NER-backfill sweep no-ops",
            descriptor.identity.id,
        )
        return deps

    merged_extras = {**dict(deps.extras), _NLP_KEY: resolved}
    logger.info(
        "analyst_deps_builder.reenrich_ner.nlp_wired analyst=%r — resolved the hosted "
        "NlpServiceClient; the NER-backfill translate-then-NER plane is wired",
        descriptor.identity.id,
    )
    return _dc_replace(deps, extras=merged_extras)


async def _wire_reenrich_translation(
    descriptor: AnalystDescriptor,
    deps: StandardDeps,
    *,
    nlp_client: Any | None,
) -> StandardDeps:
    """Resolve the hosted :class:`NlpServiceClient` and merge it into
    ``deps.extras`` under the reenrich_translation key (M13/T-1c).

    Identical wiring to :func:`_wire_reenrich_ner` (same process-lifetime hosted-NLP
    source — translate is the same ``nlp.local.legba_models`` /translate plane), only
    the extras KEY differs so the two backfill sweeps stay independently addressable.
    Degrade-not-break: a ``None`` source / resolution failure returns ``deps``
    UNMODIFIED and the sweep no-ops that tick (its own no-nlp guard)."""
    from dataclasses import replace as _dc_replace

    from ..data.analysts.deterministic_handlers.reenrich_translation import (
        NLP_DEPS_EXTRA_KEY as _NLP_KEY,
    )

    if nlp_client is None:
        logger.warning(
            "analyst_deps_builder.reenrich_translation.no_nlp_source analyst=%r — no "
            "hosted NLP client source was provisioned at bring-up; the translation-"
            "backfill sweep no-ops until one is wired",
            descriptor.identity.id,
        )
        return deps

    # The source is the LazyNlpClient holder (async .get()) in production; accept an
    # already-resolved client too (tests) via duck-typing on the async getter.
    resolved: Any | None = nlp_client
    getter = getattr(nlp_client, "get", None)
    if callable(getter):
        try:
            resolved = await getter()
        except Exception as exc:
            logger.warning(
                "analyst_deps_builder.reenrich_translation.nlp_resolve_failed "
                "analyst=%r err=%s — degrading to the no-nlp path (the sweep no-ops "
                "this tick; a later deps rebuild retries once the models-host heals)",
                descriptor.identity.id, exc,
            )
            return deps

    if resolved is None or not getattr(resolved, "supports_translate", True):
        logger.warning(  # W11-F107/F109: a gateway client with no translate_model
            "analyst_deps_builder.reenrich_translation.nlp_unresolved analyst=%r — the "
            "NLP source is None or cannot translate; the translation-backfill sweep no-ops",
            descriptor.identity.id,
        )
        return deps

    merged_extras = {**dict(deps.extras), _NLP_KEY: resolved}
    logger.info(
        "analyst_deps_builder.reenrich_translation.nlp_wired analyst=%r — resolved the "
        "hosted NlpServiceClient; the translation-backfill /translate plane is wired",
        descriptor.identity.id,
    )
    return _dc_replace(deps, extras=merged_extras)
