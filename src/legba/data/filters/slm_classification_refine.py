# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SLM classification-refinement filter (L-202).

Stream-resident migration of the legacy
:mod:`legba.subconscious.classification` cycle-phase caller. The legacy
module ran on a timer, pulled boundary-case signals out of Postgres, sent
them to an SLM in a batch, and wrote refined categories back. This filter
does the same semantic work inline on the in-flight signal: a signal
carrying ``payload["classification"]`` with a top-2 score gap below
``boundary_gap`` (or labelled with the reserved ``"other"`` label) is
re-classified by the configured SLM and the payload is updated in place.

Position in the pipeline
------------------------

This handler is intended to run *after* the deterministic
:class:`legba.data.filters.ClassifyHandler` (L-155). It looks for the
classification annotation already on the payload and only fires for
boundary cases. Signals that the upstream classifier already labelled
with high confidence flow through untouched.

LLM-bearing
-----------

Backed by the L-120 ``legba.data.stack.llm`` provider stack. The default
configuration targets gpt-oss-120b / vLLM (per the L-202 brief). The
provider is injected via the :class:`SLMPort` structural-typing port so
this module never imports a concrete provider class — the runtime wires
the live ``VLLMProviderHandler`` in at activation. Tests inject a
deterministic stub.

Idempotency (L-102 §3)
----------------------

A signal that already carries ``payload["classification"]["slm_refined"]
== True`` is a no-op (unless ``force_refine`` is set). This matches the
substrate-write contract: re-running the handler on the same
``(content_hash, handler_version)`` is a no-op.

Failure semantics (L-102 §7)
----------------------------

  * SLM raises → signal returned unmodified; the handler logs the
    failure and increments :attr:`signals_dropped_24h`. Never drops the
    signal from the stream.
  * Provider not configured → handler is a pass-through; the existing
    classification stays on the payload.
  * Malformed SLM response → same as a transient failure; original
    classification preserved.

This module depends only on the structural-typing surfaces in
``_contract.py`` and the loose :class:`SLMPort` Protocol below — no
import of ``legba.data.stack.llm`` or ``legba.data.runtime``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import (
    Any,
    Awaitable,
    ClassVar,
    Mapping,
    Protocol,
    runtime_checkable,
)

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..sources._contract import Signal
from ._contract import FilterContext, FilterHealth


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


SLM_CLASSIFY_KIND: str = "slm_classification_refine"
SLM_CLASSIFY_FAMILY: str = "filter"
SLM_CLASSIFY_SCHEMA_VERSION: str = "legba/filter.slm_classification_refine/1-0-0"
SLM_CLASSIFY_HANDLER_VERSION: str = "0.1.0"

#: Reserved low-confidence label name — mirrors the upstream classify filter.
OTHER_LABEL: str = "other"

#: Payload key the upstream :class:`ClassifyHandler` writes to.
CLASSIFICATION_PAYLOAD_KEY: str = "classification"

#: Marker the refine handler stamps so re-runs skip already-refined signals.
SLM_REFINED_FLAG: str = "slm_refined"

#: System prompt for the SLM (ported from
#: ``legba.subconscious.prompts.CLASSIFICATION_REFINEMENT_SYSTEM``). Kept
#: inline so the legacy subconscious package can be deleted in L-205
#: without breaking this handler.
SYSTEM_PROMPT: str = """\
You are a classification specialist. You output ONLY valid JSON, no prose.

Given a signal text and ML classifier scores, determine the correct \
category.

Rules:
- Consider the full text, not just keywords.
- Order categories by relevance (most relevant first).
- Respond with ONLY a JSON object. No explanation, no preamble, no markdown.
"""


# JSON Schema the SLM is asked to emit. Reused for ``response_format`` /
# ``guided_json`` style constrained decoding (vLLM supports both).
CLASSIFICATION_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "signal_id": {"type": "string"},
        "corrected_categories": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reasoning": {"type": "string"},
    },
    "required": ["signal_id", "corrected_categories", "confidence"],
}


# ---------------------------------------------------------------------------
# SLMPort — structural-typing port for the upstream LLM provider stack
# ---------------------------------------------------------------------------


@runtime_checkable
class SLMPort(Protocol):
    """Structural-typing port for the upstream SLM provider.

    Satisfied by :class:`legba.data.stack.llm.LLMProviderHandler` (via its
    ``chat_complete`` method) but declared loosely here so the handler
    module doesn't import the concrete class. Tests substitute a small
    deterministic stub.

    Either method may be implemented. ``complete`` is the subconscious-
    legacy shape (already returns a parsed dict matching the schema);
    ``chat_complete`` is the L-120 shape (returns an
    :class:`LLMResponse`-like object with a ``.content`` string the
    handler will parse). The handler tries ``complete`` first, then
    falls back to ``chat_complete``.
    """

    # Legacy subconscious-provider shape: returns a parsed dict directly.
    async def complete(  # pragma: no cover - protocol surface
        self,
        prompt: str,
        system: str = "",
        json_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


@runtime_checkable
class ChatSLMPort(Protocol):
    """L-120 ``chat_complete``-shaped port. Both ports are tried.

    Returned object must expose a ``.content`` attribute carrying the
    raw JSON string emitted by the model.
    """

    async def chat_complete(  # pragma: no cover - protocol surface
        self,
        messages: list[Mapping[str, Any]],
        *,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# Verdict — pydantic model the handler validates the SLM output against
# ---------------------------------------------------------------------------


class ClassificationVerdict(BaseModel):
    """Parsed SLM verdict for a single boundary-case signal.

    Mirrors :class:`legba.subconscious.schemas.ClassificationVerdict` but
    lives here so the legacy package can be deleted in L-205.
    """

    model_config = ConfigDict(extra="ignore")

    signal_id: str
    corrected_categories: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reasoning: str = ""

    @field_validator("corrected_categories")
    @classmethod
    def _strip_empties(cls, v: list[str]) -> list[str]:
        cleaned = [c.strip() for c in v if isinstance(c, str) and c.strip()]
        return cleaned


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class SLMClassificationRefineConfig(BaseModel):
    """Pydantic config schema for :class:`SLMClassificationRefineHandler`.

    Fields ported from the legacy ``SubconsciousConfig`` knobs that
    actually fed the per-signal call — orchestration knobs (intervals,
    batch sizes) have no analogue in stream-resident world and are
    dropped.
    """

    model_config = ConfigDict(extra="forbid")

    #: Score-gap below which a multi-label classification is considered
    #: a "boundary case" worth re-classifying. Mirrors the legacy
    #: ``classification.py`` strategy-1 threshold (0.1).
    boundary_gap: float = Field(default=0.1, ge=0.0, le=1.0)

    #: When ``True``, signals labelled :data:`OTHER_LABEL` are also
    #: candidates (legacy strategy-2 heuristic).
    refine_other: bool = True

    #: Minimum confidence the SLM must return for the verdict to be
    #: applied. Below this, the original classification stays.
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    #: Maximum number of characters of signal text to ship to the SLM.
    #: Mirrors the legacy 500-char truncation in
    #: ``classification.refine_classifications``.
    max_text_chars: int = Field(default=500, ge=64, le=32_768)

    #: When ``True``, re-refine even when
    #: ``payload["classification"]["slm_refined"] == True``. Default
    #: ``False`` (idempotent — matches L-102 §3).
    force_refine: bool = False


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class SLMClassificationRefineHandler:
    """Stream-resident SLM classification-refinement handler.

    Conforms to the L-102 §3 ``StreamHandler`` Protocol — see
    :mod:`legba.data.filters._contract`. Lives downstream of the
    deterministic :class:`ClassifyHandler` and only fires for boundary /
    ``"other"`` cases.
    """

    # --- L-102 §1 class-vars ------------------------------------------------
    kind: ClassVar[str] = SLM_CLASSIFY_KIND
    family: ClassVar[str] = SLM_CLASSIFY_FAMILY
    schema_version: ClassVar[str] = SLM_CLASSIFY_SCHEMA_VERSION
    handler_version: ClassVar[str] = SLM_CLASSIFY_HANDLER_VERSION
    config_schema: ClassVar[type[BaseModel]] = SLMClassificationRefineConfig

    #: L-102 §3: handler is idempotent on (content_hash, handler_version).
    idempotent: ClassVar[bool] = True

    #: Payload fields the handler reads / writes — declared for the
    #: registry composition check at pipeline-binding time.
    output_contract: ClassVar[Mapping[str, type]] = {
        f"payload.{CLASSIFICATION_PAYLOAD_KEY}": dict,
        f"payload.{CLASSIFICATION_PAYLOAD_KEY}.event_type": object,
        f"payload.{CLASSIFICATION_PAYLOAD_KEY}.confidence": float,
        f"payload.{CLASSIFICATION_PAYLOAD_KEY}.{SLM_REFINED_FLAG}": bool,
    }

    def __init__(
        self,
        config: SLMClassificationRefineConfig,
        *,
        slm: SLMPort | ChatSLMPort | None = None,
    ) -> None:
        """Construct a handler bound to a parsed config.

        Parameters
        ----------
        config:
            Validated handler config.
        slm:
            Optional pre-wired SLM port. In production the runtime
            injects the live ``VLLMProviderHandler`` via
            :meth:`on_configure`; in unit tests a deterministic stub is
            passed at construction.
        """
        self._config: SLMClassificationRefineConfig = config
        self._slm: SLMPort | ChatSLMPort | None = slm

        # Health counters
        self._signals_in: int = 0
        self._signals_out: int = 0
        self._signals_dropped: int = 0
        self._signals_refined: int = 0
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    @property
    def config(self) -> SLMClassificationRefineConfig:
        return self._config

    # ------------------------------------------------------------------
    # Lifecycle hooks (L-102 §1)
    # ------------------------------------------------------------------

    async def on_configure(
        self,
        ctx: FilterContext | None = None,
        *,
        slm: SLMPort | ChatSLMPort | None = None,
    ) -> None:
        """Bind / re-bind the upstream SLM port.

        The L-103 runtime will pass a ConfigureContext carrying a
        resolved StackRef → live ``VLLMProviderHandler`` instance; until
        then callers (tests, runtime adapter) inject directly.
        """
        if slm is not None:
            self._slm = slm

    async def on_activate(self, ctx: FilterContext | None = None) -> None:
        return None

    async def on_pause(self, ctx: FilterContext | None = None) -> None:
        return None

    async def on_resume(self, ctx: FilterContext | None = None) -> None:
        return None

    async def on_retire(self, ctx: FilterContext | None = None) -> None:
        self._slm = None

    # ------------------------------------------------------------------
    # transform
    # ------------------------------------------------------------------

    async def transform(
        self,
        signal: Signal,
        ctx: FilterContext,
    ) -> Signal | None:
        """Refine the signal's classification if it is a boundary case.

        Never drops a signal. Returns the (possibly mutated) signal.
        """
        self._signals_in += 1

        annotation = signal.payload.get(CLASSIFICATION_PAYLOAD_KEY)
        if not isinstance(annotation, dict):
            # Nothing to refine — pass through.
            self._signals_out += 1
            return signal

        # --- Idempotency: skip already-refined unless forced.
        if annotation.get(SLM_REFINED_FLAG) and not self._config.force_refine:
            self._signals_out += 1
            return signal

        # --- Boundary-case gate.
        if not self._is_boundary_case(annotation):
            self._signals_out += 1
            return signal

        # --- Provider not wired → pass-through (handler is degraded).
        if self._slm is None:
            ctx.logger.debug(
                "slm_classification_refine.no_provider signal_id=%s",
                signal.signal_id,
            )
            self._signals_out += 1
            return signal

        text = _extract_text(signal.payload, self._config.max_text_chars)
        if not text:
            self._signals_out += 1
            return signal

        # --- Call SLM
        try:
            verdict = await self._call_slm(
                signal_id=str(signal.signal_id),
                text=text,
                scores=_extract_scores(annotation),
            )
        except Exception as exc:                                  # noqa: BLE001
            self._last_error = f"slm_call_failed: {exc!s}"
            self._signals_dropped += 1
            ctx.logger.warning(
                "slm_classification_refine.slm_failed signal_id=%s err=%s",
                signal.signal_id, exc,
            )
            self._signals_out += 1
            return signal  # never drop

        if not verdict.corrected_categories:
            ctx.logger.debug(
                "slm_classification_refine.empty_verdict signal_id=%s",
                signal.signal_id,
            )
            self._signals_out += 1
            return signal

        if verdict.confidence < self._config.min_confidence:
            ctx.logger.debug(
                "slm_classification_refine.low_confidence "
                "signal_id=%s conf=%.3f floor=%.3f",
                signal.signal_id, verdict.confidence,
                self._config.min_confidence,
            )
            self._signals_out += 1
            return signal

        # --- Stamp the refined classification.
        out_signal = self._apply_verdict(signal, annotation, verdict)
        self._signals_refined += 1
        self._signals_out += 1
        self._last_success_at = _now()
        return out_signal

    # ------------------------------------------------------------------
    # health_check
    # ------------------------------------------------------------------

    async def health_check(
        self, ctx: FilterContext | None = None,
    ) -> FilterHealth:
        provider_wired = self._slm is not None
        state = "healthy" if provider_wired else "degraded"
        if self._last_error:
            state = "degraded"
        return FilterHealth(
            state=state,
            last_success_at=self._last_success_at,
            last_error=self._last_error,
            signals_in_24h=self._signals_in,
            signals_out_24h=self._signals_out,
            signals_dropped_24h=self._signals_dropped,
            detail={
                "provider_wired": provider_wired,
                "boundary_gap": self._config.boundary_gap,
                "refine_other": self._config.refine_other,
                "min_confidence": self._config.min_confidence,
                "refined_total": self._signals_refined,
            },
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _is_boundary_case(self, annotation: Mapping[str, Any]) -> bool:
        """Decide whether the annotation deserves SLM refinement.

        Strategy 1 (gap): the upstream :class:`ClassifyHandler` writes a
        ``label_scores`` dict — if the top-2 scores are within
        :attr:`boundary_gap` of each other, this is a boundary case.

        Strategy 2 (other): when :attr:`refine_other` is ``True``, any
        signal labelled :data:`OTHER_LABEL` is a candidate.
        """
        event_type = annotation.get("event_type")
        if self._config.refine_other:
            if isinstance(event_type, str) and event_type.lower() == OTHER_LABEL:
                return True
            if isinstance(event_type, list) and OTHER_LABEL in (
                e.lower() if isinstance(e, str) else "" for e in event_type
            ):
                return True

        scores = annotation.get("label_scores")
        if not isinstance(scores, Mapping) or len(scores) < 2:
            return False
        ordered = sorted(
            (float(v) for v in scores.values() if isinstance(v, (int, float))),
            reverse=True,
        )
        if len(ordered) < 2:
            return False
        return (ordered[0] - ordered[1]) <= self._config.boundary_gap

    async def _call_slm(
        self,
        *,
        signal_id: str,
        text: str,
        scores: dict[str, float],
    ) -> ClassificationVerdict:
        """Invoke the SLM and parse the verdict.

        Tries the legacy ``complete`` shape first (returns a parsed dict
        directly), then the L-120 ``chat_complete`` shape (returns an
        object exposing ``.content`` carrying a JSON string).
        """
        user_prompt = (
            "Classify this signal. Respond with ONLY a JSON object, "
            "nothing else.\n\n"
            f"Signal ID: {signal_id}\n"
            f"Text: {text}\n\n"
            f"ML scores: {json.dumps(scores)}\n\n"
            "Required JSON format (exactly this structure):\n"
            '{"signal_id": "' + signal_id + '", '
            '"corrected_categories": ["<best_category>", "<second_best>"], '
            '"confidence": 0.8, "reasoning": "<one sentence>"}'
        )

        complete = getattr(self._slm, "complete", None)
        if complete is not None and callable(complete):
            result = await _maybe_await(
                complete(
                    prompt=user_prompt,
                    system=SYSTEM_PROMPT,
                    json_schema=CLASSIFICATION_VERDICT_SCHEMA,
                )
            )
            if not isinstance(result, Mapping):
                raise RuntimeError(
                    "slm.complete returned non-mapping result: "
                    f"{type(result).__name__}"
                )
            # Force signal_id to match — some SLMs echo a different one.
            payload = dict(result)
            payload.setdefault("signal_id", signal_id)
            return ClassificationVerdict.model_validate(payload)

        chat_complete = getattr(self._slm, "chat_complete", None)
        if chat_complete is not None and callable(chat_complete):
            messages: list[Mapping[str, Any]] = [
                {"role": "user", "content": user_prompt},
            ]
            response = await _maybe_await(
                chat_complete(messages, system=SYSTEM_PROMPT)
            )
            content = getattr(response, "content", None)
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError(
                    "slm.chat_complete returned empty content"
                )
            parsed = _parse_json_loose(content)
            if parsed is None:
                raise RuntimeError(
                    f"slm.chat_complete returned unparseable content: "
                    f"{content[:200]!r}"
                )
            parsed.setdefault("signal_id", signal_id)
            return ClassificationVerdict.model_validate(parsed)

        raise RuntimeError(
            "wired SLM port exposes neither complete() nor chat_complete()"
        )

    def _apply_verdict(
        self,
        signal: Signal,
        annotation: Mapping[str, Any],
        verdict: ClassificationVerdict,
    ) -> Signal:
        """Return a copy of ``signal`` with the refined classification
        merged onto the payload.

        Preserves all original annotation keys (so downstream filters
        that read e.g. ``label_scores`` still see them) and adds:

          * ``event_type`` rewritten to the top SLM category.
          * ``slm_refined`` set to ``True``.
          * ``slm_confidence`` / ``slm_reasoning`` /
            ``slm_all_categories`` recorded for audit.
        """
        new_annotation = dict(annotation)
        # Preserve the original deterministic-classifier event_type for
        # audit before overwriting.
        new_annotation.setdefault(
            "pre_slm_event_type", annotation.get("event_type"),
        )
        new_annotation["event_type"] = verdict.corrected_categories[0]
        new_annotation[SLM_REFINED_FLAG] = True
        new_annotation["slm_confidence"] = float(verdict.confidence)
        new_annotation["slm_reasoning"] = verdict.reasoning
        new_annotation["slm_all_categories"] = list(verdict.corrected_categories)

        new_payload = dict(signal.payload)
        new_payload[CLASSIFICATION_PAYLOAD_KEY] = new_annotation
        return signal.model_copy(update={"payload": new_payload})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _extract_text(payload: Mapping[str, Any], max_chars: int) -> str:
    """Best-effort text extraction. Mirrors the upstream classify filter
    but truncated to the SLM-friendly length."""
    for key in ("text", "title", "summary", "body", "content", "description"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            s = v.strip()
            if max_chars and len(s) > max_chars:
                return s[:max_chars]
            return s
    return ""


def _extract_scores(annotation: Mapping[str, Any]) -> dict[str, float]:
    """Pull a usable ``label -> score`` dict from a classification
    annotation. Falls back to a synthetic mid-range dict matching the
    legacy classification.py shape when nothing usable is present.
    """
    raw = annotation.get("label_scores")
    if isinstance(raw, Mapping):
        out: dict[str, float] = {}
        for name, val in raw.items():
            if not isinstance(name, str):
                continue
            try:
                out[name] = float(val)
            except (TypeError, ValueError):
                continue
        if out:
            return out

    # Fallback synthetic — legacy behaviour for signals lacking explicit
    # per-label scores. Lets the SLM still see *some* prior.
    event_type = annotation.get("event_type")
    if isinstance(event_type, str) and event_type:
        return {event_type: 0.45, "unknown_secondary": 0.40}
    return {"other": 0.40, "unknown_secondary": 0.40}


def _parse_json_loose(text: str) -> dict[str, Any] | None:
    """Try plain JSON, then markdown-fenced JSON, then first ``{ ... }``
    block. Returns ``None`` when nothing parseable is found.

    Mirrors :func:`legba.subconscious.provider._extract_json` so behaviour
    of pre-ported callers is preserved.
    """
    text = text.strip()
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # markdown code fence
    import re
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group(1))
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    # first { ... } block
    idx_start = text.find("{")
    idx_end = text.rfind("}")
    if idx_start != -1 and idx_end > idx_start:
        try:
            result = json.loads(text[idx_start : idx_end + 1])
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
    return None


async def _maybe_await(value: Any) -> Any:
    """Await coroutines / awaitables; pass through plain values."""
    if hasattr(value, "__await__"):
        return await value  # type: ignore[no-any-return]
    return value


__all__ = [
    "CLASSIFICATION_PAYLOAD_KEY",
    "CLASSIFICATION_VERDICT_SCHEMA",
    "ChatSLMPort",
    "ClassificationVerdict",
    "OTHER_LABEL",
    "SLM_CLASSIFY_FAMILY",
    "SLM_CLASSIFY_HANDLER_VERSION",
    "SLM_CLASSIFY_KIND",
    "SLM_CLASSIFY_SCHEMA_VERSION",
    "SLM_REFINED_FLAG",
    "SLMClassificationRefineConfig",
    "SLMClassificationRefineHandler",
    "SLMPort",
    "SYSTEM_PROMPT",
]
