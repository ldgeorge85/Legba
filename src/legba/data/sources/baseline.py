# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-source baseline pipeline — modality-branched (P-06, PIVOT §4.6).

The baseline runs **once per signal, at the source** (not per consuming
target): a source pulls/ingests raw payloads, the baseline enriches each one
*once*, then the single canonical signal fans out to N targets. This is the
"enrich once, read many" property the source-first pivot buys.

Two axes drive the baseline:

  1. **Structured-filter enrichment** (always, cheap). Populate the typed
     columns the subscription layer (P-08) pushes down to SQL/NATS:
     ``language`` (from the source-provided hint or payload), ``tags`` (from
     the payload), ``geo`` / ``entity_classes`` (from scope hints + payload).
     A raw pre-baseline signal has these empty; after the baseline they are
     filled so a target's predicate can match without re-deriving.

  2. **Media tier** (``descriptor.pipeline.media``):
       * ``reference`` (default) — set/keep ``media_ref`` as a *pointer*; do
         NOT fetch the bytes. Cheap, once, for all consumers.
       * ``eager`` — for sources where the media content *is* the value
         (a pure-audio feed, an image source), fetch + process at ingest via
         a :class:`MediaExtractor` (transcribe / caption / OCR). Still once
         per signal, at the source.

  Tier 3 (on-demand, analyst-driven ``process_media``) is the async job plane
  (P-07) and is out of this module's scope; this module owns tiers 1 + 2.

The extractor library is the legitimately incremental surface (PIVOT §4.6:
"Handlers fill in per modality as endpoints come online — extension point,
not stub"). This module ships the complete *mechanism* (the eager branch
dispatches by modality to a registered extractor) plus the no-op
:class:`PassthroughTextExtractor` for text/structured. There is NO shipped
media-modality extractor: eager extraction is a DECLARED SEAM (decision D2) —
a media signal on the eager path with no real registered extractor raises
:class:`~legba.data.jobs.media.MediaEndpointNotConfiguredError` (typed, loud,
no row written) instead of fabricating a caption/transcript into the shared
pool. Real hosted Whisper/VLM extractors register against the
:class:`MediaExtractor` protocol when the endpoints come online
(``LEGBA_MEDIA_API_URL``).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from ._contract import Signal, SourceContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Media extractor protocol + registry (the eager-tier seam)
# ---------------------------------------------------------------------------


@runtime_checkable
class MediaExtractor(Protocol):
    """Turns referenced media into structured enrichment, in place.

    Called only on the ``eager`` media path. Given a signal whose
    ``media_ref`` points at the content, the extractor fetches + processes it
    (audio→transcribe, image→caption/detect, video→keyframe+caption,
    blob→OCR/parse) and returns enrichment to merge into the signal's
    payload + structured columns. ``modalities`` declares which modalities it
    serves.

    Production extractors call hosted endpoints (Whisper / VLM / OCR) —
    the same hosted-model pattern as the NLP filters. The dev/example
    extractors below run inline with no external call so the eager mechanism
    is testable on the rig.
    """

    modalities: tuple[str, ...]

    async def extract(self, signal: Signal, ctx: SourceContext) -> dict[str, Any]: ...


class PassthroughTextExtractor:
    """No-op extractor for text/structured — the reference example.

    Returns an empty enrichment dict (text needs no media fetch). Present so
    the eager branch has a registered handler for the common modalities and
    the dispatch path is exercised without a network call.
    """

    modalities: tuple[str, ...] = ("text", "structured")

    async def extract(self, signal: Signal, ctx: SourceContext) -> dict[str, Any]:
        return {}


# Media modalities the eager tier would have to fetch + process. None of them
# has a shipped extractor — eager extraction is a declared seam (D2); see the
# module docstring + MEDIA seam guard in ``_enrich_media_eager``.
MEDIA_MODALITIES: tuple[str, ...] = ("image", "audio", "video", "binary")


def default_extractor_registry() -> dict[str, MediaExtractor]:
    """Build the modality -> extractor map shipped with the core.

    Ships ONLY the text/structured passthrough. The former
    ``EchoCaptionExtractor`` (a fabricated deterministic "caption" for
    image/audio/video/binary) is GONE — it was a stub in a production path and
    poisoned the shared pool with synthetic enrichment. A host wires real
    hosted extractors by registering them for the media modalities (last
    registration wins per modality).
    """
    registry: dict[str, MediaExtractor] = {}
    for extractor in (PassthroughTextExtractor(),):
        for modality in extractor.modalities:
            registry[modality] = extractor
    return registry


# ---------------------------------------------------------------------------
# Structured-filter enrichment (tier 1 — always)
# ---------------------------------------------------------------------------


def _coerce_tag(value: Any) -> str | None:
    """Coerce a raw tag value to the ``[a-z][a-z0-9_]*`` tag shape, or drop."""
    if not isinstance(value, str):
        return None
    norm = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in value.strip().lower())
    norm = norm.strip("_")
    if not norm or not norm[0].isalpha():
        return None
    return norm[:64]


def _enrich_structured(signal: Signal, ctx: SourceContext) -> None:
    """Fill the typed structured-filter columns in place (tier 1).

    Cheap, deterministic, no external call. The full enrichment chain
    (language_detect / geocode / classify / NER filters) is the
    ``descriptor.pipeline.enrichment`` stage list — wired by the registry
    pipeline factory; this baseline does the source-provided-hint level so a
    bare source still produces a filterable signal.
    """
    # language: source hint (pre-detection) wins; else any payload hint.
    if not signal.language:
        hint = signal.language_hint or signal.payload.get("language")
        if isinstance(hint, str) and hint:
            signal.language = hint[:16]

    # geo (C2): the source's ``scope_geo`` is the PUBLISHER'S origin, not the
    # story's subject. Stamping it into the indexed ``geo`` column HERE — before
    # the in-body geocoder in the enrichment chain runs — made every state-wire
    # desk read that wire's whole world output as "its" country: the enrichment
    # promote step only APPENDS the resolved in-body ISO to ``geo`` (it never
    # demotes the origin hint), so a Cuba-war Anadolu story landed geo={TR,US}
    # and Turkey's desk pulled all 900 Anadolu world stories. Keep the origin
    # OUT of ``geo``. Park it in a payload key (mirrors telegram's
    # ``publisher_origin_nongeo`` contract) so :func:`run_baseline` can apply it
    # to ``geo`` ONLY as a post-enrichment fallback — when nothing in-body
    # resolved (a genuinely-domestic story with no other country in its body).
    if ctx.scope_geo and "publisher_origin" not in signal.payload:
        signal.payload["publisher_origin"] = list(ctx.scope_geo)

    # tags: lift payload tags into the typed, indexed column.
    if not signal.tags:
        raw_tags = signal.payload.get("tags")
        if isinstance(raw_tags, list):
            coerced = [t for t in (_coerce_tag(x) for x in raw_tags) if t]
            if coerced:
                signal.tags = coerced[:64]

    # content_hash: a source SHOULD set this; backstop so dedup (P-09) always
    # has a key (hash of canonical_url + a stable payload projection).
    if not signal.content_hash:
        basis = (signal.canonical_url or "") + "\x1f" + str(signal.payload.get("title") or "")
        signal.content_hash = hashlib.sha256(basis.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Eager media tier (tier 2)
# ---------------------------------------------------------------------------


async def _enrich_media_eager(
    signal: Signal,
    ctx: SourceContext,
    extractors: dict[str, MediaExtractor],
) -> None:
    """Fetch + process referenced media at ingest (in place).

    Dispatch by ``signal.modality`` to a registered :class:`MediaExtractor`.
    The merge is additive: extractor payload merges into ``signal.payload``;
    extractor ``tags`` extend ``signal.tags``; an ``embedding_ref`` /
    ``object_ref`` the extractor returns is stamped onto the signal.

    SEAM GUARD (A-2 / D2): a media-modality signal with NO registered real
    extractor REFUSES — typed :class:`MediaEndpointNotConfiguredError`, loud
    structured log, no row written — rather than passing the signal through
    half-processed or fabricating enrichment.
    """
    extractor = extractors.get(signal.modality)
    if extractor is None:
        from ..jobs.media import MediaEndpointNotConfiguredError

        logger.error(
            "baseline.eager.refused source_id=%s modality=%s "
            "reason=no_real_extractor seam=eager-extraction "
            "env=LEGBA_MEDIA_API_URL (no stub enrichment may land in the pool)",
            ctx.source_id, signal.modality,
        )
        raise MediaEndpointNotConfiguredError(
            f"eager media extraction for modality {signal.modality!r} on "
            f"source {ctx.source_id!r} has no real extractor registered — "
            "eager extraction is a declared seam (configure "
            "LEGBA_MEDIA_API_URL and wire hosted extractors); refusing to "
            "write the signal"
        )
    try:
        enrichment = await extractor.extract(signal, ctx)
    except Exception as exc:  # one bad extraction must not poison the pull
        logger.warning(
            "baseline.eager.extract_failed source_id=%s modality=%s err=%s",
            ctx.source_id, signal.modality, exc,
        )
        signal.raw_provenance = {
            **signal.raw_provenance,
            "baseline_media_error": str(exc),
        }
        return

    if not enrichment:
        return
    payload_add = enrichment.get("payload")
    if isinstance(payload_add, dict):
        signal.payload = {**signal.payload, **payload_add}
    tags_add = enrichment.get("tags")
    if isinstance(tags_add, list):
        coerced = [t for t in (_coerce_tag(x) for x in tags_add) if t]
        merged = list(dict.fromkeys([*signal.tags, *coerced]))
        signal.tags = merged[:64]
    for col in ("embedding_ref", "object_ref"):
        val = enrichment.get(col)
        if isinstance(val, str) and val:
            setattr(signal, col, val)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def run_baseline(
    signal: Signal,
    ctx: SourceContext,
    *,
    media: str = "reference",
    extractors: dict[str, MediaExtractor] | None = None,
    enrichment_stage: Callable[[Signal, SourceContext], Awaitable[Signal | None]] | None = None,
) -> Signal | None:
    """Run the per-source baseline over one signal; return the enriched
    signal (or ``None`` if a filter dropped it).

    Parameters
    ----------
    signal:
        The raw signal yielded by the handler.
    ctx:
        The source context (scope hints, logger, source identity).
    media:
        ``descriptor.pipeline.media`` — ``"reference"`` (default; pointer
        only) or ``"eager"`` (fetch + process media now). The eager tier is a
        declared seam: a media-modality signal with no real registered
        extractor raises ``MediaEndpointNotConfiguredError`` (no row written).
    extractors:
        modality -> :class:`MediaExtractor` map for the eager tier. Defaults
        to :func:`default_extractor_registry` (text/structured passthrough
        only — no media-modality extractor ships in-tree).
    enrichment_stage:
        Optional async ``(signal, ctx) -> signal|None`` hook for the
        ``descriptor.pipeline.enrichment`` filter chain (language_detect /
        geocode / classify / NER). Runs after tier-1 structured enrichment.
        Returning ``None`` drops the signal (ingestion filter). The actor
        wires the registry pipeline factory here; tests can omit it.

    The baseline mutates the passed signal in place AND returns it (the
    in-place mutation keeps the rss/handler-yielded object authoritative;
    the return lets a filter stage replace/drop it).
    """
    if extractors is None:
        extractors = default_extractor_registry()

    # tier 1 — always, cheap, once.
    _enrich_structured(signal, ctx)

    # tier 2 — eager media (per-source flag).
    if media == "eager" and signal.media_ref:
        await _enrich_media_eager(signal, ctx, extractors)

    # optional enrichment filter chain (language_detect/geocode/classify/ner).
    if enrichment_stage is not None:
        out = await enrichment_stage(signal, ctx)
        if out is None:
            return None
        signal = out

    # geo fallback (C2 / S-2): the publisher-origin scope hint may reach the
    # indexed ``geo`` column ONLY when the STORY CONTENT independently
    # corroborates that country — never as a blanket default. The old rule
    # stamped the outlet's home country onto EVERY signal whose body left
    # ``geo`` empty after the enrichment chain (geocode/ner) + promote, so a
    # Singapore wire's LeBron / OpenAI / F1 stories all landed geo=SG and
    # flooded Singapore's country desk (S-2). Corroboration = the origin
    # country named in the body (title/text/raw_body offline sweep) or a
    # country-class NER entity. Where the content names no country we emit NO
    # geo rather than a wrong one — a missing tag only under-includes; a wrong
    # one actively misroutes. The origin was parked in payload by
    # :func:`_enrich_structured`.
    if not signal.geo:
        origin = signal.payload.get("publisher_origin")
        if isinstance(origin, list):
            hinted = [g for g in origin if isinstance(g, str) and g]
            corroborated = _origin_corroborated_by_content(signal, hinted)
            if corroborated:
                signal.geo = corroborated

    return signal


def _origin_corroborated_by_content(
    signal: Signal, origin_iso2: list[str]
) -> list[str]:
    """Subset of ``origin_iso2`` that the story CONTENT independently attests.

    S-2 precision guard for the publisher-origin geo fallback. A publisher's
    home country is an OUTLET-level fact, not the story's subject; it may only
    reach the indexed ``geo`` column when the body corroborates it — the
    country is named in the title/text/raw_body (offline country-name sweep) or
    carried as a ``country``-class NER entity. Cheap + offline (no geocoder
    call), matching the tier-1 baseline contract. Returns ``[]`` when nothing
    corroborates, so the signal stays geo-unattributed rather than mis-tagged
    with the outlet origin.
    """
    if not origin_iso2:
        return []
    # Local import: keep the filters layer out of this module's import graph
    # (baseline is imported during source bring-up before filters are wired).
    from ..filters.geocode import (
        country_iso2s_from_country_entities,
        country_iso2s_in_text,
    )

    payload = signal.payload or {}
    attested: set[str] = set()
    for field in ("title", "text", "raw_body"):
        value = payload.get(field)
        if isinstance(value, str) and value:
            attested |= country_iso2s_in_text(value)
    attested |= country_iso2s_from_country_entities(payload.get("entities"))
    return [iso for iso in origin_iso2 if iso.upper() in attested]


__all__ = [
    "MEDIA_MODALITIES",
    "MediaExtractor",
    "PassthroughTextExtractor",
    "default_extractor_registry",
    "run_baseline",
]
