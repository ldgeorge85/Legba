# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Multimodal readiness — the modality seam test (DESIGN §7.5).

No audio/video/GIS sources or extraction models exist yet, but the substrate,
fan-out routing, and baseline enrichment are modality-first BY DESIGN. This
test proves a NON-TEXT signal flows through those seams without a text
assumption breaking it — so adding a real modality later is a drop-in
(register an extractor + a renderer) rather than a debugging hunt. It exercises:

  1. fan-out — `modality` is a real subscription discriminator;
  2. enrichment — the text enrichers skip a body-less non-text signal (no crash);
  3. ingest — the media-tier extractor registry is keyed by modality and
     resolves an unregistered modality to nothing (graceful, not an error).
"""
from __future__ import annotations

import logging

import pytest

from legba.data.filters._contract import FilterContext
from legba.data.filters.language_detect import (
    LanguageDetectConfig,
    LanguageDetectHandler,
)
from legba.data.schemas.source import Subscription
from legba.data.sources._contract import Signal
from legba.data.sources.baseline import default_extractor_registry
from legba.runtime.subscription.filter import matches

_SRC = "source.test.media"
_TEN = "shared"


def _ctx() -> FilterContext:
    return FilterContext(
        target_id="t.test",
        target_version="v1",
        filter_id="modality_seam",
        logger=logging.getLogger("test.modality_seam"),
        scope_geo=[],
    )


def _row(modality: str, **over) -> dict:
    row = {
        "id": "00000000-0000-0000-0000-000000000001",
        "source_id": _SRC,
        "owner_tenant": _TEN,
        "modality": modality,
        "geo": [],
        "tags": ["news"],
        "entity_classes": [],
        "canonical_signal_id": None,
    }
    row.update(over)
    return row


def _nontext_signal(modality: str, mime_type: str, **payload) -> Signal:
    return Signal(
        source_id=_SRC,
        owner_tenant=_TEN,
        modality=modality,
        mime_type=mime_type,
        media_ref="s3://bucket/object",
        canonical_url="https://example.test/watch",
        payload=payload,
        tags=["news"],
    )


# --- 1. Fan-out: modality is a real routing discriminator ------------------


def test_modality_routing_discriminates():
    vid = _row("video")
    txt = _row("text")

    def m(sub, row):
        return matches(sub, row, source_id=_SRC, owner_tenant=_TEN)

    # A video signal matches a video-only subscription, not a text-only one.
    assert m(Subscription(modalities=["video"]), vid) is True
    assert m(Subscription(modalities=["text"]), vid) is False
    # ...and a text signal, the inverse.
    assert m(Subscription(modalities=["text"]), txt) is True
    assert m(Subscription(modalities=["video"]), txt) is False
    # An unconstrained subscription takes any modality.
    assert m(Subscription(), vid) is True
    # Multi-modality subscriptions union.
    assert m(Subscription(modalities=["audio", "video"]), vid) is True


# --- 2. Enrichment: text enrichers skip a body-less non-text signal ---------


@pytest.mark.asyncio
async def test_language_detect_skips_bodyless_nontext():
    # A video signal carries no text body until a (future) transcript extractor
    # runs. The in-process text enricher must skip it gracefully, never crash.
    sig = _nontext_signal("video", "video/mp4")
    out = await LanguageDetectHandler(LanguageDetectConfig()).transform(sig, _ctx())
    assert out is not None            # never drops
    assert out.modality == "video"    # passed through untouched
    assert out.media_ref == "s3://bucket/object"


# --- 3. Ingest: the media-tier extractor registry is modality-keyed ---------


def test_extractor_registry_is_modality_keyed():
    reg = default_extractor_registry()
    assert isinstance(reg, dict)
    # Keys are modality strings; values satisfy the MediaExtractor protocol.
    for modality, extractor in reg.items():
        assert isinstance(modality, str)
        assert hasattr(extractor, "extract")
    # An unregistered modality resolves to nothing — the eager tier logs
    # `baseline.eager.no_extractor` and falls through (no crash).
    assert reg.get("does-not-exist-modality") is None
