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
    GLiREL relation pairs already on ``signal.payload["relations"]`` (from the
    upstream ``ner_multilingual`` stage); when those are absent it calls the
    hosted ``POST /extract`` endpoint itself via the injected
    ``NlpServiceClient`` (the SAME call NER makes — ``ner.py``). This is
    literally "the same pattern as the NLP filters."

    PAIRS, NOT POSITIONS (DQ R1): facts are ``(subject, predicate, value)``, so
    this stage needs to know which extracted head went with which tail. It used
    to reconstruct that from ``payload["entities"]`` — a document-wide, de-duped,
    NOT-text-ordered list where each entity keeps only a ``predicate`` label —
    by pairing members BY LIST INDEX. That invented relations wholesale
    ("Russia / founded by / Pavel Durov" from a post reading "Telegram founder
    Pavel Durov"; "Donetsk / founded by / Kiev" from "the war launched by the
    Kiev regime"; endpoints fused across unrelated bullets of one digest post).
    The extractor's real pairs are now carried on ``payload["relations"]`` and
    consumed directly. The entity route survives only for payloads written
    before that surface existed, and there it CORROBORATES rather than guesses:
    a candidate pair must be provably adjacent in the source text or it is
    dropped (:func:`_entities_to_triples`).
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

from .._entity_canon import (
    _REGION_ADJECTIVE_MAP,
    canonicalize_entity,
    is_demonym,
    is_junk_entity,
    is_known_org_surface,
    is_org_surface,
    is_place_surface,
    is_region_surface,
)
from ..provenance.writes import (
    resolve_fact_source_credibility,
    supersede_prior_facts,
)
from ..sources._contract import Signal
from ..vocabulary import normalize_predicate
from ..stack.nlp_service import (
    NlpServiceAuthError,
    NlpServiceClient,
    NlpServiceUnavailable,
)
from ._contract import FilterContext, FilterHealth
from ._fact_graph import edge_label_for_predicate, resolve_vertex_id, upsert_fact_edge
from .ner import (
    _MD_BOLD_RE,
    _MD_LINK_RE,
    _classify_entity_text,
    _is_nonentity_candidate,
)
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
#: DQ Phase 5 (facts/extraction quality) — bare PLURAL quantity nouns and
#: "half" that the singular ``_NUMBER_WORDS`` set missed, so a fragment subject/
#: value like "Thousands", "hundreds", "half" (live junk: "Thousands located in
#: South Africa", "half employed by Russian") is caught by ``_is_quantity_phrase``
#: exactly like "at least five". A single nominal token still keeps the endpoint.
_QUANTITY_NOUNS: frozenset[str] = frozenset({
    "half", "halves", "thousands", "hundreds", "dozens", "millions",
    "billions", "trillions", "scores", "loads", "tons", "lots", "plenty",
})
_QUANTITY_NONNOMINAL = (
    _NUMBER_WORDS | _ORDINAL_WORDS | _QUANTITY_QUALIFIERS | _QUANTITY_NOUNS
)
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
#: curated seed (0.95) or a deliberate analyst assertion. NOT a calibrated
#: probability — a deliberately conservative documented constant; the
#: data-quality gates (not this floor) own noise rejection. When the extractor
#: DOES provide a real per-triple score, ``_resolve_ingestion_confidence`` uses
#: that instead of this constant.
_INGESTION_DEFAULT_CONFIDENCE: float = 0.5


# ---------------------------------------------------------------------------
# D6 / D13 — adjective / demonymic-plural value gate, source-publication
# subjects, relation-DIRECTION sanity, and a sports-roster topic filter.
# ---------------------------------------------------------------------------
#
# The live audit (PLATFORM_HEALTH_RESULTS D6/D13) found the ingestion path
# laundering whole classes of garbage triples past the existing junk / quantity
# gates — categorically-wrong relations stamped as first-class facts:
#
#   * ADJECTIVE / DEMONYMIC-PLURAL endpoints — "France capital of Parisians":
#     "Parisians" is a demonymic plural (an adjective surface, not a typed
#     place/org/person). The W1 canon collapses national demonyms ("Iranian" ->
#     "Iran") but a city-demonym plural like "Parisians" has no country to
#     collapse to, so it is dropped here instead of becoming a fact value.
#   * REFLEXIVE-AFTER-CANON — "US located in US", "EU member of EU",
#     "China leader of America": after canonicalisation BOTH endpoints resolve
#     to the SAME canonical entity ("United States" == "United States"). The
#     pre-canon junk check misses these because the RAW surfaces differ; we
#     re-test self-reference on the CANONICAL forms.
#   * SOURCE-PUBLICATION SUBJECTS — "Reuters", "Al Jazeera", "BBC News" as the
#     SUBJECT of a geopolitical fact: the publisher is the messenger, not a
#     participant; a relation whose subject is the reporting outlet is a byline
#     artifact, not a world fact.
#   * INVERTED RELATIONS — "US located in New York" (a country located in a
#     city/state), "Washington capital of US" (the country as the value of a
#     capital-of relation). Direction sanity rejects the clear structural
#     inversions deterministically.
#   * SPORTS-ROSTER NOISE — the World-Cup feed flooding geopolitical extraction:
#     "Kylian Mbappe member of Iraq" (a footballer "member of" a national squad
#     read as a country), "Harry Kane member of Jude Bellingham" (person member
#     of person). A topic gate skips extraction on sports-dominated text, and a
#     triple-shape gate drops person-"member of"-country / person-"member of"-
#     person roster artifacts even when the text gate lets a mixed signal pass.

#: Demonymic / adjectival surface suffixes a CITY/REGION demonym plural takes
#: ("Parisians", "Londoners", "New Yorkers", "Texans") that the curated
#: national ``_DEMONYM_MAP`` does NOT cover (it only collapses NATIONAL
#: demonyms). A value surface that is a single title-cased token with no digits
#: ending in one of these is treated as an adjective/people-of surface — not a
#: typed entity — and dropped. Conservative: known countries + national
#: demonyms + multi-token names are exempted before this check fires.
_DEMONYMIC_PLURAL_SUFFIXES: tuple[str, ...] = (
    "ians", "eans", "ers", "ners", "ish", "ese", "ans",
)

#: Source / publication surface names that must never be the SUBJECT of an
#: ingestion fact (the reporting outlet is the messenger, not a participant).
#: Lower-cased, stripped surface forms. Conservative + curated to the live
#: feeds; an unknown outlet flows through (the resolver/critic still see it).
_SOURCE_PUBLICATION_SUBJECTS: frozenset[str] = frozenset({
    "reuters", "associated press", "ap", "afp", "agence france-presse",
    "bloomberg", "bbc", "bbc news", "cnn", "al jazeera", "aljazeera",
    "the guardian", "guardian", "the new york times", "new york times",
    "nyt", "the washington post", "washington post", "the times",
    "financial times", "ft", "the economist", "xinhua", "tass", "rt",
    "sputnik", "fox news", "msnbc", "nbc news", "abc news", "cbs news",
    "politico", "axios", "the telegraph", "telegraph", "the independent",
    "sky news", "npr", "voa", "voice of america", "deutsche welle", "dw",
    "the wall street journal", "wall street journal", "wsj", "newsweek",
    "the hill", "vox", "buzzfeed", "huffpost", "breitbart",
})

#: Sports-vocabulary tokens used by the topic/relevance gate. When a signal's
#: text is dominated by these (≥ ``_SPORTS_TEXT_MIN_HITS`` distinct hits),
#: extraction is skipped so a World-Cup roster feed does not pour player/squad
#: triples into the geopolitical substrate.
_SPORTS_TOKENS: frozenset[str] = frozenset({
    "world cup", "fifa", "uefa", "premier league", "la liga", "bundesliga",
    "serie a", "champions league", "euro 2024", "euro 2028", "qualifier",
    "qualifiers", "group stage", "knockout", "quarter-final", "quarter-finals",
    "semi-final", "semi-finals", "kickoff", "kick-off", "half-time",
    "halftime", "full-time", "midfielder", "midfield", "striker",
    "goalkeeper", "defender", "winger", "penalty", "free kick", "free-kick",
    "offside", "corner kick", "hat-trick", "hat trick", "goalscorer",
    "top scorer", "clean sheet", "matchday", "fixture", "fixtures", "lineup",
    "line-up", "starting xi", "squad", "roster", "transfer window",
    "substitute", "substitution", "injury time", "stoppage time",
    "extra time", "shootout", "national team", "footballer", "nba", "nfl",
    "mlb", "nhl", "cricket", "rugby", "formula 1", "grand prix", "olympics",
    "tournament", "playoff", "playoffs",
})
#: Minimum distinct sports-token hits before the topic gate fires.
_SPORTS_TEXT_MIN_HITS: int = 3

#: Predicates that assert organizational membership / squad inclusion. A PERSON
#: subject under one of these is a sports-roster / citizenship artifact
#: ("Kylian Mbappe member of Iraq"), and a PERSON value is nonsense
#: ("Harry Kane member of Jude Bellingham") — both dropped by the triple-shape
#: gate regardless of the text topic gate.
_MEMBERSHIP_PREDICATES: frozenset[str] = frozenset({
    "member of", "part of", "plays for", "member",
})

#: 'capital of' direction sanity — the SUBJECT must be a city/place (not a
#: country), the VALUE a country.
_CAPITAL_PREDICATES: frozenset[str] = frozenset({"capital of"})
#: 'located in' direction sanity — a COUNTRY subject located in a NON-country
#: value ("US located in New York") is the structural inversion (a sovereign
#: state is not located inside a city/state).
_LOCATED_IN_PREDICATES: frozenset[str] = frozenset({"located in", "capital of"})

#: Continents / supranational regions a COUNTRY legitimately sits inside, so
#: "France located in Europe" is NOT flagged as an inversion. Lower-cased.
#: Conservative + curated — a non-country value NOT in this set under a
#: country subject is the city/state inversion ("US located in New York").
_CONTINENTS_REGIONS: frozenset[str] = frozenset({
    "europe", "asia", "africa", "north america", "south america",
    "latin america", "central america", "oceania", "antarctica",
    "eurasia", "the americas", "americas", "middle east", "the middle east",
    "scandinavia", "the balkans", "balkans", "the caucasus", "caucasus",
    "central asia", "southeast asia", "south asia", "east asia",
    "west africa", "east africa", "north africa", "sub-saharan africa",
    "the eu", "european union", "the caribbean", "caribbean",
    "the gulf", "the levant", "the pacific", "the arctic",
})

#: Geographic-CONTAINMENT predicates whose VALUE must be a geographic container
#: (a country / continent-region / place-surface / known place). A value that is
#: a known NON-geographic ORGANISATION / brand / party ("Facebook located in
#: Instagram", "Alternative for Germany located in AfD") is a two-entity
#: inversion / acronym-self-reference artifact — you cannot be geographically
#: "located in" an organisation. Lower-cased, canonical (post-normalize).
_GEO_CONTAINMENT_PREDICATES: frozenset[str] = frozenset({
    "located in", "headquartered in", "based in", "capital of",
})

#: KNOWN non-geographic organisations / brands / political parties NER routinely
#: places as the VALUE of a geographic-containment relation (the social-media /
#: acronym-expansion artifact). Curated + lower-cased — an unknown value flows
#: through (the resolver/critic still see it). These are NEVER geographic
#: containers, so a "<x> located in <one of these>" triple is a structural
#: inversion / self-reference (the live D-class "Facebook located in Instagram"
#: and "Alternative for Germany located in AfD").
_KNOWN_NONGEO_ORGS: frozenset[str] = frozenset({
    # social / tech platforms + parent brands
    "facebook", "instagram", "whatsapp", "messenger", "threads", "meta",
    "twitter", "x", "tiktok", "youtube", "snapchat", "linkedin", "reddit",
    "pinterest", "telegram", "discord", "twitch", "google", "alphabet",
    "apple", "microsoft", "amazon", "netflix", "spotify", "uber", "airbnb",
    "paypal", "tesla", "openai", "anthropic", "nvidia", "oracle", "ibm",
    "samsung", "huawei", "tencent", "alibaba", "wechat", "weibo", "baidu",
    "bytedance",
    # political parties / movements (full + acronym) NER pairs as containment
    "afd", "alternative for germany", "spd", "cdu", "csu", "fdp",
    "republican party", "democratic party", "labour party", "conservative party",
    "fidesz", "syriza", "podemos", "vox", "rassemblement national",
    "national rally", "five star movement", "bjp", "congress party",
})


def _is_nongeo_containment_inversion(subject: str, predicate: str, value: str) -> bool:
    """True when a geographic-containment triple points at a NON-geographic
    organisation / brand / party as the container — a two-entity inversion or
    acronym-self-reference artifact.

    Catches the live D-class garbage the country-subject inversion check misses
    because neither endpoint is a country:
      * "Facebook located in Instagram" — two peer organisations, no geography;
      * "Alternative for Germany located in AfD" — full name + acronym of the
        SAME party (self-reference), neither a place.

    Deterministic + conservative. Fires ONLY when:
      * the predicate is a geographic-containment relation; AND
      * the VALUE is a KNOWN non-geographic org (the curated gazetteer) and is
        NOT itself a country / continent-region / place-surface / known place
        (so a brand that doubles as a place name can't be mis-dropped).

    Relies on the curated gazetteers (NOT the noisy NER class), so a legit
    "BBC located in United Kingdom" / "Eiffel Tower located in Paris" passes.
    """
    pred = normalize_predicate((predicate or "").strip().lower())
    if pred not in _GEO_CONTAINMENT_PREDICATES:
        return False
    val_norm = " ".join(str(value or "").split()).strip()
    val_low = val_norm.lower()
    if val_low not in _KNOWN_NONGEO_ORGS:
        return False
    # Defensive: never drop when the value IS a recognised geographic surface
    # (a brand that collides with a place name). The curated set excludes these
    # already, but the gazetteers are authoritative.
    _, val_cls = canonicalize_entity(val_norm, "entity")
    if val_cls in ("country", "location"):
        return False
    if val_low in _CONTINENTS_REGIONS or is_place_surface(val_norm):
        return False
    return True


def _is_adjective_or_demonymic_value(value: str) -> bool:
    """True when ``value`` is an adjectival / demonymic-PLURAL surface that is
    not a typed entity (e.g. "Parisians", "Londoners", "Texans").

    The W1 canon already collapses NATIONAL demonyms ("Iranian" -> "Iran"), so
    by the time this runs a surviving demonym surface is one with no country to
    collapse to — a city/region people-of plural. Conservative:
      * a national demonym (collapses via canon) is NOT flagged;
      * only a SINGLE title-cased token with no digits whose lowercase form
        ends in a demonymic-plural suffix is flagged.
    """
    s = " ".join(str(value or "").split()).strip()
    if not s or " " in s:
        return False  # multi-token names are not bare adjectives
    if any(ch.isdigit() for ch in s):
        return False
    if not s[:1].isupper():
        return False
    if is_demonym(s):
        return False  # a national demonym collapses via canon, not dropped here
    low = s.lower()
    return any(low.endswith(suf) and len(low) > len(suf) + 1
               for suf in _DEMONYMIC_PLURAL_SUFFIXES)


#: STRUCTURAL org relations that survive even when the SUBJECT is a known
#: outlet name. The source-publication gate targets BYLINE NOISE ("Reuters
#: reports that X") — a reporting/content relation whose subject is the
#: messenger. But an outlet is ALSO a real organization, and a legitimate
#: structural org fact about it ("BBC operates in United Kingdom",
#: "Reuters headquartered in London") is a genuine world fact, not a byline.
#: When the predicate is one of these structural relations the outlet-subject
#: drop is SUPPRESSED so the legit org fact survives. Lower-cased, canonical
#: (matched after ``normalize_predicate``).
_SOURCE_PUBLICATION_STRUCTURAL_PREDICATES: frozenset[str] = frozenset({
    "located in", "operates in", "headquartered in", "based in",
    "headquarters location",
})


def _is_source_publication_subject(subject: str) -> bool:
    """True when ``subject`` is a reporting outlet / publication name (the
    messenger), which must never be the SUBJECT of an ingestion fact."""
    s = " ".join(str(subject or "").split()).strip().lower().strip(".")
    return bool(s) and s in _SOURCE_PUBLICATION_SUBJECTS


def _value_is_country(value: str) -> bool:
    """True when ``value`` canonicalizes to a COUNTRY (the gazetteer-backed
    typing the rest of the D6 gate uses). Used to keep the source-publication
    structural-relation exemption tight (an outlet's org-jurisdiction fact like
    "BBC operates in United Kingdom" survives; a structural relation to a
    non-country place does not)."""
    _, cls = canonicalize_entity(value, "entity")
    return cls == "country"


def _is_reflexive_after_canon(subject: str, value: str) -> bool:
    """True when subject and value canonicalize to the SAME entity.

    Catches the reflexive triples the pre-canon junk check misses because the
    RAW surfaces differ but resolve to one referent: "US located in US",
    "EU member of EU", "China leader of America" ("America" -> "United States"),
    "US located in United States".
    """
    cs, _ = canonicalize_entity(subject, "entity")
    cv, _ = canonicalize_entity(value, "entity")
    if not cs or not cv:
        return False
    return cs.casefold() == cv.casefold()


def _is_inverted_relation(subject: str, predicate: str, value: str) -> bool:
    """Deterministic relation-DIRECTION sanity on the canonical endpoints.

    Rejects the clear structural inversions the live audit flagged:

      * ``capital of`` — a COUNTRY subject ("US capital of …") is reversed (the
        capital is a CITY, the value is the country); a NON-country value
        ("… capital of Parisians") is the wrong object type. "Paris capital of
        France" passes (subject not a country, value a country).
      * ``located in`` — a COUNTRY subject located in a NON-country value
        ("US located in New York"): a sovereign state is not located inside a
        city/state. "France located in Europe" / "Texas located in US"
        (non-country subject) pass.

    Uses :func:`canonicalize_entity` (the gazetteer-backed country typing) so
    the country test is reliable regardless of the noisy NER class.
    """
    pred = normalize_predicate((predicate or "").strip().lower())
    _, subj_cls = canonicalize_entity(subject, "entity")
    _, val_cls = canonicalize_entity(value, "entity")
    subj_is_country = subj_cls == "country"
    val_is_country = val_cls == "country"

    if pred in _CAPITAL_PREDICATES:
        if subj_is_country:
            return True  # "US capital of ..." — inverted
        if not val_is_country:
            return True  # "... capital of Parisians" — value not a country
        return False
    if pred in _LOCATED_IN_PREDICATES:
        # A country inside a NON-country, NON-continent value is the inversion
        # ("US located in New York"); "France located in Europe" passes.
        val_low = " ".join(str(value or "").split()).strip().lower()
        if subj_is_country and not val_is_country \
                and val_low not in _CONTINENTS_REGIONS:
            return True
        return False
    return False


def _is_roster_triple(subject: str, predicate: str, value: str) -> bool:
    """True when a membership triple is a sports-roster / nonsense artifact.

    The World-Cup feed produces "<player> member of <national-squad>" (read as
    a country) and "<player> member of <player>". A PERSON subject under a
    membership predicate whose value is a COUNTRY (a squad read as its country)
    or another PERSON is roster noise, not a geopolitical membership.

    Uses the canon for the country test + the NER classifier for the person
    test (conservative: only fires when the subject clearly classifies person).
    """
    pred = normalize_predicate((predicate or "").strip().lower())
    if pred not in _MEMBERSHIP_PREDICATES:
        return False
    subj_cls = _classify_entity_text(subject, predicate=pred, slot="subject")
    if subj_cls != "person":
        return False
    _, val_canon_cls = canonicalize_entity(value, "entity")
    if val_canon_cls == "country":
        return True  # "Kylian Mbappe member of Iraq" — squad-as-country
    # F1 (2026-07-06 adversarial review) — an ORGANIZATION value is a legit
    # membership target (an IGO / alliance / bloc: "France member of European
    # Union", "Nigeria member of African Union", "South Korea member of United
    # Nations", "Brazil member of World Trade Organization"), NEVER a roster
    # artifact. Exempt it BEFORE the value-person drop, because the NER
    # title-token heuristic mis-types a multi-word org name as person and would
    # otherwise DELETE the real membership fact. Uses the SAME gazetteer guard
    # as the M1 person-object gate (canon organization typing, which is
    # authoritative over the noisy NER class).
    if (val_canon_cls == "organization"
            or is_org_surface(value)
            or is_known_org_surface(value)):
        return False
    val_cls = _classify_entity_text(value, predicate=pred, slot="object")
    if val_cls == "person":
        return True  # "Harry Kane member of Jude Bellingham" — person/person
    return False


#: DQ Phase 5 — a tokenizer possessive artifact: an entity surface that ends in
#: a SPACE followed by an apostrophe-s ("FRANCE 24 's", "Timor - Leste 's",
#: "Donald Trump 's"). NER split a possessive into a separate, malformed entity
#: surface. A legitimate name NEVER ends in " 's" (the SPACE before the clitic is
#: the artifact), so this is a mechanical, zero-false-positive drop.
_POSSESSIVE_FRAGMENT_RE = re.compile(r"\s['’]s$")


def _is_possessive_fragment(surface: str) -> bool:
    """True when ``surface`` is a trailing spaced-possessive tokenizer artifact
    (" 's" at the end). Conservative: requires the SPACE before the clitic, so a
    legitimate possessive glued to the name ("South Korea's") is NOT flagged."""
    return bool(_POSSESSIVE_FRAGMENT_RE.search(str(surface or "")))


#: DQ Phase 5 — employment relations whose SUBJECT must be a person/org, never a
#: sovereign state. A COUNTRY subject under one of these is an inverted / nonsense
#: relation ("Germany employed by Nagelsmann", "Venezuela employed by <byline>").
#: Uses the reliable gazetteer country test (NOT the noisy NER class).
_EMPLOYMENT_PREDICATES: frozenset[str] = frozenset({
    "employed by", "spokesperson for",
})


# ---------------------------------------------------------------------------
# CW-6 — CAPITAL-AS-GOVERNMENT METONYMY
# ---------------------------------------------------------------------------
#
# News writes governments as their capitals: "tensions between Madrid and
# Rabat", "Kyiv says", "Washington's allies". NER reads the capital as a
# LOCATION, the relation extractor reads the sentence's inter-state verb, and
# the pair lands in `facts` as a geographic claim about a CITY. Verified
# read-only on the live substrate (2026-08-03), the Madrid cluster alone:
#
#     Madrid border with Europe / France / Spain / Schengen / Ceuta / Italy
#     Madrid member of Europe / Spain / EU / European Union
#     Madrid conflict with France
#
# Every one of those is Spain's government wearing its capital's name, and
# none of them is a fact about the city. K-4 R3 then harvested them into
# contested-fact questions ("which value of 'border with' for 'madrid' is
# correct?") that scored 0/3, alongside kiev/conflict-with, kiev/spokesperson-
# for and washington/ally-of — the same phenomenon, four more capitals.
#
# The gate is PREDICATE-DRIVEN, the same shape as its D6/D13 siblings, and
# scoped hard so the city keeps its real facts: "Madrid located in Spain" and
# "Madrid capital of Spain" are untouched, because those predicates are
# precisely the ones a city legitimately takes. What is dropped are relations
# only a STATE has.

#: Capital cities (and a few seats of government) that routinely stand in for
#: their state in news prose. Curated + lower-cased, the module's established
#: gazetteer pattern — an unlisted city flows through untouched.
#:
#: CITY-STATES ARE DELIBERATELY ABSENT (Singapore, Monaco, Vatican City,
#: Luxembourg City): there the city IS the state, so its inter-state relations
#: are real facts and dropping them would be the guard inventing a metonymy.
_GOVERNMENT_METONYM_SUBJECTS: frozenset[str] = frozenset({
    # Europe
    "madrid", "paris", "london", "berlin", "rome", "moscow", "kyiv", "kiev",
    "brussels", "the hague", "warsaw", "prague", "budapest", "vienna",
    "bern", "berne", "stockholm", "oslo", "copenhagen", "helsinki", "dublin",
    "lisbon", "athens", "sofia", "bucharest", "belgrade", "zagreb",
    "sarajevo", "skopje", "tirana", "minsk", "chisinau", "kishinev",
    "ljubljana", "bratislava", "riga", "vilnius", "tallinn", "reykjavik",
    "nicosia", "valletta", "podgorica", "pristina",
    # Americas
    "washington", "ottawa", "mexico city", "brasilia", "brasília",
    "buenos aires", "santiago", "lima", "bogota", "bogotá", "caracas",
    "quito", "la paz", "asuncion", "asunción", "montevideo", "havana",
    "managua", "san salvador", "tegucigalpa", "panama city", "port-au-prince",
    # MENA
    "tehran", "ankara", "cairo", "riyadh", "doha", "abu dhabi", "kuwait city",
    "manama", "muscat", "amman", "beirut", "damascus", "baghdad", "sanaa",
    "sana'a", "tripoli", "tunis", "algiers", "rabat", "khartoum",
    "jerusalem", "ramallah", "nouakchott",
    # Africa
    "addis ababa", "nairobi", "kampala", "kigali", "dar es salaam", "dodoma",
    "lusaka", "harare", "pretoria", "abuja", "accra", "dakar", "bamako",
    "ouagadougou", "niamey", "n'djamena", "conakry", "freetown", "monrovia",
    "abidjan", "yaounde", "yaoundé", "kinshasa", "brazzaville", "luanda",
    "maputo", "mogadishu", "asmara", "djibouti city", "juba",
    # Asia-Pacific
    "beijing", "peking", "tokyo", "seoul", "pyongyang", "new delhi", "delhi",
    "islamabad", "kabul", "dhaka", "colombo", "kathmandu", "thimphu",
    "naypyidaw", "bangkok", "hanoi", "phnom penh", "vientiane",
    "kuala lumpur", "jakarta", "manila", "canberra", "wellington",
    "ulaanbaatar", "taipei", "astana", "nur-sultan", "tashkent", "bishkek",
    "dushanbe", "ashgabat", "baku", "yerevan", "tbilisi",
    # seats of government that are metonyms in their own right
    "the kremlin", "kremlin", "the white house", "white house",
    "downing street", "10 downing street", "the elysee", "elysee",
    "the pentagon", "pentagon", "whitehall", "capitol hill",
})

#: Relations only a STATE has. A capital city does not sign treaties, join
#: unions, sanction anyone, take an ally or fight a war — so under one of
#: these the subject is unambiguously its government, and the triple is a
#: mis-subjected claim rather than a fact about a place.
#:
#: CONTAINMENT AND LOCATION PREDICATES ARE DELIBERATELY ABSENT — "capital of",
#: "located in", "part of", "headquartered in", "based in". Those are the
#: relations a city genuinely takes, and they are exactly the legitimate
#: Madrid facts on the live substrate ("Madrid located in Spain", "Madrid
#: capital of Spain"). A wrong VALUE under one of them ("Madrid capital of
#: France") is a direction/truth defect for a different gate; it is not
#: metonymy, and widening this set to catch it would take the real city facts
#: with it.
_STATE_ONLY_PREDICATES: frozenset[str] = frozenset({
    "border with", "borders", "neighbor of", "neighbour of",
    "member of",
    "conflict with", "at war with", "war with",
    "ally of", "allied with", "opponent of", "hostile toward",
    "sanctioned by", "signed agreement with", "diplomatic relations with",
    "recognizes", "recognises", "annexed", "occupies", "claims",
    "spokesperson for",
})


def _is_capital_metonymy(subject: str, predicate: str, value: str) -> bool:
    """True when a CAPITAL standing in for its government is being minted as
    a geographic fact ("Madrid border with France", "Kyiv conflict with X").

    Deterministic, predicate-driven and conservative. Fires ONLY when:

      * the SUBJECT is a curated capital / seat of government (city-states
        excluded — there the city IS the state); AND
      * the predicate is a relation only a STATE has (containment and location
        predicates are absent by design, so the city's real facts survive);
        AND
      * subject and value are not the same referent (that reflexive case is
        already owned by :func:`_is_reflexive_after_canon`, which has a better
        reason for it).
    """
    subj = " ".join(str(subject or "").split()).strip().lower()
    if subj not in _GOVERNMENT_METONYM_SUBJECTS:
        return False
    pred = normalize_predicate((predicate or "").strip().lower())
    if pred not in _STATE_ONLY_PREDICATES:
        return False
    return bool(str(value or "").strip())


def _is_employment_country_subject(subject: str, predicate: str) -> bool:
    """True when an employment-relation subject canonicalizes to a COUNTRY — a
    state is not "employed by" / a "spokesperson for" anyone (the inverted
    employment artifact). Gazetteer-backed so a person/org subject flows
    through."""
    pred = normalize_predicate((predicate or "").strip().lower())
    if pred not in _EMPLOYMENT_PREDICATES:
        return False
    _, subj_cls = canonicalize_entity(subject, "entity")
    return subj_cls == "country"


# ---------------------------------------------------------------------------
# DQ M1/M2/M3 (2026-07-06 fact-write audit) — predicate-argument TYPE +
# relation-DIRECTION + demonym/temporal SUBJECT gates the earlier D6 pass did
# not scope for. The live audit found the extractor still laundering:
#   * M1 — semantically absurd / direction-inverted membership ("NATO member of
#     Turkiye" inverted; "Russia member of 188,000 barrels" quantity object;
#     "Russia member of <person>" person object);
#   * M2 — demonym / relative-temporal SUBJECTS ("Chinese founded by Jin
#     Mingri", "250 years ago founded by …", "December last year operates in …");
#   * M3 — nationality-adjective VALUES that become false geographic facts
#     ("Kyiv capital of Russian", "US conflict with Iranian").
# Temporal SUBJECTS/VALUES are caught by the existing is_junk_entity loop
# (the shared canon's _is_temporal_surface is now relative-phrase aware); these
# helpers add the type/direction/demonym slice. All reuse the shared canon —
# no forked gazetteers/regexes.
# ---------------------------------------------------------------------------

#: 'member of' / 'part of' assert containment in an ORG / PLACE. The OBJECT must
#: be an org/place; a quantity/number object is already dropped by
#: is_junk_entity, so this gate owns only the PERSON-object case.
_MEMBER_PART_PREDICATES: frozenset[str] = frozenset({"member of", "part of"})

#: F2 (2026-07-06 adversarial review) — M3 demonym-VALUE normalization
#: ("Russian" -> "Russia") is SCOPED to this tight ALLOWLIST of GEO / inter-state
#: RELATIONAL predicates where a country/continent object is the natural type.
#: A demonym VALUE under a LANGUAGE / ETHNICITY / NATIONALITY predicate ("Putin
#: speaks Russian", "ethnic group: Russian", "written in Russian", "native
#: language: Chinese") is the LANGUAGE/PEOPLE, not the country, and must be left
#: intact — so an ALLOWLIST (not a denylist) is used: an unlisted predicate
#: leaves the value byte-identical, so a NEW language predicate can never
#: silently corrupt a value. Person-agentive relations ("founded by",
#: "employed by", "spokesperson for") are deliberately EXCLUDED too — their
#: object is a person/org, not the country.
_DEMONYM_VALUE_GEO_PREDICATES: frozenset[str] = frozenset({
    # geographic containment / location
    "capital of", "located in", "part of", "member of",
    "headquartered in", "based in", "operates in",
    # inter-state relations (object is naturally a country / continent)
    "conflict with", "at war with", "war with", "allied with", "ally of",
    "opponent of", "borders", "border with", "neighbor of", "neighbour of",
    "controls", "occupies", "annexed", "administers", "claims",
    "signed agreement with", "sanctioned by", "supplies to", "trades with",
    "exports to", "imports from", "diplomatic relations with",
    "recognizes", "recognises",
})


def _normalize_demonym_value(predicate: str, value: str) -> str:
    """M3: collapse a nationality-adjective / region-adjective VALUE to its
    canonical country / continent referent via the shared canon ("Russian" ->
    "Russia", "Iranian" -> "Iran", "African" -> "Africa"), so a bare adjective
    never lands as a fact value ("US conflict with Iranian" -> "… with Iran").

    SCOPED (F2) to :data:`_DEMONYM_VALUE_GEO_PREDICATES` — only a GEO / inter-
    state relational predicate normalizes; a LANGUAGE / ETHNICITY predicate
    ("Putin speaks Russian", "ethnic group Russian") leaves the value intact.
    Only a curated national demonym or region adjective is touched — every other
    value is returned UNCHANGED (this is NOT a broad canonicalization of the
    write path, which deliberately preserves raw surfaces for the resolver)."""
    pred = normalize_predicate((predicate or "").strip().lower())
    if pred not in _DEMONYM_VALUE_GEO_PREDICATES:
        return value
    s = " ".join(str(value or "").split()).strip()
    if not s:
        return value
    low = s.lower()
    if is_demonym(s) or low in _REGION_ADJECTIVE_MAP:
        canon, _ = canonicalize_entity(s, "entity")
        if canon and canon.lower() != low:
            return canon
    return value


def _is_member_part_person_object(subject: str, predicate: str, value: str) -> bool:
    """M1: 'member of' / 'part of' require an org/place OBJECT — a PERSON object
    ("Russia member of <person>") is a mis-extraction. The quantity/number
    object is already dropped by is_junk_entity; this covers the PERSON object a
    NON-person subject slips past :func:`_is_roster_triple` (which needs a
    person SUBJECT).

    GAZETTEER-GUARDED (conservative, no over-reject): a value the canon /
    gazetteers recognise as an org / country / place / region is a VALID
    containment target and is kept EVEN WHEN the noisy NER heuristic mis-types a
    multi-word org ("African Union", "European Commission") or an institution
    word as person. Only a value that is a person by the NER heuristic AND is not
    a recognised org/place is dropped (a clear person NAME like "Emmanuel
    Macron")."""
    pred = normalize_predicate((predicate or "").strip().lower())
    if pred not in _MEMBER_PART_PREDICATES:
        return False
    _, canon_cls = canonicalize_entity(value, "entity")
    if canon_cls in ("organization", "country", "location"):
        return False
    if is_org_surface(value) or is_place_surface(value) or is_region_surface(value):
        return False
    return _classify_entity_text(value, predicate=pred, slot="object") == "person"


def _is_inverted_membership(subject: str, predicate: str, value: str) -> bool:
    """M1: an ORGANIZATION is not a 'member of' a COUNTRY — the direction is
    inverted (the country is the member of the org): "NATO member of Turkiye",
    "UN member of Iran".

    Gazetteer-backed country typing + canon org typing, so a legit "Nigeria
    member of African Union" (country subject) and "EU member of WTO" (org
    value, not a country) both pass untouched. Restricted to 'member of' — a
    subdivision legitimately being 'part of' a country is left alone."""
    pred = normalize_predicate((predicate or "").strip().lower())
    if pred != "member of":
        return False
    _, subj_cls = canonicalize_entity(subject, "entity")
    if subj_cls != "organization":
        return False
    _, val_cls = canonicalize_entity(value, "entity")
    return val_cls == "country"


def _text_is_sports_dominated(text: str) -> bool:
    """Topic/relevance gate: True when ``text`` is dominated by sports
    vocabulary, so geopolitical extraction should be skipped on it.

    Counts distinct sports-token hits (multi-word / hyphenated tokens matched as
    substrings, single-word tokens matched at word boundaries). Fires only at
    ``_SPORTS_TEXT_MIN_HITS`` distinct hits so an incidental "final" / "manager"
    never trips it. Conservative — a geopolitics story that mentions one match
    is unaffected.
    """
    if not text:
        return False
    low = text.lower()
    words = set(re.findall(r"[a-z0-9]+", low))
    hits = 0
    for tok in _SPORTS_TOKENS:
        if " " in tok or "-" in tok:
            if tok in low:
                hits += 1
        elif tok in words:
            hits += 1
        if hits >= _SPORTS_TEXT_MIN_HITS:
            return True
    return False


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
    reject_sports_topic: bool = Field(
        default=True,
        description=(
            "Topic/relevance gate (D6/D13): when a signal's text is dominated "
            "by sports vocabulary (≥3 distinct sports-token hits), SKIP "
            "extraction so a World-Cup roster feed does not pour player/squad "
            "triples ('Kylian Mbappe member of Iraq') into the geopolitical "
            "substrate. Conservative — a geopolitics story mentioning one match "
            "is unaffected. ON by default; set false to disable. The "
            "per-triple sports-roster shape gate runs regardless."
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
        default_factory=lambda: ["title", "body", "summary", "raw_body", "text"],
        description=(
            "Ordered payload fields concatenated for the /extract + LLM backends. "
            "``text`` MUST stay in the set (it matches ner_multilingual's M12 "
            "field list): telegram signals carry their message body in "
            "``payload.text`` and leave title/body/summary/raw_body empty, so "
            "omitting it left this stage with NO source text on those signals — "
            "facts were still written from the upstream entities but landed with "
            "an EMPTY evidence_set.text_excerpt (the human-verification hook), "
            "the sports/topic gates never fired, and the /extract fallback was "
            "dead on the single largest slice of the feed."
        ),
    )
    pair_max_char_gap: int = Field(
        default=160, ge=1,
        description=(
            "Legacy-payload pairing bound: max characters between a candidate "
            "subject's end and its object's start before the pair is REFUSED. "
            "Only consulted when a signal predates the ``payload['relations']`` "
            "pair surface; roughly one sentence."
        ),
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
        # Legacy-payload candidate pairs REFUSED because they could not be
        # bound to one sentence (the index-pairing garbage class).
        self._triples_unbound_dropped = 0

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
        """Default backend: reuse the GLiREL relations already on the signal.

        Three routes, in strict preference order:

        1. ``payload["relations"]`` — the extractor's REAL ``(head, predicate,
           tail)`` pairs, stamped by ``ner_multilingual``. Authoritative: the
           model said these endpoints go together, so nothing is guessed.
        2. ``payload["entities"]`` — a LEGACY payload written before the pair
           surface existed. The flat entity list is grouped by predicate and
           de-duped on endpoint text, so the pairing is genuinely GONE; all we
           can do is re-bind candidates that are provably adjacent in the source
           text (:func:`_entities_to_triples`) and DROP the rest. Unbindable
           candidates are refused, not guessed.
        3. ``/extract`` — called here, the same call NER makes, when neither
           surface yields anything. This is what makes route 2's strictness
           safe: a legacy payload whose candidates all fail the binding test
           falls through to the authoritative pairs instead of to nothing.
        """
        payload = signal.payload if isinstance(signal.payload, dict) else {}
        relations = payload.get("relations")
        if isinstance(relations, list) and relations:
            triples = _relations_to_triples(relations)
            if triples:
                return triples
        entities = payload.get("entities")
        if isinstance(entities, list) and entities:
            triples, unbound = _entities_to_triples(
                entities,
                text=_offset_aligned_text(payload, self._config.text_fields),
                max_gap=self._config.pair_max_char_gap,
            )
            self._triples_unbound_dropped += unbound
            if unbound:
                ctx.logger.debug(
                    "fact_extractor.pair_unbound signal_id=%s dropped=%d kept=%d",
                    signal.signal_id, unbound, len(triples),
                )
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

    # ------------------------------------------------------------- D6/D13 gate

    @staticmethod
    def _d6_drop_reason(
        subject: str,
        predicate: str,
        value: str,
        *,
        reject_quantity_endpoints: bool = True,
    ) -> str | None:
        """Return a short reason string when a (subject, predicate, value)
        triple must be DROPPED by the D6/D13 hardened gate, else ``None``.

        Pure + deterministic + canon-aware. The order is cheapest-first; the
        returned tag is only for the drop-log. Endpoints arrive already scrubbed
        (HTML-unescaped, zero-width-stripped) and the predicate already
        normalized by the caller.

        ``reject_quantity_endpoints`` mirrors the same config flag the dedicated
        ``_is_quantity_phrase`` gate honors: when it is False the QUANTITY slice
        of the shared junk gate (spelled-out / leading-quantifier endpoints like
        "At least five") is NOT dropped here. The other junk classes
        (clock-times, residual HTML, demonym collapse, length≤2) stay always-on.
        Defaults True so the D6 pure-unit callers keep the always-on behaviour.
        """
        # Shared W1 junk gate on either RAW endpoint (clock-time / quantifier /
        # numeric / residual-HTML / length≤2). canonicalize_entity collapses
        # demonyms, so we DROP via is_junk_entity (which never flags a demonym).
        # The QUANTITY/leading-quantifier slice is gated by
        # reject_quantity_endpoints: when that flag is off, an endpoint whose
        # ONLY junk reason is a spelled-out/numeric quantity phrase ("At least
        # five") is NOT dropped here (the descriptor opted that gate off). A
        # quantity endpoint that is junk for an INDEPENDENT reason still drops.
        for endpoint in (subject, value):
            if not is_junk_entity(endpoint):
                continue
            if not reject_quantity_endpoints and _is_quantity_phrase(endpoint):
                continue  # quantity gate is off — let the quantity endpoint pass
            return "junk_entity"
        # Source/publication outlet as the SUBJECT — the messenger, not a actor.
        # Narrowed (Regression 1): the gate targets BYLINE NOISE, not legitimate
        # STRUCTURAL org facts about the outlet. An outlet IS a real organization,
        # so a structural located-in / operates-in / headquartered-in / based-in
        # relation to a real COUNTRY ("BBC operates in United Kingdom") is a
        # genuine org fact and survives; everything else with an outlet subject
        # (a reporting/content relation, or a structural relation to a non-country
        # place like "Reuters located in Gaza") is still treated as the messenger
        # and dropped. Requiring a COUNTRY value keeps the exemption tight: it
        # admits the org-jurisdiction fact without re-opening byline-shaped noise.
        if _is_source_publication_subject(subject) and not (
            predicate in _SOURCE_PUBLICATION_STRUCTURAL_PREDICATES
            and _value_is_country(value)
        ):
            return "source_publication_subject"
        # Adjective / demonymic-plural VALUE the canon could not collapse to a
        # country ("France capital of Parisians").
        if _is_adjective_or_demonymic_value(value):
            return "adjective_value"
        # Reflexive after canon ("US located in US", "EU member of EU",
        # "China leader of America").
        if _is_reflexive_after_canon(subject, value):
            return "reflexive_after_canon"
        # Inverted relation direction ("US located in New York",
        # "Washington capital of US").
        if _is_inverted_relation(subject, predicate, value):
            return "inverted_relation"
        # Two-entity inversion / acronym self-reference: a geographic-containment
        # relation pointing at a known NON-geographic org/party as the container
        # ("Facebook located in Instagram", "Alternative for Germany located in
        # AfD"). Caught here because neither endpoint is a country, so the
        # country-subject inversion check above misses it.
        if _is_nongeo_containment_inversion(subject, predicate, value):
            return "nongeo_containment_inversion"
        # DQ M2 (2026-07-06 fact audit) — a bare national demonym SUBJECT
        # ("Chinese founded by Jin Mingri", "Ukrainian conflict with Russia") is
        # a nationality adjective, not a named entity. (A demonym VALUE is
        # normalized to its country upstream in the write loop; a demonym
        # SUBJECT is a mis-extraction and dropped.) Checked before the roster
        # gate so it carries its own reason (a demonym subject can NER-classify
        # person and otherwise get tagged sports_roster_triple).
        if is_demonym(subject):
            return "demonym_subject"
        # DQ M1 — inverted membership: an org is not a 'member of' a country
        # ("NATO member of Turkiye", "UN member of Iran"). Gazetteer-backed, so
        # it deterministically owns the reason (org acronyms NER-classify person
        # and would otherwise be tagged sports_roster_triple).
        if _is_inverted_membership(subject, predicate, value):
            return "inverted_membership"
        # Sports-roster shape ("Mbappe member of Iraq" / person member of
        # person) even when the text topic gate let a mixed signal through.
        # (Kept AHEAD of the person-object gate below so a person-SUBJECT roster
        # triple keeps its established sports_roster_triple reason.)
        if _is_roster_triple(subject, predicate, value):
            return "sports_roster_triple"
        # DQ M1 — 'member of'/'part of' with a clear PERSON object that the
        # roster gate missed (its subject was not NER-classified person):
        # "Russia member of <person>". Gazetteer-guarded (a recognised org/place
        # object is never treated as a person).
        if _is_member_part_person_object(subject, predicate, value):
            return "member_part_person_object"
        # DQ Phase 5 — trailing spaced-possessive tokenizer artifact on either
        # endpoint ("FRANCE 24 's", "Donald Trump 's"): a malformed surface, not
        # an entity. Always-on (mechanical, zero false positives).
        if _is_possessive_fragment(subject) or _is_possessive_fragment(value):
            return "possessive_fragment"
        # DQ Phase 5 — NER-class inversion: a COUNTRY subject under an employment
        # relation ("Germany employed by Nagelsmann") is the inverted-employment
        # artifact. Gazetteer-backed (reliable country test).
        if _is_employment_country_subject(subject, predicate):
            return "employment_country_subject"
        # CW-6 — a CAPITAL standing in for its government ("tensions between
        # Madrid and Rabat" -> "Madrid border with ..."). Checked LAST among
        # the structural gates: it is the narrowest and the most recently
        # measured, so anything an older gate already owns keeps its
        # established reason and the drop-log stays comparable across time.
        if _is_capital_metonymy(subject, predicate, value):
            return "capital_metonymy"
        return None

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
        full_text = _concat_text(payload, cfg.text_fields)
        # Offsets on the triples index the markdown-stripped rendering the NER
        # measured against, so the per-fact excerpt is sliced from that form.
        align_text = _offset_aligned_text(payload, cfg.text_fields)
        excerpt = (align_text or full_text)[:512]
        overrides = None

        # D6/D13 topic gate: a sports-dominated signal (a World-Cup roster feed)
        # must NOT pour player/squad triples into the geopolitical substrate.
        # Conservative — fires only on text dense with sports vocabulary; a
        # geopolitics story mentioning one match is unaffected.
        if cfg.reject_sports_topic and _text_is_sports_dominated(full_text):
            ctx.logger.debug(
                "fact_extractor.sports_topic_skip signal_id=%s triples=%d",
                signal.signal_id, len(triples),
            )
            return 0

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
            # DQ M3 — normalize a nationality-adjective VALUE to its country /
            # continent lemma before the gates + dedup + write ("Russian" ->
            # "Russia"), so a bare adjective never lands as a fact value and the
            # geographic-junk class ("Kyiv capital of Russian") is at least
            # well-typed. SCOPED (F2) to geo / relational predicates — a
            # language/ethnicity predicate ("speaks Russian") is left intact.
            # Only a curated demonym / region adjective is touched.
            value = _normalize_demonym_value(predicate, value)
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
            # D6/D13 hardened gate (canon-aware). Drop, with a per-reason debug:
            #   * shared W1 junk gate on either RAW endpoint (clock-time /
            #     quantifier / numeric / residual-HTML / length≤2);
            #   * SOURCE-PUBLICATION subject (the outlet is the messenger);
            #   * ADJECTIVE / demonymic-plural VALUE ("…capital of Parisians");
            #   * REFLEXIVE-AFTER-CANON ("US located in US", "EU member of EU",
            #     "China leader of America" — both collapse to one entity);
            #   * INVERTED relation direction ("US located in New York",
            #     "Washington capital of US");
            #   * SPORTS-ROSTER triple ("Mbappe member of Iraq").
            drop_reason = self._d6_drop_reason(
                subject, predicate, value,
                reject_quantity_endpoints=cfg.reject_quantity_endpoints,
            )
            if drop_reason is not None:
                ctx.logger.debug(
                    "fact_extractor.gate_drop reason=%s signal_id=%s "
                    "subject=%r predicate=%r value=%r",
                    drop_reason, signal.signal_id, subject, predicate, value,
                )
                continue
            # Light validity gate (ON by default): drop spelled-out quantity/
            # ordinal endpoints the confidence floor can't be applied against.
            if cfg.reject_quantity_endpoints and (
                _is_quantity_phrase(subject) or _is_quantity_phrase(value)
            ):
                continue
            conf, conf_components = _resolve_ingestion_confidence_components(
                triple, cfg.backend
            )
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
                # How the confidence was derived (extractor score vs heuristic
                # floor) — provenanced into the fact's data.confidence_components.
                "confidence_components": conf_components,
                "subject_class": subj_class,
                "value_class": val_class,
                "relation_type": relation_type,
                # Quote the clause this relation was read from, not the head of
                # the document, as the fact's evidence excerpt.
                "excerpt": _triple_excerpt(align_text, triple, fallback=excerpt),
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
                    # DQ R1 corroboration ledger — which SOURCES have asserted
                    # this triple. Repeats from one source never extend it, so
                    # the noisy-OR lift can require genuine independence.
                    "source_ids": [signal.source_id],
                    "ner_class_subject": t["subject_class"],
                    "ner_class_object": t["value_class"],
                    "relation_type": t["relation_type"],
                }
                # Confidence provenance (D6/D13): how the score was derived —
                # the extractor's real per-triple score vs the documented
                # heuristic floor — so the basis is never opaque.
                if t.get("confidence_components") is not None:
                    data["confidence_components"] = t["confidence_components"]
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
                    "text_excerpt": t.get("excerpt") or excerpt,
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
                    source_id=signal.source_id,
                )
                written += 1
                if cfg.emit_graph_edges and self._graph_store is not None:
                    try:
                        # The graph keys vertices on entity_profiles.id, so the
                        # endpoints are resolved on the SAME connection that
                        # just wrote the fact. An unresolved endpoint yields
                        # None and upsert_fact_edge skips loudly — a fact whose
                        # actors are not yet entities has no place in a graph
                        # whose whole contract is stable identity.
                        await upsert_fact_edge(
                            self._graph_store,
                            subject=t["subject"],
                            subject_id=await resolve_vertex_id(
                                conn, t["subject"], t["subject_class"]
                            ),
                            subject_class=t["subject_class"],
                            predicate=t["predicate"],
                            value=t["value"],
                            value_id=await resolve_vertex_id(
                                conn, t["value"], t["value_class"]
                            ),
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
                "triples_unbound_dropped": self._triples_unbound_dropped,
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
    source_id: str | None = None,
) -> None:
    """Write one source-owned ('ingestion') fact via the §3 ON CONFLICT upsert.

    Re-ingest of the same triple+valid_from is idempotent: confidence combines
    via the bounded noisy-OR (Holes-A A2 — corroboration raises belief above any
    single source, capped 0.99, matching the analyst path; was GREATEST/max),
    lineage unions. ``source_type`` is the constant ``'ingestion'``; analyst_id /
    target_id / run_id are NULL (ingestion facts are source-owned).

    Before the insert we close any prior OPEN fact for the same
    ``(lower(subject), lower(predicate))`` whose value DIFFERS (PIECE B
    auto-supersession): the prior row gets ``valid_until=now()`` +
    ``superseded_by=<this id>`` so the canonical "what is true now" is the
    single open row. The incoming ``'ingestion'`` source_type is threaded into
    ``supersede_prior_facts`` so Holes-A's A1 tier guard engages — an ingestion
    fact can NO LONGER close an open seed/curated (authoritative) row (it was
    bypassed before because this caller omitted ``incoming_source_type``). A
    same-value re-assert closes nothing (the upsert owns it). Shares the exact
    write contract the analyst path uses (``provenance.writes``) so both
    producers agree.

    Holes-B Wave 0: the row's ``source_credibility`` is stamped = MAX over the
    backing signals' ``signals.source_credibility`` (the ``derived_from`` signal
    ids), falling back to the ingestion tier nominal (0.5) when unscored.
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
    # Holes-B Wave 0 — resolve this ingestion fact's source_credibility: MAX
    # over the backing signals' signals.source_credibility (already backfilled
    # at signal write), else the ingestion tier nominal (0.5).
    source_credibility = await resolve_fact_source_credibility(
        conn,
        source_type="ingestion",
        derived_from=derived_from,
    )
    # A1 tier guard (Holes-A) — declare the incoming source_type so an ingestion
    # fact can NOT close an open seed/curated (authoritative) row. This caller
    # previously omitted the arg, silently DISABLING the guard on the ingestion
    # path; a same-tier (ingestion vs ingestion/agent) value-change still
    # supersedes by recency exactly as before.
    await supersede_prior_facts(
        conn,
        subject=subject,
        predicate=predicate,
        value=value,
        new_fact_id=fact_id,
        incoming_source_type="ingestion",
    )
    # Identical-triple dedupe (PIECE B data quality): the 0032 open-row unique
    # index keys on the FULL quad INCLUDING valid_from, so the same
    # (subject, predicate, value) re-ingested from N signals with N distinct
    # event-times accumulates N open rows (the live "Russian located in UK" ×8
    # noise). This complements 0032 — it does NOT change the supersession key
    # (a DIFFERENT value still supersedes via supersede_prior_facts above) — by
    # collapsing the valid_from dimension for SAME-value open rows: if an open
    # row for this triple already exists (any valid_from), refresh it
    # (confidence→noisy-OR per A2, lineage union, earliest valid_from kept,
    # source_credibility→max) and skip the insert. A no-op for the row count;
    # never resurrects a closed row (the filter is open-only, matching the
    # partial index's WHERE).
    # DQ R1: corroboration requires a DISTINCT source. The relation backend
    # emits no real per-triple score, so every observation lands on the 0.5
    # floor and any re-assert used to lift it (0.50 → 0.75 → …). A recurring
    # digest format therefore MANUFACTURED corroboration for itself: the same
    # channel reposting the same boilerplate looked like independent sources
    # agreeing, and garbage triples climbed out of the floor band on repetition
    # alone. The lift is now suppressed when this source already backs the row.
    #
    # The contributing sources are tracked in ``data.source_ids`` ON THE FACT
    # rather than resolved from ``signals`` via ``derived_from``: enrichment
    # runs BEFORE ``write_canonical_signal`` (``source_actor._process_one``), so
    # at fact-write time the incoming signal has NO row in ``signals`` yet and a
    # join-based check would silently never fire. A self-contained ledger also
    # makes the corroboration basis auditable in place.
    existing_id = await conn.fetchval(
        """
        UPDATE facts
           -- A2: same-triple re-assert is CORROBORATION — combine confidences
           -- with a bounded noisy-OR so N agreeing sources raise belief above
           -- any single one (was GREATEST/max; now matches the analyst
           -- collapse_open_triple path so both producers agree).
           -- DQ R1: ...but ONLY from a source not already in this fact's
           -- data.source_ids ledger. A repeat from a source already there
           -- unions lineage and refreshes the row while leaving confidence
           -- exactly where it was.
           -- DQ Phase 5: the ceiling is 0.75 when the INCOMING observation is at
           -- or below the heuristic floor (0.5) — floor-only corroboration (the
           -- relation backend emits no real per-triple score) must NOT manufacture
           -- near-certainty ("Thousands located in South Africa" reached 0.99). A
           -- genuine sub-1.0 extractor score (> 0.5) keeps the 0.99 ceiling.
           -- DQ P5 r2: the ceiling caps NEW belief but must NEVER LOWER an
           -- already-higher genuine confidence — GREATEST(existing, capped) so a
           -- floor observation can't drag a real 0.9 fact down to 0.75.
           -- Corroboration only ever raises; floor+floor still tops out at 0.75.
           SET confidence   = CASE
                                WHEN $8::text IS NOT NULL
                                 AND COALESCE(facts.data->'source_ids', '[]'::jsonb)
                                     ? $8::text
                                THEN facts.confidence
                                ELSE GREATEST(
                                  facts.confidence,
                                  LEAST(
                                    CASE WHEN $4 <= 0.5 THEN 0.75 ELSE 0.99 END,
                                    1.0 - (1.0 - facts.confidence) * (1.0 - $4)
                                  )
                                )
                              END,
               -- Append this source to the fact's corroboration ledger (set
               -- semantics — a repeat never grows it).
               data         = CASE
                                WHEN $8::text IS NULL
                                  OR COALESCE(facts.data->'source_ids', '[]'::jsonb)
                                     ? $8::text
                                THEN facts.data
                                ELSE jsonb_set(
                                       COALESCE(facts.data, '{}'::jsonb),
                                       '{source_ids}',
                                       COALESCE(facts.data->'source_ids', '[]'::jsonb)
                                       || to_jsonb($8::text)
                                     )
                              END,
               derived_from = (SELECT array_agg(DISTINCT e)
                               FROM unnest(facts.derived_from || $5::uuid[]) e),
               valid_from   = LEAST(facts.valid_from, $6),
               -- Holes-B Wave 0: keep the MOST credible backing source.
               -- GREATEST skips NULLs (NULL only if both NULL).
               source_credibility = GREATEST(facts.source_credibility, $7),
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
        source_credibility,
        source_id,
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
            derived_from, schema_uri, source_credibility
        ) VALUES (
            $1, $2, $3, $4, $5, 'ingestion',
            $6, $7, $8, $9::jsonb, $10::jsonb,
            $11, 'iglu:legba/fact/jsonschema/2-0-0', $12
        )
        ON CONFLICT (lower(subject), lower(predicate), lower(value),
                     COALESCE(valid_from, '1970-01-01 00:00:00+00'::timestamptz))
                 WHERE valid_until IS NULL AND superseded_by IS NULL
        DO UPDATE SET
            -- A2: corroboration combines via bounded noisy-OR, matching the
            -- analyst ON CONFLICT path (was GREATEST/max). DQ Phase 5: ceiling is
            -- 0.75 when the incoming observation is at/below the heuristic floor
            -- (0.5) so floor-only corroboration cannot manufacture near-certainty;
            -- a genuine extractor score (> 0.5) keeps the 0.99 ceiling.
            -- DQ P5 r2: GREATEST(existing, capped) so the 0.75 ceiling can cap
            -- new belief but never LOWER an already-higher genuine confidence (a
            -- floor observation can't drag a real 0.9 fact to 0.75); floor+floor
            -- corroboration still tops out at 0.75.
            -- DQ R1: the lift also requires a DISTINCT source here — a repeat
            -- from a source already in the fact's ledger is a re-assert, not
            -- corroboration, so it unions lineage without raising belief.
            confidence   = CASE
                             WHEN $13::text IS NOT NULL
                              AND COALESCE(facts.data->'source_ids', '[]'::jsonb)
                                  ? $13::text
                             THEN facts.confidence
                             ELSE GREATEST(
                               facts.confidence,
                               LEAST(
                                 CASE WHEN EXCLUDED.confidence <= 0.5 THEN 0.75 ELSE 0.99 END,
                                 1.0 - (1.0 - facts.confidence) * (1.0 - EXCLUDED.confidence)
                               )
                             )
                           END,
            data         = CASE
                             WHEN $13::text IS NULL
                               OR COALESCE(facts.data->'source_ids', '[]'::jsonb)
                                  ? $13::text
                             THEN facts.data
                             ELSE jsonb_set(
                                    COALESCE(facts.data, '{}'::jsonb),
                                    '{source_ids}',
                                    COALESCE(facts.data->'source_ids', '[]'::jsonb)
                                    || to_jsonb($13::text)
                                  )
                           END,
            derived_from = (SELECT array_agg(DISTINCT e)
                            FROM unnest(facts.derived_from || EXCLUDED.derived_from) e),
            -- Holes-B Wave 0: keep the MOST credible backing source.
            source_credibility = GREATEST(facts.source_credibility,
                                          EXCLUDED.source_credibility),
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
        source_credibility,
        source_id,
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


def _relations_to_triples(relations: list[Any]) -> list[dict[str, Any]]:
    """Adapt the stamped ``payload['relations']`` pairs to the triple shape.

    Pure projection — the head/tail binding is the extractor's own, so nothing
    is inferred here. Endpoint offsets ride along so the write path can quote
    the sentence the relation was read from as the fact's evidence excerpt.
    """
    out: list[dict[str, Any]] = []
    for rel in relations:
        if not isinstance(rel, dict):
            continue
        subject = str(rel.get("subject", "")).strip()
        predicate = str(rel.get("predicate", "")).strip()
        obj = str(rel.get("object", "")).strip()
        if not subject or not predicate or not obj:
            continue
        out.append({
            "subject": subject,
            "predicate": predicate,
            "object": obj,
            # Absent → None, so the write path applies its documented floor
            # instead of laundering a fabricated 1.0.
            "confidence": rel.get("confidence"),
            "subject_start": _as_offset(rel.get("subject_start")),
            "subject_end": _as_offset(rel.get("subject_end")),
            "object_start": _as_offset(rel.get("object_start")),
            "object_end": _as_offset(rel.get("object_end")),
        })
    return out


def _as_offset(value: Any) -> int:
    """Coerce a payload offset to an int; ``-1`` means "unknown"."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _pair_is_bound(
    subj: Mapping[str, Any],
    obj: Mapping[str, Any],
    *,
    text: str,
    max_gap: int,
) -> bool:
    """Is this candidate (subject, object) pair provably adjacent in the text?

    Three constraints, ALL required — a candidate that cannot satisfy them is
    refused rather than emitted on a guess:

      * **Located** — both endpoints carry a real character offset. Without one
        there is no evidence they were ever near each other.
      * **Text order** — the subject occurs before the object. The legacy list
        has no direction, and assuming SVO order at least stops the inversions
        ("the Republican party / member of / Zelensky").
      * **Same sentence** — no sentence/bullet break between them, and no more
        than ``max_gap`` characters. This is what kills the cross-bullet
        bindings a TASS-style digest post produces, where two unrelated
        headlines in one message were fused into a single relation.

    The sentence test needs the offsets to address THIS text; when the recorded
    span doesn't match the surface we hold (a payload rendered from a different
    field set), we fall back to the distance bound alone rather than trusting a
    slice that means nothing.
    """
    s_start = _as_offset(subj.get("start"))
    s_end = _as_offset(subj.get("end"))
    o_start = _as_offset(obj.get("start"))
    if s_start < 0 or o_start < 0:
        return False
    if o_start < s_start:
        return False
    gap_from = s_end if s_end >= 0 else s_start
    if o_start - gap_from > max_gap:
        return False
    if text:
        surface = str(subj.get("text", ""))
        aligned = (
            s_end > s_start
            and text[s_start:s_end].lower() == surface.lower()
        )
        if aligned and _SENTENCE_BREAK_RE.search(text, gap_from, o_start):
            return False
    return True


#: Sentence / list-item boundary. Bullets, pipes and newlines count: a digest
#: post ("⚡️ A ... \n⚡️ B ...") is one document but many unrelated statements,
#: and binding across those breaks is what fused separate headlines into one
#: relation. Terminators INSIDE an endpoint's own span ("Apple Inc.") are never
#: consulted — only the text strictly BETWEEN the two endpoints is.
_SENTENCE_BREAK_RE = re.compile(r"[.!?;\n\r•·–—|]")


def _offset_aligned_text(payload: Mapping[str, Any], text_fields: list[str]) -> str:
    """The source text in the SAME rendering the upstream NER measured offsets
    against — concatenated fields with the markdown wrappers stripped.

    ``ner._extract_text`` strips "[**t**](url)" → "t" BEFORE the extractor sees
    the text, so every offset on ``payload['entities']`` indexes the stripped
    form. Comparing them against the raw concatenation would misalign every
    telegram signal, so the same two substitutions are applied here.
    """
    text = _concat_text(payload, text_fields)
    if not text:
        return ""
    text = _MD_LINK_RE.sub(r"\1", text)
    return _MD_BOLD_RE.sub(r"\1", text).strip()


def _triple_excerpt(
    text: str,
    triple: Mapping[str, Any],
    *,
    fallback: str,
    max_chars: int = 512,
) -> str:
    """The sentence a triple was read from — the fact's human-verification hook.

    Expands from the endpoint offsets out to the enclosing sentence/bullet
    bounds, so ``evidence_set.text_excerpt`` quotes the clause that actually
    asserts the relation rather than the head of the document. Falls back to
    ``fallback`` (the leading text) whenever the offsets are unusable, so the
    excerpt is never empty when ANY source text is available.
    """
    if not text:
        return fallback
    offsets = [
        _as_offset(triple.get(key))
        for key in ("subject_start", "subject_end", "object_start", "object_end")
    ]
    located = [o for o in offsets if 0 <= o <= len(text)]
    if not located:
        return fallback
    lo, hi = min(located), max(located)
    left = 0
    for match in _SENTENCE_BREAK_RE.finditer(text, 0, lo):
        left = match.end()
    right_match = _SENTENCE_BREAK_RE.search(text, hi)
    right = right_match.end() if right_match else len(text)
    return text[left:right].strip()[:max_chars] or fallback


def _entities_to_triples(
    entities: list[Any],
    *,
    text: str = "",
    max_gap: int = 160,
) -> tuple[list[dict[str, Any]], int]:
    """Re-bind triples from a LEGACY relation-entity list. Returns
    ``(triples, dropped_candidate_pairs)``.

    The upstream ``ner_multilingual`` stage flattens the extractor's triples
    into a de-duped entity list where each entity keeps only its ``predicate``
    label. That list is document-wide, is NOT in text order, and has already
    lost any endpoint that a previous predicate group claimed first — so the
    pairing is not recoverable by position. Pairing members by LIST INDEX (what
    this did) manufactured relations wholesale: "Russia / founded by / Pavel
    Durov" from a post reading "Telegram founder Pavel Durov", "Donetsk /
    founded by / Kiev" from "the war launched by the Kiev regime", a France24
    reporter bound as an IRGC spokesperson.

    So we no longer reconstruct — we CORROBORATE. Members are sorted into text
    order and only adjacent candidates that :func:`_pair_is_bound` can place in
    the same sentence become triples; everything else is dropped and counted.
    Dropping is the correct outcome: ``_extract_relation`` falls through to the
    authoritative ``/extract`` pairs when this yields nothing, and a fact that
    was never asserted is strictly better than one that was invented.
    """
    by_pred: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        pred = str(ent.get("predicate", "")).strip()
        surface = str(ent.get("text", "")).strip()
        if not pred or not surface:
            continue
        if pred not in by_pred:
            by_pred[pred] = []
            order.append(pred)
        by_pred[pred].append(ent)

    triples: list[dict[str, Any]] = []
    dropped = 0
    for pred in order:
        members = by_pred[pred]
        # Text order — the emission order is triple order, not reading order.
        located = sorted(
            (e for e in members if _as_offset(e.get("start")) >= 0),
            key=lambda e: _as_offset(e.get("start")),
        )
        kept = 0
        i = 0
        while i + 1 < len(located):
            subj, obj = located[i], located[i + 1]
            if not _pair_is_bound(subj, obj, text=text, max_gap=max_gap):
                # Refused: retry this object as the next candidate's subject
                # rather than consuming both.
                i += 1
                continue
            # None (not 1.0) when neither endpoint carried a score, so the
            # write-path resolver applies the default rather than a fake 1.0.
            conf = subj.get("confidence", obj.get("confidence", None))
            triples.append({
                "subject": subj.get("text", ""),
                "predicate": pred,
                "object": obj.get("text", ""),
                "confidence": conf,
                "subject_start": _as_offset(subj.get("start")),
                "subject_end": _as_offset(subj.get("end")),
                "object_start": _as_offset(obj.get("start")),
                "object_end": _as_offset(obj.get("end")),
            })
            kept += 1
            i += 2
        # Volume impact = what index-pairing WOULD have emitted, minus what
        # survived the binding test.
        dropped += max(0, len(members) // 2 - kept)
    return triples, dropped


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


def _resolve_ingestion_confidence_components(
    triple: dict[str, Any], backend: str
) -> tuple[float, dict[str, Any]]:
    """Resolve a per-triple confidence AND its provenance breakdown.

    Replaces the historical flat constant: the EXTRACTOR's real per-relation
    score is preferred whenever it is a genuine measurement; only when no real
    score is available does the clearly-documented heuristic floor apply. The
    returned ``components`` dict is provenanced into the fact's
    ``data.confidence_components`` so the basis is never opaque:

      * ``source`` — ``"extractor_score"`` when a genuine per-triple score was
        used, else ``"heuristic_floor"``;
      * ``extractor_score`` — the raw per-triple score the extractor provided
        (``None`` when absent / non-numeric);
      * ``floor`` — :data:`_INGESTION_DEFAULT_CONFIDENCE`, the documented
        conservative floor (machine-extracted ⇒ never as certain as a curated
        seed/analyst assertion); recorded even when the score was used so the
        floor in force is auditable;
      * ``backend`` — which extraction backend produced the triple;
      * ``note`` — the reason the floor was chosen, when it was.

    Score handling (unchanged semantics, now explained in-band):
      * missing / non-numeric score → the floor (``heuristic_floor``);
      * ``relation`` backend + an EXACT 1.0 → the legacy REBEL "no real score"
        sentinel → the floor; a genuine sub-1.0 GLiREL score is kept;
      * any other in-range score → used as-is (``extractor_score``).

    FOLLOW-UP (unchanged): on the relation backend the exact-1.0 sentinel guard
    is a legacy carry-over from REBEL; reconciling it so a *genuine* GLiREL 1.0
    isn't collapsed to the floor is a tracked code follow-up.

    The SLM relationship-validation stage (when on) already overrides via the
    verdict path upstream; this only governs the extractor's own score.
    """
    floor = _INGESTION_DEFAULT_CONFIDENCE
    components: dict[str, Any] = {
        "backend": backend,
        "floor": floor,
        "extractor_score": None,
        "source": "heuristic_floor",
    }
    raw = triple.get("confidence", None)
    if raw is None:
        components["note"] = "no extractor score provided"
        return floor, components
    try:
        c = float(raw)
    except (TypeError, ValueError):
        components["note"] = "extractor score not numeric"
        return floor, components
    components["extractor_score"] = c
    c = max(0.0, min(1.0, c))
    # Legacy/defensive (carried from the historical REBEL backend, which stamped
    # 1.0 on every triple): on the relation backend an exact 1.0 is treated as
    # "no real score" so it stops laundering 1.000s. GLiREL emits real scores —
    # see the FOLLOW-UP note above.
    if backend == "relation" and c >= 1.0:
        components["note"] = "relation-backend exact-1.0 sentinel → floor"
        return floor, components
    components["source"] = "extractor_score"
    return c, components


def _resolve_ingestion_confidence(triple: dict[str, Any], backend: str) -> float:
    """Backward-compatible scalar wrapper over
    :func:`_resolve_ingestion_confidence_components` (returns just the value).
    Kept so existing callers / tests that read only the float are unchanged."""
    conf, _ = _resolve_ingestion_confidence_components(triple, backend)
    return conf


__all__ = [
    "FactExtractorConfig",
    "FactExtractorHandler",
    "FactExtractorUnconfigured",
]
