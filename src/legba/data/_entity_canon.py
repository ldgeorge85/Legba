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
    "palestinian": "Palestine, State of",
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

#: Short / junk tokens NER mis-emits as entities (DQ-H4). The original TINY base
#: set, kept verbatim. Predicate-based junk classes (clock-times, quantifiers,
#: numerics, length≤2, residual HTML, bare demonyms) are layered ON TOP in
#: :func:`is_junk_entity` — this frozenset stays the literal-token base.
_JUNK_ENTITIES: frozenset[str] = frozenset({"tv", "radio", "online"})


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
#: means a malformed span the NER never should have emitted.
_HTML_RESIDUE_RE = re.compile(r"</?[a-z][^>]*>|&[a-z]+;|&#\d+;", re.IGNORECASE)

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
_AGE_TIME_RE = re.compile(
    r"""^\s*(?:
            \d[\d,]*\s*-?\s*years?\s*-?\s*old   # "51 - year - old", "24-year-old"
          | (?:a|an|the)?\s*
            (?:centur(?:y|ies)|decades?|millenni(?:um|a)|generations?)
        )\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


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
      * length ≤ 2 (``F1`` / ``Xi`` / ``Co``).

    NOT junk-dropped (the canon's strip handles them so the referent survives):
    a trailing-possessive surface (``"Abu Dhabi 's"`` strips to ``"Abu Dhabi"``,
    a real place) — :func:`canonicalize_entity` collapses it rather than dropping.

    Empty / whitespace input is NOT reported junk (the empty-name guard handles
    it) — this gate is specifically for non-empty NER spam.
    """
    raw = str(name or "")
    # Residual-HTML check on the RAW form (a strip would remove the residue, so
    # test before stripping; html.unescape inside _strip_name would also eat it).
    if _HTML_RESIDUE_RE.search(raw):
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
    if len(stripped) <= 2:
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
        # them or the collapse would land short of a country.
        "turkey",
        "kosovo",
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
})

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

    # DEMONYM collapse (DQ-H4) then ALIAS MAP — case-insensitive on the stripped
    # surface form. A national demonym ("Iranian") becomes its country ("Iran")
    # so the two stop being distinct graph nodes.
    canonical = _DEMONYM_MAP.get(low) or _ALIAS_MAP.get(low, stripped)
    # Re-strip in case an alias value introduced anything (defensive; values are
    # already clean) and to guarantee the idempotency fixed point.
    canonical = _strip_name(canonical)
    if not canonical:
        return "", cls

    # TYPE CORRECTION — gazetteer wins, then the org pattern, then place. A
    # country name is never a person; an NWS office / corporate / institutional
    # surface is never a person; a geographic surface is never a person.
    # Priority mirrors the resolver: country > organization > location.
    if _is_country_name(canonical):
        return canonical, COUNTRY_CLASS
    if canonical in _ALIAS_ORG:
        return canonical, ORGANIZATION_CLASS
    if _is_org_pattern(canonical) or is_org_surface(canonical):
        return canonical, ORGANIZATION_CLASS
    if is_place_surface(canonical):
        return canonical, LOCATION_CLASS

    return canonical, cls


__all__ = [
    "canonicalize_entity",
    "is_demonym",
    "is_junk_entity",
    "is_org_surface",
    "is_place_surface",
    "COUNTRY_CLASS",
    "ORGANIZATION_CLASS",
    "LOCATION_CLASS",
    "DEFAULT_CLASS",
    "_JUNK_ENTITIES",
    "_DEMONYM_MAP",
]
