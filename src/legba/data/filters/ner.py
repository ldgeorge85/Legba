# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Multilingual NER filter handler — HTTP-client variant (L-154, post-reshape).

Implements the L-102 §3 filter/enrichment contract. Extracts named entities
from a :class:`legba.data.sources.Signal`'s text payload by calling the
hosted Legba-models ``POST /extract`` endpoint (GLiREL zero-shot relation
extraction over spaCy NER entities). Triples → entities: each S-P-O triple
contributes its subject and object as candidate entities. Each candidate is
mapped to Legba's closed 9-value ``entity_class`` taxonomy (per
``design/legba_data_mapping.md`` §4.5) using a label-keyword heuristic, since
the /extract contract returns the relation triples as free-text spans (the
endpoint does not carry the spaCy entity label through to Legba's contract).

Architectural-drift correction (2026-05-22): the pre-reshape Legba ingestion
called this exact endpoint via :class:`legba.ingestion.models_client.ModelsClient`;
the Phase-4 in-process spaCy implementation reinvented the wheel in-process
with all the model-download + GPU footprint that entailed. This module
restores the hosted-endpoint path on the Phase-4 contract surface.

Behavior:

  * Constructor accepts an :class:`NlpServiceClient` (or factory) injected
    by the runtime when the descriptor's ``Property.StackRef`` resolves.
    Tests inject a mock client via ``httpx.MockTransport``.
  * ``on_configure`` / ``on_activate`` are near-noops: no model loading
    happens in-process. ``on_configure`` issues a single ``/health`` probe
    to surface auth failures early.
  * ``transform(signal, ctx)`` posts to ``/extract`` with the concatenated
    payload text fields, walks the triples, classifies each S/O candidate,
    and annotates ``signal.payload["entities"]`` with a list of dicts of
    shape ``{class, text, start, end, lang, confidence, predicate}``.
  * Graceful degradation: when the service is unavailable, the signal
    passes through with ``entities=[]`` and the handler records the
    failure in ``_last_error`` + ``_signals_failed`` so health probes
    flip to ``degraded``.

Vocabulary alignment (L-102 §5): the handler takes a
``vocabulary_values`` set and filters mapped classes against it. Mapped
classes outside the registry's closed taxonomy are dropped.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, ClassVar, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..sources._contract import Signal
from ..stack.nlp_service import (
    NlpServiceAuthError,
    NlpServiceClient,
    NlpServiceUnavailable,
)
from ..vocabulary import ENTITY_CLASSES
from ._contract import FilterContext, FilterHealth

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NERServiceUnconfigured(RuntimeError):
    """Handler was activated without an :class:`NlpServiceClient` binding.

    Surfaces at activation time so misconfigured descriptors fail fast at
    bind rather than at the first signal. The runtime is expected to resolve
    the ``Property.StackRef`` and pass an instantiated client to
    :meth:`NERMultilingualHandler.on_configure`.
    """


# Backwards-compat alias for callers that imported the old exception name.
# (The old in-process variant raised :class:`NERModelMissing` when a spaCy
# model wasn't installed; that failure mode no longer exists.) Old import
# sites still type-check.
class NERModelMissing(NERServiceUnconfigured):
    """Deprecated alias kept for import-site compatibility."""

    def __init__(self, model_name: str = "", language: str = "") -> None:
        self.model_name = model_name
        self.language = language
        super().__init__(
            "NERModelMissing is deprecated — the multilingual NER filter "
            "now calls the hosted /extract endpoint. Wire a NlpServiceClient "
            "via Property.StackRef('nlp.local.legba_models') instead."
        )


# ---------------------------------------------------------------------------
# Entity-class heuristics
#
# The /extract contract returns free-text subjects + objects without a typed
# label carried through (unlike spaCy's PER / ORG / LOC). We map each candidate
# string to one of the
# closed 9-value Legba ``entity_class`` set using a tiered heuristic:
#
#   1. Predicate-driven: ``place of birth``, ``capital of``, ``located in``
#      → the object slot is a location; predicate ``born in country`` →
#      object is a country.
#   2. Cue-word scan: tokens like ``Inc``, ``Corp``, ``Ltd``, ``LLC`` →
#      corporation; ``University``, ``Ministry``, ``Department``, ``Agency``
#      → organization; ``President``, ``Mr.``, ``Dr.``, two-or-more capitalised
#      tokens with no cue → person.
#   3. Fallback: ``entity`` (the generic bucket — always in the taxonomy).
#
# Operators can override via :attr:`NERMultilingualConfig.taxonomy_map`
# which keys on cue tokens, not spaCy labels.
# ---------------------------------------------------------------------------


_LOCATION_PREDICATES: frozenset[str] = frozenset({
    "place of birth", "place of death", "located in", "capital of",
    "headquarters location", "country of citizenship", "located in the administrative territorial entity",
    "country", "place of burial", "residence", "country of origin",
})

_COUNTRY_PREDICATES: frozenset[str] = frozenset({
    "country of citizenship", "country", "country of origin",
})

# Predicates where the SUBJECT is a person: e.g. "(X, spouse, Y)" → X is a
# person.
_PERSON_SUBJECT_PREDICATES: frozenset[str] = frozenset({
    "spouse", "father", "mother", "child", "sibling",
    "head of government", "head of state",
    # When the subject is an employee/student: "(Alice, employer, ACME)"
    # or "(Alice, member of, Party)" → Alice is a person.
    "employer", "member of", "educated at", "occupation",
})

# Predicates where the OBJECT is an organisation: e.g.
# "(Alice, employer, ACME)" → ACME is an organisation.
_ORG_OBJECT_PREDICATES: frozenset[str] = frozenset({
    "employer", "subsidiary", "parent organization",
    "owned by", "operator", "manufacturer", "publisher",
})

# Token cue lists. Compared case-insensitively against whole-token matches.
_CORPORATION_CUES: frozenset[str] = frozenset({
    "inc", "inc.", "corp", "corp.", "ltd", "ltd.", "llc",
    "plc", "ag", "sa", "co", "co.", "gmbh", "spa", "s.p.a.",
})
_ORGANIZATION_CUES: frozenset[str] = frozenset({
    "university", "college", "ministry", "department", "agency",
    "council", "committee", "bureau", "office", "school", "institute",
    "foundation", "association", "society", "league", "alliance",
    "party", "parliament", "congress", "senate", "court", "police",
})
_PERSON_CUES: frozenset[str] = frozenset({
    "mr", "mr.", "mrs", "mrs.", "ms", "ms.", "dr", "dr.", "prof", "prof.",
    "president", "minister", "ceo", "director", "general", "senator",
    "governor", "ambassador",
})
_EVENT_CUES: frozenset[str] = frozenset({
    "war", "battle", "summit", "conference", "election", "olympics",
    "tournament", "festival", "uprising", "revolution", "treaty",
    # Hazard / weather / geophysical event words — the geophysical feeds
    # (USGS / NWS / NASA EONET) dominate ingest, and "Severe Thunderstorm
    # Warning" / "M6.2 Earthquake" were landing in `person` via the two-
    # title-cased-tokens fallback. Classifying them as `event` is correct.
    "earthquake", "quake", "aftershock", "tsunami", "eruption", "volcano",
    "storm", "thunderstorm", "hurricane", "typhoon", "cyclone", "tornado",
    "flood", "flooding", "wildfire", "drought", "blizzard", "heatwave",
    "landslide", "avalanche", "warning", "watch", "advisory", "outbreak",
    "epidemic", "pandemic", "wildfires", "floods", "storms",
})
_SOFTWARE_CUES: frozenset[str] = frozenset({
    "linux", "windows", "android", "ios", "kubernetes", "docker",
    "python", "tensorflow", "pytorch",
})

# ---------------------------------------------------------------------------
# Non-entity rejection
#
# The /extract relation triples routinely include endpoints that are
# quantities, dates, clock
# times, percentages, or bare numbers ("4.5", "June 2026", "50%", "3 days",
# "9th"). Those are NER noise, not named entities — they were polluting the
# entity list (and miscategorising as `person`/`entity`). We drop a candidate
# only when it carries NO nominal word at all, so "Hurricane Helene",
# "Boeing 737", "M23", and "COVID-19" still pass.
# ---------------------------------------------------------------------------

# Words that denote a unit / quantity / magnitude rather than a name.
_UNIT_WORDS: frozenset[str] = frozenset({
    "percent", "percentage", "pct",
    "year", "years", "yr", "yrs", "month", "months", "week", "weeks",
    "day", "days", "hour", "hours", "hr", "hrs", "minute", "minutes",
    "min", "mins", "second", "seconds", "sec", "secs", "decade", "decades",
    "km", "kilometre", "kilometres", "kilometer", "kilometers",
    "mile", "miles", "metre", "metres", "meter", "meters",
    "foot", "feet", "ft", "inch", "inches", "yard", "yards",
    "kg", "kilogram", "kilograms", "gram", "grams", "tonne", "tonnes",
    "ton", "tons", "pound", "pounds", "lb", "lbs",
    "magnitude", "richter", "degree", "degrees", "celsius", "fahrenheit",
    "dollar", "dollars", "euro", "euros", "cent", "cents", "usd", "eur",
    "million", "billion", "trillion", "thousand", "hundred", "dozen",
    "kph", "mph", "knot", "knots",
})

_MONTHS: frozenset[str] = frozenset({
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
    "oct", "nov", "dec",
})
_WEEKDAYS: frozenset[str] = frozenset({
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "mon", "tue", "tues", "wed", "thu", "thur", "thurs",
    "fri", "sat", "sun",
})
_TIMEZONES: frozenset[str] = frozenset({
    "utc", "gmt", "est", "edt", "pst", "pdt", "cst", "cdt", "cet", "bst",
})

_ORDINAL_RE = re.compile(r"^\d+(?:st|nd|rd|th)$", re.IGNORECASE)
_CLOCK_RE = re.compile(r"^\d{1,2}(?::\d{2}){0,2}\s*(?:am|pm|a\.m\.|p\.m\.)?$", re.IGNORECASE)
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)  # any unicode letter
_STRIP_CHARS = " \t\n\r\"'`.,;:!?()[]{}<>«»“”‘’"


def _is_nominal_word(tok: str) -> bool:
    """True when ``tok`` reads as a name-bearing word (not a unit / date /
    number / clock / ordinal)."""
    w = tok.strip(_STRIP_CHARS + "%")
    if not w:
        return False
    lw = w.lower()
    if lw in _UNIT_WORDS or lw in _MONTHS or lw in _WEEKDAYS or lw in _TIMEZONES:
        return False
    if _ORDINAL_RE.match(w) or _CLOCK_RE.match(w):
        return False
    return bool(_LETTER_RE.search(w))


def _is_nonentity_candidate(text: str) -> bool:
    """True when a triple endpoint is a quantity / date / number / unit rather
    than a named entity. Conservative: rejects only when NO token is nominal."""
    t = text.strip().strip(_STRIP_CHARS)
    if len(t) < 2:
        return True
    if not _LETTER_RE.search(t):  # all digits / punctuation / symbols
        return True
    tokens = t.split()
    # Bare date token(s): every token is a month/weekday/number → not a name.
    if all(
        tok.lower().strip(_STRIP_CHARS) in _MONTHS
        or tok.lower().strip(_STRIP_CHARS) in _WEEKDAYS
        or not _LETTER_RE.search(tok)
        for tok in tokens
    ):
        return True
    # Keep if any token is genuinely nominal; else it's all units/numbers/dates.
    return not any(_is_nominal_word(tok) for tok in tokens)


def _classify_entity_text(
    text: str,
    *,
    predicate: str = "",
    slot: str = "subject",
    overrides: Mapping[str, str] | None = None,
) -> str:
    """Map a triple subject/object string to a Legba ``entity_class``.

    ``slot`` is ``"subject"`` or ``"object"`` — used to apply predicate
    heuristics to the object slot only (e.g. ``place of birth`` → the
    *object* is the location, not the subject).
    """
    t = text.strip()
    if not t:
        return "entity"
    lo_pred = (predicate or "").lower().strip()
    tokens = t.split()
    lower_tokens = {tok.lower().rstrip(",;:") for tok in tokens}

    # Operator override — exact lower-cased match against any token.
    if overrides:
        for cue, cls in overrides.items():
            if cue.lower() in lower_tokens:
                return cls

    # Predicate-driven mapping.
    if slot == "object":
        if lo_pred in _COUNTRY_PREDICATES:
            return "country"
        if lo_pred in _LOCATION_PREDICATES:
            return "location"
        if lo_pred in _ORG_OBJECT_PREDICATES:
            return "organization"
    if slot == "subject":
        if lo_pred in _PERSON_SUBJECT_PREDICATES:
            return "person"

    # Cue-token scan.
    if lower_tokens & _CORPORATION_CUES:
        return "corporation"
    if lower_tokens & _ORGANIZATION_CUES:
        return "organization"
    if lower_tokens & _PERSON_CUES:
        return "person"
    if lower_tokens & _EVENT_CUES:
        return "event"
    if lower_tokens & _SOFTWARE_CUES:
        return "software"

    # Heuristic: multi-token title-cased name with no cues → person
    # (two capitalised tokens is most often a first+last name).
    title_tokens = [tok for tok in tokens if tok[:1].isupper()]
    if len(title_tokens) >= 2:
        return "person"
    # Single token (capitalised or not) with no cues → "entity" — the
    # generic bucket. Geocoding (L-153) and other downstream enrichments
    # can refine if appropriate.
    return "entity"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class NERMultilingualConfig(BaseModel):
    """Pydantic config schema for :class:`NERMultilingualHandler` (HTTP variant).

    The handler now binds to an :class:`NlpServiceClient` via
    ``Property.StackRef("nlp.local.legba_models")`` — the runtime
    resolves the StackRef at configure-time and injects the client.

    Config fields here are descriptor-side toggles only.
    """

    model_config = ConfigDict(extra="forbid")

    # Languages are accepted for descriptor-side documentation but the
    # hosted /extract endpoint operates language-agnostically (GLiREL +
    # multilingual spaCy NER). Kept so existing descriptors don't break.
    languages: list[str] = Field(
        default_factory=lambda: ["en", "xx"],
        description=(
            "Operator-declared languages this filter is expected to see. "
            "Informative only — the hosted /extract endpoint is language-"
            "agnostic; downstream language-detect (L-150) sets payload.language."
        ),
    )
    default_language: str = Field(
        default="xx",
        description="Fallback language code stamped on entities when no signal hint is set.",
    )
    entity_taxonomy: str = Field(
        default="legba_v1",
        description="Identifier of the entity_class taxonomy this handler emits into.",
    )
    taxonomy_map: dict[str, str] | None = Field(
        default=None,
        description=(
            "Operator override mapping cue-tokens (lower-cased) to entity_class. "
            "E.g. {'mosque': 'location'}. When None the bundled heuristics apply."
        ),
    )
    min_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Drop entities below this confidence floor. The hosted /extract "
            "contract does not surface per-span entity confidence; the handler "
            "synthesises 1.0 for every triple endpoint."
        ),
    )
    max_text_chars: int = Field(
        default=2000,
        ge=1,
        description=(
            "Truncate input text to this length before posting to /extract. "
            "Default 2000 matches the legacy client; the server truncates "
            "further to its 512-token model limit."
        ),
    )
    text_fields: list[str] = Field(
        default_factory=lambda: ["title", "body", "summary", "raw_body"],
        description="Ordered list of payload fields to concatenate as /extract input.",
    )

    @field_validator("languages")
    @classmethod
    def _normalise_languages(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("at least one language must be configured")
        out: list[str] = []
        for code in v:
            if not isinstance(code, str) or not code:
                raise ValueError(f"language code must be non-empty str, got {code!r}")
            out.append(code.lower())
        return out

    @field_validator("default_language")
    @classmethod
    def _normalise_default(cls, v: str) -> str:
        return v.lower()


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NERMultilingualHandler:
    """Multilingual NER filter (HTTP client variant).

    Wire-up:

      * Constructor accepts the parsed :class:`NERMultilingualConfig`.
        Tests inject an :class:`NlpServiceClient` directly. In production
        the runtime resolves the descriptor's StackRef and calls
        :meth:`on_configure` with the client.
      * ``on_configure`` issues a single ``/health`` probe and records the
        result; the handler proceeds even on degraded health so a flaky
        service doesn't block bring-up.
      * ``transform`` posts to ``/extract`` and converts the triples into
        the contractual ``entities`` list.

    The ``vocabulary_values`` argument is the live ``entity_class`` set
    from the registry's :class:`VocabularyCache` (L-102 §5). When ``None``
    the handler uses the seed set (:data:`legba.data.vocabulary.ENTITY_CLASSES`).
    """

    # --- L-102 §1 class-vars ------------------------------------------------
    kind: ClassVar[str] = "ner_multilingual"
    family: ClassVar[str] = "filter"
    schema_version: ClassVar[str] = "legba/filter.ner_multilingual/1-0-0"
    config_schema: ClassVar[type[BaseModel]] = NERMultilingualConfig
    handler_version: ClassVar[str] = "0.2.0"  # HTTP-client variant
    idempotent: ClassVar[bool] = True

    # Composition contract (L-102 §3).
    output_contract: ClassVar[Mapping[str, type]] = {
        "payload.entities": list,
    }

    def __init__(
        self,
        config: NERMultilingualConfig,
        *,
        vocabulary_values: set[str] | None = None,
        nlp_client: NlpServiceClient | None = None,
    ) -> None:
        self._config = config
        self._vocabulary: set[str] = (
            set(vocabulary_values)
            if vocabulary_values is not None
            else set(ENTITY_CLASSES)
        )
        self._client = nlp_client
        # Health-state counters.
        self._signals_in = 0
        self._signals_out = 0
        self._signals_dropped = 0
        self._signals_failed = 0
        self._last_error: str | None = None
        self._last_success_at: datetime | None = None
        self._activated = False
        self._service_healthy: bool | None = None

        if config.default_language not in config.languages:
            raise ValueError(
                f"default_language={config.default_language!r} not in "
                f"languages={config.languages!r}"
            )

    # ------------------------------------------------------------------- props

    @property
    def config(self) -> NERMultilingualConfig:
        return self._config

    @property
    def loaded_languages(self) -> list[str]:
        """Compatibility shim: the in-process variant exposed loaded
        spaCy pipelines. The HTTP variant has no per-language state; we
        return the configured languages when the handler is activated and
        the service is reachable, ``[]`` otherwise."""
        if self._activated and self._service_healthy is not False:
            return sorted(self._config.languages)
        return []

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
        """Bind the NLP client + probe the service.

        ``nlp_client`` overrides the constructor-supplied one. The
        runtime calls this method with the resolved StackRef → live
        :class:`NlpServiceClient` instance.
        """
        if nlp_client is not None:
            self._client = nlp_client
        if self._client is None:
            raise NERServiceUnconfigured(
                "ner_multilingual requires an NlpServiceClient; wire it via "
                "Property.StackRef('nlp.local.legba_models') or pass "
                "nlp_client= in the constructor (tests)."
            )

    async def on_activate(self, ctx: FilterContext | None = None) -> None:
        """Probe the service. Records degraded state on probe failure but
        does not raise — transient failures during bring-up shouldn't
        block the runtime from activating the rest of the pipeline.
        """
        if self._client is None:
            raise NERServiceUnconfigured(
                "ner_multilingual activated without an NlpServiceClient"
            )
        try:
            await self._client.health()
            self._service_healthy = True
            self._last_success_at = datetime.now(tz=timezone.utc)
        except NlpServiceAuthError as exc:
            self._last_error = f"auth: {exc!s}"
            self._service_healthy = False
            if ctx is not None:
                ctx.logger.warning(
                    "ner_multilingual.health auth_failure target_id=%s err=%s",
                    ctx.target_id, exc,
                )
        except NlpServiceUnavailable as exc:
            self._last_error = f"unavailable: {exc!s}"
            self._service_healthy = False
            if ctx is not None:
                ctx.logger.warning(
                    "ner_multilingual.health unreachable target_id=%s err=%s",
                    ctx.target_id, exc,
                )
        self._activated = True

    async def on_pause(self, ctx: FilterContext | None = None) -> None:
        self._activated = False

    async def on_resume(self, ctx: FilterContext | None = None) -> None:
        await self.on_activate(ctx)

    async def on_retire(self, ctx: FilterContext | None = None) -> None:
        self._activated = False
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:                                   # pragma: no cover
                pass

    # ------------------------------------------------------------------ transform

    async def transform(
        self,
        signal: Signal,
        ctx: FilterContext,
    ) -> Signal | None:
        """Annotate ``signal.payload['entities']`` via the hosted /extract
        endpoint. Never returns ``None`` — NER is enrichment-only.

        Graceful degradation: on service failure the signal flows through
        with ``entities=[]`` and the handler's health flips to ``degraded``.
        """
        self._signals_in += 1
        text = self._extract_text(signal)
        if not text:
            self._signals_dropped += 1
            return self._annotate(signal, entities=[], language=None)

        language = self._pick_language(signal)
        truncated = text[: self._config.max_text_chars]

        if self._client is None:
            self._last_error = "no nlp client bound"
            ctx.logger.error(
                "ner_multilingual.no_client target_id=%s", ctx.target_id,
            )
            self._signals_failed += 1
            return self._annotate(signal, entities=[], language=None)

        try:
            data = await self._client.extract(truncated)
        except NlpServiceAuthError as exc:
            self._last_error = f"auth: {exc!s}"
            self._signals_failed += 1
            self._service_healthy = False
            ctx.logger.warning(
                "ner_multilingual.auth_error signal_id=%s err=%s",
                signal.signal_id, exc,
            )
            return self._annotate(signal, entities=[], language=language)
        except NlpServiceUnavailable as exc:
            self._last_error = f"unavailable: {exc!s}"
            self._signals_failed += 1
            self._service_healthy = False
            ctx.logger.debug(
                "ner_multilingual.unavailable signal_id=%s err=%s",
                signal.signal_id, exc,
            )
            return self._annotate(signal, entities=[], language=language)
        except Exception as exc:                                # pragma: no cover
            self._last_error = f"extract: {exc!s}"
            self._signals_failed += 1
            ctx.logger.warning(
                "ner_multilingual.extract_failed signal_id=%s err=%s",
                signal.signal_id, exc,
            )
            return self._annotate(signal, entities=[], language=language)

        self._service_healthy = True
        self._last_error = None

        triples = data.get("triples", []) if isinstance(data, dict) else []
        emitted = self._triples_to_entities(triples, text=truncated, language=language)

        self._signals_out += 1
        self._last_success_at = datetime.now(tz=timezone.utc)
        return self._annotate(signal, entities=emitted, language=language)

    # ----------------------------------------------------------- health_check

    async def health_check(self, ctx: FilterContext) -> FilterHealth:
        """Synthesise health from the in-process counters + service probe state.

        ``healthy`` when the last activation probe succeeded and no errors
        have been recorded; ``degraded`` when there is a recent error or
        the service probe failed; ``unhealthy`` when no client is bound.
        """
        if self._client is None:
            return FilterHealth(
                state="unhealthy",
                last_error=self._last_error or "no client bound",
                signals_in_24h=self._signals_in,
                signals_out_24h=self._signals_out,
                signals_dropped_24h=self._signals_dropped,
                detail={
                    "service_bound": False,
                    "languages_configured": self._config.languages,
                    "languages_loaded": [],
                },
            )
        if not self._activated:
            return FilterHealth(
                state="unhealthy",
                last_error=self._last_error or "not activated",
                signals_in_24h=self._signals_in,
                signals_out_24h=self._signals_out,
                signals_dropped_24h=self._signals_dropped,
                detail={
                    "service_bound": True,
                    "activated": False,
                    "languages_configured": self._config.languages,
                    "languages_loaded": [],
                },
            )
        # Activated with a bound client.
        if self._service_healthy is False or self._last_error:
            state = "degraded"
        else:
            state = "healthy"
        return FilterHealth(
            state=state,
            last_success_at=self._last_success_at,
            last_error=self._last_error,
            signals_in_24h=self._signals_in,
            signals_out_24h=self._signals_out,
            signals_dropped_24h=self._signals_dropped,
            detail={
                "service_bound": True,
                "activated": True,
                "service_healthy": self._service_healthy,
                "languages_configured": self._config.languages,
                "languages_loaded": self.loaded_languages,
                "signals_failed": self._signals_failed,
                "vocabulary_size": len(self._vocabulary),
            },
        )

    # ------------------------------------------------------------- internals

    def _pick_language(self, signal: Signal) -> str:
        """Resolve the language code for this signal.

        Preference order:
          1. ``signal.payload['language']`` — set by L-150 language_detect.
          2. ``signal.language_hint`` — set by some source handlers.
          3. ``self._config.default_language``.

        Normalised to its first two letters (``en-US`` → ``en``).
        """
        for src in (
            signal.payload.get("language") if isinstance(signal.payload, dict) else None,
            signal.language_hint,
        ):
            if isinstance(src, str) and src:
                code = src.lower().split("-", 1)[0].split("_", 1)[0]
                if code:
                    return code
        return self._config.default_language

    def _extract_text(self, signal: Signal) -> str:
        """Concatenate the configured payload text fields into a single
        /extract input. Title goes first to preserve the most newsworthy
        content within the 512-token model limit.
        """
        if not isinstance(signal.payload, dict):
            return ""
        parts: list[str] = []
        seen: set[str] = set()
        for fld in self._config.text_fields:
            val = signal.payload.get(fld)
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

    def _triples_to_entities(
        self,
        triples: list[dict[str, Any]],
        *,
        text: str,
        language: str,
    ) -> list[dict[str, Any]]:
        """Convert GLiREL relation triples to the contractual entity list.

        Each triple yields up to two entity candidates (subject + object).
        Duplicate-text candidates are de-duplicated, preserving the first
        occurrence (which carries its predicate context).
        """
        if not triples:
            return []
        emitted: list[dict[str, Any]] = []
        seen: set[str] = set()
        text_lower = text.lower()
        overrides = self._config.taxonomy_map
        min_conf = self._config.min_confidence

        for triple in triples:
            if not isinstance(triple, dict):
                continue
            subj = str(triple.get("subject", "")).strip()
            obj = str(triple.get("object", "")).strip()
            pred = str(triple.get("predicate", "")).strip()

            for slot, candidate in (("subject", subj), ("object", obj)):
                if not candidate:
                    continue
                # Drop quantity / date / number / unit endpoints — these are
                # NER noise, not named entities (the entity list was filling
                # with bare numbers and dates).
                if _is_nonentity_candidate(candidate):
                    continue
                # Dedup on the candidate text alone — the first occurrence
                # of an entity (along with its predicate context) wins.
                # Same entity appearing as both subject + object across
                # multiple triples is emitted once.
                key = candidate.lower()
                if key in seen:
                    continue
                seen.add(key)

                cls = _classify_entity_text(
                    candidate, predicate=pred, slot=slot, overrides=overrides,
                )
                if cls not in self._vocabulary:
                    continue
                # Locate the span in the original text (best-effort; not
                # used as a hard correctness contract — the hosted endpoint
                # doesn't return offsets).
                start, end = _find_span(text_lower, candidate.lower())
                conf = 1.0
                if conf < min_conf:
                    continue
                emitted.append({
                    "class": cls,
                    "text": candidate,
                    "start": start,
                    "end": end,
                    "lang": language,
                    "confidence": conf,
                    "predicate": pred,
                })
        return emitted

    def _annotate(
        self,
        signal: Signal,
        *,
        entities: list[dict[str, Any]],
        language: str | None,
    ) -> Signal:
        """Return a copy of ``signal`` with ``payload['entities']`` set."""
        new_payload = dict(signal.payload) if isinstance(signal.payload, dict) else {}
        new_payload["entities"] = entities
        new_payload["ner_language"] = language
        new_payload["entities_hash"] = _entities_hash(entities)
        return signal.model_copy(update={"payload": new_payload})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_span(text_lower: str, needle_lower: str) -> tuple[int, int]:
    """Best-effort substring locate. Returns ``(-1, -1)`` when not found —
    consumers treat that as "offset unknown". The hosted /extract endpoint
    doesn't return offsets so this is a degraded surface compared to
    spaCy."""
    if not needle_lower:
        return -1, -1
    idx = text_lower.find(needle_lower)
    if idx < 0:
        return -1, -1
    return idx, idx + len(needle_lower)


def _entities_hash(entities: list[dict[str, Any]]) -> str:
    """Stable hash over the canonical entity tuples — order-preserving."""
    h = hashlib.sha256()
    for e in entities:
        h.update(
            (
                str(e.get("class", "")) + "\x1f"
                + str(e.get("text", "")) + "\x1f"
                + str(e.get("start", "")) + "\x1f"
                + str(e.get("end", "")) + "\x1f"
                + str(e.get("lang", "")) + "\x1e"
            ).encode("utf-8")
        )
    return h.hexdigest()


__all__ = [
    "NERMultilingualConfig",
    "NERMultilingualHandler",
    "NERModelMissing",  # backwards-compat alias
    "NERServiceUnconfigured",
]
