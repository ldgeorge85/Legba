# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Language-detect filter handler (L-150).

Implements the L-102 filter/enrichment kind contract (§3) for per-signal
language detection. Stream-resident — runs inline between source emission
and substrate write.

Backend choice (2026-05-22 reshape):

  * ``langdetect`` is the default. It's a tiny pure-Python port of Google's
    language-detection library (~1 MB on disk, no model files), based on
    character n-gram naive Bayes. Per-call latency is sub-millisecond once
    the in-process factory is seeded.
  * ``lingua`` is supported when importable — the legacy production
    default. It uses ~200 MB of pre-loaded n-gram tables for higher
    accuracy on short/code-switched text. Kept for backwards compat with
    descriptors that wired ``backend="lingua"``; L-205 retires the dep.

The choice (a) of staying in-process (rather than path (b) — adding a
``/detect_language`` endpoint to legba-models and routing through the
hosted service) is documented in
``plans/legba_post_bringup_review.md``: language detection is sub-
millisecond per call once warmed, network round-trip would be 10-50x
slower, and the dep footprint is small enough that drift-correction
doesn't justify the wire path.

Behavior unchanged from the pre-reshape variant: reads text from the
payload in priority order, stamps ``payload["language"]`` +
``payload["language_confidence"]``, applies confidence floor +
allow-list restrictions, idempotent under ``payload["language"]``-already-
set unless ``force_redetect=True``.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, ClassVar, Mapping

from pydantic import BaseModel, ConfigDict, Field

from ..sources._contract import Signal
from ._contract import FilterContext, FilterHealth


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Optional backends
# ---------------------------------------------------------------------------


_LINGUA_AVAILABLE = False
_LANGDETECT_AVAILABLE = False

try:                                                       # pragma: no cover
    from lingua import (  # type: ignore[import-not-found]
        Language as _LinguaLanguage,
        LanguageDetectorBuilder as _LinguaBuilder,
    )

    _LINGUA_AVAILABLE = True
except Exception:                                          # pragma: no cover
    _LinguaLanguage = None      # type: ignore[assignment]
    _LinguaBuilder = None       # type: ignore[assignment]

try:                                                       # pragma: no cover
    import langdetect as _langdetect  # type: ignore[import-not-found]

    _LANGDETECT_AVAILABLE = True
except Exception:                                          # pragma: no cover
    _langdetect = None          # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class LanguageDetectConfig(BaseModel):
    """Pydantic config schema for :class:`LanguageDetectHandler`.

    Fields:

    ``min_confidence``
        Minimum confidence (0.0-1.0) required to accept the detected
        language. Anything below → ``"und"`` (undetermined).

    ``min_text_length``
        Minimum text length (characters, post-strip) required to attempt
        detection. Below this → ``"und"``.

    ``restrict_to``
        Explicit ISO 639-1 allow-list.

    ``target_languages_only``
        If ``True``, restrict accepted output to the per-target
        :attr:`FilterContext.scope_languages` set.

    ``force_redetect``
        If ``True``, re-detect even when ``payload["language"]`` is
        already set.

    ``backend``
        Which detector backend to use:

        - ``"auto"`` (default): langdetect when importable (the
          post-reshape default), else lingua.
        - ``"langdetect"``: require langdetect; raise at construction if absent.
        - ``"lingua"``: require lingua; raise at construction if absent.
    """

    model_config = ConfigDict(extra="forbid")

    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    min_text_length: int = Field(default=20, ge=1, le=100_000)
    restrict_to: list[str] | None = None
    target_languages_only: bool = False
    force_redetect: bool = False
    backend: str = Field(default="auto", pattern=r"^(auto|lingua|langdetect)$")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


UND = "und"

_UNKNOWN = (UND, 0.0)

PAYLOAD_LANGUAGE_KEY = "language"
PAYLOAD_LANGUAGE_CONFIDENCE_KEY = "language_confidence"

_TEXT_FIELDS = ("text", "title", "summary", "raw_body")


# ---------------------------------------------------------------------------
# D30: ISO-639-1 normalization + short ALL-CAPS misdetect guard
# ---------------------------------------------------------------------------


def normalize_lang(code: Any) -> str:
    """Normalize a BCP-47 / region-tagged code to its ISO-639-1 base.

    ``en-US`` → ``en``; ``zh-Hans`` / ``zh_CN`` → ``zh``; ``EN`` → ``en``;
    ``pt-BR`` → ``pt``. The base subtag is lower-cased; region / script
    subtags (after a ``-`` or ``_``) are dropped. ``und`` and empties pass
    through as ``"und"``. This is the canonical surface every code path stamps
    through so the typed ``signals.language`` column never holds an ``en-US``.
    """
    if not isinstance(code, str):
        return UND
    base = code.strip().lower().replace("_", "-").split("-", 1)[0]
    if not base or base == "unknown":
        return UND
    return base


# Short, ALL-CAPS headlines (wire-service slugs, NGO press-release headers like
# Amnesty's "URGENT ACTION: ...") strip the case + diacritic signal langdetect's
# char-n-gram model relies on, so it confidently mislabels English as de / nl /
# af. Below this length AND when the alpha text is all-uppercase we abstain to
# ``und`` rather than trust a high-confidence-but-wrong guess.
_ALLCAPS_MIN_CHARS = 40
_ALPHA_RE = re.compile(r"[^\W\d_]", re.UNICODE)


def is_short_allcaps_headline(text: str, *, min_chars: int = _ALLCAPS_MIN_CHARS) -> bool:
    """True when ``text`` is a short, all-uppercase headline (D30 misdetect guard).

    Requires at least 2 cased letters so a numeric / symbol-only string isn't
    flagged; a string with ANY lowercase letter is not all-caps and returns
    False (a normal sentence-cased headline is left to the detector).
    """
    if not text:
        return False
    if len(text.strip()) >= min_chars:
        return False
    letters = [ch for ch in text if _ALPHA_RE.match(ch)]
    if len(letters) < 2:
        return False
    return all(ch.isupper() for ch in letters)


# ---------------------------------------------------------------------------
# Detector adapter
# ---------------------------------------------------------------------------


class _DetectorAdapter:
    """Minimal uniform surface across langdetect / lingua backends.

    ``detect(text)`` returns ``(iso639_1_code, confidence_float)``.
    """

    def __init__(self, backend: str) -> None:
        self._backend = backend
        self._lingua = None
        if backend == "lingua":
            if not _LINGUA_AVAILABLE:                       # pragma: no cover
                raise RuntimeError(
                    "lingua-language-detector backend requested but "
                    "'lingua' is not importable"
                )
            self._lingua = (
                _LinguaBuilder.from_all_languages()                    # type: ignore[union-attr]
                .with_preloaded_language_models()
                .build()
            )
        elif backend == "langdetect":
            if not _LANGDETECT_AVAILABLE:
                raise RuntimeError(
                    "langdetect backend requested but 'langdetect' is "
                    "not importable"
                )
            try:
                _langdetect.DetectorFactory.seed = 0        # type: ignore[union-attr]
            except Exception:                               # pragma: no cover
                pass
        else:                                               # pragma: no cover
            raise ValueError(f"unsupported backend: {backend!r}")

    @property
    def backend(self) -> str:
        return self._backend

    def detect(self, text: str) -> tuple[str, float]:
        """Detect language + confidence. Returns ``("und", 0.0)`` on no-match."""
        if not text:
            return _UNKNOWN
        if self._backend == "lingua":
            return self._detect_lingua(text)
        return self._detect_langdetect(text)

    # -- backend implementations ---------------------------------------------

    def _detect_lingua(self, text: str) -> tuple[str, float]:
        det = self._lingua
        assert det is not None
        try:
            values = det.compute_language_confidence_values(text)
        except Exception as exc:                            # pragma: no cover
            logger.warning("lingua.compute_failed: %s", exc)
            return _UNKNOWN
        if not values:
            return _UNKNOWN
        top = values[0]
        try:
            lang = top.language
            conf = float(top.value)
        except Exception:                                   # pragma: no cover
            return _UNKNOWN
        iso = _lingua_iso(lang)
        if iso is None:
            return _UNKNOWN
        return normalize_lang(iso), conf

    def _detect_langdetect(self, text: str) -> tuple[str, float]:
        try:
            results = _langdetect.detect_langs(text)        # type: ignore[union-attr]
        except Exception:
            # langdetect raises langdetect.lang_detect_exception.LangDetectException
            # for short / unrecognized text; treat as undetermined.
            return _UNKNOWN
        if not results:
            return _UNKNOWN
        top = results[0]
        try:
            iso = str(top.lang)
            conf = float(top.prob)
        except Exception:                                   # pragma: no cover
            return _UNKNOWN
        if not iso or iso == "unknown":                     # pragma: no cover
            return _UNKNOWN
        # langdetect returns codes like 'zh-cn'; normalise to the ISO-639-1
        # base (drops region/script subtags) for consistency with the rest of
        # the pipeline. D30: en-US/zh-cn → en/zh.
        iso = normalize_lang(iso)
        if iso == UND:                                      # pragma: no cover
            return _UNKNOWN
        return iso, conf


def _lingua_iso(lang: Any) -> str | None:
    """Extract an ISO 639-1 two-letter code from a lingua Language enum."""
    try:
        code = lang.iso_code_639_1.name.lower()
    except Exception:                                       # pragma: no cover
        return None
    if not code or len(code) != 2:                          # pragma: no cover
        return None
    return code


def _resolve_backend(requested: str) -> str:
    """Resolve ``"auto"`` to a concrete backend; pass through otherwise.

    Post-reshape default order: ``langdetect`` first (tiny, no model
    files), then ``lingua`` (legacy production default, heavier).
    """
    if requested == "auto":
        if _LANGDETECT_AVAILABLE:
            return "langdetect"
        if _LINGUA_AVAILABLE:                               # pragma: no cover
            return "lingua"
        raise RuntimeError(                                 # pragma: no cover
            "language_detect handler requires either "
            "'langdetect' or 'lingua-language-detector' to be importable"
        )
    return requested


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class LanguageDetectHandler:
    """Stream-resident filter handler that stamps each signal with its
    detected ISO 639-1 language code and a confidence score.

    L-102 conformance (§3):

      * ``kind = "language_detect"``, ``family = "filter"``.
      * Idempotent on ``(content_hash, handler_version)``.
      * Declares ``output_contract``.
      * Lifecycle hooks default to no-op; the detector model is built
        eagerly in :meth:`__init__`.
      * Never returns ``None``.
    """

    # --- L-102 §1 class-vars ------------------------------------------------
    kind: ClassVar[str] = "language_detect"
    family: ClassVar[str] = "filter"
    schema_version: ClassVar[str] = "legba/filter.language_detect/1-0-0"
    config_schema: ClassVar[type[BaseModel]] = LanguageDetectConfig
    handler_version: ClassVar[str] = "0.2.0"  # langdetect default

    output_contract: ClassVar[Mapping[str, type]] = {
        f"payload.{PAYLOAD_LANGUAGE_KEY}": str,
        f"payload.{PAYLOAD_LANGUAGE_CONFIDENCE_KEY}": float,
    }

    idempotent: ClassVar[bool] = True

    def __init__(
        self,
        config: LanguageDetectConfig,
        *,
        detector: _DetectorAdapter | None = None,
    ) -> None:
        self._config = config
        self._detector = detector or _DetectorAdapter(
            backend=_resolve_backend(config.backend),
        )
        self._signals_in = 0
        self._signals_out = 0
        self._signals_dropped = 0
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------- transform

    async def transform(
        self,
        signal: Signal,
        ctx: FilterContext,
    ) -> Signal | None:
        """Annotate ``signal`` with its detected language. Never drops."""
        self._signals_in += 1

        # --- 1. Idempotency check ------------------------------------------
        existing = signal.payload.get(PAYLOAD_LANGUAGE_KEY)
        if existing and not self._config.force_redetect:
            # D30: an upstream source may have stamped a region-tagged hint
            # (e.g. `en-US` from a source's `language_hint`). Don't re-detect,
            # but DO normalize the stored value to its ISO-639-1 base so the
            # typed column never holds `en-US`. Re-stamp only when normalization
            # actually changes the value (keeps the idempotent fast path).
            normalized = normalize_lang(existing)
            if normalized != existing:
                conf = signal.payload.get(PAYLOAD_LANGUAGE_CONFIDENCE_KEY)
                return self._stamp(
                    signal,
                    lang=normalized,
                    conf=float(conf) if isinstance(conf, (int, float)) else 0.0,
                    reason="normalized_existing",
                    ctx=ctx,
                )
            self._signals_out += 1
            self._last_success_at = datetime.now(tz=timezone.utc)
            return signal

        # --- 2. Pick text to inspect ---------------------------------------
        text = _extract_text(signal.payload)

        # --- 3. Short-text fallback ----------------------------------------
        if len(text) < self._config.min_text_length:
            return self._stamp(
                signal,
                lang=UND,
                conf=0.0,
                reason="short_text",
                ctx=ctx,
            )

        # --- 3b. Short ALL-CAPS headline guard (D30) -----------------------
        # langdetect confidently mis-labels short upper-cased headlines (NGO
        # press slugs, wire-service all-caps) — the Amnesty English→de class.
        # Abstain to `und` rather than emit a high-confidence wrong language.
        if is_short_allcaps_headline(text):
            return self._stamp(
                signal,
                lang=UND,
                conf=0.0,
                reason="short_allcaps",
                ctx=ctx,
            )

        # --- 4. Run detector -----------------------------------------------
        try:
            lang, conf = self._detector.detect(text)
        except Exception as exc:                            # pragma: no cover
            self._last_error = f"detect_failed: {exc!s}"
            ctx.logger.warning(
                "language_detect.detect_failed signal_id=%s err=%s",
                signal.signal_id, exc,
            )
            return self._stamp(
                signal,
                lang=UND,
                conf=0.0,
                reason="detector_error",
                ctx=ctx,
            )

        # --- 5. Confidence floor -------------------------------------------
        if conf < self._config.min_confidence or lang == UND:
            return self._stamp(
                signal,
                lang=UND,
                conf=conf,
                reason="low_confidence",
                ctx=ctx,
            )

        # --- 6. Restrict_to / target-language-only allow-listing -----------
        if not self._language_allowed(lang, ctx=ctx):
            return self._stamp(
                signal,
                lang=UND,
                conf=conf,
                reason="not_allowed",
                ctx=ctx,
                detected=lang,
            )

        # --- 7. Stamp ------------------------------------------------------
        return self._stamp(
            signal,
            lang=lang,
            conf=conf,
            reason="ok",
            ctx=ctx,
        )

    # ----------------------------------------------------------- health_check

    async def health_check(self, ctx: FilterContext) -> FilterHealth:
        return FilterHealth(
            state="healthy",
            last_success_at=self._last_success_at,
            last_error=self._last_error,
            signals_in_24h=self._signals_in,
            signals_out_24h=self._signals_out,
            signals_dropped_24h=self._signals_dropped,
            detail={
                "backend": self._detector.backend,
                "min_confidence": self._config.min_confidence,
                "min_text_length": self._config.min_text_length,
                "restrict_to": self._config.restrict_to,
                "target_languages_only": self._config.target_languages_only,
            },
        )

    # ------------------------------------------------------- lifecycle hooks

    async def on_configure(self, ctx: FilterContext) -> None:
        return None

    async def on_activate(self, ctx: FilterContext) -> None:
        return None

    async def on_pause(self, ctx: FilterContext) -> None:
        return None

    async def on_resume(self, ctx: FilterContext) -> None:
        return None

    async def on_retire(self, ctx: FilterContext) -> None:
        return None

    # ------------------------------------------------------------- internals

    def _stamp(
        self,
        signal: Signal,
        *,
        lang: str,
        conf: float,
        reason: str,
        ctx: FilterContext,
        detected: str | None = None,
    ) -> Signal:
        """Return a copy of ``signal`` with language fields set on payload
        AND on the typed ``signal.language_hint`` field.  Before this fix
        the filter only wrote payload.language, leaving language_hint
        unset → _write_signal's `language_hint or "en"` fallback wrote
        the typed `signals.language` column as 'en' for every signal
        regardless of the detected language.  Now both surfaces carry
        the result; downstream readers prefer the typed column.
        """
        # D30: normalize at the single write surface so neither payload nor the
        # typed `language_hint`/`signals.language` column can ever hold a
        # region-tagged code (en-US) — UND passes through unchanged.
        lang = normalize_lang(lang) if lang != UND else UND
        new_payload = dict(signal.payload)
        new_payload[PAYLOAD_LANGUAGE_KEY] = lang
        new_payload[PAYLOAD_LANGUAGE_CONFIDENCE_KEY] = float(conf)

        out = signal.model_copy(
            update={"payload": new_payload, "language_hint": lang},
        )
        self._signals_out += 1
        self._last_success_at = datetime.now(tz=timezone.utc)

        if reason != "ok":
            ctx.logger.debug(
                "language_detect.stamp reason=%s lang=%s conf=%.3f "
                "detected=%s signal_id=%s",
                reason, lang, conf, detected or lang, signal.signal_id,
            )
        return out

    def _language_allowed(self, lang: str, *, ctx: FilterContext) -> bool:
        if self._config.restrict_to is not None:
            allowed = {l.lower() for l in self._config.restrict_to}
            if lang.lower() not in allowed:
                return False
        if self._config.target_languages_only:
            scope = {l.lower() for l in ctx.scope_languages}
            if lang.lower() not in scope:
                return False
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_text(payload: Mapping[str, Any]) -> str:
    """Pull the best available text field for language detection."""
    for key in _TEXT_FIELDS:
        val = payload.get(key)
        if val is None:
            continue
        if isinstance(val, bytes):
            try:
                s = val.decode("utf-8", "replace")
            except Exception:                               # pragma: no cover
                continue
        elif isinstance(val, str):
            s = val
        else:
            s = str(val)
        s = s.strip()
        if s:
            return s
    return ""


__all__ = [
    "LanguageDetectConfig",
    "LanguageDetectHandler",
    "PAYLOAD_LANGUAGE_KEY",
    "PAYLOAD_LANGUAGE_CONFIDENCE_KEY",
    "UND",
    "normalize_lang",
    "is_short_allcaps_headline",
]
