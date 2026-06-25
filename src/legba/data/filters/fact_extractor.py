# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``fact_extractor`` enrichment stage — altitude-0 extraction (anchor §5 PIECE 2).

A descriptor-gated ``descriptor.pipeline.enrichment`` stage (the registry
pipeline factory) that turns each in-flight :class:`Signal` into
``(subject, predicate, value)`` facts and writes them to the ``facts`` table
(``source_type='ingestion'``). Lighting these rows up makes ``fact_decay``,
the knowledge-graph leg, and Consult's ``query_facts`` / ``inspect_entity``
tools come alive — they read an empty store today.

STATUS:
  * ``backend="relation"`` (DEFAULT) — LIVE, zero new model infra. Reuses the
    GLiREL relation triples already on ``signal.payload["entities"]`` (from the
    upstream ``ner_multilingual`` stage); when those are absent it calls the
    hosted ``POST /extract`` endpoint itself via the injected
    ``NlpServiceClient`` (the SAME call NER makes — ``ner.py``). This is
    literally "the same pattern as the NLP filters."
  * ``backend="llm"`` — OPT-IN, declared (the "8B hosted model" path). Routes
    the signal text through the analyst LLM provider plane via an injected
    ``llm_handler_factory`` (the ``SLMPort`` pattern). NO STUB: it raises a
    loud ``ValueError`` if selected without a wired ``llm_handler_factory``
    (mirrors ``ner_multilingual``'s ValueError in ``pipeline.py``). The model
    id is whatever stack component the operator points ``llm_component_id`` at.

DISCIPLINE (anchor §7): this stage is thin orchestration —
  * triple-quality gate reuses ``ner._is_nonentity_candidate`` (no fork),
  * event-time precedence reuses ``source_actor._entry_logical_ts`` (no fork),
  * the AGE edge leg reuses ``filters._fact_graph`` over ``PostgresStore.cypher()``,
  * the facts write is the §3 ``ON CONFLICT`` upsert (the same idempotency
    contract the filters honor).

Enrichment-only: ``transform`` NEVER drops the signal and NEVER raises on an
extractor/LLM/parse failure — it logs, flips health to ``degraded``, and
returns the signal unchanged (degrade-not-drop).
"""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, ClassVar, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ..provenance.writes import supersede_prior_facts
from ..sources._contract import Signal
from ..vocabulary import normalize_predicate
from ..stack.nlp_service import (
    NlpServiceAuthError,
    NlpServiceClient,
    NlpServiceUnavailable,
)
from ._contract import FilterContext, FilterHealth
from ._fact_graph import edge_label_for_predicate, upsert_fact_edge
from .ner import _classify_entity_text, _is_nonentity_candidate
from .slm_relationship_validate import (
    CORRECTED_TYPE_KEY,
    SLM_VALIDATED_FLAG,
    VALID_KEY,
    VALIDATION_CONFIDENCE_KEY,
    VALIDATION_REASONING_KEY,
    _SLMValidationError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Light validity gate — spelled-out quantity / ordinal endpoints (opt-in)
# ---------------------------------------------------------------------------
#
# The relation backend reuses entities that often carry NO usable per-triple
# score (so they fall to the 0.75 ingestion default; the historical REBEL
# backend stamped a uniform synthetic 1.0). Either way a confidence floor
# cannot reliably filter noise — categorically-wrong triples land at the same
# score as the good ones (live audit: "World Cup leader of sixth", "FBI
# controls At least five"). ``ner._is_nonentity_candidate`` already drops
# numeric/date/unit endpoints, but it lets SPELLED-OUT quantity phrases
# through ("sixth", "five", "at least five") because they contain letters.
#
# This gate, when a descriptor opts in via ``reject_quantity_endpoints``,
# drops a triple whose subject OR value is *entirely* spelled-out numbers /
# ordinals / quantity-qualifiers — the clearest, lowest-risk slice of the
# noise. Conservative by construction: a single genuinely-nominal token (a
# real name) keeps the endpoint, so "five US senators" is kept while "at
# least five" is dropped.

_NUMBER_WORDS: frozenset[str] = frozenset({
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
    "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred",
    "thousand", "million", "billion", "trillion", "dozen", "couple",
})
_ORDINAL_WORDS: frozenset[str] = frozenset({
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
    "eighth", "ninth", "tenth", "eleventh", "twelfth", "thirteenth",
    "twentieth", "thirtieth", "last", "next",
})
#: Filler tokens that carry no entity content on their own.
_QUANTITY_QUALIFIERS: frozenset[str] = frozenset({
    "at", "least", "most", "more", "less", "than", "about", "around",
    "approximately", "nearly", "almost", "over", "under", "up", "to",
    "of", "and", "or", "the", "a", "an", "some", "several", "many", "few",
    "multiple", "numerous", "minimum", "maximum",
})
_QUANTITY_NONNOMINAL = _NUMBER_WORDS | _ORDINAL_WORDS | _QUANTITY_QUALIFIERS
_QUANTITY_STRIP = " \t\n\r\"'`.,;:!?()[]{}<>«»“”‘’%-"


def _is_quantity_phrase(text: str) -> bool:
    """True when EVERY token of ``text`` is a spelled-out number, ordinal, or
    quantity-qualifier (e.g. "sixth", "at least five", "several thousand").

    Conservative: a single nominal token (a real name) ⇒ ``False`` (kept).
    Empty / no-token strings ⇒ ``False`` (this gate makes no claim about them;
    the existing required-field + ``_is_nonentity_candidate`` checks own those).
    """
    tokens = [tok.strip(_QUANTITY_STRIP).lower() for tok in text.split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return False
    return all(t in _QUANTITY_NONNOMINAL or t.isdigit() for t in tokens)


# ---------------------------------------------------------------------------
# NER-junk gate — self-referential / entity-escaped / empty endpoints
# ---------------------------------------------------------------------------
#
# The live audit found the ingestion path laundered NER junk that a confidence
# floor can't catch and ``_is_nonentity_candidate`` /
# ``_is_quantity_phrase`` don't cover:
#
#   * SELF-REFERENTIAL triples — subject == value, or one endpoint is a proper
#     substring of the other ("Putin" → "Vladimir Putin"): a co-reference
#     artifact, not a relation.
#   * HTML-entity-escaped endpoints ("Macron&#39;s", "AT&amp;T") that leaked
#     un-unescaped text into the substrate.
#   * empty / pure-numeric / pure-punctuation endpoints.
#
# Conservative by construction: legitimate distinct-name triples
# ("Macron"/leader of/"France", "BBC"/operates in/"United Kingdom") PASS.

#: Detects an HTML entity (named ``&amp;`` / numeric ``&#39;`` / hex ``&#x27;``).
_HTML_ENTITY_RE = re.compile(r"&(#x?[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]{1,31});")
#: Trim for the junk normalization (mirrors the quantity-gate strip set).
_JUNK_STRIP = " \t\n\r\"'`.,;:!?()[]{}<>«»“”‘’-"


def _junk_norm(text: str) -> str:
    """Light normalization for self-reference comparison: casefold, strip,
    collapse internal whitespace. (Does NOT strip HTML entities — the entity
    check runs on the raw endpoint.)"""
    return " ".join(text.split()).strip(_JUNK_STRIP).casefold()


# DQ-H3: leadership predicates (canonical, lowercase-spaced) the ingestion
# extractor must NOT write — seed/curated own current heads of state/government.
_LEADERSHIP_PREDICATES = frozenset({
    "leader of", "head of state", "head of government",
})


#: Zero-width / bidi formatting chars that NER drags into entity surfaces and
#: that break dedup/display. Stripped by the shared pre-write scrub (DQ-H4).
_ZERO_WIDTH_RE = re.compile("[​‌‍‎‏﻿]")


def _scrub_entity_surface(text: str) -> str:
    """Shared pre-write scrub for a fact endpoint (DQ-H4 chokepoint).

    HTML-unescapes ("Benjamin Netanyahu&#039;s" -> "...'s"), strips zero-width /
    bidi formatting chars, and collapses whitespace. One place so HTML-entity
    and zero-width junk can't leak into facts via the ingestion write path the
    way it did before (the NER-junk gate ran on the un-normalized surface)."""
    if not text:
        return ""
    s = html.unescape(text)
    s = _ZERO_WIDTH_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def _is_junk_triple(subject: str, predicate: str, value: str) -> bool:
    """True when a ``(subject, predicate, value)`` triple is NER junk that must
    be dropped before the write (drop+log, never raise).

    Rejects when, after light normalization (casefold / strip / collapse-ws):
      * either endpoint is empty, pure-numeric, or pure-punctuation;
      * either endpoint carries an HTML entity (``&...;`` / ``&#``);
      * subject == value (self-referential), OR one endpoint is a PROPER
        substring (whole-token-boundary, after normalization) of the other
        ("Putin" ⊂ "Vladimir Putin") — a co-reference artifact, not a relation.

    Conservative: a distinct-name pair ("Macron"/"France", "BBC"/"United
    Kingdom") is NOT junk.

    DQ-H3: LEADERSHIP relations are rejected outright — current heads of
    state/government are SEED/CURATED territory, never ingestion. News NER
    extracting "X leader of Y" is unreliable (live junk: "Adolf Hitler leader of
    Germany", "Didier Deschamps leader of Algeria", "DA leader of seven years")
    and pollutes the authoritative leader surface grounding + the agency read
    tools rely on. The world_baseline / wikidata_leaders seed adapters own these.
    """
    # DQ-H3: drop ingestion-asserted leadership facts (the extractor only ever
    # writes source_type='ingestion').
    if normalize_predicate(predicate) in _LEADERSHIP_PREDICATES:
        return True
    # HTML-entity escape leaked into the endpoint text → junk (raw, pre-norm).
    if _HTML_ENTITY_RE.search(subject) or _HTML_ENTITY_RE.search(value):
        return True
    s = _junk_norm(subject)
    v = _junk_norm(value)
    # Empty / pure-punctuation (norm strips both to "") → junk.
    if not s or not v:
        return True
    # Pure-numeric endpoint (e.g. "2026", "1,234") → junk.
    if _is_pure_numeric(s) or _is_pure_numeric(v):
        return True
    # Exact self-reference.
    if s == v:
        return True
    # Proper-substring self-reference at a token boundary ("putin" ⊂ "vladimir
    # putin"). Guard the boundary so "Iran" vs "Iranian" is NOT a match.
    if _is_token_subphrase(s, v) or _is_token_subphrase(v, s):
        return True
    return False


def _is_pure_numeric(norm: str) -> bool:
    """True when the normalized endpoint is only digits / numeric punctuation
    (no letters at all)."""
    stripped = norm.replace(",", "").replace(".", "").replace(" ", "")
    stripped = stripped.replace("%", "").replace("+", "").replace("-", "")
    return bool(stripped) and stripped.isdigit()


def _is_token_subphrase(inner: str, outer: str) -> bool:
    """True when ``inner`` is a PROPER, token-aligned subphrase of ``outer``
    (both already normalized). Whole-token alignment avoids "iran" matching
    "iranian"; proper avoids the ``inner == outer`` case (handled separately)."""
    if inner == outer or not inner:
        return False
    inner_toks = inner.split()
    outer_toks = outer.split()
    if len(inner_toks) >= len(outer_toks):
        return False
    # Sliding window over outer tokens for an exact contiguous run.
    n = len(inner_toks)
    for i in range(len(outer_toks) - n + 1):
        if outer_toks[i:i + n] == inner_toks:
            return True
    return False


#: Default confidence for an ingestion fact whose extractor provided NO usable
#: per-triple score (a reused relation entity often carries no score, and the
#: historical REBEL backend stamped a synthetic 1.0 — see the module-level note
#: — so a missing/sentinel score must NOT land at 1.0).
#: Below 1.0 so ingestion (machine-extracted) facts are never as certain as a
#: curated seed (0.95) or a deliberate analyst assertion.
_INGESTION_DEFAULT_CONFIDENCE: float = 0.75


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FactExtractorUnconfigured(RuntimeError):
    """Selected backend is missing a required dependency (no-stub).

    Surfaces at construction/configure so a misconfigured descriptor fails
    fast rather than silently no-op'ing at the first signal.
    """


# ---------------------------------------------------------------------------
# Event-time precedence (shared with the source actor — no fork)
# ---------------------------------------------------------------------------


def _event_time(signal: Signal) -> datetime:
    """Resolve ``valid_from`` event-time for a signal.

    Reuses the EXACT precedence the source actor's cursor uses
    (``runtime/source_actor._entry_logical_ts``): payload ``_published_at_dt``
    / ``_last_seen_dt`` / ``_event_dt`` (handler-stamped logical timestamps),
    else the signal's ``fetched_at``. Always tz-aware UTC.
    """
    payload = signal.payload if isinstance(signal.payload, dict) else {}
    for key in ("_published_at_dt", "_last_seen_dt", "_event_dt"):
        val = payload.get(key)
        if isinstance(val, datetime):
            return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    fa = signal.fetched_at
    return fa if fa.tzinfo else fa.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class FactExtractorConfig(BaseModel):
    """Pydantic config for :class:`FactExtractorHandler`.

    Descriptor-side toggles only. The per-source ``enrichment`` gate (adding
    this stage to a descriptor) IS the cost throttle — do NOT enable it on
    high-volume/low-value feeds (earthquakes/GeoJSON).
    """

    model_config = ConfigDict(extra="forbid")

    backend: str = Field(
        default="relation",
        description="'relation' (reuse GLiREL triples, default) or 'llm' (8B provider plane).",
    )
    min_confidence: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description=(
            "Drop triples below this confidence floor. NOTE: the 'relation' "
            "backend reuses GLiREL triples, whose reused entities often carry "
            "no usable per-triple score (they fall to the 0.75 default) — so "
            "this floor cannot reliably discriminate noise on that backend "
            "(use reject_quantity_endpoints)."
        ),
    )
    reject_quantity_endpoints: bool = Field(
        default=True,
        description=(
            "Light validity gate: drop triples whose subject or value is "
            "entirely spelled-out numbers / ordinals / quantity-qualifiers "
            "('sixth', 'at least five'). Filters the worst relation noise that "
            "the confidence floor cannot (reused triples often lack a usable "
            "score). "
            "Conservative — a single real-name token keeps the endpoint. ON by "
            "default (graph-and-data Wave-1b item 4); set false to disable.",
        ),
    )
    relation_allowlist: list[str] | None = Field(
        default=None,
        description=(
            "Optional relation-type allowlist. When set (non-empty), a triple "
            "is KEPT only if its predicate maps to one of these canonical "
            "relation types (via the same predicate→edge map the AGE leg uses, "
            "_fact_graph.edge_label_for_predicate) — e.g. ['LocatedIn', "
            "'MemberOf', 'PartOf']. The canonical type is stamped onto the "
            "fact's data.relation_type. None/empty = keep all (no typing "
            "filter, type still stamped). Conservative default OFF so a tight "
            "list can't silently over-reject; opt in per noisy descriptor.",
        ),
    )
    max_facts_per_signal: int = Field(
        default=50, ge=1,
        description="Hard cap on facts written per signal (row-explosion/cost guard).",
    )
    text_fields: list[str] = Field(
        default_factory=lambda: ["title", "body", "summary", "raw_body"],
        description="Ordered payload fields concatenated for the /extract + LLM backends.",
    )
    max_text_chars: int = Field(
        default=2000, ge=1,
        description="Truncate the concatenated text to this length before extraction.",
    )
    emit_graph_edges: bool = Field(
        default=False,
        description="Emit nexus → AGE edges (facts-first; ship false, flip after proof).",
    )
    llm_component_id: str | None = Field(
        default=None,
        description="Stack component id for backend='llm' (the 8B model the operator hosts).",
    )

    # --- SLM relationship-validation stage (opt-in, W3) --------------------
    #
    # When ON, extracted triples are routed through the
    # ``slm_relationship_validate`` SLM before they become facts. OFF by
    # default: the default path adds NO LLM hop and is byte-identical to
    # today. Budget-gated by ``slm_validate_max_triples`` + the per-source
    # enrichment gate; degrade-not-drop (an SLM failure keeps the triples).
    slm_validate_relations: bool = Field(
        default=False,
        description=(
            "Opt-in: route extracted triples through the SLM "
            "relationship-validator before they become facts. Drops/flags "
            "contradicted or low-confidence relations. OFF by default (adds "
            "an LLM hop into ingest — never litellm, budget-gated, "
            "degrade-not-drop). Requires an llm_handler_factory wired at "
            "construction (the pipeline builder threads it)."
        ),
    )
    slm_validate_drop_invalid: bool = Field(
        default=True,
        description=(
            "When the validator marks a triple invalid (or below "
            "slm_validate_min_confidence), DROP it (default) rather than "
            "writing it as a fact. False = keep it but stamp the validation "
            "verdict into the fact's data (flag-not-drop). Only consulted "
            "when slm_validate_relations is on."
        ),
    )
    slm_validate_min_confidence: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description=(
            "A triple the SLM marks valid but with validation_confidence "
            "below this floor is treated as invalid (dropped/flagged per "
            "slm_validate_drop_invalid). 0.0 = trust the bool verdict only. "
            "Only consulted when slm_validate_relations is on."
        ),
    )
    slm_validate_max_triples: int = Field(
        default=50, ge=1, le=500,
        description=(
            "Cap on triples validated per signal (SLM cost guard). Only "
            "consulted when slm_validate_relations is on."
        ),
    )
    slm_validate_component_id: str | None = Field(
        default=None,
        description=(
            "Stack component id the SLM validator targets. Absent, the "
            "pipeline builder falls back to LEGBA_SLM_RELATIONSHIP_VALIDATE_"
            "COMPONENT then the shared default SLM component. Only consulted "
            "when slm_validate_relations is on."
        ),
    )

    def validated_backend(self) -> str:
        if self.backend not in ("relation", "llm"):
            raise ValueError(
                f"fact_extractor backend must be 'relation' or 'llm', got {self.backend!r}"
            )
        return self.backend


# ---------------------------------------------------------------------------
# LLM extraction prompt (backend='llm')
# ---------------------------------------------------------------------------


_LLM_SYSTEM = (
    "You extract factual (subject, predicate, value) triples from a news text. "
    "Return ONLY a JSON array of objects with keys subject, predicate, value, "
    "and optional confidence in [0,1]. predicate is a short lower-case relation "
    "phrase. Extract only concrete entity-to-entity or entity-to-attribute "
    "relations actually stated; do not invent. Return [] if none."
)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class FactExtractorHandler:
    """Ingest-time fact-extraction enrichment stage. See module docstring."""

    # --- L-102 §1 class-vars ------------------------------------------------
    kind: ClassVar[str] = "fact_extractor"
    family: ClassVar[str] = "filter"
    schema_version: ClassVar[str] = "legba/filter.fact_extractor/1-0-0"
    config_schema: ClassVar[type[BaseModel]] = FactExtractorConfig
    handler_version: ClassVar[str] = "0.1.0"
    idempotent: ClassVar[bool] = True

    # Writes to the `facts` table, not to payload — empty output contract.
    output_contract: ClassVar[Mapping[str, type]] = {}

    def __init__(
        self,
        config: FactExtractorConfig,
        *,
        pg_pool: Any,
        nlp_client: NlpServiceClient | None = None,
        llm_handler_factory: Any | None = None,
        graph_store: Any | None = None,
        relationship_validator: Any | None = None,
    ) -> None:
        self._config = config
        backend = config.validated_backend()
        if pg_pool is None:
            raise FactExtractorUnconfigured(
                "fact_extractor requires a pg_pool (the facts write target); "
                "pass pg_pool= (no stub)."
            )
        if backend == "llm" and llm_handler_factory is None:
            raise FactExtractorUnconfigured(
                "fact_extractor backend='llm' requires an llm_handler_factory; "
                "wire it via build_filter_handler(llm_handler_factory=...) + the "
                "dapr_host _source_enrichment_factory call (Task 2b) — no stub."
            )
        if config.slm_validate_relations and relationship_validator is None:
            raise FactExtractorUnconfigured(
                "fact_extractor slm_validate_relations=True requires a wired "
                "relationship_validator (the slm_relationship_validate handler "
                "over the provider plane — never litellm). The pipeline builder "
                "constructs it from llm_handler_factory; pass "
                "relationship_validator= — no stub."
            )
        self._pool = pg_pool
        self._nlp_client = nlp_client
        self._llm_handler_factory = llm_handler_factory
        self._graph_store = graph_store
        # Opt-in SLM relationship-validation stage (W3). When wired + the flag
        # is on, extracted triples are SLM-validated before the facts write.
        self._relationship_validator = relationship_validator

        # Health-state counters (mirror NER's pattern).
        self._signals_in = 0
        self._signals_out = 0
        self._facts_written = 0
        self._signals_failed = 0
        self._last_error: str | None = None
        self._last_success_at: datetime | None = None
        self._activated = False
        self._service_healthy: bool | None = None
        self._degraded_this_call = False
        # SLM-validation stage counters.
        self._triples_slm_validated = 0
        self._triples_slm_dropped = 0

    # ------------------------------------------------------------------- props

    @property
    def config(self) -> FactExtractorConfig:
        return self._config

    @property
    def is_activated(self) -> bool:
        return self._activated

    # ------------------------------------------------------- lifecycle hooks

    async def on_configure(
        self,
        ctx: FilterContext | None = None,
        *,
        nlp_client: NlpServiceClient | None = None,
    ) -> None:
        if nlp_client is not None:
            self._nlp_client = nlp_client

    async def on_activate(self, ctx: FilterContext | None = None) -> None:
        self._activated = True
        self._service_healthy = True

    async def on_pause(self, ctx: FilterContext | None = None) -> None:
        self._activated = False

    async def on_resume(self, ctx: FilterContext | None = None) -> None:
        await self.on_activate(ctx)

    async def on_retire(self, ctx: FilterContext | None = None) -> None:
        self._activated = False
        if self._nlp_client is not None:
            try:
                await self._nlp_client.aclose()
            except Exception:                                # pragma: no cover
                pass

    # ------------------------------------------------------------------ transform

    async def transform(
        self,
        signal: Signal,
        ctx: FilterContext,
    ) -> Signal | None:
        """Extract facts from ``signal`` and write them to ``facts``.

        Enrichment-only: ALWAYS returns the signal unchanged (never drops,
        never raises). On any extractor/LLM/parse/DB failure it logs, flips
        health to degraded, and returns the signal.
        """
        self._signals_in += 1
        # Per-call degrade flag: the backend helpers set this on a soft
        # extractor/LLM failure (they return [] rather than raise). The
        # success path below must NOT clobber it back to healthy.
        self._degraded_this_call = False
        try:
            triples = await self._extract_triples(signal, ctx)
            if self._config.slm_validate_relations and triples:
                triples = await self._slm_validate_triples(signal, triples, ctx)
            written = await self._write_facts(signal, triples, ctx)
            self._facts_written += written
            self._signals_out += 1
            if not self._degraded_this_call:
                self._service_healthy = True
                self._last_error = None
            self._last_success_at = datetime.now(tz=timezone.utc)
        except Exception as exc:                             # pragma: no cover
            self._signals_failed += 1
            self._service_healthy = False
            self._last_error = f"transform: {exc!s}"
            ctx.logger.warning(
                "fact_extractor.transform_failed signal_id=%s err=%s",
                signal.signal_id, exc,
            )
        return signal

    # ----------------------------------------------------------- triple build

    async def _extract_triples(
        self, signal: Signal, ctx: FilterContext
    ) -> list[dict[str, Any]]:
        """Return the raw triple dicts for the configured backend."""
        backend = self._config.backend
        if backend == "llm":
            return await self._extract_llm(signal, ctx)
        return await self._extract_relation(signal, ctx)

    async def _extract_relation(
        self, signal: Signal, ctx: FilterContext
    ) -> list[dict[str, Any]]:
        """Default backend: reuse GLiREL relation triples already on the signal.

        Reads ``payload["entities"]`` (relation entities carry their ``predicate``
        context per ``ner.py``) and reconstructs ``(subject, predicate, value)``
        by pairing consecutive subject/object entities that share a predicate.
        When ``entities`` is absent/empty (NER off or upstream-skipped), calls
        ``/extract`` itself — the same call NER makes — so the stage is
        self-sufficient.
        """
        payload = signal.payload if isinstance(signal.payload, dict) else {}
        entities = payload.get("entities")
        triples = _entities_to_triples(entities) if isinstance(entities, list) else []
        if triples:
            return triples

        # Fallback: call /extract ourselves (same as ner.py).
        if self._nlp_client is None:
            return []
        text = _concat_text(payload, self._config.text_fields)
        if not text:
            return []
        truncated = text[: self._config.max_text_chars]
        try:
            data = await self._nlp_client.extract(truncated)
        except (NlpServiceAuthError, NlpServiceUnavailable) as exc:
            self._service_healthy = False
            self._degraded_this_call = True
            self._last_error = f"extract: {exc!s}"
            ctx.logger.debug(
                "fact_extractor.extract_unavailable signal_id=%s err=%s",
                signal.signal_id, exc,
            )
            return []
        return data.get("triples", []) if isinstance(data, dict) else []

    async def _extract_llm(
        self, signal: Signal, ctx: FilterContext
    ) -> list[dict[str, Any]]:
        """Opt-in 8B backend: prompt the analyst LLM plane for triples.

        On any LLM/parse failure: log + return [] (degrade, never raise).
        """
        payload = signal.payload if isinstance(signal.payload, dict) else {}
        text = _concat_text(payload, self._config.text_fields)
        if not text:
            return []
        truncated = text[: self._config.max_text_chars]
        component_id = self._config.llm_component_id
        if not component_id or self._llm_handler_factory is None:
            # Construction already guards this for backend='llm', but be safe.
            raise FactExtractorUnconfigured(
                "fact_extractor backend='llm' needs llm_component_id + a factory"
            )
        try:
            handler = await self._llm_handler_factory(component_id)
            resp = await handler.chat_complete(
                [{"role": "user", "content": truncated}],
                system=_LLM_SYSTEM,
            )
            content = getattr(resp, "content", None)
            if content is None and isinstance(resp, dict):
                content = resp.get("content")
            parsed = _parse_llm_triples(content or "")
        except Exception as exc:
            self._service_healthy = False
            self._degraded_this_call = True
            self._last_error = f"llm: {exc!s}"
            ctx.logger.warning(
                "fact_extractor.llm_failed signal_id=%s err=%s",
                signal.signal_id, exc,
            )
            return []
        return parsed

    # ------------------------------------------------------- slm validation

    async def _slm_validate_triples(
        self,
        signal: Signal,
        triples: list[dict[str, Any]],
        ctx: FilterContext,
    ) -> list[dict[str, Any]]:
        """Opt-in SLM relationship-validation of extracted triples (W3).

        Routes each ``{subject, predicate, object}`` triple through the wired
        :class:`SLMRelationshipValidateHandler` (the provider plane — never
        litellm) BEFORE the facts write. The SLM stamps each triple with a
        ``valid`` bool, an optional ``corrected_type``, and a
        ``validation_confidence``. We then:

          * drop (or flag, per ``slm_validate_drop_invalid``) triples the SLM
            marks invalid OR below ``slm_validate_min_confidence``;
          * re-type surviving triples whose verdict carries a
            ``corrected_type`` (the predicate becomes the corrected relation).

        Degrade-not-drop: only consulted when ``slm_validate_relations`` is on
        AND a validator is wired (construction guards that). On an SLM failure
        the triples flow through UNVALIDATED — never silently dropped.
        """
        validator = self._relationship_validator
        if validator is None:  # pragma: no cover — guarded at construction
            return triples

        payload = signal.payload if isinstance(signal.payload, dict) else {}
        source_text = _concat_text(payload, self._config.text_fields)[
            : self._config.max_text_chars
        ]
        # Cap the batch the SLM sees (cost guard) — surplus triples flow
        # through unvalidated rather than being dropped.
        cap = self._config.slm_validate_max_triples
        batch = triples[:cap]
        try:
            await validator.validate_triples(batch, source_text=source_text)
        except _SLMValidationError as exc:
            # Degrade-not-drop: keep every triple, log, flip health degraded.
            self._service_healthy = False
            self._degraded_this_call = True
            self._last_error = f"slm_validate: {exc.cause!s}"
            ctx.logger.warning(
                "fact_extractor.slm_validate_failed signal_id=%s err=%s",
                signal.signal_id, exc.cause,
            )
            return triples

        min_conf = self._config.slm_validate_min_confidence
        drop_invalid = self._config.slm_validate_drop_invalid
        kept: list[dict[str, Any]] = []
        for triple in triples:
            # Surplus triples beyond the cap carry no verdict — keep as-is.
            if SLM_VALIDATED_FLAG not in triple:
                kept.append(triple)
                continue
            self._triples_slm_validated += 1
            valid = bool(triple.get(VALID_KEY))
            conf = triple.get(VALIDATION_CONFIDENCE_KEY)
            below_floor = (
                isinstance(conf, (int, float)) and float(conf) < min_conf
            )
            rejected = (not valid) or below_floor
            # Apply a corrected relation type to the predicate when offered
            # AND the triple survives (a corrected type on a still-rejected
            # triple is moot).
            corrected = triple.get(CORRECTED_TYPE_KEY)
            if rejected:
                self._triples_slm_dropped += 1
                if drop_invalid:
                    continue  # drop — never becomes a fact
                # flag-not-drop: keep, the verdict is carried into the fact's
                # data by _write_facts (it reads these keys off the triple).
                kept.append(triple)
                continue
            if isinstance(corrected, str) and corrected.strip():
                triple["predicate"] = corrected.strip()
            kept.append(triple)
        return kept

    # ----------------------------------------------------------- facts write

    async def _write_facts(
        self,
        signal: Signal,
        triples: list[dict[str, Any]],
        ctx: FilterContext,
    ) -> int:
        """Filter + write surviving triples to ``facts``. Returns count."""
        if not triples:
            return 0
        cfg = self._config
        valid_from = _event_time(signal)
        payload = signal.payload if isinstance(signal.payload, dict) else {}
        geo = payload.get("geo") if isinstance(payload.get("geo"), dict) else {}
        geo_lat = geo.get("lat")
        geo_lon = geo.get("lon")
        excerpt = _concat_text(payload, cfg.text_fields)[:512]
        overrides = None

        # Relation-type allowlist (canonical edge labels). Empty/None disables
        # the typing FILTER (type is still stamped on every kept fact).
        allowset = {a.strip() for a in (cfg.relation_allowlist or []) if a.strip()}

        prepared: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for triple in triples:
            if not isinstance(triple, dict):
                continue
            # DQ-H4 shared pre-write scrub on BOTH endpoints — HTML-unescape +
            # strip zero-width chars BEFORE the junk/dedup gates so escaped
            # ("…&#039;s") and zero-width-polluted surfaces are normalized once.
            subject = _scrub_entity_surface(str(triple.get("subject", "")))
            # Converge the predicate vocabulary at the write path: lowercase +
            # map any CamelCase form ("LeaderOf") onto the canonical
            # lowercase-spaced one ("leader of") so the lower(predicate) dedup /
            # supersession key agrees with the seed/analyst write paths.
            predicate = normalize_predicate(
                str(triple.get("predicate", "")).strip().lower()
            )
            value = _scrub_entity_surface(str(triple.get("object", triple.get("value", ""))))
            if not subject or not predicate or not value:
                continue
            # Reuse the NER numbers/dates/units rejection on BOTH endpoints.
            if _is_nonentity_candidate(subject) or _is_nonentity_candidate(value):
                continue
            # NER-junk gate: drop self-referential ("Putin"→"Vladimir Putin"),
            # HTML-entity-escaped, or empty/numeric/punct endpoints the
            # confidence floor can't catch (drop+log, never raise).
            if _is_junk_triple(subject, predicate, value):
                ctx.logger.debug(
                    "fact_extractor.junk_drop signal_id=%s subject=%r value=%r",
                    signal.signal_id, subject, value,
                )
                continue
            # Light validity gate (ON by default): drop spelled-out quantity/
            # ordinal endpoints the confidence floor can't be applied against.
            if cfg.reject_quantity_endpoints and (
                _is_quantity_phrase(subject) or _is_quantity_phrase(value)
            ):
                continue
            conf = _resolve_ingestion_confidence(triple, cfg.backend)
            if conf < cfg.min_confidence:
                continue
            # Canonical relation type (shared with the AGE edge leg). When an
            # allowlist is configured, keep only triples whose canonical type is
            # on it; the generic CoOccursWith fallback is included only when the
            # allowlist explicitly lists it.
            relation_type = edge_label_for_predicate(predicate)
            if allowset and relation_type not in allowset:
                continue
            key = (subject.lower(), predicate, value.lower())
            if key in seen:
                continue
            seen.add(key)
            subj_class = _classify_entity_text(
                subject, predicate=predicate, slot="subject", overrides=overrides
            )
            val_class = _classify_entity_text(
                value, predicate=predicate, slot="object", overrides=overrides
            )
            prepared.append({
                "subject": subject,
                "predicate": predicate,
                "value": value,
                "confidence": conf,
                "subject_class": subj_class,
                "value_class": val_class,
                "relation_type": relation_type,
                # Carry the SLM verdict (when the opt-in validation stage ran)
                # so it is provenanced into the fact's data jsonb below.
                "slm_validated": triple.get(SLM_VALIDATED_FLAG),
                "slm_valid": triple.get(VALID_KEY),
                "slm_confidence": triple.get(VALIDATION_CONFIDENCE_KEY),
                "slm_reasoning": triple.get(VALIDATION_REASONING_KEY),
            })
            if len(prepared) >= cfg.max_facts_per_signal:
                break

        if not prepared:
            return 0

        written = 0
        async with self._pool.acquire() as conn:
            for t in prepared:
                fact_id = uuid4()
                data = {
                    "signal_id": str(signal.signal_id),
                    "extractor": "fact_extractor",
                    "backend": cfg.backend,
                    "ner_class_subject": t["subject_class"],
                    "ner_class_object": t["value_class"],
                    "relation_type": t["relation_type"],
                }
                if t.get("slm_validated"):
                    # Provenance the SLM relationship-validation verdict.
                    data["slm_validated"] = True
                    data["slm_valid"] = bool(t.get("slm_valid"))
                    if t.get("slm_confidence") is not None:
                        data["slm_confidence"] = float(t["slm_confidence"])
                    if t.get("slm_reasoning"):
                        data["slm_reasoning"] = str(t["slm_reasoning"])
                evidence_set = {
                    "signal_id": str(signal.signal_id),
                    "text_excerpt": excerpt,
                }
                await _insert_ingestion_fact(
                    conn,
                    fact_id=fact_id,
                    subject=t["subject"],
                    predicate=t["predicate"],
                    value=t["value"],
                    confidence=t["confidence"],
                    valid_from=valid_from,
                    geo_lat=geo_lat,
                    geo_lon=geo_lon,
                    data=data,
                    evidence_set=evidence_set,
                    derived_from=[signal.signal_id],
                )
                written += 1
                if cfg.emit_graph_edges and self._graph_store is not None:
                    try:
                        await upsert_fact_edge(
                            self._graph_store,
                            subject=t["subject"],
                            subject_class=t["subject_class"],
                            predicate=t["predicate"],
                            value=t["value"],
                            value_class=t["value_class"],
                            fact_id=str(fact_id),
                        )
                    except Exception as exc:                 # pragma: no cover
                        ctx.logger.debug(
                            "fact_extractor.edge_skip signal_id=%s err=%s",
                            signal.signal_id, exc,
                        )
        return written

    # ----------------------------------------------------------- health_check

    async def health_check(self, ctx: FilterContext) -> FilterHealth:
        if not self._activated:
            state = "unhealthy"
        elif self._service_healthy is False or self._last_error:
            state = "degraded"
        else:
            state = "healthy"
        return FilterHealth(
            state=state,
            last_success_at=self._last_success_at,
            last_error=self._last_error,
            signals_in_24h=self._signals_in,
            signals_out_24h=self._signals_out,
            signals_dropped_24h=0,
            detail={
                "activated": self._activated,
                "backend": self._config.backend,
                "facts_written": self._facts_written,
                "signals_failed": self._signals_failed,
                "emit_graph_edges": self._config.emit_graph_edges,
                "slm_validate_relations": self._config.slm_validate_relations,
                "triples_slm_validated": self._triples_slm_validated,
                "triples_slm_dropped": self._triples_slm_dropped,
            },
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


async def _insert_ingestion_fact(
    conn: Any,
    *,
    fact_id: Any,
    subject: str,
    predicate: str,
    value: str,
    confidence: float,
    valid_from: datetime | None,
    geo_lat: float | None,
    geo_lon: float | None,
    data: dict[str, Any],
    evidence_set: dict[str, Any],
    derived_from: list[Any],
) -> None:
    """Write one source-owned ('ingestion') fact via the §3 ON CONFLICT upsert.

    Re-ingest of the same triple+valid_from is idempotent: confidence lifts to
    the max, lineage unions — the substrate idempotency contract the filters
    already honor. ``source_type`` is the constant ``'ingestion'``; analyst_id /
    target_id / run_id are NULL (ingestion facts are source-owned).

    Before the insert we close any prior OPEN fact for the same
    ``(lower(subject), lower(predicate))`` whose value DIFFERS (PIECE B
    auto-supersession): the prior row gets ``valid_until=now()`` +
    ``superseded_by=<this id>`` so the canonical "what is true now" is the
    single open row. A same-value re-assert closes nothing (the upsert owns
    it). Shares the exact write contract the analyst path uses
    (``provenance.writes.supersede_prior_facts``) so both producers agree.
    """
    # Ingestion facts MUST carry an event-time. `_event_time` always returns a
    # tz-aware datetime (payload logical-ts precedence → signal.fetched_at), so
    # a NULL here means a caller bypassed it — which would make the temporal
    # triple key fall back to the '1970-01-01' sentinel and mask replay/dedup
    # detection. Fail loud rather than silently collapse distinct event-times.
    if valid_from is None:
        raise ValueError(
            "ingestion fact valid_from is NULL "
            f"(subject={subject!r} predicate={predicate!r}); "
            "_event_time must stamp an event-time"
        )
    await supersede_prior_facts(
        conn,
        subject=subject,
        predicate=predicate,
        value=value,
        new_fact_id=fact_id,
    )
    # Identical-triple dedupe (PIECE B data quality): the 0032 open-row unique
    # index keys on the FULL quad INCLUDING valid_from, so the same
    # (subject, predicate, value) re-ingested from N signals with N distinct
    # event-times accumulates N open rows (the live "Russian located in UK" ×8
    # noise). This complements 0032 — it does NOT change the supersession key
    # (a DIFFERENT value still supersedes via supersede_prior_facts above) — by
    # collapsing the valid_from dimension for SAME-value open rows: if an open
    # row for this triple already exists (any valid_from), refresh it
    # (confidence→max, lineage union, earliest valid_from kept) and skip the
    # insert. A no-op for the row count; never resurrects a closed row (the
    # filter is open-only, matching the partial index's WHERE).
    existing_id = await conn.fetchval(
        """
        UPDATE facts
           SET confidence   = GREATEST(facts.confidence, $4),
               derived_from = (SELECT array_agg(DISTINCT e)
                               FROM unnest(facts.derived_from || $5::uuid[]) e),
               valid_from   = LEAST(facts.valid_from, $6),
               updated_at   = now()
         WHERE id = (
                 SELECT id FROM facts
                  WHERE lower(subject)   = lower($1)
                    AND lower(predicate) = lower($2)
                    AND lower(value)     = lower($3)
                    AND valid_until IS NULL
                    AND superseded_by IS NULL
                  ORDER BY valid_from ASC, created_at ASC
                  LIMIT 1
               )
        RETURNING id
        """,
        subject,
        predicate,
        value,
        float(confidence),
        list(derived_from),
        valid_from,
    )
    if existing_id is not None:
        # An open row already carries this exact triple — refreshed in place,
        # no duplicate inserted.
        return
    await conn.execute(
        """
        INSERT INTO facts (
            id, subject, predicate, value, confidence, source_type,
            valid_from, geo_lat, geo_lon, data, evidence_set,
            derived_from, schema_uri
        ) VALUES (
            $1, $2, $3, $4, $5, 'ingestion',
            $6, $7, $8, $9::jsonb, $10::jsonb,
            $11, 'iglu:legba/fact/jsonschema/2-0-0'
        )
        ON CONFLICT (lower(subject), lower(predicate), lower(value),
                     COALESCE(valid_from, '1970-01-01 00:00:00+00'::timestamptz))
                 WHERE valid_until IS NULL AND superseded_by IS NULL
        DO UPDATE SET
            confidence   = GREATEST(facts.confidence, EXCLUDED.confidence),
            derived_from = (SELECT array_agg(DISTINCT e)
                            FROM unnest(facts.derived_from || EXCLUDED.derived_from) e),
            updated_at   = now()
        """,
        fact_id,
        subject,
        predicate,
        value,
        float(confidence),
        valid_from,
        geo_lat,
        geo_lon,
        json.dumps(data),
        json.dumps(evidence_set),
        list(derived_from),
    )


def _concat_text(payload: Mapping[str, Any], text_fields: list[str]) -> str:
    """Concatenate the configured payload text fields (title first)."""
    parts: list[str] = []
    seen: set[str] = set()
    for fld in text_fields:
        val = payload.get(fld)
        if not val:
            continue
        if not isinstance(val, str):
            val = str(val)
        stripped = val.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        parts.append(stripped)
    return "\n".join(parts)


def _entities_to_triples(entities: list[Any]) -> list[dict[str, Any]]:
    """Reconstruct (subject, predicate, object) triples from relation entities.

    The upstream ``ner_multilingual`` stage de-dupes triple endpoints into a
    flat ``entities`` list where each entity carries its ``predicate`` context
    (``ner.py``). We pair entities that share a predicate: the first is the
    subject, the second the object. A single entity for a predicate yields no
    triple (no object endpoint). This is a best-effort reconstruction; when it
    is lossy the ``/extract`` fallback in ``_extract_relation`` provides the
    authoritative triples.
    """
    by_pred: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        pred = str(ent.get("predicate", "")).strip()
        text = str(ent.get("text", "")).strip()
        if not pred or not text:
            continue
        if pred not in by_pred:
            by_pred[pred] = []
            order.append(pred)
        by_pred[pred].append(ent)

    triples: list[dict[str, Any]] = []
    for pred in order:
        members = by_pred[pred]
        # Pair consecutive (subject, object) members sharing the predicate.
        for i in range(0, len(members) - 1, 2):
            subj = members[i]
            obj = members[i + 1]
            # None (not 1.0) when neither endpoint carried a score, so the
            # write-path resolver applies the default rather than a fake 1.0.
            conf = subj.get("confidence", obj.get("confidence", None))
            triples.append({
                "subject": subj.get("text", ""),
                "predicate": pred,
                "object": obj.get("text", ""),
                "confidence": conf,
            })
    return triples


def _parse_llm_triples(content: str) -> list[dict[str, Any]]:
    """Parse the LLM backend's JSON-array response into triple dicts.

    Tolerant: strips ```json fences, finds the first JSON array, and maps
    ``value`` → ``object`` so downstream code reads one shape.
    """
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        arr = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(arr, list):
        return []
    out: list[dict[str, Any]] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        out.append({
            "subject": item.get("subject", ""),
            "predicate": item.get("predicate", ""),
            "object": item.get("value", item.get("object", "")),
            # None (not 1.0) when the LLM omitted a score, so the write path's
            # confidence resolver applies the sane default instead of laundering
            # a fabricated 1.0.
            "confidence": item.get("confidence", None),
        })
    return out


def _clamp_conf(value: Any) -> float:
    try:
        c = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(1.0, c))


def _resolve_ingestion_confidence(triple: dict[str, Any], backend: str) -> float:
    """Resolve a REAL per-triple confidence for an ingestion fact.

    The live relation backend (GLiREL) emits a real per-relation score, but the
    reused entity payload often carries NO usable per-triple score, and the
    historical REBEL backend stamped a synthetic 1.0 on every triple. Neither a
    missing score nor an exact-1.0 sentinel is a real measurement, so we never
    let those land at 1.0:

      * a missing score (``confidence`` absent / non-numeric) → the sane
        default :data:`_INGESTION_DEFAULT_CONFIDENCE` (0.75, below seed/agent);
      * on the ``relation`` backend, a score of EXACTLY 1.0 is treated as the
        legacy "no real score" sentinel → also the default; a genuine sub-1.0
        score (a real GLiREL relation score, e.g. 0.9) is kept;
      * any other provided, in-range score (e.g. an ``llm``-backend 0.8) is
        used as-is.

    FOLLOW-UP: now that GLiREL emits real per-relation scores, the exact-1.0
    sentinel handling on the relation backend is a legacy/defensive guard
    carried over from REBEL; reconciling it so a *genuine* GLiREL 1.0 isn't
    collapsed to 0.75 is a tracked code follow-up. The logic below is unchanged.

    The SLM relationship-validation stage (when on) already overrides via the
    verdict path upstream; this only governs the extractor's own score.
    """
    raw = triple.get("confidence", None)
    if raw is None:
        return _INGESTION_DEFAULT_CONFIDENCE
    try:
        c = float(raw)
    except (TypeError, ValueError):
        return _INGESTION_DEFAULT_CONFIDENCE
    c = max(0.0, min(1.0, c))
    # Legacy/defensive (carried from the historical REBEL backend, which stamped
    # 1.0 on every triple): on the relation backend an exact 1.0 is treated as
    # "no real score" so it stops laundering 1.000s. GLiREL emits real scores —
    # see the FOLLOW-UP note above.
    if backend == "relation" and c >= 1.0:
        return _INGESTION_DEFAULT_CONFIDENCE
    return c


__all__ = [
    "FactExtractorConfig",
    "FactExtractorHandler",
    "FactExtractorUnconfigured",
]
