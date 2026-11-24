# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The geocoder's text-sweep RULES (lane N-B): two homonym classes the
country-name sweep in :mod:`legba.data.filters.geocode` could not tell apart.

1. **PERSON-NAME GUARD.** "Republicans elect Jim Jordan as House minority
   leader" filed under the country JORDAN (signal 0bbbf4c0, 11-10): the sweep
   matches every whole-word country name, and a person's name is not a place.
   The upstream NER (``ner_multilingual``, which runs before ``geocode`` in the
   enrichment chain) already typed "Jim Jordan" as ``person``. The guard blanks
   every ``person``-class surface out of the text before the sweep — and, when
   a person's LAST token is itself a country name, every bare occurrence of
   that token too, because copy repeats the surname alone ("Republicans pick
   Jordan for the last two years"). Blanking keeps every offset (same-length
   spaces), so the R4 positional ladder is unchanged for everything else. The
   same class covers "Chad" (a given name), "Georgia" / "Jordan" as given names
   and any other person a NER types correctly; a NER miss fails OPEN (today's
   behaviour), never closed.

2. **AMBIGUOUS-NAME DISAMBIGUATION by co-occurring places.** "Ebola cases in
   DR Congo pass 11,000" filed under CG (Brazzaville) — d3437c51, 0bf738d4,
   757bedb3, fe03b154 — because pycountry's name index knows only CG's short
   name "Congo" and the DRC's ISO name "Congo, The Democratic Republic of the";
   "DR Congo" matched as bare "Congo". Two repairs, both table-driven:

   * :data:`EXTRA_COUNTRY_NAMES` — the unambiguous long/short forms the index
     lacked ("DR Congo", "DRC", "Congo-Kinshasa" → CD; "Congo-Brazzaville",
     "Republic of the Congo" → CG). Longest-first matching means "DR Congo"
     now wins over the "Congo" inside it.
   * :data:`AMBIGUOUS_NAMES` — a bare surface that names more than one country
     resolves by the PLACES that co-occur with it in the signal's text (title +
     summary + body): Kinshasa / Kivu / Goma / Ituri / Tshisekedi → CD,
     Brazzaville / Pointe-Noire / Sassou → CG. With no cue (or cues for both)
     the rule's ``default`` holds — today's answer — so the rule only ever
     moves a row on positive evidence.

   :data:`QUERY_NAMES` is the geocoder query for a resolved ISO whose pycountry
   name the backend does not resolve to the right country (the DRC's inverted
   ISO name; bare "Congo" resolves to Brazzaville on Nominatim).

A leaf: stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

#: Country surfaces the pycountry-built name index lacks (lower-cased).
EXTRA_COUNTRY_NAMES: dict[str, str] = {
    "dr congo": "CD",
    "d.r. congo": "CD",
    "drc": "CD",
    "democratic republic of congo": "CD",
    "democratic republic of the congo": "CD",
    "congo-kinshasa": "CD",
    "congo kinshasa": "CD",
    "congo-brazzaville": "CG",
    "congo brazzaville": "CG",
    "republic of the congo": "CG",
    "republic of congo": "CG",
    # Lane N-O: the Spanish "EE. UU." (Estados Unidos) — its bare "EE" was
    # resolving to ESTONIA (es copy, 7-day sample 11-13).
    "ee. uu.": "US",
    "ee.uu.": "US",
    # Lane N-U: pycountry knows GB only as "United Kingdom" and its official
    # "...of Great Britain and Northern Ireland"; English copy writes
    # "Britain" / "Great Britain" far more often ("New Britain", the PNG
    # island and the Connecticut city, is blanked by NON_COUNTRY_PLACE_NAMES).
    "great britain": "GB",
    "britain": "GB",
}

# ---------------------------------------------------------------------------
# Lane N-O — tokens that are NOT place references (the 11-13 official-source
# mis-tags: "91 FR 60505" -> France, "GI Breuer" -> Gibraltar, image-hash hex
# "...56BA9105E..." -> Bosnia, "SDN List" -> Sudan). Keyed by the SOURCE's
# language (``payload.language``); ``"*"`` applies in every language. A token
# listed here is never read as a bare ISO code; a full country NAME still
# resolves. Add a token only when the copy in that language uses it for
# something else far more often than for the country (a news body names the
# country, it does not write its code).
# ---------------------------------------------------------------------------

#: Uppercase abbreviations that collide with an ISO-3166 alpha-2/alpha-3 code.
ABBREVIATION_COLLISIONS: dict[str, frozenset[str]] = {
    "*": frozenset({
        "CAF",   # Confederation of African Football (alpha-3 Central African Rep.)
        "FM",    # foreign minister / radio band (Micronesia)
        "MP",    # member of parliament (Northern Mariana Is.)
        "TV",    # television (Tuvalu)
        "GW",    # gigawatt (Guinea-Bissau)
        "AG",    # Attorney General / Aktiengesellschaft (Antigua)
        "KG",    # Kommanditgesellschaft / kilogram (Kyrgyzstan)
        "KM",    # kilometres, "KM 12" (Comoros)
    }),
    "en": frozenset({
        "FR",    # Federal Register citation "91 FR 60505" (France)
        "SDN",   # OFAC "SDN List" (Sudan alpha-3)
        "ITA",   # International Trade Administration (Italy alpha-3)
        "SR",    # State Road (Suriname)
        "SE",    # compass direction / Airbus SE (Sweden)
        "VI",    # Roman numeral, "MMI VI" (US Virgin Is.)
        "AD",    # anno Domini / advertisement / NWS bullet "* AD" (Andorra)
        "GS",    # government security "7.06% GS 2041" (S. Georgia)
        "HT",    # Hindustan Times (Haiti)
        "KP",    # Khyber Pakhtunkhwa (North Korea)
        "MK",    # Member of Knesset (North Macedonia)
        "ST",    # ST Engineering (Sao Tome)
        "SS",    # steamship "SS United States" (South Sudan)
        "SK",    # SK Hynix / SK Telecom (Slovakia)
        "FK",    # football club (Falklands)
        "MM",    # mile marker (Myanmar)
        "EC",    # European / Election Commission (Ecuador)
        "BI",    # Bank Indonesia (Burundi)
        "CI",    # (Cote d'Ivoire)
        "SH",    # Shanghai (St Helena)
        "TR",    # Taiwan Railway (Turkey)
        "NA",    # National Assembly / not applicable (Namibia)
        "GP",    # general practitioner / Grand Prix (Guadeloupe)
        "GL",    # OFAC "general license (GL): GL W" (Greenland) - roll 74
        "EE",    # OFAC "General Licenses EE and FF" / EE the UK carrier (Estonia) - roll 74
    }),
    "de": frozenset({
        "GI",    # Generalinspekteur (Gibraltar)
        "BM",    # Bundesminister(in) (Bermuda)
        "BA",    # Bundesagentur / Bachelor (Bosnia)
        "SE",    # Societas Europaea (Sweden)
        "BW",    # Bundeswehr (Botswana)
    }),
    "fr": frozenset({
        "JO",    # Jeux Olympiques (Jordan)
    }),
    "es": frozenset({
        "EE",    # "EE. UU." (Estonia)
    }),
}


def abbreviation_collisions(language: str | None) -> frozenset[str]:
    """The non-place uppercase tokens for a source language (plus ``"*"``)."""
    lang = (language or "").strip().lower()[:2]
    return ABBREVIATION_COLLISIONS["*"] | ABBREVIATION_COLLISIONS.get(lang, frozenset())


#: Country NAMES that are ordinary words in a language: matched only when the
#: surface is Capitalized ("une réunion" is a meeting, "La Réunion" the island;
#: "a turkey" is a bird). German capitalizes every noun, so nothing is listed.
COMMON_WORD_NAMES: dict[str, frozenset[str]] = {
    "*": frozenset({"georgia"}),   # the CSS font "font-family: georgia"
    "en": frozenset({"turkey", "jersey", "china", "chad", "guinea"}),
    "fr": frozenset({"réunion"}),
}


def is_common_word_name(surface: str, language: str | None) -> bool:
    """True when ``surface`` is a lower-case ordinary word, not the country."""
    if not surface or not surface[0].islower():
        return False
    lang = (language or "").strip().lower()[:2]
    key = surface.lower()
    return key in COMMON_WORD_NAMES["*"] or key in COMMON_WORD_NAMES.get(lang, frozenset())


#: NER ``location`` surfaces that are not places on Earth's map — the
#: backend resolves them anyway ("The American Declaration of Freedom in
#: Space" -> a street in PARIS, 4872a035, 11-06). Lower-cased.
NON_PLACE_SURFACES: dict[str, frozenset[str]] = {
    "*": frozenset({
        "space", "outer space", "cyberspace", "orbit", "low earth orbit",
        "earth", "the moon", "moon", "mars", "internet", "online", "homeland",
        "center", "centre",
    }),
    "de": frozenset({"weltraum", "weltall", "all", "internet"}),
}


def is_non_place_surface(surface: str, language: str | None) -> bool:
    lang = (language or "").strip().lower()[:2]
    key = (surface or "").strip().lower()
    return key in NON_PLACE_SURFACES["*"] or key in NON_PLACE_SURFACES.get(lang, frozenset())


_MARKUP = re.compile(
    r"<[^<>]{0,4000}>"                       # an HTML tag with its attributes
    r"|&lt;.{0,400}?&gt;"                    # an escaped tag
    r"|(?:https?://|www\.)[^\s\"'<>]+",      # a URL (its slug is not the story)
    re.IGNORECASE | re.DOTALL,
)


#: Sub-national place names that CONTAIN a country name — the whole-word sweep
#: read NWS "southern New Jersey" as JERSEY (7-day sample, 11-13).
NON_COUNTRY_PLACE_NAMES: tuple[str, ...] = (
    "New Jersey", "Jersey City", "New Mexico", "Mexico Beach", "New Britain",
)
_NON_COUNTRY_PLACES = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(re.escape(n) for n in NON_COUNTRY_PLACE_NAMES)
    + r")(?![A-Za-z])", re.IGNORECASE,
)


def blank_markup(text: str) -> str:
    """``text`` with HTML tags, escaped tags and URLs LOWER-CASED and the
    :data:`NON_COUNTRY_PLACE_NAMES` blanked — same length, so every offset the
    positional ladder reads is unchanged. Lower-casing markup means it can
    never yield a bare ISO code (the 11-13 BMVg ``<img src=".../32CBAA36A2A7
    FDD56BA9105E...">`` hex hash was Bosnia; a percent-encoded ``%D8%AD``
    slug was Andorra) while a URL slug's country NAME ("/australia-news/")
    still counts as it did; a ``font-family: georgia`` style is the
    lower-case common word :data:`COMMON_WORD_NAMES` drops."""
    if not text:
        return text
    if "<" in text or "&lt;" in text or "://" in text or "www." in text:
        text = _MARKUP.sub(lambda m: m.group(0).lower(), text)
    return _NON_COUNTRY_PLACES.sub(lambda m: " " * len(m.group(0)), text)


# ---------------------------------------------------------------------------
# Lane N-U — DEMONYM evidence ("Russian strikes hit Kharkiv", "North Korean
# troops"). An adjective is WEAKER evidence than a country name ("Russian
# dressing", "Chinese New Year", "American Airlines"), so the geocoder reads it
# only as a LOW-PRECEDENCE candidate: English copy, the title / lead zone only,
# Capitalized, after every named-country candidate, never for a source with a
# declared country. Off unless :data:`DEMONYM_LEAD_EVIDENCE` is on.
# ---------------------------------------------------------------------------

#: The rule flag. See ``GeocodeHandler._derive_candidates`` for the tier.
DEMONYM_LEAD_EVIDENCE: bool = False

#: Demonyms never read as evidence: the referent is ambiguous ("Georgian"
#: architecture / the era, "Congolese" names two states, "Dominican" a friar or
#: two states, "Guinean" three).
DEMONYM_EXCLUDED: frozenset[str] = frozenset({
    "georgian", "congolese", "dominican", "guinean",
})

#: Phrases in which a demonym does not name the country (lower-cased; blanked
#: before the demonym sweep, same length).
DEMONYM_NON_PLACE_PHRASES: tuple[str, ...] = (
    "russian dressing", "russian roulette", "russian doll", "russian dolls",
    "chinese new year", "chinese whispers", "chinese food", "chinese takeaway",
    "chinese medicine", "chinese taipei",
    "american airlines", "american express", "american eagle", "american football",
    "american idol", "american dream", "american indian", "american indians",
    "american samoa", "native american", "native americans", "african american",
    "african americans", "asian american", "asian americans", "latin american",
    "central american", "south american", "north american", "pan american",
    "mexican american", "american league", "american legion",
    "indian ocean", "indian premier league", "indian summer", "west indian",
    "french fries", "french toast", "french press", "french open",
    "turkish airlines", "turkish delight", "swiss cheese", "swiss army knife",
    "danish pastry", "spanish flu", "german shepherd", "brazilian wax",
    "mexican wave", "irish coffee", "dutch auction", "afghan hound",
    "british airways", "british columbia", "british virgin islands",
    "british indian ocean territory", "british museum", "japanese knotweed",
    "japanese encephalitis", "greek yogurt", "greek salad", "belgian waffle",
    "thai curry", "canadian football league", "australian open",
)
_DEMONYM_NON_PLACE_RE = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(re.escape(p) for p in sorted(
        DEMONYM_NON_PLACE_PHRASES, key=len, reverse=True)) + r")(?![A-Za-z])",
    re.IGNORECASE,
)


def blank_demonym_non_place_phrases(text: str) -> str:
    """``text`` with :data:`DEMONYM_NON_PLACE_PHRASES` blanked (same length)."""
    if not text:
        return text
    return _DEMONYM_NON_PLACE_RE.sub(lambda m: " " * len(m.group(0)), text)


#: Canonical names in the profile's demonym map the country index lacks.
_DEMONYM_TARGET_OVERRIDES: dict[str, str] = {
    "turkey": "TR", "palestine": "PS", "kosovo": "XK",
}


def build_demonym_index(
    demonym_map: Mapping[str, str], country_names: Mapping[str, str],
) -> dict[str, str]:
    """``{lower-case demonym: ISO2}`` from the profile's demonym map (demonym
    -> canonical country name), minus :data:`DEMONYM_EXCLUDED`."""
    out: dict[str, str] = {}
    for dem, name in demonym_map.items():
        key = str(dem).strip().lower()
        low = str(name).strip().lower()
        iso = country_names.get(low) or _DEMONYM_TARGET_OVERRIDES.get(low)
        if key and iso and key not in DEMONYM_EXCLUDED:
            out[key] = iso
    return out


_DEMONYM_RE_CACHE: dict[int, re.Pattern[str]] = {}


def first_demonym_iso2(
    text: str, demonyms: Mapping[str, str], *, language: str | None = None,
) -> str | None:
    """ISO2 of the EARLIEST Capitalized demonym in ``text`` (English or
    undeclared language only), after :data:`DEMONYM_NON_PLACE_PHRASES` are
    blanked. The caller passes a title / lead zone, already person-masked."""
    lang = (language or "").strip().lower()[:2]
    if not text or not demonyms or lang not in ("", "en"):
        return None
    rx = _DEMONYM_RE_CACHE.get(id(demonyms))
    if rx is None:
        alts = "|".join(re.escape(d) for d in sorted(demonyms, key=len, reverse=True))
        rx = re.compile(r"(?<![A-Za-z])(" + alts + r")s?(?![A-Za-z])", re.IGNORECASE)
        _DEMONYM_RE_CACHE[id(demonyms)] = rx
    for m in rx.finditer(blank_demonym_non_place_phrases(text)):
        if m.group(1)[0].isupper():
            return demonyms.get(m.group(1).lower())
    return None


#: The geocoder query for an ISO whose pycountry ``name`` is a poor query.
QUERY_NAMES: dict[str, str] = {
    "CD": "Democratic Republic of the Congo",
    "CG": "Republic of the Congo",
    # Lane N-O: pycountry's inverted formal names are bad Nominatim queries —
    # the live cache holds "korea, republic of" -> SINGAPORE (a Korea
    # Investment Corp office) and "taiwan, province of china" -> CHINA (a
    # Fujian museum). Every ISO with a ``common_name`` queries by it
    # (``geocode.geocoder_query_name``); these have none.
    "FM": "Micronesia",
    "PS": "Palestine",
    "VA": "Vatican City",
    "VG": "British Virgin Islands",
    "VI": "United States Virgin Islands",
    "BQ": "Caribbean Netherlands",
}


@dataclass(frozen=True)
class AmbiguousName:
    """A bare country surface that names more than one state."""

    surface: str
    default: str
    #: ``(iso2, cue forms)`` — a cue is a place/officeholder name that
    #: only one of the candidates contains.
    cues: tuple[tuple[str, tuple[str, ...]], ...]

    def resolve(self, context: str) -> str:
        """The ISO the co-occurring cues pick; ``default`` when none or tied."""
        hits = [iso for iso, forms in self.cues if _any_form(forms, context)]
        return hits[0] if len(hits) == 1 else self.default


def _any_form(forms: Iterable[str], text: str) -> bool:
    for f in forms:
        if re.search(r"(?<![A-Za-z])" + re.escape(f) + r"(?![A-Za-z])", text, re.IGNORECASE):
            return True
    return False


#: Keyed by the lower-cased bare surface the name sweep matched.
AMBIGUOUS_NAMES: dict[str, AmbiguousName] = {
    "congo": AmbiguousName(
        surface="congo",
        default="CG",
        cues=(
            ("CD", (
                "DRC", "DR Congo", "Kinshasa", "Kivu", "North Kivu", "South Kivu",
                "Goma", "Bukavu", "Ituri", "Bunia", "Beni", "Butembo", "Uvira",
                "Kasai", "Kisangani", "Lubumbashi", "Katanga", "Mbandaka",
                "Equateur", "Tshisekedi", "Kabila", "M23", "FARDC", "MONUSCO",
                "Congolese army",
            )),
            ("CG", (
                "Brazzaville", "Pointe-Noire", "Sassou", "Sassou Nguesso",
                "Republic of the Congo",
            )),
        ),
    ),
    # Lane N-U: the US STATE. NWS / US-agency copy ("Flood Watch for
    # Chatham County, Georgia"; "Georgia and South Carolina") filed under the
    # country GE. A state cue — another US state, a county, a Georgia city,
    # the weather service — resolves the bare name to US; a Caucasus cue
    # (Tbilisi, Abkhazia, Georgian Dream) to GE; none, or both, keeps GE.
    "georgia": AmbiguousName(
        surface="georgia",
        default="GE",
        cues=(
            ("US", (
                "Alabama", "Alaska", "Arizona", "Arkansas", "California",
                "Colorado", "Connecticut", "Delaware", "Florida", "Hawaii",
                "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky",
                "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan",
                "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska",
                "Nevada", "New Hampshire", "New York", "North Carolina",
                "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania",
                "Rhode Island", "South Carolina", "South Dakota", "Tennessee",
                "Texas", "Utah", "Vermont", "Virginia", "West Virginia",
                "Wisconsin", "Wyoming", "County", "Counties", "Atlanta",
                "Savannah", "Macon", "Augusta", "Valdosta", "Kemp",
                "Peach State", "National Weather Service", "NWS",
                "Georgia Tech", "University of Georgia", "Fulton",
            )),
            ("GE", (
                "Tbilisi", "Batumi", "Kutaisi", "Abkhazia", "South Ossetia",
                "Tskhinvali", "Georgian Dream", "Kobakhidze", "Kavelashvili",
                "Zourabichvili", "Ivanishvili", "Caucasus", "Black Sea",
            )),
        ),
    ),
}


def disambiguate(surface: str, iso2: str, context: str | None) -> str:
    """Apply :data:`AMBIGUOUS_NAMES` to one name-sweep hit."""
    rule = AMBIGUOUS_NAMES.get((surface or "").strip().lower())
    if rule is None or not context:
        return iso2
    return rule.resolve(context)


def person_surfaces(entities: Any) -> list[str]:
    """The ``person``-class NER surfaces on a payload's ``entities``."""
    out: list[str] = []
    if not isinstance(entities, (list, tuple)):
        return out
    for ent in entities:
        if not isinstance(ent, Mapping):
            continue
        if str(ent.get("class") or "").strip().lower() != "person":
            continue
        text = ent.get("text")
        if isinstance(text, str) and text.strip() and text.strip() not in out:
            out.append(text.strip())
    return out


#: Country names that are also attested PERSONAL names (given names or
#: surnames): "Jim Jordan", "Chad Smith", "Israel Adesanya", "Georgia Meloni",
#: "India Walton", "Kenya Barris", "Kathy Ireland", "Cuba Gooding". Lane N-U:
#: the person-name guard blanks a country name inside a ``person``-class NER
#: surface ONLY when the country is one of these. The NER also labels "US
#: House", "Panama Canal", "Government of Liberia", "Democratic Republic of
#: Congo", "McDonald's Australia", "Saab Denmark" and "Qatar MotoGP" as
#: ``person`` (roll 74: 56 of 1,038 re-tag changes dropped a country the title
#: plainly names); none of those countries is a personal name, so they stay.
PERSON_NAME_COUNTRIES: frozenset[str] = frozenset({
    "jordan", "chad", "israel", "georgia", "india", "kenya", "ireland",
    "france", "cuba", "jamaica", "china", "monaco",
})

#: Words that make a span an INSTITUTION or PLACE even when every country in it
#: is a personal-name homonym ("Israel Police", "Jordan Valley", "Georgia
#: Tech", "India Today", "Chad Army"). Lower-cased.
INSTITUTION_OR_PLACE_WORDS: frozenset[str] = frozenset({
    "house", "senate", "congress", "parliament", "assembly", "council",
    "government", "govt", "ministry", "minister", "department", "state",
    "republic", "kingdom", "federal", "national", "federation", "union",
    "army", "navy", "air", "force", "forces", "police", "guard", "military",
    "defense", "defence", "court", "embassy", "consulate", "bank", "treasury",
    "airlines", "airways", "telecom", "university", "college", "tech",
    "institute", "school", "today", "times", "post", "news", "daily", "tv",
    "radio", "valley", "river", "sea", "gulf", "coast", "canal", "strait",
    "lake", "island", "islands", "border", "region", "province", "city",
    "team", "squad", "cup", "open", "league", "fc", "united", "olympic",
    "president", "presidency", "prime", "office", "authority", "agency",
    "company", "corp", "group", "inc", "ltd", "foundation", "fondation", "committee",
    "policy", "midterms",
})

#: Lower-case connectives that never sit inside a personal name the guard
#: needs ("Government of Liberia", "King of Jordan").
_NAME_CONNECTIVES = frozenset({
    "of", "the", "and", "for", "in", "de", "du", "des", "la", "le", "del", "da",
})

_NAME_TOKEN = re.compile(r"[^\W\d_][\w'’.\-]*", re.UNICODE)


#: ALL-CAPS country abbreviations a ``person`` span can carry ("US House",
#: "U.S. Republicans", "UK Treasury") — dots removed, lower-cased. Only these
#: count: "SK Hynix", "MK", "GTA VI" are not country mentions.
_COUNTRY_CODE_TOKENS = frozenset({
    "us", "usa", "uk", "uae", "drc", "rdc", "prc", "dprk", "rok", "ksa",
})


def _country_tokens(tokens: list[str], country_names: Mapping[str, str]) -> list[str]:
    """Every country surface (1–4 token n-grams, case-insensitive) in
    ``tokens``, plus every ALL-CAPS :data:`_COUNTRY_CODE_TOKENS` token."""
    found: list[str] = []
    low = [re.sub(r"['’]s$", "", t.lower()).strip(".") for t in tokens]
    for n in (4, 3, 2, 1):
        for i in range(len(low) - n + 1):
            gram = " ".join(low[i:i + n])
            if gram in country_names or gram.replace(" ", "") in country_names:
                found.append(gram)
    for t in tokens:
        bare = t.replace(".", "")
        if bare.isupper() and bare.lower() in _COUNTRY_CODE_TOKENS:
            found.append(bare.lower())
    return found


def is_personal_name_span(name: str, country_names: Mapping[str, str]) -> bool:
    """Whether the guard may blank ``name`` (a ``person``-class NER surface).

    A span that names NO country is blanked as before (blanking it cannot cost
    a country). A span that names one is blanked only when it is shaped like a
    personal name — two to four Capitalized tokens, no possessive, no
    lower-case connective, no institution/place word — AND every country in it
    is a :data:`PERSON_NAME_COUNTRIES` homonym. So "Jim Jordan" / "Chad Smith"
    / "Israel Adesanya" are blanked; "US House", "Panama Canal", "Government
    of Liberia", "Democratic Republic of Congo", "McDonald's Australia" stay.
    """
    tokens = _NAME_TOKEN.findall(name)
    if not tokens:
        return False
    countries = _country_tokens(tokens, country_names)
    if not countries:
        return True
    if not all(c in PERSON_NAME_COUNTRIES for c in countries):
        return False
    if not 2 <= len(tokens) <= 4:
        return False
    for t in tokens:
        low = t.lower()
        if low in _NAME_CONNECTIVES or low in INSTITUTION_OR_PLACE_WORDS:
            return False
        if low.endswith(("'s", "’s", "s'", "s’")) or not t[0].isupper():
            return False
    return True


def _blank(text: str, pattern: str, flags: int = 0) -> str:
    return re.sub(
        r"(?<![A-Za-z])" + pattern + r"(?![A-Za-z])",
        lambda m: " " * len(m.group(0)), text, flags=flags,
    )


def mask_person_names(
    text: str, entities: Any, *, country_names: Mapping[str, str],
) -> str:
    """``text`` with every person-name surface blanked (same length), plus
    every bare occurrence of a person's surname when that surname is a
    country name in ``country_names``."""
    if not text:
        return text
    persons = person_surfaces(entities)
    if not persons:
        return text
    out = text
    for name in sorted(persons, key=len, reverse=True):
        if not is_personal_name_span(name, country_names):
            # Lane N-U: a mislabelled institution / place ("US House",
            # "Panama Canal") — its country is the story's, not a person's.
            continue
        out = _blank(out, re.escape(name).replace(r"\ ", r"\s+"))
        parts = name.split()
        if len(parts) >= 2 and parts[-1].lower() in country_names:
            # Case-sensitive: a surname is capitalized. A bare surname right
            # after a LOCATIVE preposition ("King of Jordan", "in Jordan") is
            # the place, and stays.
            out = re.sub(
                r"(?<![A-Za-z])" + re.escape(parts[-1]) + r"(?![A-Za-z])",
                lambda m: m.group(0) if _locative_before(m.string, m.start())
                else " " * len(m.group(0)),
                out,
            )
    return out


#: A bare surname after one of these words is read as the PLACE.
_LOCATIVES = frozenset({
    "in", "of", "to", "from", "into", "across", "near", "at", "inside",
    "toward", "towards", "via", "visit", "visits", "visited",
})


def _locative_before(text: str, start: int) -> bool:
    prev = re.findall(r"[A-Za-z]+", text[max(0, start - 24):start])
    return bool(prev) and prev[-1].lower() in _LOCATIVES


def signal_context(payload: Mapping[str, Any] | None, *, max_chars: int = 4000) -> str:
    """The text a disambiguation cue may come from: title, summary, body."""
    if not isinstance(payload, Mapping):
        return ""
    parts: list[str] = []
    for key in ("title", "summary", "distilled_body", "text", "raw_body"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(v[:max_chars])
    return " \n ".join(parts)


__all__ = [
    "ABBREVIATION_COLLISIONS",
    "AMBIGUOUS_NAMES",
    "AmbiguousName",
    "COMMON_WORD_NAMES",
    "DEMONYM_EXCLUDED",
    "DEMONYM_LEAD_EVIDENCE",
    "DEMONYM_NON_PLACE_PHRASES",
    "EXTRA_COUNTRY_NAMES",
    "INSTITUTION_OR_PLACE_WORDS",
    "PERSON_NAME_COUNTRIES",
    "is_personal_name_span",
    "NON_PLACE_SURFACES",
    "QUERY_NAMES",
    "abbreviation_collisions",
    "blank_demonym_non_place_phrases",
    "blank_markup",
    "build_demonym_index",
    "first_demonym_iso2",
    "disambiguate",
    "is_common_word_name",
    "is_non_place_surface",
    "mask_person_names",
    "person_surfaces",
    "signal_context",
]
