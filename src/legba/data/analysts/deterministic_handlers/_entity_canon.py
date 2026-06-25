# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entity-name canonicalization — surface-form merge + NER type correction.

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

Idempotent: ``canonicalize_entity(*canonicalize_entity(name, cls)) ==
canonicalize_entity(name, cls)`` — the strip + alias + type passes all reach a
fixed point in one application.
"""

from __future__ import annotations

import html
import re
from functools import lru_cache

import pycountry

from ...vocabulary import ENTITY_CLASSES

# ---------------------------------------------------------------------------
# Canonical class strings — drawn from the closed taxonomy, never invented.
# ---------------------------------------------------------------------------

#: The class a country name is forced onto. Member of ENTITY_CLASSES.
COUNTRY_CLASS = "country"
#: The class an obvious-organization surface pattern (NWS …) is forced onto.
ORGANIZATION_CLASS = "organization"
#: The generic fallback bucket (taxonomy default).
DEFAULT_CLASS = "entity"

assert COUNTRY_CLASS in ENTITY_CLASSES
assert ORGANIZATION_CLASS in ENTITY_CLASSES
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
}

#: Aliases that resolve to a supranational body, not an ISO country. The
#: gazetteer won't match these, so they carry their own class hint here.
_ALIAS_ORG: frozenset[str] = frozenset({"European Union"})


# ---------------------------------------------------------------------------
# DEMONYM MAP — national demonym/adjective → canonical country (DQ-H4). NER
# emits demonyms ("Iranian", "Israeli") as first-class entities distinct from
# their country ("Iran co-occurs with Iranian" — same referent), inflating
# graph centrality. Collapse the clear NATIONAL demonyms to their country so
# they merge. CURATED (not a suffix regex) so surnames like "Meloni" / words
# like "Asian"/"European" (no single country) are never mis-collapsed. Values
# are the same canonical forms the alias map + gazetteer use.
# ---------------------------------------------------------------------------

_DEMONYM_MAP: dict[str, str] = {
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
}

#: Short / junk tokens NER mis-emits as entities (DQ-H4). Kept TINY + obviously
#: non-entity so legitimate short orgs ("US", "UK", "EU", "UN") are never
#: dropped. Matched case-insensitively on the stripped surface form.
_JUNK_ENTITIES: frozenset[str] = frozenset({"tv", "radio", "online"})


def is_demonym(name: str) -> bool:
    """True when ``name`` is a curated NATIONAL demonym (collapses to a country)."""
    return _strip_name(str(name or "")).lower() in _DEMONYM_MAP


def is_junk_entity(name: str) -> bool:
    """True when ``name`` is a known junk/non-entity token (DQ-H4 gate)."""
    return _strip_name(str(name or "")).lower() in _JUNK_ENTITIES


# ---------------------------------------------------------------------------
# COUNTRY GAZETTEER — built from pycountry, the SAME ISO-3166-1 dataset the
# `iso_countries` table is generated from (scripts/_gen_iso_countries_seed.py).
# Keys are lowercased country names / common / official names. Membership in
# this set forces COUNTRY_CLASS. Kept module-level + lazy so import is cheap.
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _country_name_set() -> frozenset[str]:
    names: set[str] = set()
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

    # TYPE CORRECTION — gazetteer wins, then the org pattern. A country name is
    # never a person; an NWS office is never a person.
    if _is_country_name(canonical):
        return canonical, COUNTRY_CLASS
    if canonical in _ALIAS_ORG:
        return canonical, ORGANIZATION_CLASS
    if _is_org_pattern(canonical):
        return canonical, ORGANIZATION_CLASS

    return canonical, cls


__all__ = [
    "canonicalize_entity",
    "is_demonym",
    "is_junk_entity",
    "COUNTRY_CLASS",
    "ORGANIZATION_CLASS",
    "DEFAULT_CLASS",
]
