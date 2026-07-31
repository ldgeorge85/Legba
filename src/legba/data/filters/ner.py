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

from .._entity_canon import DEFAULT_CLASS, canonicalize_entity
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
# R8 (2026-07) put a deterministic gazetteer tier IN FRONT of tiers 1-2 and
# guarded tier 1 against inverted triples — see the R8 block further down for
# the full precedence and the live mis-classifications that forced it.
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
# R8 (2026-07 enrichment-quality sweep) — HIGH-PRECISION SIGNALS BEFORE THE
# PERSON DEFAULT.
#
# The tiered heuristic above ends in "two capitalised tokens with no cue →
# person". That default was assigning `person` to "White House", "Yonhap News",
# "Russian Federation" and "State Duma", while real places ("Kyiv", "Crete",
# "Yekaterinburg", "Germany") fell to the generic `entity` bucket and inverted
# GLiREL triples typed people as places ("Seyyed Ali Khamenei" → location,
# "Keir Starmer" → location). Downstream, entity auto-merge requires the SAME
# SPECIFIC CLASS on both sides (`_entity_candidates._class_compatible`), so
# unstable classes were the proximate blocker on 570 exact-key duplicate
# clusters (Zelensky ×9, Trump ×7, ...).
#
# The repair is deterministic and precision-first: consult the SHARED CANON
# gazetteer (`legba.data._entity_canon` — the same ISO-3166 dataset the
# `iso_countries` table is generated from, plus the curated org / place / region
# surfaces the resolver already trusts) BEFORE any predicate or cue heuristic,
# then honorifics, then the (now guarded) predicate mapping, then the existing
# cue ladder. Nothing here invents a class outside the closed taxonomy, and the
# original fallback still runs last — this only shrinks how often it is reached.
# ---------------------------------------------------------------------------

#: Classes the canon may assert. A canon hit is authoritative: these come from
#: whole-surface gazetteers, not from shape heuristics.
_CANON_CLASSES: frozenset[str] = frozenset({"country", "organization", "location"})

#: LEADING personal titles / honorifics. A surface whose FIRST token is one of
#: these (with at least one token after it) names a person — "Seyyed Ali
#: Khamenei", "Ayatollah Ali Khamenei", "Sheikh Mohammed", "King Salman". The
#: canon probe runs FIRST, so the place readings ("Prince Edward Island",
#: "King County", "Mount Hermon") are already resolved and never reach here.
_PERSON_HONORIFICS: frozenset[str] = frozenset({
    "mr", "mrs", "ms", "miss", "dr", "prof", "sir", "lord", "lady", "dame",
    "rev", "fr", "rabbi", "imam", "sheikh", "shaikh", "sheik", "seyyed",
    "sayyed", "sayyid", "syed", "ayatollah", "mullah", "hojjatoleslam",
    "pope", "cardinal", "bishop", "archbishop", "patriarch",
    "king", "queen", "prince", "princess", "emir", "sultan", "tsar", "sheikha",
    "gen", "col", "maj", "capt", "lt", "sgt", "adm", "cmdr", "brig",
})

#: ALL-CAPS acronyms that are NOT organizations — diseases, economic /
#: technical measures, newsroom shorthand. Everything else matching
#: :data:`_ACRONYM_RE` is an institution far more often than not (IAEA, IRGC,
#: SBU, FSB, IDF, TASS, HTS, SDF), and the alternative was the generic bucket.
_NON_ORG_ACRONYMS: frozenset[str] = frozenset({
    "COVID", "SARS", "MERS", "HIV", "AIDS", "EBOLA", "FLU",
    "GDP", "CPI", "PPI", "IPO", "ETF", "FX", "VAT",
    "API", "URL", "PDF", "FAQ", "GPS", "DNA", "RNA", "LGBT", "NGO", "IED",
    "AI", "IT", "TV", "PR", "HR", "EV", "UAV", "ICBM", "SUV",
    # ALL-CAPS headline residue — an ordinary English word is not an acronym.
    "THE", "AND", "NOT", "BUT", "FOR", "ALL", "NEW", "TOP", "KEY", "MORE",
    "WAR", "OIL", "GAS", "AID", "DEAD", "LIVE", "JUST", "OVER",
})

#: An acronym surface: a single 3-5 letter ALL-CAPS token. Below 3 letters the
#: collision rate with ordinary abbreviations is too high to call.
_ACRONYM_RE = re.compile(r"^[A-Z]{3,5}$")

#: A multi-token surface whose tokens are ALL capitalised words — the exact
#: shape the person fallback exists for. Used to refuse an INVERTED GLiREL
#: triple that would type such a surface as a place ("(Iran, country of
#: citizenship, Seyyed Ali Khamenei)"). Single-token surfaces are deliberately
#: excluded so an unknown city ("Damietta") still takes the predicate's word
#: for it.
_TITLECASE_WORD_RE = re.compile(r"^[A-Z][\w'’\-]*$", re.UNICODE)


def _looks_like_personal_name(tokens: list[str]) -> bool:
    """True for a multi-token, all-capitalised, cue-free surface.

    Deliberately shape-only: by the time this runs the canon has already
    claimed every country / organization / place surface it knows, so what is
    left in this shape is overwhelmingly a personal name.
    """
    if len(tokens) < 2:
        return False
    return all(_TITLECASE_WORD_RE.match(tok) for tok in tokens)


#: A leading English article, stripped for the second canon probe only.
_ARTICLE_PREFIX_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)


def _canon_class(text: str) -> str | None:
    """Class the shared canon asserts for ``text``, or ``None``.

    Probes the surface as-is and, when that yields nothing, with a leading
    article stripped — so "the White House" types the same as "White House"
    (article twins carrying the SAME class is what lets them fold together).
    """
    probes = [text]
    unarticled = _ARTICLE_PREFIX_RE.sub("", text, count=1)
    if unarticled and unarticled != text:
        probes.append(unarticled)
    for probe in probes:
        try:
            canonical, cls = canonicalize_entity(probe, DEFAULT_CLASS)
        except Exception:                                        # pragma: no cover
            return None
        if canonical and cls in _CANON_CLASSES:
            return cls
    return None

# ---------------------------------------------------------------------------
# M11 — non-Latin NER routing (translate-then-NER).
#
# The hosted /extract endpoint runs spaCy ``en_core_web_trf`` (English-only)
# to seed the entity spans GLiREL relates. Non-Latin scripts (Arabic /
# Cyrillic / Hebrew / CJK / Devanagari / Thai) yield essentially ZERO spaCy
# spans → zero triples → zero entities (measured live: `ar` 1,880 signals /
# 0 with entities; Russian + Ukrainian telegram likewise once M12 lands). The
# fix routes these through the /translate (NLLB-200) endpoint on the SAME
# hosted plane FIRST, then NERs the English translation. Latin-script
# languages (fr / es / de / pt / it / nl / tr …) are deliberately EXCLUDED:
# English spaCy still recognises their proper nouns (measured live: fr/es/de
# all extract), so translating them would burn NLLB calls for little gain.
#
# The set below is exactly the NLLB_LANG_CODES (legba-models/app/main.py)
# whose script is non-Latin. Operators can extend it via
# ``NERMultilingualConfig.translate_languages``.
# ---------------------------------------------------------------------------

_NON_LATIN_TRANSLATE_LANGS: frozenset[str] = frozenset({
    "ar", "fa", "he", "ru", "uk", "zh", "ja", "ko", "hi", "th", "ur",
})

# A Latin letter: ASCII + Latin-1 supplement/extended-A/B + extended additional.
_LATIN_CHAR_RE = re.compile(r"[A-Za-zÀ-ɏḀ-ỿ]")

# Script -> coarse NLLB source-lang, used ONLY as a fallback when
# language_detect missed a non-Latin body (returned 'und' / 'xx'). Ordered
# strong-signal-first: kana -> ja and hangul -> ko are exclusive scripts, so
# they win over Han (which Japanese shares) when present.
_SCRIPT_RANGES: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("ja", re.compile(r"[぀-ヿ]")),                    # Hiragana+Katakana
    ("ko", re.compile(r"[가-힣ᄀ-ᇿ]")),       # Hangul
    ("ru", re.compile(r"[Ѐ-ԯ]")),                    # Cyrillic (+suppl.)
    ("ar", re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿ]")),  # Arabic
    ("he", re.compile(r"[֐-׿]")),                    # Hebrew
    ("hi", re.compile(r"[ऀ-ॿ]")),                    # Devanagari
    ("th", re.compile(r"[฀-๿]")),                    # Thai
    ("zh", re.compile(r"[一-鿿㐀-䶿]")),       # Han (JP shares)
)


def _is_majority_non_latin(text: str) -> bool:
    """True when most alphabetic characters in ``text`` are non-Latin script.

    Used to catch signals whose language_detect result is missing / ``und`` /
    ``xx`` but whose body is clearly a non-Latin script the English NER cannot
    read."""
    latin = 0
    total = 0
    for ch in text:
        if not ch.isalpha():
            continue
        total += 1
        if _LATIN_CHAR_RE.match(ch):
            latin += 1
    if total == 0:
        return False
    return latin * 2 < total


def _dominant_script_lang(text: str) -> str | None:
    """Best-effort coarse source-language from the dominant non-Latin script.

    Returns an NLLB source-lang code (``ru`` / ``ar`` / ``zh`` / ...) or
    ``None`` when no non-Latin script is present. Kana → ``ja`` and Hangul →
    ``ko`` short-circuit (exclusive scripts); otherwise the highest-count
    script wins. This is a fallback ONLY — the detected language
    (language_detect) is always preferred. Ukrainian shares Cyrillic with
    Russian, so an *undetected* Ukrainian body infers ``ru`` here; NLLB still
    translates it acceptably and named entities survive either way."""
    best_lang: str | None = None
    best_count = 0
    for lang, pat in _SCRIPT_RANGES:
        c = len(pat.findall(text))
        if c == 0:
            continue
        if lang in ("ja", "ko"):
            return lang  # exclusive script — strong signal, take immediately
        if c > best_count:
            best_count, best_lang = c, lang
    return best_lang

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

#: Markdown residue in raw feed text (B0-7 — telegram ``payload.text`` carries
#: "[**title**](url)" verbatim). Stripped BEFORE the /extract hop so the NER
#: never sees link/bold syntax and cannot emit spans wearing it (mid-name
#: residue like "S**ergey" that a name-level junk gate can't reject without
#: losing the referent). Conservative by construction: the link pattern
#: requires "](" ADJACENCY (a legitimate bracket in prose — "[sic] (see
#: appendix)" — has no adjacent "](", so it is untouched) and the bold pattern
#: only rewrites PAIRED "**…**" markers. The URL group disallows whitespace
#: (real markdown URLs never carry spaces — they'd be %-encoded), so a
#: malformed "](" followed by prose cannot swallow words up to a distant ")".
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)\s]*\)")
_MD_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")


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

    Precedence (R8): operator overrides, then the shared-canon gazetteer
    (country / organization / place — deterministic whole-surface matches),
    then leading honorifics, then the predicate mapping, then the cue ladder,
    then acronym shape, and only then the original article / two-capitalised-
    tokens fallbacks. Every tier above the fallback is high-precision by
    construction, so the person default is reached far less often — see the
    R8 block above for why that matters to entity auto-merge.
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

    # CORPORATION cue first — the canon folds every company surface into the
    # broader ``organization``, and ``corporation`` is the more specific (and
    # merge-compatible) class, so the legal-form suffix keeps its precedence.
    if lower_tokens & _CORPORATION_CUES:
        return "corporation"

    # R8 tier 1 — SHARED CANON GAZETTEER. A country name / demonym / alias, a
    # curated region or place, an org suffix / head / infix, a known org
    # acronym or masthead. Authoritative: whole-surface dataset matches beat
    # every shape heuristic below, including a garbage predicate.
    canon_cls = _canon_class(t)
    if canon_cls is not None:
        return canon_cls

    # R8 tier 2 — LEADING HONORIFIC. "Seyyed Ali Khamenei" was typing location
    # off an inverted triple; a personal title in front of a name settles it.
    if len(tokens) >= 2 and tokens[0].lower().rstrip(".") in _PERSON_HONORIFICS:
        return "person"

    # Predicate-driven mapping.
    if slot == "object":
        # R8 — a place predicate may NOT overrule an obvious personal-name
        # shape. GLiREL routinely inverts its triples ("(Iran, country of
        # citizenship, Seyyed Ali Khamenei)"), and the canon has already
        # claimed every place surface it recognises, so a cue-free multi-token
        # capitalised leftover here is a person ("Keir Starmer"), not a place.
        # A single-token unknown ("Damietta") still takes the predicate.
        place_predicate = (
            lo_pred in _COUNTRY_PREDICATES or lo_pred in _LOCATION_PREDICATES
        )
        if place_predicate and _looks_like_personal_name(tokens):
            pass
        elif lo_pred in _COUNTRY_PREDICATES:
            return "country"
        elif lo_pred in _LOCATION_PREDICATES:
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

    # An ARTICLE-prefixed surface ("the/a/an X") is never a person — persons do
    # not take an article ("the Indian Ocean", "The Economist", "the Kerch
    # Strait", "the Russian Foreign Ministry"), but organisations / locations /
    # events do. With no cue matched above, such a surface must fall through to
    # the generic `entity` bucket rather than the person default below.
    # (E6-faucet-2, 2026-07-12 review: the two-cap-tokens→person default is the
    # mechanical root of the 53% person-skew AND the article-twin merge blockage
    # — persons never auto-merge, so a mis-classed "the X" can never fold onto
    # its bare "X" twin. An exact match — no trailing-punct strip — so a name
    # initial "A." is not mistaken for the article "a".)
    # R8 tier 3 — a bare ALL-CAPS acronym is an institution far more often than
    # anything else in this feed (IAEA / IRGC / SBU / FSB / IDF / HTS), and the
    # alternative is the generic bucket, which never auto-merges. Diseases and
    # economic / technical shorthand are excluded by name.
    if (
        len(tokens) == 1
        and _ACRONYM_RE.match(t)
        and t not in _NON_ORG_ACRONYMS
    ):
        return "organization"

    if tokens and tokens[0].lower() in {"the", "a", "an"}:
        return "entity"

    # Heuristic: multi-token title-cased name with no cues → person
    # (two capitalised tokens is most often a first+last name). Kept for
    # UN-prefixed surfaces — in news text those really are mostly people.
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
        default_factory=lambda: ["title", "body", "summary", "raw_body", "text"],
        description=(
            "Ordered list of payload fields to concatenate as /extract input. "
            "``text`` is included (M12) so telegram signals — which carry their "
            "message body in ``payload.text`` and leave title/summary/raw_body "
            "empty — get NER'd instead of silently extracting nothing "
            "(matches the language_detect filter's field set)."
        ),
    )
    translate_before_ner: bool = Field(
        default=True,
        description=(
            "M11 — when True, non-Latin-script signals (by detected language or "
            "script) are translated to English via the hosted /translate "
            "(NLLB-200) endpoint BEFORE the /extract NER hop, because the "
            "endpoint's spaCy en_core_web_trf is English-only and extracts ~0 "
            "entities from Arabic / Cyrillic / CJK / etc. text. Best-effort: a "
            "translate failure falls back to extracting the original text. Set "
            "False to disable (the non-Latin gap then stays open)."
        ),
    )
    translate_languages: list[str] = Field(
        default_factory=lambda: sorted(_NON_LATIN_TRANSLATE_LANGS),
        description=(
            "Source languages routed through translate-then-NER. Default = the "
            "NLLB-200 source codes whose script the English NER cannot read "
            "(ar/fa/he/ru/uk/zh/ja/ko/hi/th/ur). Latin-script languages are "
            "excluded by default (English spaCy still recognises their proper "
            "nouns); operators can add them here. Codes must be in the hosted "
            "NLLB set or the translate call 4xx's and falls back to direct NER."
        ),
    )
    translate_target_language: str = Field(
        default="en",
        description="NLLB target language for the pre-NER translate hop.",
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

    @field_validator("translate_languages")
    @classmethod
    def _normalise_translate_langs(cls, v: list[str]) -> list[str]:
        return [c.lower() for c in v if isinstance(c, str) and c]

    @field_validator("translate_target_language")
    @classmethod
    def _normalise_translate_target(cls, v: str) -> str:
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
        # M11 — non-Latin source languages routed through translate-then-NER.
        self._translate_langs: set[str] = {
            c.lower() for c in config.translate_languages
        }
        # Health-state counters.
        self._signals_in = 0
        self._signals_out = 0
        self._signals_dropped = 0
        self._signals_failed = 0
        self._translate_calls = 0
        self._translate_failures = 0
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

        # M11 — the hosted /extract runs spaCy en_core_web_trf (English-only),
        # so a non-Latin-script body extracts ~0 entities. Translate it to
        # English first (NLLB-200 on the same hosted plane) and NER the
        # translation. Best-effort: a translate failure falls back to
        # extracting the original text (still contributes geo/time; the gap
        # stays explicit via the translate-failure counter, never silent).
        extract_text = truncated
        # M13 — the English translation the M11 route produces is DISCARDED after
        # NER today, which forces every downstream reader (journal / chronicle /
        # slice renderers) to narrate over the raw non-Latin title and the
        # transliterated NER surface (the Rubio-inversion class). Persist it:
        # ``payload.text_en`` = the combined-text translation NER already ran on,
        # and ``payload.title_en`` = a SEPARATE short title translation (one extra
        # hosted /translate call). Best-effort like the translate hop — a failure
        # leaves the field absent, never fails the run. Only for signals that
        # actually went through the translate route (translate_src not None).
        text_en: str | None = None
        title_en: str | None = None
        translate_src = self._translate_source_lang(language, truncated)
        if translate_src is not None:
            translated = await self._maybe_translate(
                truncated, translate_src, ctx, signal,
            )
            if translated:
                text_en = translated[: self._config.max_text_chars]
                extract_text = text_en
            # Translate the TITLE separately — it is short (its own +1 call) and
            # is what every slice/journal row renders. Respect the same max-chars
            # truncation. Only when a non-empty raw title exists.
            raw_title = (
                signal.payload.get("title")
                if isinstance(signal.payload, dict) else None
            )
            if isinstance(raw_title, str) and raw_title.strip():
                title_src = raw_title.strip()[: self._config.max_text_chars]
                translated_title = await self._maybe_translate(
                    title_src, translate_src, ctx, signal,
                )
                if translated_title:
                    title_en = translated_title[: self._config.max_text_chars]

        try:
            data = await self._client.extract(extract_text)
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
        emitted = self._triples_to_entities(triples, text=extract_text, language=language)
        relations = self._triples_to_relations(
            triples, text=extract_text, language=language,
        )

        self._signals_out += 1
        self._last_success_at = datetime.now(tz=timezone.utc)
        return self._annotate(
            signal,
            entities=emitted,
            language=language,
            text_en=text_en,
            title_en=title_en,
            relations=relations,
        )

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
                "translate_calls": self._translate_calls,
                "translate_failures": self._translate_failures,
                "translate_languages": sorted(self._translate_langs),
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

    def _translate_source_lang(self, language: str, text: str) -> str | None:
        """M11 — decide whether (and from which source language) to translate a
        signal to English before the /extract NER hop.

        Returns the NLLB source-language code to translate FROM, or ``None`` to
        run NER directly on the original text (English + Latin-script bodies).

        Preference:
          1. The detected language (language_detect / language_hint, via
             :meth:`_pick_language`) when it is a configured non-Latin source
             lang — the reliable primary signal.
          2. Fallback for a missed/`und`/`xx` detection: if the body is
             majority non-Latin script, infer a coarse source lang from the
             dominant script. Only used when (1) did not already match.
        """
        if not self._config.translate_before_ner or self._client is None:
            return None
        target = self._config.translate_target_language
        lang = (language or "").lower()
        if lang and lang != target and lang in self._translate_langs:
            return lang
        if lang not in self._translate_langs and _is_majority_non_latin(text):
            inferred = _dominant_script_lang(text)
            if (
                inferred
                and inferred != target
                and inferred in self._translate_langs
            ):
                return inferred
        return None

    async def _maybe_translate(
        self,
        text: str,
        source_lang: str,
        ctx: FilterContext,
        signal: Signal,
    ) -> str | None:
        """Translate ``text`` -> target language via the hosted /translate
        (NLLB-200) endpoint. Best-effort: returns the translated string, or
        ``None`` on any failure so the caller falls back to the original text.

        Translate failures increment a counter surfaced in health detail but do
        NOT flip the handler to ``degraded`` on their own — the subsequent
        /extract on the original text may still succeed (e.g. English signals
        are never translated), and a translate-only outage is a partial
        (non-Latin-only) degradation, not a full NER outage.
        """
        if self._client is None:                                # pragma: no cover
            return None
        try:
            data = await self._client.translate(
                text,
                source_lang=source_lang,
                target_lang=self._config.translate_target_language,
            )
        except (NlpServiceAuthError, NlpServiceUnavailable) as exc:
            self._translate_failures += 1
            self._last_error = f"translate {source_lang}: {exc!s}"
            ctx.logger.debug(
                "ner_multilingual.translate_failed signal_id=%s src=%s err=%s",
                signal.signal_id, source_lang, exc,
            )
            return None
        except Exception as exc:                                # pragma: no cover
            self._translate_failures += 1
            self._last_error = f"translate {source_lang}: {exc!s}"
            ctx.logger.warning(
                "ner_multilingual.translate_error signal_id=%s src=%s err=%s",
                signal.signal_id, source_lang, exc,
            )
            return None
        translated = data.get("translated") if isinstance(data, dict) else None
        if isinstance(translated, str) and translated.strip():
            self._translate_calls += 1
            return translated.strip()
        return None

    def _extract_text(self, signal: Signal) -> str:
        """Concatenate the configured payload text fields into a single
        /extract input. Title goes first to preserve the most newsworthy
        content within the 512-token model limit.

        Markdown link/bold syntax is stripped from the joined text (B0-7):
        telegram ``payload.text`` arrives as raw markdown ("[**title**](url)"),
        and feeding it verbatim makes the NER emit spans still wearing the
        syntax ("Ayatollah Ali Khamenei**](https://f24.my", "S**ergey").
        Defense-in-depth with the canon's junk gate — this fixes the MID-NAME
        residue class the name-level gate can't reject without losing the
        referent. Conservative: only "[text](url)" -> text and "**text**" ->
        text; legitimate brackets in prose are untouched.
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
        text = "\n".join(parts)
        # Link wrapper first — its inner text may itself be bold-wrapped
        # ("[**t**](url)" -> "**t**" -> "t").
        text = _MD_LINK_RE.sub(r"\1", text)
        text = _MD_BOLD_RE.sub(r"\1", text)
        return text.strip()

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

    def _triples_to_relations(
        self,
        triples: list[dict[str, Any]],
        *,
        text: str,
        language: str,
    ) -> list[dict[str, Any]]:
        """Preserve the /extract head/tail PAIRS as a first-class payload surface.

        :meth:`_triples_to_entities` flattens the same triples into the
        contractual entity list and de-dupes on the endpoint TEXT — which
        destroys the pairing. An entity already emitted by an earlier triple is
        skipped in a later one, so the surviving flat list no longer says which
        head went with which tail, and a downstream consumer that re-pairs it by
        position invents relations that were never extracted (live: a post
        reading "Telegram founder Pavel Durov" yielded "Russia / founded by /
        Pavel Durov" once "Telegram" had been de-duped into an earlier group).

        Facts are built from PAIRS, so the pairs are stamped ALONGSIDE the
        entities instead of being reconstructed by position downstream. Both
        endpoints must clear the same gates the entity list applies (quantity /
        date / number rejection + the configured class vocabulary), so
        ``relations`` never carries an endpoint ``entities`` would have refused.

        Best-effort character offsets are attached for each endpoint: the object
        is located AFTER the subject where possible, so a repeated surface
        ("Iran … Iran") resolves to the occurrence that actually participates in
        this relation rather than always the first one.
        """
        if not triples:
            return []
        emitted: list[dict[str, Any]] = []
        text_lower = text.lower()
        overrides = self._config.taxonomy_map

        for triple in triples:
            if not isinstance(triple, dict):
                continue
            subj = str(triple.get("subject", "")).strip()
            obj = str(triple.get("object", "")).strip()
            pred = str(triple.get("predicate", "")).strip()
            if not subj or not obj or not pred:
                continue
            if _is_nonentity_candidate(subj) or _is_nonentity_candidate(obj):
                continue
            subj_class = _classify_entity_text(
                subj, predicate=pred, slot="subject", overrides=overrides,
            )
            obj_class = _classify_entity_text(
                obj, predicate=pred, slot="object", overrides=overrides,
            )
            if subj_class not in self._vocabulary or obj_class not in self._vocabulary:
                continue
            subj_start, subj_end = _find_span(text_lower, subj.lower())
            obj_start, obj_end = _find_span(
                text_lower, obj.lower(), start_at=subj_end if subj_end >= 0 else 0,
            )
            if obj_start < 0:
                # No occurrence after the subject — fall back to the first one
                # (the object legitimately precedes the subject in the surface
                # form: "Cupertino-based Apple Inc.").
                obj_start, obj_end = _find_span(text_lower, obj.lower())
            emitted.append({
                "subject": subj,
                "predicate": pred,
                "object": obj,
                "subject_class": subj_class,
                "object_class": obj_class,
                "subject_start": subj_start,
                "subject_end": subj_end,
                "object_start": obj_start,
                "object_end": obj_end,
                "lang": language,
                # The hosted /extract contract returns no per-relation score.
                # Carry one ONLY when present so the fact write path applies its
                # documented floor rather than laundering a fabricated 1.0.
                "confidence": triple.get("confidence"),
            })
        return emitted

    def _annotate(
        self,
        signal: Signal,
        *,
        entities: list[dict[str, Any]],
        language: str | None,
        text_en: str | None = None,
        title_en: str | None = None,
        relations: list[dict[str, Any]] | None = None,
    ) -> Signal:
        """Return a copy of ``signal`` with ``payload['entities']`` set.

        M13: when the M11 translate route ran, ``text_en`` (the combined-text
        translation NER consumed) and ``title_en`` (the separate title
        translation) are stamped onto the payload so downstream readers narrate
        over English rather than the raw non-Latin surface. Both are best-effort:
        a ``None`` (translate failure / not the translate route) leaves the field
        absent, never an empty/placeholder value."""
        new_payload = dict(signal.payload) if isinstance(signal.payload, dict) else {}
        new_payload["entities"] = entities
        new_payload["ner_language"] = language
        new_payload["entities_hash"] = _entities_hash(entities)
        # The extractor's REAL head/tail pairs, kept beside the flattened entity
        # list so the fact stage never has to guess which endpoints went
        # together. Always stamped (even empty) on a route that ran the
        # extractor, so a consumer can distinguish "ran, no pairs" from "this
        # payload predates the pair surface" (absent key → legacy).
        if relations is not None:
            new_payload["relations"] = relations
        if text_en:
            new_payload["text_en"] = text_en
        if title_en:
            new_payload["title_en"] = title_en
        return signal.model_copy(update={"payload": new_payload})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_span(
    text_lower: str, needle_lower: str, *, start_at: int = 0
) -> tuple[int, int]:
    """Best-effort substring locate. Returns ``(-1, -1)`` when not found —
    consumers treat that as "offset unknown". The hosted /extract endpoint
    doesn't return offsets so this is a degraded surface compared to
    spaCy.

    ``start_at`` bounds the search to the text at/after that index, which lets
    a relation resolve its object to the occurrence FOLLOWING its subject
    rather than always the document's first mention of that surface.
    """
    if not needle_lower:
        return -1, -1
    idx = text_lower.find(needle_lower, max(0, start_at))
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
