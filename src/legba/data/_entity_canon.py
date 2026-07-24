# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entity-name canonicalization — surface-form merge + NER type correction.

SHARED CANON SPINE (W1 / remediation #1)
----------------------------------------
This module is the ONE canon every downstream producer routes through —
ingestion, the analyst resolver (``entity_resolution``), the reifier, and
``proposed_edge_governance``. It lives at ``legba.data._entity_canon`` (the
shared ``data`` layer) deliberately: a canon under
``legba.data.analysts.deterministic_handlers`` would force ingestion / the
reifier into a layering violation to reach it. The OLD path
(``legba.data.analysts.deterministic_handlers._entity_canon``) is a thin
re-export shim so existing imports keep working unchanged.

Hard layering rule: this module MUST NOT import from ``legba.data.analysts.*``
(that would re-introduce the violation + a circular import). It depends only on
``legba.data.vocabulary`` (a sibling leaf) + stdlib + pycountry.

THE PROBLEM THIS FIXES (data-quality audit, live-DB P1)
-------------------------------------------------------
``entity_resolution`` deduped NER spans by ``(text.lower(), entity_class)`` and
upserted that surface form verbatim. Two failure modes followed:

  * **Fragmentation.** ``{US, U.S., USA, United States, America}`` produced 9
    separate ``entity_profiles`` across 4 incompatible classes; ``"Trump"`` /
    ``"Donald Trump"`` / ``"Donald Trump's"`` / ``"Donald Trump 's"`` /
    ``"The Trump administration"`` fragmented further, several carrying raw
    HTML-entity garbage (``"Cape Verde&#039;s"``, ``"&apos;"``).
  * **Mistyping.** ``"United States"`` existed as BOTH a ``country`` AND a
    ``person``; 18 NWS forecast offices (``"NWS St Louis"``) were typed
    ``person``.

THE FIX (pure, deterministic — no SLM, no network, no GPU)
----------------------------------------------------------
:func:`canonicalize_entity` runs BEFORE the dedup key + upsert, so fragmented
surface forms converge onto ONE ``(lower(canonical_name), entity_class)`` row
and mistypes are corrected at write:

  1. **STRIP** — HTML-unescape (``html.unescape`` handles ``&#039;`` / ``&apos;``
     / ``&amp;`` / numeric + named), drop trailing possessives (``'s`` / `` 's``
     / ``’s``), strip surrounding quotes + punctuation, collapse whitespace.
  2. **ALIAS MAP** — a small, curated, conservative surface-form → canonical
     dict for the highest-frequency country collisions (US / UK / UAE / EU …).
     Unknown names pass through with only the strip applied.
  3. **TYPE CORRECTION** — a deterministic country gazetteer (pycountry — the
     SAME dataset the ``iso_countries`` table is generated from; see
     ``scripts/_gen_iso_countries_seed.py``) forces a canonicalized country
     name to class ``"country"`` (NEVER ``"person"``); an organization surface
     pattern (``"NWS …"`` / ``"National Weather Service"``) forces
     ``"organization"`` instead of ``"person"``. All target classes are members
     of the closed :data:`legba.data.vocabulary.ENTITY_CLASSES` taxonomy — no
     class string is invented here.

JUNK GATE (:func:`is_junk_entity`)
----------------------------------
The live review confirmed several recurring NER junk classes that were reaching
facts/entities/nexuses. The gate now rejects, predicate-driven (in addition to
the original tiny ``{tv, radio, online}`` base set):

  * **clock-times** — ``"6:53PM MDT"``, ``"10:00PM AKDT"`` (NWS forecast spans);
  * **leading-quantifier** — ``"More than 450,000"``, ``"hundreds of thousands"``;
  * **pure-numeric / percent / currency** — ``"1,200"``, ``"45%"``, ``"$3.2bn"``;
  * **length ≤ 2** — ``"F1"``, ``"Xi"``, ``"Co"`` (but NOT alias keys like
    ``US`` / ``UK`` / ``EU`` / ``UN``, which are exempted FIRST);
  * **residual HTML** — a span still carrying ``<img …>`` / ``</p>`` / ``&…;``.

Bare national demonyms are NOT junk-dropped: they are ROUTED through
:data:`_DEMONYM_MAP` so :func:`canonicalize_entity` collapses them to their
country (``"Iranian"`` → ``"Iran"``). Junk-dropping a demonym would lose the
referent entirely; ``proposed_edge_governance`` depends on
``is_junk_entity`` returning False for a demonym so the collapse happens
instead of the edge being discarded. Accordingly any alias key
(``US`` → United States), any country name, and any curated demonym is exempted
BEFORE the predicate checks, so legitimate short forms are never dropped.

Idempotent: ``canonicalize_entity(*canonicalize_entity(name, cls)) ==
canonicalize_entity(name, cls)`` — the strip + alias + type passes all reach a
fixed point in one application.
"""

from __future__ import annotations

import html
import re
from functools import lru_cache

try:
    import pycountry
except ModuleNotFoundError:  # pragma: no cover — slim deploy images (the registry)
    # omit the gazetteer dep. The full ISO-3166 set loads only where pycountry is
    # installed (the runtime); slim images fall back to the curated subset below.
    # This keeps a registry-side import of the handler package from hard-crashing.
    pycountry = None  # type: ignore[assignment]

from .vocabulary import ENTITY_CLASSES

# ---------------------------------------------------------------------------
# Canonical class strings — drawn from the closed taxonomy, never invented.
# ---------------------------------------------------------------------------

#: The class a country name is forced onto. Member of ENTITY_CLASSES.
COUNTRY_CLASS = "country"
#: The class an obvious-organization surface pattern (NWS …) is forced onto.
ORGANIZATION_CLASS = "organization"
#: The class a place / geographic surface (Quay/Tower/Islands/known city) is
#: forced onto — NEVER ``person``. Member of ENTITY_CLASSES.
LOCATION_CLASS = "location"
#: The generic fallback bucket (taxonomy default).
DEFAULT_CLASS = "entity"

assert COUNTRY_CLASS in ENTITY_CLASSES
assert ORGANIZATION_CLASS in ENTITY_CLASSES
assert LOCATION_CLASS in ENTITY_CLASSES
assert DEFAULT_CLASS in ENTITY_CLASSES


# ---------------------------------------------------------------------------
# STRIP — HTML entities, possessives, surrounding punctuation, whitespace.
# ---------------------------------------------------------------------------

#: Surrounding quote / punctuation characters peeled from both ends. Mirrors
#: ``ner._STRIP_CHARS`` (the upstream NER filter's strip set) so the two stay
#: consistent — a span the NER filter accepted, stripped the same way here.
_STRIP_CHARS = " \t\n\r\"'`.,;:!?()[]{}<>«»“”‘’"

#: Trailing possessive forms. ``html.unescape`` has already turned ``&#039;`` /
#: ``&apos;`` into a straight apostrophe and ``&#8217;`` into a curly one, and
#: the NER spacing variant ``"Donald Trump 's"`` leaves a space before the
#: clitic, so all three apostrophe glyphs + an optional space are matched.
_POSSESSIVE_RE = re.compile(r"\s*['’ʼ‘]s\Z", re.IGNORECASE)

_WHITESPACE_RE = re.compile(r"\s+")

#: Zero-width / invisible characters NER drags into a span (U+200B ZWSP, U+200C
#: ZWNJ, U+200D ZWJ, U+2060 WORD JOINER, U+FEFF BOM). Left in place they fork a
#: cluster ("​World Cup" vs "World Cup") and slip past the strip/whitespace
#: passes, so they are removed BEFORE any normalization (DQ P4 — zero-width leak).
_ZERO_WIDTH_CHARS = "​‌‍⁠﻿"
_ZERO_WIDTH_RE = re.compile(f"[{_ZERO_WIDTH_CHARS}]")

#: Leading article the/a/an (case-insensitive) — stripped for the class-agnostic
#: identity fold + map lookups so "the United Kingdom" folds onto "United
#: Kingdom" and "The Costa Rican" reaches the demonym map. A word-boundary guards
#: "theater"/"another" from a spurious strip.
_ARTICLE_RE = re.compile(r"^(?:the|a|an)\b\s*", re.IGNORECASE)

#: All non-alphanumeric runs — collapsed to nothing to build the identity fold
#: key ("U.S. Navy" and "US Navy" collapse to the same key once aliased).
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _strip_leading_article(s: str) -> str:
    """Drop a single leading article (the/a/an). Guarded: never blanks a name.

    Returns ``s`` unchanged when there is no leading article OR the remainder
    would be empty / < 2 chars (so "The" alone, or "A B", is left intact).
    """
    if not s:
        return s
    m = _ARTICLE_RE.match(s)
    if not m:
        return s
    rest = s[m.end():].strip()
    if len(rest) < 2:
        return s
    return rest


def _strip_name(name: str) -> str:
    """STRIP pass: HTML-unescape, drop possessive, peel punctuation, collapse WS.

    Pure + idempotent. Applied to BOTH the alias-map key lookup and the final
    surface form so an aliased name and a passed-through name are normalised the
    same way.
    """
    if not name:
        return ""
    # HTML entities first — ``Cape Verde&#039;s`` -> ``Cape Verde's`` so the
    # possessive strip below can then fire, and ``&amp;`` -> ``&``.
    s = html.unescape(name)
    # Drop zero-width / invisible chars so they never fork a cluster or survive
    # into the surface form (they slip past .strip() + the whitespace collapse).
    s = _ZERO_WIDTH_RE.sub("", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    # Peel a trailing possessive BEFORE the punctuation strip — otherwise the
    # punctuation strip would eat the apostrophe and leave a dangling ``s``.
    prev = None
    while prev != s:
        prev = s
        s = _POSSESSIVE_RE.sub("", s).strip()
    # Peel surrounding quotes / punctuation (both ends), then re-collapse.
    s = s.strip(_STRIP_CHARS)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s


# ---------------------------------------------------------------------------
# ALIAS MAP — curated, conservative surface-form → canonical country name.
# Keys are matched case-insensitively against the STRIPPED name. Values are
# canonical country names that the gazetteer below recognises (so the type
# correction then fires and stamps COUNTRY_CLASS).
# ---------------------------------------------------------------------------

_ALIAS_MAP: dict[str, str] = {
    # United States
    "us": "United States",
    "u.s.": "United States",
    "u.s": "United States",
    "usa": "United States",
    "u.s.a.": "United States",
    "u.s.a": "United States",
    "united states": "United States",
    "united states of america": "United States",
    "america": "United States",
    "the united states": "United States",
    # United Kingdom
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "u.k": "United Kingdom",
    "britain": "United Kingdom",
    "great britain": "United Kingdom",
    "united kingdom": "United Kingdom",
    # Other high-frequency collisions
    "uae": "United Arab Emirates",
    "u.a.e.": "United Arab Emirates",
    "united arab emirates": "United Arab Emirates",
    "eu": "European Union",
    "european union": "European Union",
    "drc": "Democratic Republic of the Congo",
    "south korea": "South Korea",
    "north korea": "North Korea",
    "russia": "Russia",
    # Supranational bodies kept as legitimate short orgs (NOT junk-dropped by
    # the length≤2 predicate): UN / WHO / NATO are real organizations.
    "un": "United Nations",
    "u.n.": "United Nations",
    "united nations": "United Nations",
}

#: Aliases that resolve to a supranational body, not an ISO country. The
#: gazetteer won't match these, so they carry their own class hint here.
_ALIAS_ORG: frozenset[str] = frozenset({"European Union", "United Nations"})


# ---------------------------------------------------------------------------
# DEMONYM MAP — national demonym/adjective → canonical country (DQ-H4). NER
# emits demonyms ("Iranian", "Israeli") as first-class entities distinct from
# their country ("Iran co-occurs with Iranian" — same referent), inflating
# graph centrality. Collapse the clear NATIONAL demonyms to their country so
# they merge. CURATED (not a suffix regex) so surnames like "Meloni" / words
# like "Asian"/"European" (no single country) are never mis-collapsed. Values
# are the same canonical forms the alias map + gazetteer use.
# ---------------------------------------------------------------------------

#: COMPREHENSIVE nationality-demonym → canonical country. The original curated
#: set was too small — the live review (D14 / proposed_edge_governance) showed
#: clear NATIONAL demonyms ("Albanian", "Belgian", "Kenyan", "Liberian",
#: "Bangladeshi") falling through ``is_demonym`` → re-leaking as distinct graph
#: nodes. This is now a wide curated map covering the ISO-3166-1 nations whose
#: single-country demonym is UNAMBIGUOUS. Values are the canonical forms the
#: gazetteer (``_country_name_set``) + alias map recognise, so a collapse is
#: followed by COUNTRY_CLASS typing. Curated (NOT a suffix regex): surnames
#: ("Meloni") + multi-country adjectives ("Asian", "European", "Arab",
#: "Latin", "African") are deliberately NOT here so they are never mis-collapsed.
_DEMONYM_MAP: dict[str, str] = {
    # --- original curated core (kept verbatim) -------------------------------
    "american": "United States",
    "british": "United Kingdom",
    "french": "France",
    "german": "Germany",
    "italian": "Italy",
    "spanish": "Spain",
    "russian": "Russia",
    "ukrainian": "Ukraine",
    "chinese": "China",
    "japanese": "Japan",
    "indian": "India",
    "pakistani": "Pakistan",
    "iranian": "Iran",
    "iraqi": "Iraq",
    "israeli": "Israel",
    # Resolve to the bare "Palestine" (now in the country gazetteer) so the
    # demonym, the bare name, and the plural all fold to ONE country referent.
    "palestinian": "Palestine",
    "syrian": "Syria",
    "lebanese": "Lebanon",
    "yemeni": "Yemen",
    "saudi": "Saudi Arabia",
    "egyptian": "Egypt",
    "turkish": "Turkey",
    "qatari": "Qatar",
    "afghan": "Afghanistan",
    "polish": "Poland",
    "canadian": "Canada",
    "mexican": "Mexico",
    "brazilian": "Brazil",
    "argentine": "Argentina",
    "argentinian": "Argentina",
    "australian": "Australia",
    "indonesian": "Indonesia",
    "nigerian": "Nigeria",
    "venezuelan": "Venezuela",
    "sudanese": "Sudan",
    "south korean": "South Korea",
    "north korean": "North Korea",
    # --- comprehensive expansion (live-review misses + wide ISO coverage) ----
    # Europe
    "albanian": "Albania",
    "austrian": "Austria",
    "belgian": "Belgium",
    "bosnian": "Bosnia and Herzegovina",
    "bulgarian": "Bulgaria",
    "croatian": "Croatia",
    "cypriot": "Cyprus",
    "czech": "Czechia",
    "danish": "Denmark",
    "dutch": "Netherlands",
    "estonian": "Estonia",
    "finnish": "Finland",
    "greek": "Greece",
    "hungarian": "Hungary",
    "icelandic": "Iceland",
    "irish": "Ireland",
    "latvian": "Latvia",
    "lithuanian": "Lithuania",
    "luxembourgish": "Luxembourg",
    "macedonian": "North Macedonia",
    "maltese": "Malta",
    "moldovan": "Moldova",
    "montenegrin": "Montenegro",
    "norwegian": "Norway",
    "portuguese": "Portugal",
    "romanian": "Romania",
    "serbian": "Serbia",
    "slovak": "Slovakia",
    "slovenian": "Slovenia",
    "swedish": "Sweden",
    "swiss": "Switzerland",
    "belarusian": "Belarus",
    "georgian": "Georgia",
    "armenian": "Armenia",
    "azerbaijani": "Azerbaijan",
    "kosovar": "Kosovo",
    # Middle East / Central & South Asia
    "jordanian": "Jordan",
    "kuwaiti": "Kuwait",
    "omani": "Oman",
    "bahraini": "Bahrain",
    "emirati": "United Arab Emirates",
    "kazakh": "Kazakhstan",
    "uzbek": "Uzbekistan",
    "turkmen": "Turkmenistan",
    "tajik": "Tajikistan",
    "kyrgyz": "Kyrgyzstan",
    "bangladeshi": "Bangladesh",
    "sri lankan": "Sri Lanka",
    "nepalese": "Nepal",
    "nepali": "Nepal",
    "bhutanese": "Bhutan",
    "maldivian": "Maldives",
    "burmese": "Myanmar",
    "myanmarese": "Myanmar",
    # East / Southeast Asia & Pacific
    "thai": "Thailand",
    "vietnamese": "Vietnam",
    "cambodian": "Cambodia",
    "laotian": "Laos",
    "malaysian": "Malaysia",
    "singaporean": "Singapore",
    "filipino": "Philippines",
    "philippine": "Philippines",
    "mongolian": "Mongolia",
    "taiwanese": "Taiwan, Province of China",
    "bruneian": "Brunei Darussalam",
    "fijian": "Fiji",
    "papua new guinean": "Papua New Guinea",
    "new zealander": "New Zealand",
    # Africa
    "kenyan": "Kenya",
    "liberian": "Liberia",
    "ethiopian": "Ethiopia",
    "ghanaian": "Ghana",
    "senegalese": "Senegal",
    "ivorian": "Côte d'Ivoire",
    "malian": "Mali",
    "nigerien": "Niger",
    "chadian": "Chad",
    "cameroonian": "Cameroon",
    "congolese": "Congo",
    "angolan": "Angola",
    "mozambican": "Mozambique",
    "zambian": "Zambia",
    "zimbabwean": "Zimbabwe",
    "malawian": "Malawi",
    "tanzanian": "Tanzania",
    "ugandan": "Uganda",
    "rwandan": "Rwanda",
    "burundian": "Burundi",
    "somali": "Somalia",
    "eritrean": "Eritrea",
    "djiboutian": "Djibouti",
    "south african": "South Africa",
    "namibian": "Namibia",
    "botswanan": "Botswana",
    "moroccan": "Morocco",
    "algerian": "Algeria",
    "tunisian": "Tunisia",
    "libyan": "Libya",
    "mauritanian": "Mauritania",
    "gabonese": "Gabon",
    "togolese": "Togo",
    "beninese": "Benin",
    "guinean": "Guinea",
    "gambian": "Gambia",
    "sierra leonean": "Sierra Leone",
    "madagascan": "Madagascar",
    "malagasy": "Madagascar",
    # Americas
    "colombian": "Colombia",
    "peruvian": "Peru",
    "chilean": "Chile",
    "bolivian": "Bolivia",
    "ecuadorian": "Ecuador",
    "paraguayan": "Paraguay",
    "uruguayan": "Uruguay",
    "guatemalan": "Guatemala",
    "honduran": "Honduras",
    "salvadoran": "El Salvador",
    "nicaraguan": "Nicaragua",
    "costa rican": "Costa Rica",
    "panamanian": "Panama",
    "cuban": "Cuba",
    "dominican": "Dominican Republic",
    "haitian": "Haiti",
    "jamaican": "Jamaica",
    "trinidadian": "Trinidad and Tobago",
}

# ---------------------------------------------------------------------------
# REGION-ADJECTIVE MAP — multi-country / continental adjectives (DQ P4, the
# operator's Africa/African/Africans case). The DEMONYM map deliberately
# EXCLUDES these (a continent is not a single country), so they leaked as
# distinct nodes ("African" person + "Africa" location + "Africans"). This is a
# SEPARATE, tight, unambiguous curated map: each value is a CONTINENT (a
# LOCATION, never a country). Deliberately conservative — "arab", "latin",
# "western", "eastern", "scandinavian" are ambiguous and are NOT here.
# ---------------------------------------------------------------------------
_REGION_ADJECTIVE_MAP: dict[str, str] = {
    "african": "Africa",
    "european": "Europe",
    "europe": "Europe",
    "asian": "Asia",
    "north american": "North America",
    "south american": "South America",
    "oceanian": "Oceania",
    "antarctic": "Antarctica",
}


def _collapse_target(low: str) -> str | None:
    """Map a demonym / region-adjective / alias key → its canonical referent.

    Pure + deterministic. ``low`` is an already-stripped, lower-cased surface
    form. Returns the canonical name a collapse should land on, else ``None``
    (pass-through). Order: region adjective (continent) → national demonym
    (country) → alias (country/org). A GUARDED de-pluralization then retries the
    singular ONLY when the singular is itself a demonym / region adjective
    ("Africans"→"African"→Africa, "Americans"→"American"→United States) — never
    blind stemming, so an arbitrary plural name is left untouched.
    """
    if low in _REGION_ADJECTIVE_MAP:
        return _REGION_ADJECTIVE_MAP[low]
    if low in _DEMONYM_MAP:
        return _DEMONYM_MAP[low]
    if low in _ALIAS_MAP:
        return _ALIAS_MAP[low]
    # Guarded de-pluralization: strip a single trailing 's' and retry, but ONLY
    # when the singular is a known demonym / region adjective (len(singular) > 3).
    if len(low) > 4 and low.endswith("s"):
        singular = low[:-1]
        if len(singular) > 3:
            if singular in _REGION_ADJECTIVE_MAP:
                return _REGION_ADJECTIVE_MAP[singular]
            if singular in _DEMONYM_MAP:
                return _DEMONYM_MAP[singular]
    return None


#: Short / junk tokens NER mis-emits as entities (DQ-H4). The original TINY base
#: set, kept verbatim. Predicate-based junk classes (clock-times, quantifiers,
#: numerics, length≤2, residual HTML, bare demonyms) are layered ON TOP in
#: :func:`is_junk_entity` — this frozenset stays the literal-token base.
_JUNK_ENTITIES: frozenset[str] = frozenset({"tv", "radio", "online"})

#: Pure articles / closed-class function words NER occasionally emits as a bare
#: "entity" ("the", "and", "of"). These are NEVER a referent, so they must be
#: junk-rejected — the DQ P4 merge review found a bare "the" (len 3, so the
#: length≤2 rule missed it) being ELECTED as a fold survivor. Curated + tight:
#: only articles / conjunctions / prepositions (a closed class), never a content
#: word, and only checked AFTER the alias/demonym/country exemption so a real
#: short form (US/UK/EU/UN) is unaffected. The ≤2-char members ("a", "an", "of",
#: "to", …) are already caught by the length rule; the ≥3-char members ("the",
#: "and", "but", "for", "from", "with", "into", "onto") are the ones this adds.
_STOPWORD_ENTITIES: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "but", "nor",
    "of", "to", "in", "on", "at", "by", "for", "from", "with", "as",
    "into", "onto", "off", "out", "up", "down",
})

#: Bare spelled-out cardinal / ordinal number-words NER emits as a standalone
#: "entity" ("Two", "first"). Like the stopword set, these are a closed class
#: that is NEVER a real referent on its own, yet they slip past the numeric
#: predicate (which only matches DIGITS) and the length rule (>2 chars), so the
#: DQ P4 merge review found "Two" (295 links) and "first" (326 links) elected as
#: fold survivors. Mirrors ``legba.data.filters.fact_extractor._NUMBER_WORDS`` /
#: ``_ORDINAL_WORDS`` but is kept self-contained here (the canon is a leaf module
#: — importing fact_extractor would be circular). Checked AFTER the alias /
#: demonym / country exemption, so a real short form (US/UK/EU/UN) is unaffected.
#: ``last`` / ``next`` are deliberately excluded (positional adverbs, not numbers).
_NUMBER_WORD_ENTITIES: frozenset[str] = frozenset({
    # cardinals
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
    "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred",
    "thousand", "million", "billion", "trillion", "dozen", "couple",
    # ordinals
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
    "eighth", "ninth", "tenth", "eleventh", "twelfth", "thirteenth",
    "twentieth", "thirtieth",
})

# ---------------------------------------------------------------------------
# DQ M7 (2026-07-06 nexus-write audit) — VAGUE single-token endpoints the two
# nexus gates (proposed_edge_governance / relationship_reifier) minted as
# relationship endpoints and pushed into the signed / hostility graph:
#   "West hostile to the Islamic Revolution", "IRNA co occurs with Islamic",
#   "United States hostile to Leader", "Europe co occurs with annual".
# A bare directional / bloc adjective, a bare ideological adjective, a bare
# generic leadership role, or a bare frequency adjective is NEVER a resolvable
# actor — as a nexus endpoint it is noise. Consumed by :func:`is_junk_entity`.
# ---------------------------------------------------------------------------

#: Bare vague tokens that are never a specific actor. CURATED + tight
#: (whole-surface, single-token). A REAL single-token actor (a country / an
#: alias / a demonym → country) is EXEMPTED in :func:`is_junk_entity` BEFORE this
#: set is consulted, so "China" / "NATO" / "Iran" / "Hamas" / "Houthi" are never
#: touched. Deliberately EXCLUDES sectarian / ethnic nouns (Shia / Sunni / Arab /
#: Muslim) and institution fragments (State / Army / Report / Parliament) — those
#: collide with real referents too readily; only the unambiguously-vague
#: directional / ideological / role / frequency adjectives are members.
_VAGUE_ENDPOINT_TOKENS: frozenset[str] = frozenset({
    # directional / bloc adjectives — no single referent as a bare token
    "west", "western", "eastern", "northern", "southern",
    # ideological adjective (audit: "Islamic") + closest sibling
    "islamic", "islamist",
    # generic leadership role (audit: "Leader")
    "leader", "leaders", "leadership",
    # frequency adjectives (audit: "annual")
    "annual", "annually", "yearly", "daily", "weekly", "monthly",
    "quarterly", "biannual", "semiannual", "biennial",
    # E1 / j4 (2026-07-10): the BARE "Resistance" fragment. The M12/reenrich
    # telegram backfill minted it as a `person` and the reifier forged
    # `Resistance —hostile to→ United States`, which led the journal's entry.
    # Matched EXACT-on-stripped (is_junk_entity :1078 tests `low in`), and the
    # canon strips a leading article first, so this rejects ONLY "Resistance" /
    # "the Resistance" — never "Axis of Resistance" or "French Resistance"
    # (a real coalition/movement keeper keeps its full surface).
    "resistance",
})

#: M20 (2026-07-06 mining audit) — TRUNCATED institution / agency abbreviations
#: the NER emitted as clipped fragments ("Parl" from "Parliament", "Fed" from a
#: "Federal …" span). These are NOT the full institution WORDS deliberately
#: excluded from :data:`_VAGUE_ENDPOINT_TOKENS` above (a whole "Parliament" /
#: "State" collides with real referents) — they are broken clippings that name no
#: single actor as a bare nexus endpoint. Surfacing them as hostile-edge
#: endpoints ("Parl hostile to X", "Fed hostile to Y") amplifies an NER error
#: into headline geopolitical signal. Consumed by :func:`is_junk_entity`.
_TRUNCATED_INSTITUTION_FRAGMENTS: frozenset[str] = frozenset({
    "parl", "fed",
})

#: DQ M7 — bare PLURAL quantifier words ("Hundreds", "Millions", "Thousands")
#: the nexus gates minted as endpoints ("Hundreds hostile to Israel", "Iraq
#: involved in millions"). The SINGULAR magnitude words already live in
#: :data:`_NUMBER_WORD_ENTITIES`; their plurals slip past it (and past the
#: DIGIT-only numeric predicate + the "hundreds of X" ``_QUANTIFIER_RE``, which
#: needs the trailing "of X"). A bare quantifier plural is never a referent.
_QUANTIFIER_PLURAL_ENTITIES: frozenset[str] = frozenset({
    "hundreds", "thousands", "millions", "billions", "trillions", "dozens",
})


# ---------------------------------------------------------------------------
# JUNK PREDICATES — the live junk classes the review confirmed. Each matches on
# the STRIPPED surface form (post html-unescape, post possessive/punct strip).
# ---------------------------------------------------------------------------

#: Clock-times: "6:53PM MDT", "10:00PM AKDT", "23:00", "9 AM". Hour:minute(:sec)
#: optionally followed by AM/PM and a 2-4 letter tz abbreviation; or a bare
#: "<hour> AM/PM". Anchored — the WHOLE stripped name must be a clock time.
_CLOCK_RE = re.compile(
    r"""^\s*
        \d{1,2}:\d{2}(?::\d{2})?      # H:MM or H:MM:SS
        \s*(?:[AP]M)?                 # optional AM/PM
        \s*(?:[A-Z]{2,4})?            # optional timezone abbrev (MDT/AKDT/UTC)
        \s*$
        |
        ^\s*\d{1,2}\s*[AP]M\s*(?:[A-Z]{2,4})?\s*$   # bare "9 PM", "9PM EDT"
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Leading quantifier phrases: "More than 450,000", "hundreds of thousands",
#: "at least 12", "up to 3 million", "tens of thousands", "over 1,000". The
#: span LEADS with a quantifier word/number-range, not a named referent.
_QUANTIFIER_RE = re.compile(
    r"""^\s*(?:
            (?:more|less|fewer|greater)\s+than\b
          | at\s+(?:least|most)\b
          | up\s+to\b
          | (?:an\s+)?estimated\b
          | (?:about|around|nearly|approximately|over|under|roughly)\b
          | (?:tens|hundreds|thousands|millions|billions)\s+of\b
          | (?:dozens|scores)\s+of\b
        )
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Pure numeric / percentage / currency: "1,200", "45%", "$3.2bn", "3.5 million",
#: "€12", "12.5", "1.2bn". Anchored — no letters other than a magnitude suffix
#: (k/m/bn/tn) or a leading currency symbol/code. A name carrying real words
#: (e.g. "Boeing 737") is NOT matched.
_NUMERIC_RE = re.compile(
    r"""^\s*
        [-+]?
        (?:[$€£¥]|usd|eur|gbp|jpy|cny|rs\.?)?   # optional currency prefix
        \s*
        \d[\d,]*(?:\.\d+)?                       # the number itself
        \s*
        (?:%|percent|k|m|bn|tn|billion|million|thousand|trillion)?  # magnitude
        \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Residual HTML: the span still carries a tag (<img …>, </p>) or an unescaped
#: entity (&nbsp;, &#039;). After a clean strip these are gone; their presence
#: means a malformed span the NER never should have emitted. The original pattern
#: required a COMPLETE tag with a closing ``>`` — truncated NER spans
#: ("Iran</p", "/>Iranian", "the Middle East.</p", "… < a") slipped through. The
#: junk gate now ALSO rejects any span still carrying a bare ``<`` or ``>`` (see
#: :func:`is_junk_entity`), which catches every partial-tag residue class; the
#: residue-stripped fold (:func:`_strip_residue_for_fold`) still folds
#: "Iran</p" onto "Iran" so the historical merge migration re-points it.
_HTML_RESIDUE_RE = re.compile(r"</?[a-z][^>]*>|&[a-z]+;|&#\d+;", re.IGNORECASE)


def _strip_residue_for_fold(s: str) -> str:
    """Return ``s`` with HTML / markdown residue + partial-tag fragments removed.

    Used ONLY to build the identity fold (NOT the forward surface form — a
    residue-bearing span stays junk-rejected at write time). Turns "Iran</p" ->
    "Iran", "/>Iranian" -> "Iranian", "the Middle East.</p" -> "the Middle East.",
    "State's < a" -> "State's"; and (B0-7, the telegram markdown leak)
    "[**Ali Khamenei**](https://x.y)" -> "Ali Khamenei",
    "Ayatollah Ali Khamenei**](https://f24.my" -> "Ayatollah Ali Khamenei",
    so a junk-shaped historical row folds onto its clean survivor.
    Pure + deterministic + idempotent.
    """
    if not s:
        return ""
    s = html.unescape(s)
    s = _ZERO_WIDTH_RE.sub("", s)
    # Complete tags first (<img …>, </p>).
    s = re.sub(r"<[^>]*>", " ", s)
    # A trailing partial tag: a '<' with no closing '>' to end-of-string.
    s = re.sub(r"<[^>]*$", " ", s)
    # A leading partial tag END fragment ('/>' or bare '>').
    s = re.sub(r"^\s*/?>\s*", " ", s)
    # Any HTML entity that survived the unescape (defensive).
    s = re.sub(r"&[a-z]+;|&#\d+;", " ", s, flags=re.IGNORECASE)
    # Markdown residue (B0-7): a COMPLETE link/image wrapper first
    # ("[**t**](url)" -> "**t**", "![alt](url)" -> "alt"), …
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)
    # … then a TRAILING partial-link fragment — anything from a residual "](
    # to end-of-string (the truncated-span analogue of the partial tag above;
    # "Khamenei**](https://f24.my" drops the URL tail), …
    s = re.sub(r"\]\(.*$", " ", s, flags=re.DOTALL)
    # … then bare bold markers left behind by either pass.
    s = s.replace("**", "")
    return _WHITESPACE_RE.sub(" ", s).strip()

#: Money / currency tokens the live review found leaking as entities:
#: "S$2,500", "US$ 525 million", "$3.2bn", "€12 billion", "Rs. 1,000 crore".
#: The pure-numeric ``_NUMERIC_RE`` only covers a SINGLE leading symbol/code
#: ("$3.2bn"); a compound currency PREFIX ("S$", "US$", "C$", "HK$", "A$",
#: "NZ$", "R$") in front of a number is the gap. Anchored — the whole stripped
#: name must be a currency-amount (a leading currency cluster, a number, an
#: optional magnitude word). A name with real trailing words ("Boeing 737") is
#: NOT matched (no currency cluster, no anchor).
_MONEY_RE = re.compile(
    r"""^\s*
        [-+]?
        (?:                                  # currency cluster (required)
            [A-Za-z]{0,3}\s*[$€£¥₹₩]          #   "S$", "US$", "HK$", bare "$"
          | (?:usd|eur|gbp|jpy|cny|inr|krw|aud|cad|chf|hkd|sgd|rmb|rs|rs\.)
        )
        \s*
        \d[\d,]*(?:\.\d+)?                    # the amount
        \s*
        (?:%|k|m|bn|tn|billion|million|thousand|trillion|crore|lakh)?  # magnitude
        \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Sports / competition-structure NOISE surfaces NER mis-emits as entities from
#: the World-Cup feed: "World Cup", "Group F" (and "Group A".."Group Z"),
#: "Round of 16", "Champions League". These are event/competition scaffolding,
#: not typed geopolitical entities — they double-count and pollute the graph.
#: Curated literal set (lower-cased) + a "Group <letter>" / "Round of N" shape.
#: Conservative: a real org that merely CONTAINS one of these words is unaffected
#: (the literal set is whole-surface, the shapes are anchored).
_SPORTS_NOISE_LITERALS: frozenset[str] = frozenset({
    "world cup", "champions league", "europa league", "premier league",
    "la liga", "bundesliga", "serie a", "ligue 1", "euro 2024", "euro 2028",
    "copa america", "africa cup of nations", "afcon", "super bowl",
    "world series", "stanley cup", "olympics", "olympic games",
    "grand prix", "wimbledon", "us open", "french open", "australian open",
    "group stage", "knockout stage", "quarter-finals", "semi-finals",
    "the final", "the semifinal", "the quarterfinal",
})
_SPORTS_NOISE_RE = re.compile(
    r"""^\s*(?:
            group\s+[a-z]                 # "Group F", "Group A".."Group Z"
          | round\s+of\s+\d+              # "Round of 16"
          | (?:quarter|semi)[\s-]?finals?  # "Quarter-final(s)"
          | matchday\s+\d+                # "Matchday 3"
        )\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Age / time-span tokens the live review found leaking as entities:
#: "51 - year - old", "2,600 - year - old", "24 - year - old", "centuries",
#: "decades", "a century", "millennia". NER drags the hyphenated age compound
#: in verbatim (spaced hyphens are the GLiREL tokenization), and bare
#: time-span nouns are not entities. Anchored on the stripped surface.
#: NOTE (adversarial #2): bare SINGULAR "century" is deliberately NOT matched
#: here — it is a real brand ("Century Aluminum"); "a century"/"the century"
#: (modified) is caught by :func:`_is_temporal_surface` instead, and the plural
#: "centuries" stays junk here.
_AGE_TIME_RE = re.compile(
    r"""^\s*(?:
            \d[\d,]*\s*-?\s*years?\s*-?\s*old   # "51 - year - old", "24-year-old"
          | (?:a|an|the)?\s*
            (?:centuries|decades?|millenni(?:um|a)|generations?)
        )\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


# ---------------------------------------------------------------------------
# DQ M5 (2026-07-06 audit) — entity-write junk classes the earlier predicates
# missed: number+unit quantity ("188,000 barrels", "770 bln won", "four million
# euros"), a possessive-KINSHIP referring expression ("Donald Trump's son"), and
# a bare temporal / duration surface ("last week", "the 21st century", "Today").
# These were the P5 FACT gate's remit but were never reached on the entity path;
# they are added here (the ONE canon) so is_junk_entity rejects them everywhere.
# ---------------------------------------------------------------------------

#: Measurement / currency UNIT nouns that trail a quantity ("188,000 barrels",
#: "70 bln euros", "seven tons"). A surface built ONLY of numbers + qualifiers +
#: one of these unit nouns is a quantity, never an entity. Curated + conservative
#: — a nominal token anywhere in the surface keeps it (so "Boeing 737", "five US
#: senators", "Boat People" are NOT flagged).
_UNIT_NOUNS: frozenset[str] = frozenset({
    "barrel", "barrels", "bpd", "bbl", "bbls",
    "won", "euro", "euros", "dollar", "dollars", "yen", "yuan", "rupee",
    "rupees", "pound", "pounds", "ruble", "rubles", "rouble", "roubles",
    "dinar", "dinars", "riyal", "riyals", "rial", "rials", "shekel",
    "shekels", "franc", "francs", "lira", "peso", "pesos", "hryvnia",
    "hryvnias", "hryvnie", "dirham", "dirhams", "rand", "baht", "ringgit",
    "cent", "cents", "percent", "crore", "lakh",
    "tonne", "tonnes", "ton", "tons", "kg", "kilograms", "kilogram",
    "litre", "litres", "liter", "liters", "gallon", "gallons",
    "hectare", "hectares", "acre", "acres", "km", "kilometre", "kilometres",
    "kilometer", "kilometers", "mile", "miles", "metre", "metres", "meter",
    "meters", "mw", "gw", "kwh", "twh", "mwh", "kt", "ounce", "ounces",
    "troop", "troops", "soldier", "soldiers", "missile", "missiles",
    "drone", "drones", "rocket", "rockets", "warhead", "warheads",
    "people", "casualties", "casualty", "death", "deaths",
})

#: Currency symbol characters + compound codes ("$", "US$", "usd"), recognised as
#: a currency marker inside :func:`_is_quantity_unit_phrase`. A bare symbol token
#: ("$"), a compound-prefixed number ("US$525", "$5B"), or a bare code ("usd")
#: all mark the surface as carrying money — the review found the spaced-symbol
#: shapes ("$ 307 mln", "€ 1.5B") leaking because the old set held only currency
#: WORDS ("dollar"), not symbols.
_CURRENCY_SYMBOL_CHARS = "$€£¥₹₩₽"
_CURRENCY_CODES: frozenset[str] = frozenset({
    "usd", "eur", "gbp", "jpy", "cny", "inr", "krw", "aud", "cad", "chf",
    "hkd", "sgd", "rmb", "rs", "us$", "s$", "c$", "hk$", "a$", "nz$", "r$",
    "n$", "aed", "sar", "qar", "kwd", "zar", "ngn", "php", "idr", "myr",
})
_CURRENCY_PREFIX_RE = re.compile(rf"^[a-z]{{0,3}}[{re.escape(_CURRENCY_SYMBOL_CHARS)}]")
#: A magnitude abbreviation glued to a number token ("1.5B", "307mln", "5bn").
_MAGNITUDE_SUFFIX_RE = re.compile(
    r"(?:k|m|mn|mln|mil|b|bn|bln|bil|t|tn|tln|trn|trln)$", re.IGNORECASE)


def _qty_number_core(t: str) -> tuple[bool, bool]:
    """Return ``(is_number, wore_currency_prefix)`` for a candidate quantity token.

    Strips an optional leading currency cluster (``$`` / ``US$`` / ``€``) and a
    trailing magnitude / percent suffix (``B`` / ``mln`` / ``%``) and reports
    whether the remainder is a bare number — so ``"$5B"``, ``"€1.5b"``,
    ``"307mln"`` and ``"US$525"`` all read as numbers (the second flag also
    marks the surface as carrying a currency)."""
    s = t
    m = _CURRENCY_PREFIX_RE.match(s)
    had_cur = bool(m)
    if m:
        s = s[m.end():]
    s = s.rstrip("%")
    s = _MAGNITUDE_SUFFIX_RE.sub("", s)
    return (bool(_NUM_TOKEN_RE.match(s)), had_cur)

#: COUNT-noun "units" (point/seat/vote) that also occur in PLACE names
#: ("Five Points", "Four Points" — real neighborhoods use spelled number-WORDS).
#: Junk quantity only when paired with a DIGIT number ("300 seats", "45 votes"),
#: NOT a number-word — so a number-word place name is kept (adversarial #4).
_COUNT_UNIT_NOUNS: frozenset[str] = frozenset({
    "vote", "votes", "seat", "seats", "point", "points",
})

#: Magnitude words / abbreviations that count as part of the NUMBER, not a unit
#: ("770 bln won", "20 M barrels"). Distinct from _UNIT_NOUNS.
_MAGNITUDE_TOKENS: frozenset[str] = frozenset({
    "bn", "bln", "b", "mn", "mln", "m", "k", "tn", "tln", "trn", "trln", "t",
    "billion", "million", "thousand", "trillion", "hundred", "bil", "mil",
})

#: Filler tokens carrying no entity content inside a quantity phrase. Extended
#: (2026-07-12 review) with the connectors / approximators / time-words the live
#: relic shapes wear: "per" ("per day"), "almost"/"just"/"only", "additional"/
#: "further"/"initial", "between"/"as"/"high"/"low", "cubic" ("cubic meters"),
#: "worth", and the temporal tails ("per day", "-year-old"). These fire ONLY
#: alongside a real number + a currency/unit marker (see _is_quantity_unit_phrase),
#: so a real name ("New Year", "Fort Worth") is never flagged.
_QTY_QUALIFIERS: frozenset[str] = frozenset({
    "of", "the", "a", "an", "and", "at", "least", "most", "more", "less",
    "than", "about", "around", "approximately", "nearly", "over", "under",
    "up", "to", "some", "several", "roughly",
    "per", "almost", "just", "only", "additional", "further", "initial",
    "between", "estimated", "as", "high", "low", "cubic", "worth", "point",
    "day", "days", "year", "years", "hour", "hours", "month", "months",
    "week", "weeks",
})

#: A bare number token: leading digit, then digits / thousands-separators /
#: decimals only ("188,000", "1.34", "55.3").
_NUM_TOKEN_RE = re.compile(r"^[0-9][0-9,.]*$")


def _is_quantity_unit_phrase(stripped: str) -> bool:
    """True when ``stripped`` is ENTIRELY a number + a measurement/currency unit.

    Conservative by construction: EVERY token must be a number
    (digit-token / spelled number-word / magnitude), a quantity qualifier, or a
    unit noun, AND the surface must contain at least one number AND at least one
    unit noun. A single nominal token (a real name) keeps the surface — so
    "Boeing 737", "Group of 20", "five US senators" are NOT flagged, while
    "188,000 barrels" / "770 bln won" / "four million euros" are.
    """
    tokens = [t.strip(_STRIP_CHARS).lower() for t in stripped.split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return False
    has_digit = has_num_word = has_unit = has_count_unit = False
    for t in tokens:
        if _NUM_TOKEN_RE.match(t):
            has_digit = True
        elif t in _NUMBER_WORD_ENTITIES or t in _MAGNITUDE_TOKENS:
            has_num_word = True
        elif t in _UNIT_NOUNS:
            has_unit = True
        elif t in _COUNT_UNIT_NOUNS:
            has_count_unit = True
        elif t in _QTY_QUALIFIERS:
            continue
        elif t in _CURRENCY_CODES or all(ch in _CURRENCY_SYMBOL_CHARS for ch in t):
            # a bare currency symbol ("$") / code ("usd") — a money marker.
            has_unit = True
        else:
            # a number wearing a currency prefix and/or magnitude suffix
            # ("$5B", "€1.5b", "307mln", "US$525") — the spaced-symbol shapes the
            # old set missed. A currency prefix ALSO marks the surface as money.
            is_num, had_cur = _qty_number_core(t)
            if not is_num:
                return False  # a nominal token — not a pure quantity phrase
            has_digit = True
            if had_cur:
                has_unit = True
    # A measurement/currency unit is junk with ANY number (digit or word):
    # "188,000 barrels", "four million euros", "seven tons".
    if has_unit and (has_digit or has_num_word):
        return True
    # A count-noun unit (point/seat/vote) is junk ONLY with a DIGIT number
    # ("300 seats", "45 votes") — a number-WORD form is a place ("Five Points").
    if has_count_unit and has_digit:
        return True
    return False


#: Possessive-KINSHIP referring expression ("Donald Trump's son", "Netanyahu's
#: wife"): a name + possessive clitic + a SPECIFIC kinship noun. Not a resolvable
#: entity. The kinship set is deliberately TIGHT (singular blood/marriage
#: relations only) so a real title ("The Battle for the World's Children") — the
#: generic "children" / "family" — is NOT caught.
_POSSESSIVE_KINSHIP_RE = re.compile(
    r"['’ʼ‘]s\s+(?:son|sons|daughter|daughters|wife|husband|brother|brothers"
    r"|sister|sisters|father|mother|widow|nephew|niece|cousin|grandson"
    r"|granddaughter)\s*$",
    re.IGNORECASE,
)

#: Temporal MODIFIERS that turn a brand-ambiguous noun into a clear duration
#: phrase ("last week", "the 21st century", "this month").
_TEMPORAL_MODIFIERS: frozenset[str] = frozenset({
    "the", "a", "an", "this", "last", "next", "coming", "past", "previous",
    "first", "second", "third", "fourth", "fifth", "sixth",
})

#: Bare single-token temporal surfaces that are ALWAYS junk — no common
#: brand/entity collision ("yesterday", "morning", "midnight").
_TEMPORAL_BARE_JUNK: frozenset[str] = frozenset({
    "weekend", "month", "year", "decade", "morning", "afternoon", "evening",
    "night", "midnight", "yesterday", "tomorrow", "hour",
})

#: Brand-AMBIGUOUS temporal nouns: real single-token referents exist (Today the
#: NBC show, Noon the retailer, Century Aluminum, The Week / The Day outlets), so
#: these are junk ONLY when carrying a temporal modifier ("last week", "the day")
#: — NEVER as a bare token (adversarial #2).
_TEMPORAL_MODIFIED_NOUNS: frozenset[str] = frozenset({
    "today", "noon", "century", "centuries", "week", "weeks", "day", "days",
    "month", "months", "year", "years", "quarter", "quarters", "hour", "hours",
    "decade", "decades", "weekend",
})

_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_ORDINAL_CENTURY_RE = re.compile(r"^\d{1,3}(?:st|nd|rd|th)\s+century$", re.IGNORECASE)
_PAST_N_RE = re.compile(
    r"^past\s+\d+\s+(?:hours?|days?|weeks?|months?|years?)$", re.IGNORECASE
)

#: DQ M2 (2026-07-06 fact-write audit) — a RELATIVE-past/forward duration phrase
#: ("250 years ago", "3 days ago", "two decades earlier"). A real entity is
#: NEVER "N <unit> ago", so this is a zero-false-positive temporal reject that
#: the bare/modified-noun logic below misses (it LEADS with a number, not a
#: temporal modifier). Anchored on the whole surface.
_RELATIVE_AGO_RE = re.compile(
    r"^\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?"
    r"|decades?|centuries)\s+(?:ago|earlier|later|back)$",
    re.IGNORECASE,
)

#: DQ M2 — calendar MONTH names, used ONLY in COMBINATION with a RELATIVE
#: temporal modifier ("December last year", "last November"). A BARE month is
#: deliberately NOT junk (name collisions: Theresa May, the name "August",
#: "March on Washington"), and a NAMED CALENDAR DATE / event ("September 11",
#: "October 7", "March 2022") is NOT junk either (F4) — a month is temporal only
#: when a relative modifier is present AND every other token is temporal. Full
#: names only (abbreviations like "mar"/"may"/"aug" collide too readily).
_MONTH_NAMES: frozenset[str] = frozenset({
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
})

#: F4 (2026-07-06 review) — the RELATIVE temporal modifiers that turn a month /
#: noun into junk ("last November", "December last year"). A subset of
#: _TEMPORAL_MODIFIERS excluding the article/positional members ("the"/"a"/"an"/
#: "first".."sixth") so a bare "Month <day-or-year-number>" is NOT dropped.
_RELATIVE_TEMPORAL_MODIFIERS: frozenset[str] = frozenset({
    "last", "next", "this", "coming", "previous", "past",
})


def _is_temporal_surface(stripped: str) -> bool:
    """True when ``stripped`` is a bare/modified temporal-duration surface (junk).

    Conservative on brand collisions (adversarial #2): a brand-ambiguous bare
    token ("Today", "Noon", "Century", "Week", "Day") is KEPT; it is junk only
    with a temporal modifier ("last week", "the day"). Clearly-temporal bare
    tokens ("yesterday", "morning", "midnight") and every date/period SHAPE
    ("2026", "the 21st century", "past 24 hours") stay junk.
    """
    low = stripped.strip().lower()
    if not low:
        return False
    # Date / period SHAPES — allow a leading article ("the 2026", "the 21st
    # century", "the past 24 hours").
    no_article = _ARTICLE_RE.sub("", low).strip()
    if (
        _YEAR_RE.match(no_article)
        or _ORDINAL_CENTURY_RE.match(no_article)
        or _PAST_N_RE.match(no_article)
        or _RELATIVE_AGO_RE.match(no_article)     # DQ M2 — "250 years ago"
    ):
        return True
    tokens = low.split()
    if len(tokens) == 1:
        return tokens[0] in _TEMPORAL_BARE_JUNK  # brand-ambiguous bare -> kept
    # <modifier(s)> <temporal noun>: "last week", "the first day", "a year".
    if tokens[0] in _TEMPORAL_MODIFIERS and tokens[-1] in _TEMPORAL_MODIFIED_NOUNS:
        mids = tokens[1:-1]
        if all(t in _TEMPORAL_MODIFIERS or t.isdigit() for t in mids):
            return True
    # DQ M2 / F4 — a MONTH is a relative-date phrase ("December last year",
    # "last November") ONLY when it carries a RELATIVE modifier (last/next/this/
    # coming/previous/past) AND every non-month token is temporal. A bare
    # "Month <day/year>" ("September 11", "October 7", "March 2022") is a named
    # calendar DATE / event and is KEPT; a bare month or a name ("Theresa May",
    # "Black September", "March on Washington") is likewise untouched.
    if any(t in _MONTH_NAMES for t in tokens):
        others = [t for t in tokens if t not in _MONTH_NAMES]
        if (
            others
            and any(t in _RELATIVE_TEMPORAL_MODIFIERS for t in others)
            and all(
                t in _TEMPORAL_MODIFIERS
                or t in _TEMPORAL_MODIFIED_NOUNS
                or t in _TEMPORAL_BARE_JUNK
                or t.isdigit()
                for t in others
            )
        ):
            return True
    return False


def is_demonym(name: str) -> bool:
    """True when ``name`` is a curated NATIONAL demonym (collapses to a country)."""
    return _strip_name(str(name or "")).lower() in _DEMONYM_MAP


def is_junk_entity(name: str) -> bool:
    """True when ``name`` is a known junk / non-entity token.

    Predicate-driven gate (DQ-H4 + live review). Legitimate short forms are
    EXEMPTED first — an alias key (``US``/``UK``/``EU``/``UN``), a curated
    demonym (it collapses to a country, it is not *junk*), and any country name
    are never reported junk, so they survive the length / numeric predicates.

    Then the junk classes are tested on the STRIPPED surface form:
      * the literal base set (``tv`` / ``radio`` / ``online``);
      * residual HTML (tag or unescaped entity still present);
      * clock-times (``6:53PM MDT``);
      * leading-quantifier phrases (``More than 450,000``);
      * pure numeric / percent / currency (``45%``, ``$3.2bn``);
      * money / currency amounts with a compound prefix (``S$2,500``,
        ``US$ 525 million``);
      * age / time-span tokens (``51 - year - old``, ``centuries``);
      * sports / competition-structure noise (``World Cup``, ``Group F``);
      * pure article / function word (``the`` / ``and`` / ``of``);
      * vague bloc / adjective / role singleton (``West`` / ``Islamic`` /
        ``Leader`` / ``annual``) + bare quantifier plural (``Hundreds`` /
        ``Millions``) — DQ M7 (nexus endpoints);
      * truncated institution / agency abbreviation (``Parl`` / ``Fed``) — M20
        (an NER clipping, not an actor);
      * markdown / URL residue (``http(s)://``, ``](``, ``**``, a pipe, an
        embedded newline) — B0-7 (telegram ``payload.text`` carries raw
        markdown into /extract);
      * length ≤ 2 (``F1`` / ``Xi`` / ``Co``) or length > 120 (a swallowed
        sentence / headline, never a name — live junk band starts at 153,
        max 688; genuine treaty/UN-body full names run to 118).

    NOT junk-dropped (the canon's strip handles them so the referent survives):
    a trailing-possessive surface (``"Abu Dhabi 's"`` strips to ``"Abu Dhabi"``,
    a real place) — :func:`canonicalize_entity` collapses it rather than dropping.

    Empty / whitespace input is NOT reported junk (the empty-name guard handles
    it) — this gate is specifically for non-empty NER spam.
    """
    raw = str(name or "")
    # Residual-HTML check on the RAW form (a strip would remove the residue, so
    # test before stripping; html.unescape inside _strip_name would also eat it).
    # A COMPLETE tag / entity, OR any bare '<'/'>' left over from a truncated
    # partial tag ("Iran</p", "/>Iranian", "… < a") — both are malformed spans.
    if _HTML_RESIDUE_RE.search(raw) or "<" in raw or ">" in raw:
        return True
    # B0-7 (2026-07-10 live junk audit) — markdown / URL residue, also tested
    # on the RAW form (the strip peels edge punctuation and collapses newlines,
    # which would hide the residue). Telegram payload.text carries raw markdown
    # ("[**title**](url)") into /extract, and NER emits spans still wearing it
    # ("Ayatollah Ali Khamenei**](https://f24.my").
    low_raw = raw.lower()
    # A URL scheme anywhere — a name carrying "http(s)://" is link residue.
    if "http://" in low_raw or "https://" in low_raw:
        return True
    # Markdown link-syntax residue ("…**](https://f24.my").
    if "](" in raw:
        return True
    # Markdown bold residue — "**" is never part of a real name.
    if "**" in raw:
        return True
    # A pipe is table / feed-delimiter residue ("Reuters | World"), not a name.
    if "|" in raw:
        return True
    # An embedded newline / carriage return — an entity span never crosses a
    # line break; this is a multi-line NER swallow.
    if "\n" in raw or "\r" in raw:
        return True

    stripped = _strip_name(raw)
    if not stripped:
        return False  # empty handled by the caller's MIN_NAME_LEN guard
    low = stripped.lower()

    # EXEMPT legitimate short/known forms FIRST so they survive the predicates.
    if low in _ALIAS_MAP or low in _DEMONYM_MAP or _is_country_name(stripped):
        return False

    if low in _JUNK_ENTITIES:
        return True
    # Pure article / function word ("the", "and", "of") — never a referent, so
    # it can never be elected a merge survivor (DQ P4 §E).
    if low in _STOPWORD_ENTITIES:
        return True
    # Bare spelled-out number-word / ordinal ("Two", "first") — a closed class
    # that slips past the DIGIT-only numeric predicate; never a referent (DQ P4).
    if low in _NUMBER_WORD_ENTITIES:
        return True
    # DQ M7 — a bare vague adjective / bloc / role singleton ("West", "Islamic",
    # "Leader", "annual") or a bare quantifier plural ("Hundreds", "Millions").
    # A real single-token actor (country / alias / demonym) was already exempted
    # above, so only genuinely vague endpoints reach here.
    if low in _VAGUE_ENDPOINT_TOKENS or low in _QUANTIFIER_PLURAL_ENTITIES:
        return True
    # M20 — a TRUNCATED institution / agency abbreviation ("Parl", "Fed") is an
    # NER clipping, not an actor. Dropped so the graph-mining hostile-edge / broker
    # shortlist stops amplifying it into headline signal.
    if low in _TRUNCATED_INSTITUTION_FRAGMENTS:
        return True
    if _CLOCK_RE.match(stripped):
        return True
    if _QUANTIFIER_RE.match(stripped):
        return True
    if _NUMERIC_RE.match(stripped):
        return True
    if _MONEY_RE.match(stripped):
        return True
    if _AGE_TIME_RE.match(stripped):
        return True
    if low in _SPORTS_NOISE_LITERALS or _SPORTS_NOISE_RE.match(stripped):
        return True
    # DQ M5 — number+unit quantity ("188,000 barrels", "770 bln won", "four
    # million euros"), possessive-kinship ("Donald Trump's son"), bare temporal
    # ("last week", "the 21st century", "Today"). Ported from the P5 fact gate.
    if _is_quantity_unit_phrase(stripped):
        return True
    if _POSSESSIVE_KINSHIP_RE.search(stripped):
        return True
    if _is_temporal_surface(stripped):
        return True
    if len(stripped) <= 2:
        return True
    # B0-7 — overlong cap at 120 (raised from 80 by the adversarial review):
    # genuine referents run long — full treaty/convention/UN-body names (the
    # UNRWA official name = 82, the Hague cultural-property convention = 91,
    # longest live legit = 118) — while the live junk band starts at 153
    # (153/183/688-char swallowed sentences). Past 120 is prose the NER
    # dragged in whole, never a referent. No length ceiling existed before.
    if len(stripped) > 120:
        return True
    return False


# ---------------------------------------------------------------------------
# COUNTRY GAZETTEER — built from pycountry, the SAME ISO-3166-1 dataset the
# `iso_countries` table is generated from (scripts/_gen_iso_countries_seed.py).
# Keys are lowercased country names / common / official names. Membership in
# this set forces COUNTRY_CLASS. Kept module-level + lazy so import is cheap.
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _country_name_set() -> frozenset[str]:
    names: set[str] = set()
    if pycountry is not None:
        for c in pycountry.countries:
            names.add(c.name.lower())
            common = getattr(c, "common_name", None)
            official = getattr(c, "official_name", None)
            if isinstance(common, str):
                names.add(common.lower())
            if isinstance(official, str):
                names.add(official.lower())
    # A few canonical short names pycountry stores in non-obvious forms, so a
    # name an alias resolves to ("Russia", "South Korea", …) still matches the
    # gazetteer and gets COUNTRY_CLASS. Conservative + curated.
    names.update({
        "russia",
        "south korea",
        "north korea",
        "united states",
        "united kingdom",
        "united arab emirates",
        "democratic republic of the congo",
        "czechia",
        "iran",
        "syria",
        "vietnam",
        "laos",
        "moldova",
        "tanzania",
        "bolivia",
        "venezuela",
        # Short / common forms a curated demonym value resolves to that
        # pycountry stores under a different head (or not at all): "Türkiye"
        # vs the common "Turkey", and Kosovo (no ISO-3166-1 assignment). Both
        # are demonym-map values, so the COUNTRY_CLASS typing must recognise
        # them or the collapse would land short of a country. "turkiye" is the
        # common ASCII spelling of the official "Türkiye" (pycountry's head) that
        # the gazetteer would otherwise miss, so the direction/type gates that
        # rely on COUNTRY typing (DQ M1 inverted-membership) recognise it.
        "turkey",
        "turkiye",
        "kosovo",
        # pycountry stores PS as "Palestine, State of" with no common_name, so
        # bare "Palestine" (the dominant live surface form) never typed as a
        # country and kept leaking as entity/location/person (DQ P4). Add the
        # bare form; the "palestinian" demonym resolves to it too (below).
        "palestine",
    })
    return frozenset(names)


def _is_country_name(name: str) -> bool:
    return bool(name) and name.lower() in _country_name_set()


# ---------------------------------------------------------------------------
# ORGANIZATION surface patterns — must NEVER be typed 'person'.
# ---------------------------------------------------------------------------

#: NWS forecast offices ("NWS St Louis", "NWS Mobile AL") + the expanded form.
#: Matched on the STRIPPED name. These are organizations, never people.
_ORG_PREFIX_RE = re.compile(r"^(nws)\b", re.IGNORECASE)
_ORG_CONTAINS = ("national weather service",)


def _is_org_pattern(name: str) -> bool:
    if not name:
        return False
    if _ORG_PREFIX_RE.match(name):
        return True
    lo = name.lower()
    return any(token in lo for token in _ORG_CONTAINS)


# ---------------------------------------------------------------------------
# ORG-SURFACE GAZETTEER — corporate / institutional surface forms whose name
# carries an organization suffix or "Bank of …" head. W2's entity resolver
# consumes this so 'Bank of England' / 'Nippon Steel' / 'Hyundai Motor Group'
# are class-typed organization, NEVER person. Surname collisions (a PERSON
# named "Michelle Steel") must NOT be caught — so a bare suffix word alone is
# not enough: it must be a MULTI-token surface whose suffix is a trailing
# org-suffix token, OR lead with a recognised institutional head ("Bank of").
# ---------------------------------------------------------------------------

#: Trailing org-suffix tokens. A surface ending in one of these (with ≥1 token
#: before it) is an organization. Matched case-insensitively on the LAST token
#: (suffixes that are themselves multi-word, e.g. "Motor Group", are folded in
#: via the contiguous-phrase check below).
_ORG_SUFFIX_TOKENS: frozenset[str] = frozenset({
    "inc", "inc.", "corp", "corp.", "corporation", "co", "co.", "ltd", "ltd.",
    "llc", "plc", "gmbh", "ag", "sa", "s.a.", "nv", "n.v.", "spa", "s.p.a.",
    "group", "holdings", "holding", "industries", "motors", "motor",
    "steel", "airlines", "airways", "bank", "telecom", "pharmaceuticals",
    "pharma", "technologies", "systems", "partners", "ventures", "capital",
    "asset", "financial", "insurance", "petroleum", "energy", "electric",
    "electronics", "automotive", "aerospace", "logistics", "shipping",
    # Institutional / governmental org suffixes — the live review (D7) found
    # these mis-typed as PERSON ("Falkland Islands Legislative Assembly"). A
    # multi-token surface ending in one of these is an institution, never a
    # person. ("administration" is deliberately EXCLUDED: "The Trump
    # administration" stays the curated person phrase — the conservative
    # no-over-merge contract — and the resolver handles the executive-body case.)
    "assembly", "legislature", "parliament", "congress", "senate",
    "ministry", "department", "committee", "commission", "council",
    "authority", "agency", "bureau", "directorate", "secretariat",
    "tribunal", "court", "judiciary",
    "university", "college", "institute", "institution", "academy",
    "foundation", "association", "federation", "union", "league",
    "organization", "organisation", "society", "syndicate", "consortium",
    # F1 (2026-07-06 review) — IGO / regional-bloc heads that end in these
    # ("East African Community", "Pacific Islands Forum", "Caribbean Community"):
    # a multi-token surface ending here names a body, never a person, so the
    # membership fact ("Kenya member of East African Community") is preserved.
    "community", "forum",
    # Military / paramilitary / mission org suffixes — the live review (DQ P4)
    # found these mis-typed PERSON ("225th Separate Assault Regiment", "United
    # Cajun Navy", "Frasers Centrepoint Trust"). A multi-token surface ending in
    # one of these names a formation / body, never a person.
    "trust", "regiment", "brigade", "battalion", "corps", "navy", "army",
    "force", "forces", "guard", "mission", "project",
})

#: Multi-word org suffix phrases (trailing). "Hyundai Motor Group" → "Motor
#: Group"; "Mitsubishi Heavy Industries" → "Heavy Industries". A surface ending
#: in one of these is an organization.
_ORG_SUFFIX_PHRASES: tuple[str, ...] = (
    "motor group", "motor company", "heavy industries", "steel industries",
    "holdings group", "financial group", "banking group", "media group",
    "petroleum corp", "national bank", "central bank", "reserve bank",
)

#: Institutional HEADS — surface forms that LEAD with these are organizations
#: ("Bank of England", "Bank of America", "University of …", "Ministry of …").
_ORG_HEAD_RE = re.compile(
    r"""^\s*(?:
            bank\s+of\b
          | university\s+of\b
          | ministry\s+of\b
          | department\s+of\b
          | bureau\s+of\b
          | federal\s+reserve\b
          | european\s+central\s+bank\b
          | people'?s\s+bank\b
          | reserve\s+bank\s+of\b
          | central\s+bank\s+of\b
        )
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Institutional INFIXES — an institution word followed by "of"/"on" anywhere in
#: the surface ("European Court of Justice", "Commission on Human Rights",
#: "Ministry of Foreign Affairs", "Department of Defense"). These name a body,
#: never a person, even when the TRAILING token isn't an org-suffix.
_ORG_INFIX_RE = re.compile(
    r"""\b(?:
            court | ministry | department | commission | committee
          | council | bureau | directorate | secretariat | agency
          | authority | assembly | parliament | congress | senate
          | board | tribunal | institute | university | college
          | federation | association | organisation | organization
        )\s+(?:of|on|for)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

_ORG_TOKEN_STRIP = " \t\"'`.,;:!?()[]{}"


def is_org_surface(name: str) -> bool:
    """True when ``name`` is an organization SURFACE form (suffix/head gazetteer).

    Pure + deterministic. Backs downstream class-typing so a corporate /
    institutional name is never typed ``person``. Conservative on the
    surname-collision boundary: a bare org-suffix word alone is NOT an org
    (a PERSON may be surnamed "Steel" / "Bank"), so a trailing suffix only
    counts when the surface has ≥2 tokens.

    Recognises:
      * an institutional HEAD ("Bank of England", "University of Oxford",
        "Ministry of Finance");
      * an institutional INFIX ("European Court of Justice", "Commission on
        Human Rights" — an institution word + of/on/for);
      * a trailing multi-word org phrase ("Hyundai Motor Group", "Mitsubishi
        Heavy Industries");
      * a trailing single org-suffix token on a multi-token surface
        ("Nippon Steel", "Toyota Motor", "Acme Inc", "Goldman Group",
        "Falkland Islands Legislative Assembly").

    Examples that must be FALSE: "Michelle Steel" (a person surnamed Steel —
    'steel' is a suffix token but 'michelle steel' is exempted by the curated
    person guard below), "Steel" alone (single token).
    """
    stripped = _strip_name(str(name or ""))
    if not stripped:
        return False
    lo = stripped.lower()

    # DQ M6 — a curated sports team / supranational-org acronym is an
    # organization regardless of token count (these are whole-surface matches,
    # zero false-positive by construction).
    if lo in _SPORTS_TEAM_SURFACES or lo in _KNOWN_ORG_SURFACES:
        return True

    # Institutional head ("Bank of England") — unambiguous, accept immediately.
    if _ORG_HEAD_RE.match(stripped):
        return True
    # Institutional infix ("European Court of Justice", "Commission on Human
    # Rights") — an institution word + of/on/for names a body, never a person.
    if _ORG_INFIX_RE.search(stripped):
        return True

    tokens = [t.strip(_ORG_TOKEN_STRIP) for t in lo.split()]
    tokens = [t for t in tokens if t]
    if len(tokens) < 2:
        return False  # a single bare token (incl. a lone "Steel"/"Bank") is not org

    # Trailing multi-word org phrase.
    for phrase in _ORG_SUFFIX_PHRASES:
        if lo.endswith(phrase):
            return True

    # Trailing single org-suffix token on a multi-token surface. Guard the
    # surname-collision case: a 2-token "<FirstName> Steel/Bank/..." where the
    # first token is a common given name is a PERSON, not an org. We keep this
    # guard tiny + curated (the resolver does the heavy person/org call); here
    # we only avoid the obvious false-positive on suffix words that double as
    # surnames ("steel", "bank", "co").
    last = tokens[-1]
    if last in _ORG_SUFFIX_TOKENS:
        if len(tokens) == 2 and last in _SURNAME_LIKE_SUFFIXES \
                and tokens[0] in _COMMON_GIVEN_NAMES:
            return False
        return True

    return False


#: Org-suffix tokens that ALSO occur as common surnames — only these trigger the
#: 2-token person guard above. (Most suffixes — "inc", "gmbh", "plc" — never
#: appear as a surname, so they need no guard.) "court"/"union"/"board" double
#: as English surnames ("Margaret Court"), so the 2-token given-name guard
#: protects them too; a 3+-token institution ("High Court of Justice",
#: "European Union", "Falkland Islands Legislative Assembly") is unaffected.
_SURNAME_LIKE_SUFFIXES: frozenset[str] = frozenset({
    "steel", "bank", "co", "court", "union", "board",
})

#: A tiny curated set of common given names used only to disambiguate the
#: "<Given> <SurnameLikeSuffix>" person case (e.g. "Michelle Steel"). Not a
#: general name list — just enough to dodge the documented false-positive.
_COMMON_GIVEN_NAMES: frozenset[str] = frozenset({
    "michelle", "john", "james", "robert", "michael", "david", "william",
    "mary", "patricia", "jennifer", "linda", "elizabeth", "susan", "sarah",
    "richard", "joseph", "thomas", "charles", "daniel", "matthew", "andrew",
    "george", "frank", "danny", "billy",
})


# ---------------------------------------------------------------------------
# PLACE / LOCATION surface gazetteer — geographic / built-environment surfaces
# the live review (D7) found mis-typed as PERSON ("Robertson Quay", "CITIC
# Tower", "Yerevan", "Earth"). A surface ending in a geographic-feature token,
# OR a known city/place name, is a LOCATION — never a person. Consumed by
# :func:`canonicalize_entity` (and downstream the entity resolver) so the place
# is typed ``location`` instead of falling to the title-case → person heuristic.
# Conservative: a trailing feature token only counts on a MULTI-token surface
# (a lone "Tower"/"Bridge" is not a place), and the city gazetteer is curated.
# ---------------------------------------------------------------------------

#: Trailing geographic-feature tokens. A multi-token surface ending in one of
#: these ("Robertson Quay", "CITIC Tower", "Falkland Islands") is a location.
_PLACE_SUFFIX_TOKENS: frozenset[str] = frozenset({
    "quay", "tower", "towers", "bridge", "square", "plaza", "park",
    "gardens", "stadium", "arena", "airport", "station", "terminal",
    "harbour", "harbor", "port", "bay", "beach", "island", "islands",
    "isles", "peninsula", "cape", "valley", "canyon", "mountain", "mountains",
    "hills", "river", "lake", "falls", "desert", "district", "borough",
    "county", "province", "prefecture", "region", "territory", "border",
    "crossing", "highlands", "lowlands", "delta", "strait", "gulf", "sea",
    "channel", "reef", "atoll", "village", "town", "city", "metro",
    "boulevard", "avenue", "street", "road", "highway", "junction",
    # Built-environment / religious-site heads that also appear TRAILING
    # ("Blue Mosque", "Diriyah Fort", "St Paul's Cathedral", "Ramstein Air
    # Base") — the live review (DQ P4) found "Temple of Apollo" / "Mount
    # Erciyes" mis-typed PERSON. The leading forms are handled by
    # :data:`_PLACE_HEAD_RE`; these cover the trailing forms.
    "temple", "mount", "fort", "palace", "mosque", "cathedral", "base",
})

#: Place HEADS — a surface LEADING with one of these + a following token is a
#: geographic / built-environment place ("Temple of Apollo", "Mount Erciyes",
#: "Fort Bragg", "Palace of Westminster"), never a person.
_PLACE_HEAD_RE = re.compile(
    r"^\s*(?:temple|mount|mt|fort|palace|mosque|cathedral|basilica|shrine)\b\s+\S",
    re.IGNORECASE,
)

#: Multi-word place suffix phrases (trailing).
_PLACE_SUFFIX_PHRASES: tuple[str, ...] = (
    "legislative assembly", "national park", "world heritage site",
)

#: Known city / place gazetteer (curated) — single-token place names NER
#: routinely mis-types as person ("Yerevan", "Earth"). Lower-cased. Kept to
#: unambiguous, high-frequency world cities + a few planetary/landmass names;
#: a name here is NOT a country (those are handled by the country gazetteer).
_KNOWN_PLACES: frozenset[str] = frozenset({
    # planetary / landmass
    "earth", "moon", "mars", "europe", "asia", "africa", "antarctica",
    "north america", "south america",
    "eurasia", "oceania", "arctic", "scandinavia", "balkans", "caucasus",
    "patagonia", "siberia", "kashmir", "tibet", "sahara", "amazon",
    # world cities NER mis-types (curated, non-exhaustive)
    "yerevan", "tbilisi", "baku", "astana", "tashkent", "bishkek",
    "ashgabat", "dushanbe", "kyiv", "kiev", "minsk", "chisinau",
    "moscow", "beijing", "shanghai", "tokyo", "seoul", "pyongyang",
    "bangkok", "jakarta", "manila", "hanoi", "singapore", "kuala lumpur",
    "delhi", "mumbai", "karachi", "dhaka", "colombo", "kathmandu",
    "tehran", "baghdad", "damascus", "beirut", "amman", "riyadh",
    "doha", "dubai", "abu dhabi", "kuwait city", "muscat", "sanaa",
    "cairo", "tripoli", "tunis", "algiers", "rabat", "khartoum",
    "nairobi", "addis ababa", "lagos", "abuja", "accra", "dakar",
    "kinshasa", "luanda", "harare", "lusaka", "kampala", "kigali",
    "johannesburg", "cape town", "pretoria", "casablanca",
    "london", "paris", "berlin", "madrid", "rome", "vienna", "warsaw",
    "prague", "budapest", "athens", "lisbon", "amsterdam", "brussels",
    "geneva", "zurich", "munich", "frankfurt", "milan", "barcelona",
    "stockholm", "oslo", "copenhagen", "helsinki", "dublin", "istanbul",
    "ankara", "washington", "new york", "los angeles", "chicago",
    "toronto", "ottawa", "mexico city", "bogota", "lima", "santiago",
    "buenos aires", "brasilia", "sao paulo", "rio de janeiro", "caracas",
    "sydney", "melbourne", "canberra", "wellington", "auckland",
    "hong kong", "taipei", "macau",
})


def is_place_surface(name: str) -> bool:
    """True when ``name`` is a place / geographic SURFACE form (LOCATION).

    Pure + deterministic. Backs class-typing so a geographic surface is never
    typed ``person``. Recognises:
      * a KNOWN city / landmass name ("Yerevan", "Earth") — single-token OK;
      * a trailing geographic-feature phrase ("… Legislative Assembly" is an
        institution and handled by :func:`is_org_surface`; "… National Park"
        here);
      * a trailing geographic-feature token on a MULTI-token surface
        ("Robertson Quay", "CITIC Tower", "Falkland Islands").

    Conservative: a lone feature token ("Tower", "Bridge") is NOT a place, and
    a country name is intentionally not handled here (the country gazetteer
    owns COUNTRY_CLASS, which outranks LOCATION).
    """
    stripped = _strip_name(str(name or ""))
    if not stripped:
        return False
    lo = stripped.lower()

    if lo in _KNOWN_PLACES:
        return True

    # Leading place head ("Temple of Apollo", "Mount Erciyes", "Fort Bragg").
    if _PLACE_HEAD_RE.match(stripped):
        return True

    for phrase in _PLACE_SUFFIX_PHRASES:
        if lo.endswith(phrase):
            return True

    tokens = [t.strip(_ORG_TOKEN_STRIP) for t in lo.split()]
    tokens = [t for t in tokens if t]
    if len(tokens) < 2:
        return False  # a lone feature token ("Tower") is not a place
    if tokens[-1] in _PLACE_SUFFIX_TOKENS:
        return True
    return False


# ---------------------------------------------------------------------------
# DQ M6 (2026-07-06 audit) — CONSERVATIVE class relabel for the 29.5% of rows
# that fall to the generic 'entity' bucket. Two tight, unambiguous gazetteers:
# geographic REGIONS -> location, and sports TEAMS / supranational ORG acronyms
# -> organization. Curated whole-surface (or article-stripped whole-surface)
# matches only, so a real entity is NEVER mis-relabeled.
# ---------------------------------------------------------------------------

#: Named geographic REGIONS (sub/supra-national) that are unambiguous LOCATIONS
#: but are not ISO countries, so the country gazetteer misses them and NER
#: routinely mis-types them person/organization ("the West Bank" -> org because
#: it ends in "bank"). Article-stripped whole-surface match. Curated: only
#: unambiguous places (no "Georgia"-style country/state homonyms).
_KNOWN_REGIONS: frozenset[str] = frozenset({
    "west bank", "gaza", "gaza strip", "middle east", "the levant", "levant",
    "strait of hormuz", "horn of africa", "sahel", "maghreb", "mesopotamia",
    "anatolia", "donbas", "donbass", "crimea", "golan heights", "golan",
    "sinai", "sinai peninsula", "kurdistan", "nagorno-karabakh", "transnistria",
    "abkhazia", "south ossetia", "xinjiang", "west coast", "east coast",
    "gulf coast", "west africa", "east africa", "north africa",
    "southern africa", "central africa", "sub-saharan africa", "the balkans",
    "south china sea", "east china sea", "sea of azov", "indo-pacific",
    "asia-pacific", "pacific rim", "arabian peninsula", "iberian peninsula",
    "korean peninsula", "great lakes region",
})


def is_region_surface(name: str) -> bool:
    """True when ``name`` is a curated geographic REGION (a LOCATION).

    Article-stripped, lower-cased whole-surface match against :data:`_KNOWN_REGIONS`.
    Conservative — only unambiguous non-country regions are members.
    """
    stripped = _strip_name(str(name or ""))
    if not stripped:
        return False
    return _strip_leading_article(stripped).lower() in _KNOWN_REGIONS


#: Well-known sports TEAMS (franchises) NER mis-types person/entity. Curated
#: whole-surface (lower-cased) set — ZERO false positives by construction (a
#: full "City Nickname" surface, never a bare nickname). Bounded to major North
#: American leagues + a few globally-covered clubs that appear in the feed.
_SPORTS_TEAM_SURFACES: frozenset[str] = frozenset({
    # MLB (the live example + division-mates)
    "minnesota twins", "new york yankees", "boston red sox", "los angeles dodgers",
    "chicago cubs", "san francisco giants", "houston astros", "atlanta braves",
    # NBA
    "los angeles lakers", "boston celtics", "golden state warriors",
    "chicago bulls", "new york knicks", "miami heat",
    # NFL
    "green bay packers", "dallas cowboys", "kansas city chiefs",
    "new england patriots", "pittsburgh steelers", "san francisco 49ers",
    # NHL
    "toronto maple leafs", "montreal canadiens", "detroit red wings",
    # global football clubs (unambiguous full names)
    "manchester united", "manchester city", "real madrid", "bayern munich",
    "paris saint-germain", "inter milan", "ac milan", "borussia dortmund",
})


def is_sports_team_surface(name: str) -> bool:
    """True when ``name`` is a curated sports TEAM full surface (an organization)."""
    stripped = _strip_name(str(name or ""))
    if not stripped:
        return False
    return stripped.lower() in _SPORTS_TEAM_SURFACES


#: Supranational / institutional ORG acronyms + short names that are single
#: tokens (so the org-suffix gazetteer, which needs >=2 tokens, misses them) and
#: are unambiguously organizations — never person/location. Curated whole-surface
#: (lower-cased). Kept tight to well-known bodies that appear in the feed.
_KNOWN_ORG_SURFACES: frozenset[str] = frozenset({
    "opec", "opec+", "nato", "asean", "brics", "ecowas", "mercosur", "gcc",
    "imf", "wto", "unicef", "unesco", "unhcr", "interpol", "opcw", "iaea",
    "wipo", "unctad", "unrwa",
})


def is_known_org_surface(name: str) -> bool:
    """True when ``name`` is a curated supranational/institutional org acronym."""
    stripped = _strip_name(str(name or ""))
    if not stripped:
        return False
    return stripped.lower() in _KNOWN_ORG_SURFACES


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def canonicalize_entity(name: str, ner_class: str) -> tuple[str, str]:
    """Canonicalize one NER mention → ``(canonical_name, canonical_class)``.

    Pure + deterministic + idempotent. See the module docstring for the full
    pipeline. ``ner_class`` is the upstream NER ``entity_class``; it is honoured
    unless a higher-confidence signal (gazetteer / org pattern) overrides it.

    Contract:
      * A name matching the country gazetteer (after strip + alias) is forced to
        :data:`COUNTRY_CLASS` — NEVER ``'person'``.
      * A name matching an organization surface pattern (``"NWS …"`` /
        ``"National Weather Service"``) is forced to :data:`ORGANIZATION_CLASS`
        — NEVER ``'person'``.
      * Otherwise the (stripped) name + the incoming class pass through.

    An empty / whitespace-only / fully-stripped-away name returns ``("", cls)``
    so the caller's existing ``MIN_NAME_LEN`` guard drops it (unchanged).
    """
    cls = (str(ner_class or DEFAULT_CLASS).strip() or DEFAULT_CLASS)

    stripped = _strip_name(str(name or ""))
    if not stripped:
        return "", cls

    low = stripped.lower()
    # JUNK reject (DQ-H4) — drop known non-entity tokens ("TV") so they never
    # reach facts/entities/nexuses. Returning "" makes the caller's empty-name
    # guard drop the mention (same contract as a fully-stripped-away name).
    if low in _JUNK_ENTITIES:
        return "", cls

    # DEMONYM / REGION-ADJECTIVE / ALIAS collapse (DQ-H4 + P4) — a national
    # demonym ("Iranian"→"Iran"), a continental adjective ("African"→"Africa"),
    # a plural ("Africans"/"Americans"), or an alias ("US") collapses to its
    # canonical referent so the surface forms stop being distinct graph nodes.
    # The lookup runs on the stripped surface AND on an article-stripped variant
    # ("the United Kingdom"→"United Kingdom", "The Costa Rican"→"Costa Rica").
    # A map hit REPLACES the display; with NO hit the display keeps its article
    # ("The Trump administration" stays a person phrase, unchanged).
    collapsed = _collapse_target(low)
    if collapsed is None:
        article_stripped = _strip_leading_article(stripped)
        if article_stripped.lower() != low:
            collapsed = _collapse_target(article_stripped.lower())
    canonical = collapsed if collapsed is not None else stripped
    # Re-strip in case an alias value introduced anything (defensive; values are
    # already clean) and to guarantee the idempotency fixed point.
    canonical = _strip_name(canonical)
    if not canonical:
        return "", cls

    # TYPE CORRECTION — gazetteer wins, then region, then the org pattern, then
    # place. A country name is never a person; an NWS office / corporate /
    # institutional surface is never a person; a geographic surface is never a
    # person. Priority mirrors the resolver: country > organization > location.
    if _is_country_name(canonical):
        return canonical, COUNTRY_CLASS
    # DQ M6 — a curated geographic REGION is an unambiguous LOCATION; it is
    # checked BEFORE the org gazetteer so "the West Bank" (ends in "bank") types
    # location, not organization. Adversarial #3: NEVER downgrade a confident
    # PERSON classification — several region tokens are also surnames
    # ("Golan"/Menahem Golan, "Levant"/Oscar Levant, "Sinai", "Anatolia"), so a
    # mention NER typed 'person' keeps person; only a non-person mention relabels.
    if cls != "person" and is_region_surface(canonical):
        return canonical, LOCATION_CLASS
    if canonical in _ALIAS_ORG:
        return canonical, ORGANIZATION_CLASS
    if _is_org_pattern(canonical) or is_org_surface(canonical):
        return canonical, ORGANIZATION_CLASS
    if is_place_surface(canonical):
        return canonical, LOCATION_CLASS

    return canonical, cls


def identity_fold(name: str) -> str:
    """Class-agnostic dedup key for an entity surface form (DQ P4).

    Pure + deterministic + idempotent. Two surface forms that name the same
    referent under all the canon's rules (alias / demonym / region-adjective /
    plural collapse, article strip, residue strip, zero-width removal, case +
    punctuation normalization) fold to the SAME key, so the de-fragmentation
    merge can cluster them WITHOUT the entity_class. The key is lower-case,
    alphanumeric-only (no spaces / punctuation), and never contains an article
    or markup residue.

    Pipeline:
      1. strip HTML/partial-tag residue + zero-width chars (so "Iran</p" folds
         onto "Iran" — a junk-SHAPED historical row still re-points to its clean
         survivor);
      2. run :func:`canonicalize_entity` (alias / demonym / region / plural
         collapse + strip) with the generic class;
      3. strip a leading article;
      4. lower-case, drop zero-width, collapse every non-alphanumeric run.

    Stability: ``identity_fold(x)`` re-folds to itself — the key is already
    article-free, residue-free, and alphanumeric, so a second pass is a no-op.
    An empty / fully-stripped-away input yields ``""`` (the caller treats an
    empty fold as "no identity" and skips it).
    """
    de_residue = _strip_residue_for_fold(str(name or ""))
    canon, _cls = canonicalize_entity(de_residue, DEFAULT_CLASS)
    # Fall back to the de-residued raw if the canon dropped it as literal junk,
    # so a junk-literal cluster still gets a stable non-empty key of its own.
    canon = canon or de_residue
    canon = _strip_leading_article(canon.strip())
    low = _ZERO_WIDTH_RE.sub("", canon.lower())
    return _NON_ALNUM_RE.sub("", low)


#: DQ M4 — leading-article regex for :func:`lookup_key`, mirroring the SQL
#: ``regexp_replace(..., '^(the|a|an)\s+', '')`` used by the alias/article-aware
#: pre-lookup so the Python-computed key and the DB-side normalization agree
#: byte-for-byte.
_LOOKUP_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)


def lookup_key(name: str) -> str:
    """Article/case/whitespace-normalized key for the entity PRE-LOOKUP (DQ M4).

    Lower-cases, collapses whitespace, and strips a single leading article
    (the/a/an) — nothing else, so it matches EXACTLY the DB-side normalization
    ``regexp_replace(regexp_replace(lower(btrim(x)),'^(the|a|an)\\s+',''),
    '\\s+',' ','g')`` the alias/article-aware pre-lookup applies to
    ``canonical_name`` and to each ``merged_aliases`` element. So "the Strait of
    Hormuz" and "Strait of Hormuz" produce the SAME key and converge onto the
    existing keeper instead of forking a new row. Pure + deterministic.
    """
    s = _WHITESPACE_RE.sub(" ", str(name or "")).strip().lower()
    s = _LOOKUP_ARTICLE_RE.sub("", s).strip()
    return _WHITESPACE_RE.sub(" ", s).strip()


# ---------------------------------------------------------------------------
# DQ M8 (2026-07-06 nexus-write audit) — the NEXUS SELF-EDGE gate.
#
# nexus subject/object are FREE TEXT (no entity FK), so the P4 entity fold never
# rewrote them. Two endpoints that name the SAME referent under the canon
# therefore leaked as a self-loop edge: a continent + its adjective ("Africa" co
# occurs with "African"), a country + its demonym / plural ("Iran"/"Iranians",
# "Israel"/"Israeli"), an alias pair ("US"/"United States"), or a plain
# singular / plural the canon does not map ("Houthi"/"Houthis"). A self-loop is
# not a relationship. Both nexus producers drop an edge for which
# :func:`same_referent` is True.
# ---------------------------------------------------------------------------


def _singularize_surface(low: str) -> str:
    """Guarded regular-plural → singular for a lower-cased canonical surface.

    Strips ONLY a clear regular plural, and ONLY on a SINGLE-token surface whose
    stem stays >= 5 chars, so "houthis"->"houthi" / "militants"->"militant" fold
    while short / ambiguous forms ("hamas" vs "hama", "gas") and multi-word
    country names ("united states") are left untouched. Used ONLY by
    :func:`same_referent`. Pure + deterministic + idempotent on a singular."""
    if " " in low:
        return low  # only fold a single-token plural; phrases collapse via canon
    if len(low) > 6 and low.endswith("ies"):
        return low[:-3] + "y"
    if len(low) > 6 and low.endswith("es") and low[-3] in "sxz":
        return low[:-2]
    if len(low) > 5 and low.endswith("s") and not low.endswith("ss"):
        return low[:-1]
    return low


def same_referent(a: str, b: str) -> bool:
    """True when two entity surfaces name the SAME referent under the canon (M8).

    Backs the nexus SELF-EDGE gate: an edge whose subject and object are the same
    referent is a self-loop, not a relationship, and is dropped by both nexus
    producers. Both surfaces route through :func:`canonicalize_entity` (which
    collapses demonyms / regions / aliases + strips); if still distinct, a
    GUARDED single-token regular-plural normalization (:func:`_singularize_surface`)
    is applied to each and compared, so a plain plural the canon does not map
    ("Houthi"/"Houthis") still folds.

    Conservative: two genuinely distinct names return False; an empty / fully-
    stripped-away endpoint returns False (the caller's own guards handle it).
    Pure + deterministic + symmetric."""
    ca, _ = canonicalize_entity(str(a or ""), DEFAULT_CLASS)
    cb, _ = canonicalize_entity(str(b or ""), DEFAULT_CLASS)
    if not ca or not cb:
        return False
    la, lb = ca.lower(), cb.lower()
    if la == lb:
        return True
    return _singularize_surface(la) == _singularize_surface(lb)


__all__ = [
    "canonicalize_entity",
    "identity_fold",
    "lookup_key",
    "same_referent",
    "is_demonym",
    "is_junk_entity",
    "is_org_surface",
    "is_place_surface",
    "is_region_surface",
    "is_sports_team_surface",
    "is_known_org_surface",
    "COUNTRY_CLASS",
    "ORGANIZATION_CLASS",
    "LOCATION_CLASS",
    "DEFAULT_CLASS",
    "_JUNK_ENTITIES",
    "_DEMONYM_MAP",
    "_REGION_ADJECTIVE_MAP",
    "_VAGUE_ENDPOINT_TOKENS",
    "_QUANTIFIER_PLURAL_ENTITIES",
]
