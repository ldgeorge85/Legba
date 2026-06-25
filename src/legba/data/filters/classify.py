# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Classification filter handler — HTTP-client variant (L-155, post-reshape).

Implements the L-102 §3 filter/enrichment-kind contract for taxonomy-driven
event-type / severity / sentiment classification of in-flight signals.
Calls the hosted Legba-models ``POST /classify`` endpoint (DeBERTa-v3
zero-shot) rather than the pre-reshape in-process embedding + cosine
backend.

Architectural-drift correction (2026-05-22): the in-process backend
(``zero_shot_embedding`` via BGE-M3 cosine over label anchors,
``fine_tuned`` via a loaded classifier model) is replaced with HTTP
calls to the hosted endpoint. The hosted endpoint accepts custom labels
per request, so the taxonomy-schema concept is preserved end-to-end.

Backends (all behind the same handler):

  * ``zero_shot_hosted`` (default) — POST /classify with the configured
    labels (or omit ``labels`` to use the server defaults). Returns the
    top category + confidence + per-label scores.
  * ``rules_then_hosted`` — apply operator-supplied regex rules first;
    fall back to the hosted endpoint for signals that don't match any rule.

Output annotation lives on ``Signal.payload['classification']`` (per L-102
KC-2). The annotation is a dict::

    {
        "event_type": <str | list[str]>,    # list when multi_label=True
        "severity":   <str | None>,
        "sentiment":  <str | None>,
        "confidence": <float>,
        "backend_used": <str>,
        "schema": <taxonomy_schema name>,
        "label_scores": {label: score, ...},
    }

Sub-confidence labels collapse to ``"other"`` so downstream graph code can
treat unknowns uniformly. Graceful degradation: on service failure the
signal flows through with an ``"other"`` annotation and the handler's
health flips to ``degraded``.

The legacy ``fine_tuned`` backend is preserved as a config option but
documented as deprecated — production routes through ``zero_shot_hosted``.
The pre-reshape ``EmbeddingPort`` Protocol (sentiment seeding via local
embedding) retired in L-205 along with the in-process embedding path; the
hosted ``/classify`` endpoint subsumes the sentiment surface.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import datetime, timezone
from typing import (
    Any,
    Awaitable,
    ClassVar,
    Literal,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..sources._contract import Signal
from ..stack.nlp_service import (
    NlpServiceAuthError,
    NlpServiceClient,
    NlpServiceUnavailable,
)
from ._contract import FilterContext, FilterHealth


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLASSIFY_KIND: str = "classify"
CLASSIFY_FAMILY: str = "filter"
CLASSIFY_SCHEMA_VERSION: str = "legba/filter.classify/1-0-0"
CLASSIFY_HANDLER_VERSION: str = "0.2.0"  # HTTP-client variant

#: Reserved label name for sub-confidence / unmatched signals.
OTHER_LABEL: str = "other"

#: Sentiment label set; fixed across all taxonomies.
SENTIMENT_LABELS: tuple[str, ...] = ("positive", "negative", "neutral")

#: Default sentiment "anchor" descriptions. Kept for backwards compat with
#: sentiment-via-embedding callers. The hosted variant classifies against
#: the bare label names.
DEFAULT_SENTIMENT_SEEDS: dict[str, str] = {
    "positive": (
        "Positive, favourable, optimistic, good news, success, gains, "
        "improvement, agreement, progress."
    ),
    "negative": (
        "Negative, hostile, pessimistic, bad news, failure, losses, "
        "decline, conflict, harm, setback."
    ),
    "neutral": (
        "Neutral, factual, balanced, unremarkable, routine reporting, "
        "no clear emotional direction."
    ),
}


# ---------------------------------------------------------------------------
# Embedding port (legacy — kept for backwards compatibility)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class Label(BaseModel):
    """One taxonomy label. Operator-supplied.

    ``description`` is a short natural-language description of the class.
    ``examples`` are a small list of exemplar text strings. Both are
    optional; the hosted /classify endpoint accepts bare label names but
    consumers may want to keep descriptions for documentation.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(default="", max_length=4096)
    examples: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_not_reserved(cls, v: str) -> str:
        if v.strip().lower() == OTHER_LABEL:
            raise ValueError(
                f"label name {v!r} collides with reserved {OTHER_LABEL!r}; "
                f"pick a different label name"
            )
        if v != v.strip():
            raise ValueError("label name must not have leading/trailing whitespace")
        return v


class Rule(BaseModel):
    """One regex rule for the ``rules_then_hosted`` backend.

    A rule matches if ``re.search(pattern, text, flags)`` is truthy.
    First matching rule wins; ``label`` is used as the event_type with
    ``confidence=rule.confidence`` and ``backend_used="rule"``.
    """

    model_config = ConfigDict(extra="forbid")

    pattern: str
    label: str
    flags: int = int(re.IGNORECASE | re.MULTILINE)
    severity: str | None = None
    confidence: float = 1.0

    @field_validator("pattern")
    @classmethod
    def _compileable(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as exc:                                 # pragma: no cover
            raise ValueError(f"invalid regex pattern: {exc}") from exc
        return v


class ClassifyConfig(BaseModel):
    """Pydantic config schema for :class:`ClassifyHandler` (HTTP variant)."""

    model_config = ConfigDict(extra="forbid")

    # ``zero_shot_embedding`` retained as a legacy alias mapped to
    # ``zero_shot_hosted`` at activation time so existing descriptors don't
    # break. ``fine_tuned`` is kept but deprecated.
    backend: Literal[
        "zero_shot_hosted",
        "zero_shot_embedding",  # legacy alias
        "rules_then_hosted",
        "rules_then_zero_shot",  # legacy alias
        "fine_tuned",
    ] = "zero_shot_hosted"

    taxonomy_schema: str = Field(..., min_length=1, max_length=128)

    labels: list[Label] = Field(default_factory=list)
    severity_taxonomy: list[Label] | None = None

    sentiment: bool = False
    min_confidence: float = Field(default=0.4, gt=0.0, le=1.0)
    multi_label: bool = False

    max_text_chars: int = Field(default=4096, ge=64, le=32_768)

    #: Fine-tuned classifier model path. Deprecated; preserved for legacy
    #: descriptors.
    model_path: str | None = None

    rules: list[Rule] = Field(default_factory=list)

    #: Sentiment label set used at the hosted endpoint when ``sentiment=True``.
    #: Defaults to the canonical positive/negative/neutral triplet.
    sentiment_labels: list[str] = Field(
        default_factory=lambda: list(SENTIMENT_LABELS),
        description="Label names sent to /classify for sentiment inference.",
    )

    sentiment_seeds: dict[str, str] = Field(default_factory=dict)

    #: When True, the hosted /classify call omits the ``labels`` field and
    #: relies on the server's built-in 9-category default set. Useful for
    #: descriptors that don't define a per-target taxonomy.
    use_server_defaults: bool = False

    @model_validator(mode="after")
    def _check_invariants(self) -> "ClassifyConfig":
        if self.backend in ("zero_shot_hosted", "zero_shot_embedding",
                             "rules_then_hosted", "rules_then_zero_shot"):
            if not self.use_server_defaults and not self.labels:
                raise ValueError(
                    "labels must be non-empty for backend "
                    f"{self.backend!r} (or set use_server_defaults=True)"
                )
        if self.backend == "fine_tuned" and not self.model_path:
            raise ValueError(
                "model_path is required when backend == 'fine_tuned'"
            )
        if self.backend in ("rules_then_hosted", "rules_then_zero_shot") and not self.rules:
            raise ValueError(
                f"rules must be non-empty for backend {self.backend!r}"
            )
        # Label name uniqueness.
        seen: set[str] = set()
        for label in self.labels:
            key = label.name.lower()
            if key in seen:
                raise ValueError(f"duplicate label name in taxonomy: {label.name!r}")
            seen.add(key)
        if self.severity_taxonomy is not None:
            sev_seen: set[str] = set()
            for label in self.severity_taxonomy:
                key = label.name.lower()
                if key in sev_seen:
                    raise ValueError(
                        f"duplicate label name in severity_taxonomy: {label.name!r}"
                    )
                sev_seen.add(key)
        return self


# ---------------------------------------------------------------------------
# Predicted classification (internal — collapsed into a dict on the payload)
# ---------------------------------------------------------------------------


class Classification(BaseModel):
    """Internal result envelope. Serialised to a plain dict before being
    attached to the signal payload."""

    model_config = ConfigDict(extra="forbid")

    event_type: str | list[str]
    severity: str | None = None
    sentiment: str | None = None
    confidence: float
    backend_used: Literal["zero_shot", "fine_tuned", "rule"]
    taxonomy_schema: str
    label_scores: dict[str, float] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def _effective_backend(declared: str) -> str:
    """Map legacy aliases to the HTTP-variant backend names."""
    if declared in ("zero_shot_embedding",):
        return "zero_shot_hosted"
    if declared in ("rules_then_zero_shot",):
        return "rules_then_hosted"
    return declared


class ClassifyHandler:
    """Taxonomy-driven classification filter — HTTP variant (L-155).

    Wire-up:

      * Constructor accepts the parsed :class:`ClassifyConfig` and an
        optional :class:`NlpServiceClient` (typically injected by the
        runtime when the descriptor's ``Property.StackRef`` resolves).
      * ``on_configure`` accepts a ``nlp_client=`` keyword that runtime
        wiring (or tests) can use to inject the client after construction.
      * ``transform`` POSTs to the hosted /classify endpoint per signal,
        with the configured labels.
    """

    # --- L-102 §1 envelope class-vars. --------------------------------
    kind: ClassVar[str] = CLASSIFY_KIND
    family: ClassVar[str] = CLASSIFY_FAMILY
    schema_version: ClassVar[str] = CLASSIFY_SCHEMA_VERSION
    handler_version: ClassVar[str] = CLASSIFY_HANDLER_VERSION
    config_schema: ClassVar[type[BaseModel]] = ClassifyConfig

    #: Names that this handler adds to ``Signal.payload``.
    output_contract: ClassVar[Mapping[str, type]] = {
        "payload.classification": dict,
        "payload.classification.event_type": object,  # str | list[str]
        "payload.classification.confidence": float,
        "payload.classification.backend_used": str,
        "payload.classification.schema": str,
    }

    def __init__(
        self,
        config: ClassifyConfig,
        *,
        nlp_client: NlpServiceClient | None = None,
        fine_tuned_loader: "FineTunedLoader | None" = None,
    ) -> None:
        self._config: ClassifyConfig = config
        self._client: NlpServiceClient | None = nlp_client
        self._fine_tuned_loader: "FineTunedLoader | None" = fine_tuned_loader

        # Fine-tuned model handle (legacy path).
        self._classifier: "Classifier | None" = None

        # Compiled rules.
        self._compiled_rules: list[tuple[re.Pattern[str], Rule]] = []

        # Health counters.
        self._signals_in_24h: list[datetime] = []
        self._signals_out_24h: list[datetime] = []
        self._signals_dropped_24h: list[datetime] = []
        self._signals_failed_24h: list[datetime] = []
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None

        self._activated: bool = False
        self._service_healthy: bool | None = None

        # Effective backend after alias resolution.
        self._effective_backend = _effective_backend(self._config.backend)

    # ------------------------------------------------------------------
    # Public accessors (used by tests)
    # ------------------------------------------------------------------

    @property
    def config(self) -> ClassifyConfig:
        return self._config

    @property
    def is_activated(self) -> bool:
        return self._activated

    @property
    def effective_backend(self) -> str:
        return self._effective_backend

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_configure(
        self,
        *,
        nlp_client: NlpServiceClient | None = None,
        fine_tuned_loader: "FineTunedLoader | None" = None,
    ) -> None:
        """Bind / re-bind the upstream substrate dependencies.

        Runtime wiring resolves the descriptor's
        ``Property.StackRef("nlp.local.legba_models")`` and passes the
        live client here. Tests do the same with a mock-transport client.
        """
        if nlp_client is not None:
            self._client = nlp_client
        if fine_tuned_loader is not None:
            self._fine_tuned_loader = fine_tuned_loader
        # Compile any regex rules eagerly so bad regexes fail at configure.
        self._compiled_rules = [
            (re.compile(rule.pattern, rule.flags), rule)
            for rule in self._config.rules
        ]

    async def on_activate(self, *args: Any, **kwargs: Any) -> None:
        """Materialise the chosen backend.

          * hosted variants — probe ``/health`` to surface auth errors early.
          * fine-tuned — load the classifier model.
        """
        # Defensive: in case on_configure was skipped.
        if self._config.rules and not self._compiled_rules:
            self._compiled_rules = [
                (re.compile(rule.pattern, rule.flags), rule)
                for rule in self._config.rules
            ]
        backend = self._effective_backend
        if backend in ("zero_shot_hosted", "rules_then_hosted"):
            if self._client is not None:
                try:
                    await self._client.health()
                    self._service_healthy = True
                except NlpServiceAuthError as exc:
                    self._last_error = f"auth: {exc!s}"
                    self._service_healthy = False
                except NlpServiceUnavailable as exc:
                    self._last_error = f"unavailable: {exc!s}"
                    self._service_healthy = False
        if backend == "fine_tuned":
            self._classifier = await self._load_classifier()
        self._activated = True
        self._last_success_at = _now()

    async def on_pause(self, *args: Any, **kwargs: Any) -> None:
        """Drop the activation flag + fine-tuned model handle. Idempotent."""
        self._classifier = None
        self._activated = False

    async def on_resume(self, *args: Any, **kwargs: Any) -> None:
        await self.on_activate(*args, **kwargs)

    async def on_retire(self, *args: Any, **kwargs: Any) -> None:
        await self.on_pause(*args, **kwargs)
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:                                   # pragma: no cover
                pass

    # ------------------------------------------------------------------
    # Transform
    # ------------------------------------------------------------------

    async def transform(
        self,
        signal: Signal,
        ctx: FilterContext,
    ) -> Signal | None:
        """Apply the configured backend to ``signal`` and annotate.

        Returns the (possibly mutated) signal. The handler never *drops*
        signals — sub-confidence outputs are tagged ``"other"`` and
        service-failure outputs are tagged ``"other"`` with the original
        confidence preserved as ``0.0``.
        """
        self._record_in()
        try:
            text = _signal_text(signal, self._config.max_text_chars)
            if not text:
                self._annotate(
                    signal,
                    Classification(
                        event_type=OTHER_LABEL,
                        confidence=0.0,
                        backend_used="zero_shot",
                        taxonomy_schema=self._config.taxonomy_schema,
                    ),
                )
                self._record_out()
                return signal

            cls = await self._classify(text)
            self._annotate(signal, cls)
            self._last_success_at = _now()
            self._record_out()
            return signal
        except Exception as exc:                                # noqa: BLE001
            self._last_error = f"transform failed: {exc}"
            logger.exception("classify.transform failed signal=%s", signal.signal_id)
            self._record_failed()
            # On unrecoverable error: stamp "other" + keep the signal in
            # the stream.
            self._annotate(
                signal,
                Classification(
                    event_type=OTHER_LABEL,
                    confidence=0.0,
                    backend_used="zero_shot",
                    taxonomy_schema=self._config.taxonomy_schema,
                ),
            )
            return signal

    async def _classify(self, text: str) -> Classification:
        backend = self._effective_backend
        sentiment: str | None = None
        if self._config.sentiment:
            sentiment = await self._infer_sentiment(text)

        if backend == "rules_then_hosted":
            matched = self._match_rules(text)
            if matched is not None:
                rule_label, rule = matched
                sev = rule.severity or await self._infer_severity(text)
                return Classification(
                    event_type=rule_label,
                    severity=sev,
                    sentiment=sentiment,
                    confidence=float(rule.confidence),
                    backend_used="rule",
                    taxonomy_schema=self._config.taxonomy_schema,
                )
            # Fall-through to hosted zero-shot.
            return await self._classify_zero_shot_hosted(text, sentiment=sentiment)

        if backend == "zero_shot_hosted":
            return await self._classify_zero_shot_hosted(text, sentiment=sentiment)

        if backend == "fine_tuned":
            return await self._classify_fine_tuned(text, sentiment=sentiment)

        raise RuntimeError(f"unsupported backend: {backend!r}")

    # ------------------------------------------------------------------
    # Hosted /classify
    # ------------------------------------------------------------------

    async def _classify_zero_shot_hosted(
        self,
        text: str,
        *,
        sentiment: str | None,
    ) -> Classification:
        if self._client is None:
            self._last_error = "no nlp client bound"
            self._service_healthy = False
            return Classification(
                event_type=OTHER_LABEL,
                severity=None,
                sentiment=sentiment,
                confidence=0.0,
                backend_used="zero_shot",
                taxonomy_schema=self._config.taxonomy_schema,
            )

        labels: list[str] | None = None
        if not self._config.use_server_defaults:
            labels = [label.name for label in self._config.labels]
        try:
            data = await self._client.classify(text, labels=labels)
        except NlpServiceAuthError as exc:
            self._last_error = f"auth: {exc!s}"
            self._service_healthy = False
            return Classification(
                event_type=OTHER_LABEL,
                severity=None,
                sentiment=sentiment,
                confidence=0.0,
                backend_used="zero_shot",
                taxonomy_schema=self._config.taxonomy_schema,
            )
        except NlpServiceUnavailable as exc:
            self._last_error = f"unavailable: {exc!s}"
            self._service_healthy = False
            return Classification(
                event_type=OTHER_LABEL,
                severity=None,
                sentiment=sentiment,
                confidence=0.0,
                backend_used="zero_shot",
                taxonomy_schema=self._config.taxonomy_schema,
            )

        self._service_healthy = True
        self._last_error = None
        scores: dict[str, float] = {}
        raw_scores = data.get("scores", {}) if isinstance(data, dict) else {}
        if isinstance(raw_scores, dict):
            for k, v in raw_scores.items():
                try:
                    scores[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
        top_name = data.get("category") if isinstance(data, dict) else None
        top_score = data.get("confidence", 0.0) if isinstance(data, dict) else 0.0
        try:
            top_score = float(top_score)
        except (TypeError, ValueError):
            top_score = 0.0

        # Severity — separate /classify call against severity_taxonomy when
        # configured. We do this after the main classify so a service that's
        # down for severity-only doesn't kill the event_type assignment.
        severity = await self._infer_severity(text)

        threshold = self._config.min_confidence
        if self._config.multi_label:
            chosen = [name for name, score in scores.items() if score >= threshold]
            if chosen:
                top = max(scores[name] for name in chosen)
                return Classification(
                    event_type=sorted(chosen),
                    severity=severity,
                    sentiment=sentiment,
                    confidence=float(top),
                    backend_used="zero_shot",
                    taxonomy_schema=self._config.taxonomy_schema,
                    label_scores=scores,
                )
            # Fall through to single-label "other" tagging.

        if not top_name or top_score < threshold:
            return Classification(
                event_type=OTHER_LABEL,
                severity=severity,
                sentiment=sentiment,
                confidence=float(top_score),
                backend_used="zero_shot",
                taxonomy_schema=self._config.taxonomy_schema,
                label_scores=scores,
            )

        return Classification(
            event_type=[str(top_name)] if self._config.multi_label else str(top_name),
            severity=severity,
            sentiment=sentiment,
            confidence=float(top_score),
            backend_used="zero_shot",
            taxonomy_schema=self._config.taxonomy_schema,
            label_scores=scores,
        )

    async def _infer_severity(self, text: str) -> str | None:
        """Run a second /classify call against the severity taxonomy.

        Returns ``None`` when severity_taxonomy isn't configured, the client
        isn't bound, the service is unavailable, or the top score is below
        the threshold.
        """
        if not self._config.severity_taxonomy or self._client is None:
            return None
        labels = [label.name for label in self._config.severity_taxonomy]
        try:
            data = await self._client.classify(text, labels=labels)
        except (NlpServiceAuthError, NlpServiceUnavailable):
            return None
        top_name = data.get("category") if isinstance(data, dict) else None
        top_score = data.get("confidence", 0.0) if isinstance(data, dict) else 0.0
        try:
            top_score = float(top_score)
        except (TypeError, ValueError):
            return None
        if not top_name or top_score < self._config.min_confidence:
            return None
        return str(top_name)

    async def _infer_sentiment(self, text: str) -> str | None:
        """Run a /classify call against the sentiment label set."""
        if self._client is None:
            return None
        labels = list(self._config.sentiment_labels) or list(SENTIMENT_LABELS)
        try:
            data = await self._client.classify(text, labels=labels)
        except (NlpServiceAuthError, NlpServiceUnavailable):
            return None
        top_name = data.get("category") if isinstance(data, dict) else None
        if not top_name:
            return None
        # Sentiment uses a softer threshold than event-type — pick the
        # top label even when scores are all close.
        return str(top_name)

    # ------------------------------------------------------------------
    # Fine-tuned (legacy)
    # ------------------------------------------------------------------

    async def _classify_fine_tuned(
        self,
        text: str,
        *,
        sentiment: str | None,
    ) -> Classification:
        if self._classifier is None:
            self._classifier = await self._load_classifier()
        predict = getattr(self._classifier, "predict_batch", None)
        if predict is not None:
            preds = await _maybe_await(predict([text]))
            label, score = preds[0]
        else:
            single = getattr(self._classifier, "predict", None)
            if single is None:
                raise RuntimeError(
                    "fine-tuned classifier must expose predict() or "
                    "predict_batch()"
                )
            label, score = await _maybe_await(single(text))
        threshold = self._config.min_confidence
        if score < threshold:
            return Classification(
                event_type=OTHER_LABEL,
                sentiment=sentiment,
                confidence=float(score),
                backend_used="fine_tuned",
                taxonomy_schema=self._config.taxonomy_schema,
                label_scores={label: float(score)},
            )
        return Classification(
            event_type=[label] if self._config.multi_label else label,
            sentiment=sentiment,
            confidence=float(score),
            backend_used="fine_tuned",
            taxonomy_schema=self._config.taxonomy_schema,
            label_scores={label: float(score)},
        )

    async def _load_classifier(self) -> "Classifier":
        if self._fine_tuned_loader is not None:
            loaded = await _maybe_await(
                self._fine_tuned_loader(self._config.model_path or "")
            )
            return loaded
        if not self._config.model_path:
            raise RuntimeError(
                "fine_tuned backend requires model_path or fine_tuned_loader"
            )
        import importlib

        spec = self._config.model_path
        if ":" in spec:
            module_path, attr = spec.rsplit(":", 1)
            module = importlib.import_module(module_path)
            loader = getattr(module, attr)
            return await _maybe_await(loader(spec))
        module = importlib.import_module(spec)
        loader = getattr(module, "load")
        return await _maybe_await(loader(spec))

    # ------------------------------------------------------------------
    # Rules
    # ------------------------------------------------------------------

    def _match_rules(self, text: str) -> tuple[str, Rule] | None:
        for pattern, rule in self._compiled_rules:
            if pattern.search(text):
                return rule.label, rule
        return None

    # ------------------------------------------------------------------
    # Annotation
    # ------------------------------------------------------------------

    @staticmethod
    def _annotate(signal: Signal, cls: Classification) -> None:
        """Write the classification onto the signal payload in-place."""
        signal.payload["classification"] = {
            "event_type": cls.event_type,
            "severity": cls.severity,
            "sentiment": cls.sentiment,
            "confidence": cls.confidence,
            "backend_used": cls.backend_used,
            "schema": cls.taxonomy_schema,
            "label_scores": cls.label_scores,
        }

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health_check(self, ctx: FilterContext | None = None) -> FilterHealth:
        state = "healthy"
        if not self._activated:
            state = "degraded"
        if self._last_error:
            state = "degraded"
        if self._service_healthy is False:
            state = "degraded"
        return FilterHealth(
            state=state,
            last_success_at=self._last_success_at,
            last_error=self._last_error,
            signals_in_24h=self._count_24h(self._signals_in_24h),
            signals_out_24h=self._count_24h(self._signals_out_24h),
            signals_dropped_24h=self._count_24h(self._signals_dropped_24h),
            detail={
                "backend": self._effective_backend,
                "declared_backend": self._config.backend,
                "schema": self._config.taxonomy_schema,
                "label_count": len(self._config.labels),
                "severity_label_count": len(self._config.severity_taxonomy or []),
                "multi_label": self._config.multi_label,
                "sentiment": self._config.sentiment,
                "min_confidence": self._config.min_confidence,
                "service_bound": self._client is not None,
                "service_healthy": self._service_healthy,
                "signals_failed_24h": self._count_24h(self._signals_failed_24h),
            },
        )

    # ------------------------------------------------------------------
    # Internals — 24h counters
    # ------------------------------------------------------------------

    def _record_in(self) -> None:
        self._signals_in_24h.append(_now())
        self._prune(self._signals_in_24h)

    def _record_out(self) -> None:
        self._signals_out_24h.append(_now())
        self._prune(self._signals_out_24h)

    def _record_dropped(self) -> None:
        self._signals_dropped_24h.append(_now())
        self._prune(self._signals_dropped_24h)

    def _record_failed(self) -> None:
        self._signals_failed_24h.append(_now())
        self._prune(self._signals_failed_24h)

    @staticmethod
    def _prune(bucket: list[datetime]) -> None:
        cutoff = _now().timestamp() - 86_400.0
        while bucket and bucket[0].timestamp() < cutoff:
            bucket.pop(0)

    @staticmethod
    def _count_24h(bucket: list[datetime]) -> int:
        cutoff = _now().timestamp() - 86_400.0
        return sum(1 for ts in bucket if ts.timestamp() >= cutoff)


# ---------------------------------------------------------------------------
# Fine-tuned classifier protocols (loader contract — legacy)
# ---------------------------------------------------------------------------


@runtime_checkable
class Classifier(Protocol):
    """Loose Protocol for a fine-tuned classifier model."""

    def predict(self, text: str) -> tuple[str, float]: ...  # pragma: no cover

    def predict_batch(
        self, texts: list[str]
    ) -> list[tuple[str, float]]: ...  # pragma: no cover


class FineTunedLoader(Protocol):
    """Callable that loads + returns a :class:`Classifier`."""

    def __call__(self, path: str) -> "Classifier | Awaitable[Classifier]": ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _signal_text(signal: Signal, max_chars: int) -> str:
    """Best-effort text extraction from a raw signal."""
    pieces: list[str] = []
    for key in ("title", "headline", "subject"):
        v = signal.payload.get(key)
        if isinstance(v, str) and v.strip():
            pieces.append(v.strip())
    for key in ("body", "content", "text", "summary", "message", "description"):
        v = signal.payload.get(key)
        if isinstance(v, str) and v.strip():
            pieces.append(v.strip())
            break
    joined = "\n".join(pieces).strip()
    if max_chars and len(joined) > max_chars:
        joined = joined[:max_chars]
    return joined


def _build_label_anchor(label: Label) -> str:
    """Render a label into the string that would be embedded by the legacy
    backend. Kept for backwards compat with tests that reference the helper."""
    parts: list[str] = [label.name]
    if label.description:
        parts.append(label.description)
    if label.examples:
        for ex in label.examples[:20]:
            if isinstance(ex, str) and ex.strip():
                parts.append(ex.strip())
    return "\n".join(parts).strip()


def _taxonomy_fingerprint(
    schema: str,
    labels: Sequence[Label],
    severity: Sequence[Label],
) -> str:
    """Stable fingerprint of a taxonomy. Kept for backwards compat with
    tests that reference the helper. The HTTP variant does not cache
    embeddings — each /classify call is stateless on the client side."""
    import hashlib
    import json

    payload: dict[str, Any] = {
        "schema": schema,
        "labels": [
            {"name": l.name, "description": l.description, "examples": l.examples}
            for l in labels
        ],
        "severity": [
            {"name": l.name, "description": l.description, "examples": l.examples}
            for l in severity
        ],
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity. Kept for backwards compat with tests that
    reference the helper."""
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} vs {len(b)}")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or hasattr(value, "__await__"):
        return await value
    return value


__all__ = [
    "CLASSIFY_FAMILY",
    "CLASSIFY_HANDLER_VERSION",
    "CLASSIFY_KIND",
    "CLASSIFY_SCHEMA_VERSION",
    "Classification",
    "Classifier",
    "ClassifyConfig",
    "ClassifyHandler",
    "DEFAULT_SENTIMENT_SEEDS",
    "FineTunedLoader",
    "Label",
    "OTHER_LABEL",
    "Rule",
    "SENTIMENT_LABELS",
]
