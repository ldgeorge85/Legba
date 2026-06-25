# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Media-processing job types (P-07 / PIVOT §4.6 tier 3).

The ``process_media`` job takes a ``media_ref`` + an ``extraction`` kind and
produces a DERIVED signal carrying the extracted content. This module defines:

  * :class:`MediaExtraction` — the extraction-kind taxonomy (transcribe /
    caption / ocr / detect). Modality + requested extraction selects the hosted
    handler (audio→transcribe, image→caption/detect, …).
  * :class:`ProcessMediaInput` — the typed view over ``JobEnvelope.input_refs``
    for the ``process_media`` kind (keeps the generic envelope generic).
  * :class:`MediaExtractionResult` — the thin hosted-client return shape.
  * :func:`build_derived_signal` — assembles the DERIVED :class:`Signal` from a
    raw parent row + an extraction result, stamping P-01 provenance + lineage.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid5, NAMESPACE_URL

from pydantic import BaseModel, ConfigDict, Field

from ..sources._contract import Signal


class MediaEndpointNotConfiguredError(RuntimeError):
    """No real media-extraction endpoint is configured (A-2 / G3 close).

    Raised — never worked around — wherever media extraction would otherwise
    have to fabricate output: ``process_media`` enqueue + execution, and
    ``media: "eager"`` source activation. No stub transcript/caption may ever
    land in the shared signal pool; the seam fails loud instead. Configure
    ``LEGBA_MEDIA_API_URL`` (and wire real extractors for the eager tier) to
    open the path.
    """


def configured_media_endpoint() -> str | None:
    """The configured hosted media-extraction endpoint, or ``None``.

    Single source of truth for the ``LEGBA_MEDIA_API_URL`` check shared by the
    enqueue-side refusal (agency tool), the worker-side client factory and the
    eager-tier activation guard.
    """
    return (os.getenv("LEGBA_MEDIA_API_URL") or "").strip() or None


# audio→transcribe; image→caption / detect; video→transcribe (+keyframe later);
# blob→ocr. The handler library fills in per modality as endpoints come online.
MediaExtraction = Literal["transcribe", "caption", "ocr", "detect"]

# Modality default extraction — used when the caller passes a modality but no
# explicit extraction kind.
DEFAULT_EXTRACTION: dict[str, MediaExtraction] = {
    "audio": "transcribe",
    "video": "transcribe",
    "image": "caption",
    "binary": "ocr",
}


class ProcessMediaInput(BaseModel):
    """Typed view over ``JobEnvelope.input_refs`` for ``process_media``.

    ``derived_from`` is the raw signal the extraction enriches — the lineage
    parent. ``media_ref`` is the object-store URI / external URL to process
    (a REFERENCE, per the modality-first contract — the worker fetches it, the
    substrate never inlined it).
    """

    model_config = ConfigDict(extra="forbid")

    media_ref: str
    extraction: MediaExtraction
    derived_from: UUID                  # raw signal id (lineage parent)
    modality: Literal["audio", "video", "image", "binary"] = "audio"
    mime_type: str | None = None
    language_hint: str | None = None    # for transcribe — source language hint

    @classmethod
    def from_envelope_refs(cls, refs: dict[str, Any]) -> "ProcessMediaInput":
        return cls.model_validate(refs)


class MediaExtractionResult(BaseModel):
    """What the thin hosted media client returns.

    ``text`` is the extracted content (a transcript, a caption, OCR text).
    ``detail`` carries kind-specific extras (segments, confidence, detections).
    ``model`` + ``source`` record which endpoint produced it. ``source`` is
    ``hosted`` ONLY — the former clearly-marked stub edge is gone (A-2 / G3):
    a result that did not come from a real endpoint is structurally
    unrepresentable, so no fabricated extraction can ever land in the pool.
    """

    model_config = ConfigDict(extra="forbid")

    extraction: MediaExtraction
    text: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    model: str = ""
    source: Literal["hosted"] = "hosted"
    latency_ms: float | None = None


def _derived_signal_id(job_id: UUID, derived_from: UUID, extraction: str) -> UUID:
    """Deterministic derived-signal id.

    Derived deterministically from (job_id, parent, extraction) so a replayed
    job lands the SAME row id → the ``ON CONFLICT (id) DO NOTHING`` insert is a
    true idempotent no-op even if the idempotency ledger were bypassed. Belt +
    suspenders with the ledger claim.
    """
    return uuid5(NAMESPACE_URL, f"process_media:{job_id}:{derived_from}:{extraction}")


def build_derived_signal(
    *,
    job_id: UUID,
    parent_row: dict[str, Any],
    inp: ProcessMediaInput,
    result: MediaExtractionResult,
) -> Signal:
    """Assemble the DERIVED :class:`Signal` from a raw parent + extraction.

    The derived signal:
      * inherits the parent's ``source_id`` / ``owner_tenant`` (the observation
        still originates from the parent's source),
      * is stamped ``produced_by_kind='job'`` + ``produced_by_id=<job_id>``
        (P-01 provenance),
      * carries ``derived_from=[parent_id]`` (lineage),
      * inherits the parent's ``modality`` (G3) — the extraction's output IS a
        text transcript/caption/OCR string, but a modality-pinned subscription
        (e.g. one scoped to ``modality='audio'``) must still see the derived
        output of an audio job; stamping ``text`` unconditionally dropped the
        derived row out of every modality-pinned slice. Falls back to ``text``
        only when the parent row carries no modality,
      * keeps ``media_ref`` pointing at the processed media (audit trail),
      * inherits the parent's structured-filter columns — ``geo`` / ``tags`` /
        ``entity_classes`` / ``language`` — so a geo-/tag-scoped target's
        subscription matches the derived row exactly like it matched the raw
        parent (A-2: derived rows must not fall out of scoped slices),
      * carries the extracted text in ``payload['text']`` + the model/source
        metadata in ``raw_provenance``.
    """
    parent_id = inp.derived_from
    content = result.text or ""
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _parent_list(col: str) -> list[str]:
        val = parent_row.get(col)
        return [str(v) for v in val] if val else []

    return Signal(
        signal_id=_derived_signal_id(job_id, parent_id, inp.extraction),
        source_id=str(parent_row.get("source_id", "")),
        source_version=str(parent_row.get("source_version", "")),
        produced_by_id=str(job_id),
        produced_by_kind="job",
        fetched_at=datetime.now(tz=timezone.utc),
        owner_tenant=str(parent_row.get("owner_tenant", "default")),
        # Inherit the parent's modality (G3) so modality-pinned subscriptions
        # match the derived output. Fall back to 'text' only when absent.
        modality=str(parent_row.get("modality") or "text"),
        mime_type="text/plain",
        media_ref=inp.media_ref,
        payload={
            "extraction": inp.extraction,
            "text": content,
            "detail": result.detail,
            "parent_signal_id": str(parent_id),
        },
        language_hint=inp.language_hint,
        # Inherit the parent's structured-filter columns (A-2): the derived
        # text is ABOUT the same observation, so it stays inside the same
        # geo/tag/entity scope its parent matched.
        language=parent_row.get("language") or None,
        geo=_parent_list("geo"),
        tags=_parent_list("tags"),
        entity_classes=_parent_list("entity_classes"),
        raw_provenance={
            "job_id": str(job_id),
            "job_kind": "process_media",
            "extraction": inp.extraction,
            "model": result.model,
            "model_source": result.source,
            "latency_ms": result.latency_ms,
        },
        content_hash=content_hash,
        derived_from=[parent_id],
    )


__all__ = [
    "DEFAULT_EXTRACTION",
    "MediaEndpointNotConfiguredError",
    "MediaExtraction",
    "MediaExtractionResult",
    "ProcessMediaInput",
    "build_derived_signal",
    "configured_media_endpoint",
]
