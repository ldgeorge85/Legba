# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SLM relationship-validation filter (L-202).

Stream-resident migration of the legacy
``legba.subconscious.service._handle_relationship_validation_trigger``
NATS handler. The legacy callsite received a payload of relation-extracted
relationship triples + source text on
``legba.subconscious.relationships``, asked the SLM to validate each,
and (for valid ones) inserted them into ``proposed_edges`` along with a
reification heuristic for HostileTo + SuppliesWeaponsTo / FundedBy
collisions.

In the stream-resident world the same per-signal validation happens
inline on a signal whose payload carries a ``relationships`` array of
``{subject, predicate, object}`` triples (populated by an upstream RE
filter — GLiREL or similar). The handler stamps each triple with a
``valid`` bool, an optional ``corrected_type``, a confidence float, and
the SLM's reasoning. Substrate writes (``proposed_edges`` insertion,
reification flagging) happen at the pipeline's output stage; this
filter is enrichment-only.

LLM-bearing
-----------

Backed by the L-120 ``legba.data.stack.llm`` provider stack via the
:class:`SLMPort` structural-typing port (shared with the sibling
:class:`SLMClassificationRefineHandler` and
:class:`SLMEntityResolveHandler`).

Idempotency (L-102 §3)
----------------------

A triple that already carries a ``slm_validated`` flag is a no-op
unless ``force_validate`` is set.

Failure semantics (L-102 §7)
----------------------------

  * SLM raises → triple stays unvalidated; counters track the drop.
  * Provider not configured → pass-through (handler is degraded).
  * Never drops the signal from the stream.

This module reuses the loose ports + JSON-parsing helpers from the
sibling :mod:`slm_classification_refine` module so the legacy
subconscious provider can be deleted in L-205 without orphaning shared
code.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import (
    Any,
    ClassVar,
    Mapping,
    Sequence,
)

from pydantic import BaseModel, ConfigDict, Field

from ..sources._contract import Signal
from ._contract import FilterContext, FilterHealth
from .slm_classification_refine import (
    ChatSLMPort,
    SLMPort,
    _maybe_await,
    _parse_json_loose,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class _SLMValidationError(RuntimeError):
    """Internal — the SLM call failed during validation.

    Carries the underlying ``cause`` so callers can log it and apply
    degrade-not-drop semantics (the triples are left unvalidated, never
    dropped on the failure path).
    """

    def __init__(self, cause: BaseException) -> None:
        super().__init__(f"slm_validation_failed: {cause!s}")
        self.cause = cause


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


SLM_RELATIONSHIP_VALIDATE_KIND: str = "slm_relationship_validate"
SLM_RELATIONSHIP_VALIDATE_FAMILY: str = "filter"
SLM_RELATIONSHIP_VALIDATE_SCHEMA_VERSION: str = (
    "legba/filter.slm_relationship_validate/1-0-0"
)
SLM_RELATIONSHIP_VALIDATE_HANDLER_VERSION: str = "0.1.0"

#: Payload key carrying the array of triples to validate.
RELATIONSHIPS_PAYLOAD_KEY: str = "relationships"

#: Per-triple fields stamped by this handler.
VALID_KEY: str = "valid"
CORRECTED_TYPE_KEY: str = "corrected_type"
VALIDATION_CONFIDENCE_KEY: str = "validation_confidence"
VALIDATION_REASONING_KEY: str = "validation_reasoning"
SLM_VALIDATED_FLAG: str = "slm_validated"

#: Reification-flag key — set when the upstream pipeline should consider
#: reifying the edge as a Nexus (legacy ``service.py`` heuristic).
NEEDS_REIFICATION_KEY: str = "needs_reification"

#: Edge-type set that triggers the legacy reification heuristic when
#: paired with a known HostileTo relationship between the same actors.
_REIFICATION_TRIGGER_TYPES: frozenset[str] = frozenset({
    "SuppliesWeaponsTo",
    "FundedBy",
})

#: System prompt — ported from
#: ``legba.subconscious.prompts.RELATIONSHIP_VALIDATION_SYSTEM``.
SYSTEM_PROMPT: str = """\
You are a relationship extraction validator for an intelligence \
analysis system.

Your task: validate relationship triples extracted by the upstream RE \
model. Each triple has a subject, predicate (relationship type), and \
object.

Rules:
- A triple is **valid** if the relationship accurately reflects the \
source text.
- A triple is **invalid** if the extraction is wrong, hallucinated, or \
the relationship type is incorrect.
- If the relationship type is wrong but a relationship exists, provide \
a corrected_type.
- Common relationship types: allied_with, opposes, part_of, located_in, \
leads, member_of, supplies, sanctions, controls, subsidiary_of, \
SuppliesWeaponsTo, FundedBy, HostileTo.
- Output MUST be valid JSON matching the schema below.
"""


# JSON Schema the SLM is asked to emit for a batch of verdicts.
RELATIONSHIP_VALIDATION_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "triple_index": {"type": "integer"},
                    "valid": {"type": "boolean"},
                    "corrected_type": {"type": ["string", "null"]},
                    "confidence": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                    "reasoning": {"type": "string"},
                },
                "required": ["triple_index", "valid"],
            },
        },
    },
    "required": ["verdicts"],
}


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


class RelationshipVerdict(BaseModel):
    """Parsed SLM verdict for one triple.

    Mirrors :class:`legba.subconscious.schemas.RelationshipVerdict` but
    adds a ``confidence`` field for stream-resident use (the legacy
    schema hard-coded a fixed confidence at substrate-write time).
    """

    model_config = ConfigDict(extra="ignore")

    triple_index: int
    valid: bool
    corrected_type: str | None = None
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    reasoning: str = ""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class SLMRelationshipValidateConfig(BaseModel):
    """Pydantic config schema for :class:`SLMRelationshipValidateHandler`."""

    model_config = ConfigDict(extra="forbid")

    #: Maximum number of source-text characters to include in the SLM
    #: prompt. Mirrors the legacy 1000-char truncation in
    #: :meth:`SubconsciousService._handle_relationship_validation_trigger`.
    max_source_chars: int = Field(default=1000, ge=100, le=32_768)

    #: Maximum number of triples per signal to validate. Caps SLM cost on
    #: signals with very large extracted-relationship lists.
    max_triples_per_signal: int = Field(default=50, ge=1, le=500)

    #: Optional pre-known set of HostileTo relationships, keyed by the
    #: lowercased entity pair ``"<a>|<b>"`` (canonical order). When
    #: provided + a triple's effective type is in
    #: :data:`_REIFICATION_TRIGGER_TYPES` and the same entity pair has a
    #: HostileTo entry here → the triple is flagged with
    #: :data:`NEEDS_REIFICATION_KEY=True`. The runtime can substitute an
    #: async ``hostile_lookup`` callable later for live registry lookups.
    known_hostile_pairs: list[str] = Field(default_factory=list)

    #: When ``True``, re-validate triples that already carry
    #: ``slm_validated``. Default ``False`` (idempotent per L-102 §3).
    force_validate: bool = False


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class SLMRelationshipValidateHandler:
    """Stream-resident SLM relationship-validation handler.

    Conforms to the L-102 §3 ``StreamHandler`` Protocol — see
    :mod:`legba.data.filters._contract`.
    """

    # --- L-102 §1 class-vars ------------------------------------------------
    kind: ClassVar[str] = SLM_RELATIONSHIP_VALIDATE_KIND
    family: ClassVar[str] = SLM_RELATIONSHIP_VALIDATE_FAMILY
    schema_version: ClassVar[str] = SLM_RELATIONSHIP_VALIDATE_SCHEMA_VERSION
    handler_version: ClassVar[str] = SLM_RELATIONSHIP_VALIDATE_HANDLER_VERSION
    config_schema: ClassVar[type[BaseModel]] = SLMRelationshipValidateConfig

    idempotent: ClassVar[bool] = True

    output_contract: ClassVar[Mapping[str, type]] = {
        f"payload.{RELATIONSHIPS_PAYLOAD_KEY}": list,
        f"payload.{RELATIONSHIPS_PAYLOAD_KEY}[].{VALID_KEY}": bool,
        f"payload.{RELATIONSHIPS_PAYLOAD_KEY}[].{SLM_VALIDATED_FLAG}": bool,
        f"payload.{RELATIONSHIPS_PAYLOAD_KEY}[].{VALIDATION_CONFIDENCE_KEY}": float,
    }

    def __init__(
        self,
        config: SLMRelationshipValidateConfig,
        *,
        slm: SLMPort | ChatSLMPort | None = None,
    ) -> None:
        self._config: SLMRelationshipValidateConfig = config
        self._slm: SLMPort | ChatSLMPort | None = slm
        self._hostile_pairs: set[str] = {
            self._canonicalize_pair(p) for p in config.known_hostile_pairs
        }

        self._signals_in: int = 0
        self._signals_out: int = 0
        self._signals_dropped: int = 0
        self._triples_seen: int = 0
        self._triples_valid: int = 0
        self._triples_invalid: int = 0
        self._triples_reclassified: int = 0
        self._triples_reification_flagged: int = 0
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    @property
    def config(self) -> SLMRelationshipValidateConfig:
        return self._config

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_configure(
        self,
        ctx: FilterContext | None = None,
        *,
        slm: SLMPort | ChatSLMPort | None = None,
    ) -> None:
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
        """Validate triples on the signal payload in place.

        Never drops a signal.
        """
        self._signals_in += 1

        triples = signal.payload.get(RELATIONSHIPS_PAYLOAD_KEY)
        if not isinstance(triples, list) or not triples:
            self._signals_out += 1
            return signal

        # Pass-through when provider isn't wired.
        if self._slm is None:
            ctx.logger.debug(
                "slm_relationship_validate.no_provider signal_id=%s",
                signal.signal_id,
            )
            self._signals_out += 1
            return signal

        # Filter to triples that actually need validation.
        candidates = self._select_candidates(triples)

        if not candidates:
            self._signals_out += 1
            return signal

        source_text = _extract_source_text(signal.payload, self._config.max_source_chars)

        try:
            await self._validate_candidates(candidates, source_text=source_text)
        except _SLMValidationError as exc:
            # Degrade-not-drop: SLM failure leaves the triples unvalidated;
            # the signal flows on unchanged. The drop-counter tracks the SLM
            # call failure for health (this is not a dropped *signal*).
            self._signals_dropped += 1
            ctx.logger.warning(
                "slm_relationship_validate.slm_failed signal_id=%s err=%s",
                signal.signal_id, exc.cause,
            )
            self._signals_out += 1
            return signal

        new_payload = dict(signal.payload)
        new_payload[RELATIONSHIPS_PAYLOAD_KEY] = list(triples)
        out_signal = signal.model_copy(update={"payload": new_payload})
        self._signals_out += 1
        self._last_success_at = _now()
        return out_signal

    # ------------------------------------------------------------------
    # health_check
    # ------------------------------------------------------------------

    async def health_check(
        self, ctx: FilterContext | None = None,
    ) -> FilterHealth:
        state = "healthy" if self._slm is not None else "degraded"
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
                "slm_wired": self._slm is not None,
                "hostile_pairs_known": len(self._hostile_pairs),
                "triples_valid_total": self._triples_valid,
                "triples_invalid_total": self._triples_invalid,
                "triples_reclassified_total": self._triples_reclassified,
                "triples_reification_flagged_total": (
                    self._triples_reification_flagged
                ),
            },
        )

    # ------------------------------------------------------------------
    # Reusable validation entry point (shared with fact_extractor wire)
    # ------------------------------------------------------------------

    async def validate_triples(
        self,
        triples: list[dict[str, Any]],
        *,
        source_text: str,
    ) -> list[dict[str, Any]]:
        """Validate a bare list of ``{subject, predicate, object}`` triples.

        Mutates each candidate triple in place with the SLM verdict
        (``slm_validated`` / ``valid`` / ``corrected_type`` / confidence /
        reasoning) and returns the same list. Honors the same idempotency,
        candidate-selection, and ``max_triples_per_signal`` gates as
        :meth:`transform`, but operates on the triple list directly rather
        than on a signal payload — this is the entry point the
        ``fact_extractor`` ingest-time wire calls.

        Degrade-not-drop: on SLM failure this raises
        :class:`_SLMValidationError` (with the candidate triples left
        UNVALIDATED) so the caller can distinguish "SLM down" from "all
        triples valid" and choose to keep the triples rather than drop them.
        When the provider is unwired the list is returned unchanged (no
        error).
        """
        if self._slm is None or not triples:
            return triples
        candidates = self._select_candidates(triples)
        if not candidates:
            return triples
        await self._validate_candidates(candidates, source_text=source_text)
        return triples

    def _select_candidates(
        self, triples: list[dict[str, Any]],
    ) -> list[tuple[int, dict[str, Any]]]:
        """Filter ``triples`` to those that actually need validation.

        Shared by :meth:`transform` and :meth:`validate_triples`: skips
        already-validated triples (unless ``force_validate``), skips triples
        missing a subject or object, and caps at ``max_triples_per_signal``.
        """
        candidates: list[tuple[int, dict[str, Any]]] = []
        for idx, triple in enumerate(triples):
            if not isinstance(triple, dict):
                continue
            if (
                triple.get(SLM_VALIDATED_FLAG)
                and not self._config.force_validate
            ):
                continue
            subj = (triple.get("subject") or "").strip()
            obj = (triple.get("object") or "").strip()
            if not subj or not obj:
                continue
            candidates.append((idx, triple))
            if len(candidates) >= self._config.max_triples_per_signal:
                break
        return candidates

    async def _validate_candidates(
        self,
        candidates: list[tuple[int, dict[str, Any]]],
        *,
        source_text: str,
    ) -> None:
        """Call the SLM for ``candidates`` and apply verdicts in place.

        Raises :class:`_SLMValidationError` (wrapping the cause) on SLM
        failure so callers can implement degrade-not-drop semantics. The
        candidate triples are left unvalidated on failure.
        """
        self._triples_seen += len(candidates)
        try:
            verdicts = await self._call_slm(
                source_text=source_text,
                triples=[t for _, t in candidates],
            )
        except Exception as exc:                                  # noqa: BLE001
            self._last_error = f"slm_call_failed: {exc!s}"
            raise _SLMValidationError(exc) from exc

        verdict_by_index = {v.triple_index: v for v in verdicts}
        for slot, (orig_idx, triple) in enumerate(candidates):
            # Try slot index first (legacy SLMs sometimes index by slot,
            # not original triple index), then original index.
            verdict = verdict_by_index.get(slot) or verdict_by_index.get(orig_idx)
            if verdict is None:
                continue
            self._apply_verdict_to_triple(triple, verdict)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _call_slm(
        self,
        *,
        source_text: str,
        triples: Sequence[Mapping[str, Any]],
    ) -> list[RelationshipVerdict]:
        """Invoke the SLM and parse the batch of verdicts.

        Returned list may be shorter than ``triples`` — verdicts are
        keyed by ``triple_index`` so missing entries fall through as
        unvalidated.
        """
        # Pass a slot-indexed view of the triples so the SLM doesn't see
        # any non-consecutive indices.
        triple_payload = [
            {
                "triple_index": idx,
                "subject": t.get("subject", ""),
                "predicate": (
                    t.get("predicate")
                    or t.get("relationship_type")
                    or ""
                ),
                "object": t.get("object", ""),
            }
            for idx, t in enumerate(triples)
        ]

        prompt = (
            "Validate the following relationship triples extracted from "
            "source text.\n\n"
            f"Source text: {source_text}\n\n"
            f"Triples:\n{json.dumps(triple_payload, indent=2)}\n\n"
            "Output JSON schema (one verdict per triple):\n"
            f"{json.dumps(RELATIONSHIP_VALIDATION_VERDICT_SCHEMA, indent=2)}"
        )

        raw = await self._dispatch(prompt)
        return self._parse_batch(raw)

    async def _dispatch(self, prompt: str) -> Any:
        complete = getattr(self._slm, "complete", None)
        if complete is not None and callable(complete):
            return await _maybe_await(
                complete(
                    prompt=prompt,
                    system=SYSTEM_PROMPT,
                    json_schema=RELATIONSHIP_VALIDATION_VERDICT_SCHEMA,
                )
            )
        chat_complete = getattr(self._slm, "chat_complete", None)
        if chat_complete is not None and callable(chat_complete):
            response = await _maybe_await(
                chat_complete(
                    [{"role": "user", "content": prompt}],
                    system=SYSTEM_PROMPT,
                )
            )
            content = getattr(response, "content", None)
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError(
                    "slm.chat_complete returned empty content"
                )
            parsed = _parse_json_loose(content)
            if parsed is None:
                # Some SLMs return a bare array — wrap it.
                try:
                    arr = json.loads(content)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        "slm.chat_complete returned unparseable content"
                    ) from exc
                if not isinstance(arr, list):
                    raise RuntimeError(
                        f"slm.chat_complete returned unexpected shape: "
                        f"{type(arr).__name__}"
                    )
                parsed = {"verdicts": arr}
            return parsed
        raise RuntimeError(
            "wired SLM port exposes neither complete() nor chat_complete()"
        )

    def _parse_batch(self, raw: Any) -> list[RelationshipVerdict]:
        """Parse the SLM's response into a list of verdicts.

        Accepts either ``{"verdicts": [...]}`` (the configured shape) or
        a bare ``[...]`` list (legacy / lenient SLMs).
        """
        if isinstance(raw, Mapping) and "verdicts" in raw:
            items = raw["verdicts"]
        elif isinstance(raw, list):
            items = raw
        elif isinstance(raw, Mapping):
            items = [raw]  # single-verdict shape
        else:
            raise RuntimeError(
                f"unexpected SLM response shape: {type(raw).__name__}"
            )
        verdicts: list[RelationshipVerdict] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            try:
                verdicts.append(RelationshipVerdict.model_validate(item))
            except Exception as exc:                              # noqa: BLE001
                logger.warning(
                    "slm_relationship_validate.verdict_parse_failed item=%r err=%s",
                    item, exc,
                )
        return verdicts

    def _apply_verdict_to_triple(
        self,
        triple: dict[str, Any],
        verdict: RelationshipVerdict,
    ) -> None:
        """Mutate ``triple`` in place with the validation annotation."""
        triple[SLM_VALIDATED_FLAG] = True
        triple[VALID_KEY] = bool(verdict.valid)
        triple[CORRECTED_TYPE_KEY] = verdict.corrected_type
        triple[VALIDATION_CONFIDENCE_KEY] = float(verdict.confidence)
        triple[VALIDATION_REASONING_KEY] = verdict.reasoning

        if verdict.valid:
            self._triples_valid += 1
        else:
            self._triples_invalid += 1
        if verdict.corrected_type:
            self._triples_reclassified += 1

        # Reification heuristic — only fires when the effective type is
        # in the trigger set + the entity pair has a known HostileTo
        # relationship.
        effective_type = (
            verdict.corrected_type
            or triple.get("predicate")
            or triple.get("relationship_type")
            or ""
        )
        if effective_type in _REIFICATION_TRIGGER_TYPES:
            pair = self._canonicalize_pair_from_triple(triple)
            if pair in self._hostile_pairs:
                triple[NEEDS_REIFICATION_KEY] = True
                self._triples_reification_flagged += 1

    @staticmethod
    def _canonicalize_pair(raw: str) -> str:
        """Canonicalise a ``"<a>|<b>"`` pair string by lowercasing and
        sorting. Idempotent."""
        parts = [p.strip().lower() for p in raw.split("|", 1)]
        if len(parts) != 2:
            return raw.strip().lower()
        a, b = parts
        if not a or not b:
            return raw.strip().lower()
        return f"{a}|{b}" if a < b else f"{b}|{a}"

    @staticmethod
    def _canonicalize_pair_from_triple(triple: Mapping[str, Any]) -> str:
        subj = str(triple.get("subject", "")).strip().lower()
        obj = str(triple.get("object", "")).strip().lower()
        if not subj or not obj:
            return ""
        return f"{subj}|{obj}" if subj < obj else f"{obj}|{subj}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _extract_source_text(payload: Mapping[str, Any], max_chars: int) -> str:
    """Pull the source text the RE model worked from. Prefers an
    explicit ``source_text`` field on the payload, falls back to the
    standard text fields.
    """
    for key in ("source_text", "text", "body", "content", "title", "summary"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            s = v.strip()
            if max_chars and len(s) > max_chars:
                return s[:max_chars]
            return s
    return ""


__all__ = [
    "CORRECTED_TYPE_KEY",
    "NEEDS_REIFICATION_KEY",
    "RELATIONSHIPS_PAYLOAD_KEY",
    "RELATIONSHIP_VALIDATION_VERDICT_SCHEMA",
    "RelationshipVerdict",
    "SLMRelationshipValidateConfig",
    "SLMRelationshipValidateHandler",
    "SLM_RELATIONSHIP_VALIDATE_FAMILY",
    "SLM_RELATIONSHIP_VALIDATE_HANDLER_VERSION",
    "SLM_RELATIONSHIP_VALIDATE_KIND",
    "SLM_RELATIONSHIP_VALIDATE_SCHEMA_VERSION",
    "SLM_VALIDATED_FLAG",
    "SYSTEM_PROMPT",
    "VALIDATION_CONFIDENCE_KEY",
    "VALIDATION_REASONING_KEY",
    "VALID_KEY",
]
