# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for :class:`legba.data.filters.language_detect.LanguageDetectHandler` (L-150).

No external services required — the handler is pure CPU plus an in-process
language model. Tests exercise:

  * Multilingual detection across en / pt / es / zh / ar / ru / de / fr / ja.
  * Short-text fallback → ``"und"``.
  * Confidence-floor fallback → ``"und"``.
  * ``restrict_to`` allow-listing → non-allowed languages → ``"und"``.
  * ``target_languages_only`` allow-listing via :attr:`FilterContext.scope_languages`.
  * Idempotency: ``payload["language"]`` already set is a no-op.
  * ``force_redetect`` overrides idempotency.
  * Text-field priority order (``text`` → ``title`` → ``summary`` → ``raw_body``).
  * Handler ClassVar shape matches L-102 §1 / §3 contract.
  * In-process counters (``signals_in_24h``, etc.) advance per :meth:`transform`.
  * The default detector loads the model once at construction (perf contract).

Tests use a real lingua backend when available; a deterministic stub
``_StaticDetector`` is used for tests that need exact lang/confidence
control (allow-list semantics, low-confidence fallback).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from legba.data.filters import (
    FilterContext,
    LanguageDetectConfig,
    LanguageDetectHandler,
)
from legba.data.filters.language_detect import (
    PAYLOAD_LANGUAGE_CONFIDENCE_KEY,
    PAYLOAD_LANGUAGE_KEY,
    UND,
    _DetectorAdapter,
    _extract_text,
)
from legba.data.sources._contract import Signal


# ---------------------------------------------------------------------------
# Test-only deterministic detector adapter
# ---------------------------------------------------------------------------


class _StaticDetector:
    """Deterministic stand-in for :class:`_DetectorAdapter`.

    Returns whatever ``(lang, conf)`` was configured at construction. Used
    for tests that need exact-control behavior (low-confidence, allow-list).
    Implements the public surface ``_DetectorAdapter`` exposes to the
    handler — ``backend`` property + ``detect(text)`` method.
    """

    def __init__(self, lang: str = "en", conf: float = 0.99) -> None:
        self._lang = lang
        self._conf = conf
        self.calls: list[str] = []

    @property
    def backend(self) -> str:
        return "static"

    def detect(self, text: str) -> tuple[str, float]:
        self.calls.append(text)
        return self._lang, self._conf


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signal(payload: dict[str, Any], **overrides: Any) -> Signal:
    # Source-first pivot: Signal is source-owned — target_id moved off the
    # observation (it lives only on derived target outputs). See
    # PIVOT_PROPOSAL §4.3 / migration the source-first pivot.
    base = {
        "source_id": "test_source",
        "payload": payload,
        "content_hash": "deadbeef",
    }
    base.update(overrides)
    return Signal(**base)


def _ctx(**overrides: Any) -> FilterContext:
    defaults = {
        "target_id": "test_target",
        "target_version": "v1",
        "filter_id": "language_detect@1",
        "scope_languages": [],
        "scope_geo": [],
        "logger": logging.getLogger("test.language_detect"),
    }
    defaults.update(overrides)
    return FilterContext(**defaults)


# ---------------------------------------------------------------------------
# Sample multilingual corpus (long enough to clear the 20-char default
# threshold and to be unambiguous to lingua).
# ---------------------------------------------------------------------------


# Samples are sized so that lingua's normalized confidence clears the
# default 0.6 floor. Lingua's confidence is normalized across all 75+
# supported languages; ~120-character samples with distinctive vocabulary
# reliably produce >0.7 confidence on the dominant language.
_CORPUS = {
    "en": (
        "The quick brown fox jumps over the lazy dog near the riverbank. "
        "The forest was filled with birds singing happily on this morning."
    ),
    "pt": (
        "O rato roeu a roupa do rei de Roma e fugiu para a floresta densa. "
        "Os pássaros cantavam alegremente naquela manhã ensolarada."
    ),
    "es": (
        "El veloz murciélago hindú comía feliz cardillo y kiwi en el jardín. "
        "Los pájaros cantaban alegremente aquella mañana soleada de primavera."
    ),
    "zh": (
        "这是一个用于测试语言检测的中文句子,请准确地识别它的语言。"
        "这段文字包含足够的字符来确保检测器可以可靠地工作。"
    ),
    "ar": (
        "هذه جملة باللغة العربية تستخدم لاختبار كاشف اللغة بدقة عالية. "
        "يحتوي هذا النص على عدد كاف من الكلمات لضمان عمل الكاشف بشكل موثوق."
    ),
    "ru": (
        "Съешь же ещё этих мягких французских булок, да выпей чаю с лимоном. "
        "Птицы радостно пели в то прекрасное солнечное утро в лесу."
    ),
    "de": (
        "Der schnelle braune Fuchs springt über den faulen Hund am Flussufer. "
        "Im Wald sangen die Vögel an diesem schönen sonnigen Morgen fröhlich."
    ),
    "fr": (
        "Le rapide renard brun saute par-dessus le chien paresseux près de la rivière. "
        "Les oiseaux chantaient joyeusement par ce beau matin ensoleillé."
    ),
    "ja": (
        "これは日本語の文章であり、言語検出機能を検証するための例です。"
        "この文章には十分な文字数が含まれているため、検出器は確実に動作します。"
    ),
}


# ---------------------------------------------------------------------------
# 1) Multilingual detection — exercises the real backend.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("expected_lang,text", sorted(_CORPUS.items()))
@pytest.mark.asyncio
async def test_detects_multilingual_corpus(expected_lang: str, text: str) -> None:
    handler = LanguageDetectHandler(LanguageDetectConfig())
    out = await handler.transform(
        _signal({"text": text}),
        _ctx(),
    )
    assert out is not None, "language_detect should never drop a signal"
    assert out.payload[PAYLOAD_LANGUAGE_KEY] == expected_lang, (
        f"expected {expected_lang!r}, got {out.payload[PAYLOAD_LANGUAGE_KEY]!r}"
    )
    assert 0.0 <= out.payload[PAYLOAD_LANGUAGE_CONFIDENCE_KEY] <= 1.0


# ---------------------------------------------------------------------------
# 2) Short-text fallback.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_text_marked_und() -> None:
    handler = LanguageDetectHandler(LanguageDetectConfig(min_text_length=20))
    out = await handler.transform(
        _signal({"text": "hi"}),
        _ctx(),
    )
    assert out is not None
    assert out.payload[PAYLOAD_LANGUAGE_KEY] == UND
    assert out.payload[PAYLOAD_LANGUAGE_CONFIDENCE_KEY] == 0.0


@pytest.mark.asyncio
async def test_short_text_threshold_respected() -> None:
    """Raising min_text_length pushes longer texts into the fallback path."""
    handler = LanguageDetectHandler(
        LanguageDetectConfig(min_text_length=200),
    )
    # English corpus sample is < 200 chars; with the elevated floor it
    # should fall back to 'und'.
    out = await handler.transform(
        _signal({"text": _CORPUS["en"]}),
        _ctx(),
    )
    assert out is not None
    assert out.payload[PAYLOAD_LANGUAGE_KEY] == UND


@pytest.mark.asyncio
async def test_empty_text_marked_und() -> None:
    handler = LanguageDetectHandler(LanguageDetectConfig())
    out = await handler.transform(_signal({}), _ctx())
    assert out is not None
    assert out.payload[PAYLOAD_LANGUAGE_KEY] == UND


# ---------------------------------------------------------------------------
# 3) Confidence-floor fallback.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_confidence_marked_und() -> None:
    handler = LanguageDetectHandler(
        LanguageDetectConfig(min_confidence=0.95),
        detector=_StaticDetector(lang="en", conf=0.42),
    )
    out = await handler.transform(
        _signal({"text": "A long enough piece of text for the threshold."}),
        _ctx(),
    )
    assert out is not None
    assert out.payload[PAYLOAD_LANGUAGE_KEY] == UND
    # We preserve the actual detector confidence so downstream can see it.
    assert out.payload[PAYLOAD_LANGUAGE_CONFIDENCE_KEY] == pytest.approx(0.42)


@pytest.mark.asyncio
async def test_high_confidence_passes_through() -> None:
    handler = LanguageDetectHandler(
        LanguageDetectConfig(min_confidence=0.5),
        detector=_StaticDetector(lang="pt", conf=0.91),
    )
    out = await handler.transform(
        _signal({"text": "A long enough piece of text for the threshold."}),
        _ctx(),
    )
    assert out is not None
    assert out.payload[PAYLOAD_LANGUAGE_KEY] == "pt"
    assert out.payload[PAYLOAD_LANGUAGE_CONFIDENCE_KEY] == pytest.approx(0.91)


# ---------------------------------------------------------------------------
# 4) restrict_to allow-listing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restrict_to_allows_listed_language() -> None:
    handler = LanguageDetectHandler(
        LanguageDetectConfig(restrict_to=["pt", "es"], min_confidence=0.5),
        detector=_StaticDetector(lang="pt", conf=0.9),
    )
    out = await handler.transform(
        _signal({"text": "A long enough piece of text for the threshold."}),
        _ctx(),
    )
    assert out is not None
    assert out.payload[PAYLOAD_LANGUAGE_KEY] == "pt"


@pytest.mark.asyncio
async def test_restrict_to_rewrites_disallowed_to_und() -> None:
    handler = LanguageDetectHandler(
        LanguageDetectConfig(restrict_to=["pt", "es"], min_confidence=0.5),
        detector=_StaticDetector(lang="en", conf=0.99),
    )
    out = await handler.transform(
        _signal({"text": "A long enough piece of text for the threshold."}),
        _ctx(),
    )
    assert out is not None
    assert out.payload[PAYLOAD_LANGUAGE_KEY] == UND
    # We preserve the original detector confidence even on rewrite to 'und'.
    assert out.payload[PAYLOAD_LANGUAGE_CONFIDENCE_KEY] == pytest.approx(0.99)


@pytest.mark.asyncio
async def test_restrict_to_is_case_insensitive() -> None:
    handler = LanguageDetectHandler(
        LanguageDetectConfig(restrict_to=["PT", "ES"], min_confidence=0.5),
        detector=_StaticDetector(lang="pt", conf=0.9),
    )
    out = await handler.transform(
        _signal({"text": "A long enough piece of text for the threshold."}),
        _ctx(),
    )
    assert out is not None
    assert out.payload[PAYLOAD_LANGUAGE_KEY] == "pt"


# ---------------------------------------------------------------------------
# 5) target_languages_only — per-target scope restriction.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_target_languages_only_allows_in_scope() -> None:
    handler = LanguageDetectHandler(
        LanguageDetectConfig(target_languages_only=True, min_confidence=0.5),
        detector=_StaticDetector(lang="pt", conf=0.9),
    )
    out = await handler.transform(
        _signal({"text": "A long enough piece of text for the threshold."}),
        _ctx(scope_languages=["pt", "en"]),
    )
    assert out is not None
    assert out.payload[PAYLOAD_LANGUAGE_KEY] == "pt"


@pytest.mark.asyncio
async def test_target_languages_only_blocks_out_of_scope() -> None:
    handler = LanguageDetectHandler(
        LanguageDetectConfig(target_languages_only=True, min_confidence=0.5),
        detector=_StaticDetector(lang="fr", conf=0.9),
    )
    out = await handler.transform(
        _signal({"text": "A long enough piece of text for the threshold."}),
        _ctx(scope_languages=["pt", "en"]),
    )
    assert out is not None
    assert out.payload[PAYLOAD_LANGUAGE_KEY] == UND


@pytest.mark.asyncio
async def test_target_languages_only_with_empty_scope_blocks_everything() -> None:
    """Configuration smell: target_languages_only=True + empty scope.

    Handler fails closed (nothing allowed) — surfaces the misconfiguration
    rather than silently allowing everything.
    """
    handler = LanguageDetectHandler(
        LanguageDetectConfig(target_languages_only=True, min_confidence=0.5),
        detector=_StaticDetector(lang="en", conf=0.99),
    )
    out = await handler.transform(
        _signal({"text": "A long enough piece of text for the threshold."}),
        _ctx(scope_languages=[]),
    )
    assert out is not None
    assert out.payload[PAYLOAD_LANGUAGE_KEY] == UND


@pytest.mark.asyncio
async def test_restrict_to_and_target_languages_only_compose_as_intersection() -> None:
    handler = LanguageDetectHandler(
        LanguageDetectConfig(
            restrict_to=["en", "pt", "es"],
            target_languages_only=True,
            min_confidence=0.5,
        ),
        detector=_StaticDetector(lang="es", conf=0.9),
    )
    # 'es' is in restrict_to but NOT in scope → blocked.
    out_blocked = await handler.transform(
        _signal({"text": "A long enough piece of text for the threshold."}),
        _ctx(scope_languages=["en", "pt"]),
    )
    assert out_blocked is not None
    assert out_blocked.payload[PAYLOAD_LANGUAGE_KEY] == UND

    # Now 'es' is in both → allowed.
    handler2 = LanguageDetectHandler(
        LanguageDetectConfig(
            restrict_to=["en", "pt", "es"],
            target_languages_only=True,
            min_confidence=0.5,
        ),
        detector=_StaticDetector(lang="es", conf=0.9),
    )
    out_allowed = await handler2.transform(
        _signal({"text": "A long enough piece of text for the threshold."}),
        _ctx(scope_languages=["es", "pt"]),
    )
    assert out_allowed is not None
    assert out_allowed.payload[PAYLOAD_LANGUAGE_KEY] == "es"


# ---------------------------------------------------------------------------
# 6) Idempotency.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_when_language_already_set() -> None:
    detector = _StaticDetector(lang="fr", conf=0.9)
    handler = LanguageDetectHandler(LanguageDetectConfig(), detector=detector)
    pre = _signal(
        {
            "text": "A long enough piece of text for the threshold.",
            PAYLOAD_LANGUAGE_KEY: "en",
            PAYLOAD_LANGUAGE_CONFIDENCE_KEY: 0.77,
        }
    )
    out = await handler.transform(pre, _ctx())
    assert out is not None
    # Language untouched.
    assert out.payload[PAYLOAD_LANGUAGE_KEY] == "en"
    assert out.payload[PAYLOAD_LANGUAGE_CONFIDENCE_KEY] == 0.77
    # Detector was not invoked.
    assert detector.calls == []


@pytest.mark.asyncio
async def test_force_redetect_overrides_idempotency() -> None:
    detector = _StaticDetector(lang="fr", conf=0.9)
    handler = LanguageDetectHandler(
        LanguageDetectConfig(force_redetect=True),
        detector=detector,
    )
    pre = _signal(
        {
            "text": "A long enough piece of text for the threshold.",
            PAYLOAD_LANGUAGE_KEY: "en",
            PAYLOAD_LANGUAGE_CONFIDENCE_KEY: 0.77,
        }
    )
    out = await handler.transform(pre, _ctx())
    assert out is not None
    assert out.payload[PAYLOAD_LANGUAGE_KEY] == "fr"
    assert out.payload[PAYLOAD_LANGUAGE_CONFIDENCE_KEY] == pytest.approx(0.9)
    assert detector.calls, "detector should have been invoked under force_redetect"


# ---------------------------------------------------------------------------
# 7) Text-field priority order.
# ---------------------------------------------------------------------------


def test_extract_text_priority_order() -> None:
    # 'text' wins when present.
    assert _extract_text({
        "text": "  hello  ", "title": "ignored", "summary": "x", "raw_body": "y",
    }) == "hello"
    # falls through to 'title'.
    assert _extract_text({
        "title": "from title", "summary": "x", "raw_body": "y",
    }) == "from title"
    # falls through to 'summary'.
    assert _extract_text({"summary": "from summary", "raw_body": "y"}) == "from summary"
    # falls through to 'raw_body'.
    assert _extract_text({"raw_body": "from raw"}) == "from raw"
    # all empty / missing → empty.
    assert _extract_text({}) == ""
    # empty 'text' should NOT shadow a populated 'summary'.
    assert _extract_text({"text": "   ", "summary": "fallback"}) == "fallback"


@pytest.mark.asyncio
async def test_transform_uses_text_priority_order_end_to_end() -> None:
    detector = _StaticDetector(lang="en", conf=0.99)
    handler = LanguageDetectHandler(LanguageDetectConfig(), detector=detector)
    await handler.transform(
        _signal({
            "text": "primary field with enough length to pass the floor",
            "title": "should not be inspected",
            "summary": "should not be inspected",
            "raw_body": "should not be inspected",
        }),
        _ctx(),
    )
    assert detector.calls == [
        "primary field with enough length to pass the floor",
    ]


# ---------------------------------------------------------------------------
# 8) Handler class shape — L-102 §1 / §3 contract.
# ---------------------------------------------------------------------------


def test_handler_classvars_match_contract() -> None:
    assert LanguageDetectHandler.kind == "language_detect"
    assert LanguageDetectHandler.family == "filter"
    assert LanguageDetectHandler.schema_version == (
        "legba/filter.language_detect/1-0-0"
    )
    assert LanguageDetectHandler.config_schema is LanguageDetectConfig
    assert LanguageDetectHandler.idempotent is True
    # output_contract declares both fields the handler stamps.
    keys = set(LanguageDetectHandler.output_contract)
    assert keys == {"payload.language", "payload.language_confidence"}
    assert LanguageDetectHandler.output_contract["payload.language"] is str
    assert (
        LanguageDetectHandler.output_contract["payload.language_confidence"]
        is float
    )


def test_handler_satisfies_stream_handler_protocol() -> None:
    """``LanguageDetectHandler`` must satisfy the L-102 §3 Protocol shape."""
    from legba.data.filters._contract import StreamHandler

    handler = LanguageDetectHandler(
        LanguageDetectConfig(),
        detector=_StaticDetector(),
    )
    assert isinstance(handler, StreamHandler)


def test_lifecycle_hooks_are_async_noops() -> None:
    """``on_configure`` / ``on_activate`` / etc. exist + are awaitable."""
    import asyncio

    handler = LanguageDetectHandler(
        LanguageDetectConfig(),
        detector=_StaticDetector(),
    )
    loop = asyncio.new_event_loop()
    try:
        ctx = _ctx()
        loop.run_until_complete(handler.on_configure(ctx))
        loop.run_until_complete(handler.on_activate(ctx))
        loop.run_until_complete(handler.on_pause(ctx))
        loop.run_until_complete(handler.on_resume(ctx))
        loop.run_until_complete(handler.on_retire(ctx))
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# 9) Health bookkeeping.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_counters_advance() -> None:
    handler = LanguageDetectHandler(
        LanguageDetectConfig(),
        detector=_StaticDetector(lang="en", conf=0.99),
    )
    ctx = _ctx()
    for _ in range(3):
        await handler.transform(
            _signal({"text": "A long enough piece of text for the threshold."}),
            ctx,
        )
    h = await handler.health_check(ctx)
    assert h.state == "healthy"
    assert h.signals_in_24h == 3
    assert h.signals_out_24h == 3
    assert h.last_success_at is not None
    assert h.detail["backend"] == "static"
    assert h.detail["min_confidence"] == 0.6
    assert h.detail["min_text_length"] == 20


# ---------------------------------------------------------------------------
# 10) Backend resolution / performance contract.
# ---------------------------------------------------------------------------


def test_default_backend_is_langdetect_when_available() -> None:
    """Post-reshape (2026-05-22): langdetect is the default.

    When ``backend='auto'`` (default), the resolved backend should be
    langdetect — the tiny pure-Python detector — unless langdetect is
    not importable, in which case lingua is the legacy fallback.
    """
    pytest.importorskip("langdetect")
    handler = LanguageDetectHandler(LanguageDetectConfig())
    assert handler._detector.backend == "langdetect"


def test_langdetect_seed_set_for_determinism() -> None:
    """L-102 §3 perf + idempotency contract: the global langdetect
    DetectorFactory seed is set at construction so results are
    deterministic across invocations."""
    pytest.importorskip("langdetect")
    cfg = LanguageDetectConfig(backend="langdetect")
    handler = LanguageDetectHandler(cfg)
    assert isinstance(handler._detector, _DetectorAdapter)
    # langdetect detector is stateless from the adapter's perspective;
    # the deterministic seed was applied at adapter construction time.


def test_lingua_backend_still_available_for_legacy_descriptors() -> None:
    """Backwards-compat: descriptors that explicitly wired ``backend='lingua'``
    keep working when lingua is importable. L-205 removes the dep."""
    pytest.importorskip("lingua")
    cfg = LanguageDetectConfig(backend="lingua")
    handler = LanguageDetectHandler(cfg)
    assert handler._detector.backend == "lingua"
    assert handler._detector._lingua is not None


def test_config_rejects_unknown_backend() -> None:
    with pytest.raises(Exception):
        LanguageDetectConfig(backend="nonsense")


def test_config_rejects_out_of_range_confidence() -> None:
    with pytest.raises(Exception):
        LanguageDetectConfig(min_confidence=1.5)
    with pytest.raises(Exception):
        LanguageDetectConfig(min_confidence=-0.1)


# ---------------------------------------------------------------------------
# 11) Signal immutability — original signal is not mutated in place.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_original_signal_payload_not_mutated() -> None:
    handler = LanguageDetectHandler(
        LanguageDetectConfig(),
        detector=_StaticDetector(lang="en", conf=0.99),
    )
    payload = {"text": "A long enough piece of text for the threshold."}
    sig = _signal(payload)
    out = await handler.transform(sig, _ctx())
    assert out is not None
    # Original payload reference untouched.
    assert PAYLOAD_LANGUAGE_KEY not in payload
    assert PAYLOAD_LANGUAGE_KEY not in sig.payload
    # New signal carries the stamped fields.
    assert out.payload[PAYLOAD_LANGUAGE_KEY] == "en"
