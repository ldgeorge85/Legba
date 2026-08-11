# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entity-class typing for the NER filter — the tier ladder and its gazetteers.

Extracted from :mod:`legba.data.filters.ner` in 2026-08 (W3-C), which crossed the
1,500-line module gate. The seam is the one the file's own section banners
already drew: everything here answers ONE question — given a free-text triple
endpoint and its predicate, which of the closed 9-value ``entity_class`` values
is it — and nothing here knows about HTTP, the hosted /extract contract, script
detection, or the handler lifecycle, which is the rest of ``ner.py``.

Nothing moved changed. ``ner.py`` re-exports :func:`_classify_entity_text`, so
every import site (``fact_extractor``, the filter tests) is untouched by the
split; the names are re-exported rather than repointed deliberately, because a
module split that forces its importers to move is a refactor pretending to be a
cleanup.

The ladder, in precedence order:

  operator overrides -> corporation legal-form suffix -> the SHARED CANON
  gazetteer (country / organization / place) -> a leading honorific -> the
  predicate mapping -> the cue ladder -> acronym shape -> article prefix ->
  the two-capitalised-tokens person default.

Two later passes matter enough to name here, and both are documented in full at
their own section banners below: R8 (2026-07) put the canon gazetteer IN FRONT
of the predicate and cue tiers, because the person default was typing "White
House" and "Russian Federation" as people; W3-C (2026-08) added the person GATE,
because a 60-pair audit of the phonetic-alias backlog found 15% of the sampled
``person`` rows were NWS forecast zones, villages, militant groups and one Uzbek
prepositional phrase.
"""

from __future__ import annotations

import re
from typing import Mapping

from .._entity_canon import DEFAULT_CLASS, canonicalize_entity, leads_with_direction


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
    # W3-C (2026-08-04) — competition names. "Asian Championships" / "Asian
    # Championship" were BOTH sitting in the live person table and were one of
    # the nine non-people in the 60-pair phonetic-alias sample; the same census
    # found "World Cup Group K" / "World Cup Group G". A competition is an
    # event, and "tournament" was already here — the words the feed actually
    # uses were not.
    "championship", "championships", "cup", "games", "qualifier", "qualifiers",
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


# ---------------------------------------------------------------------------
# W3-C (2026-08-04) — THE PERSON GATE.
#
# THE MEASUREMENT. A 60-pair random sample of the V-G6 phonetic-alias backlog
# found NINE pairs (15%) whose rows are `entity_class='person'` and denote no
# human: NWS forecast zones ("Eastern Pendleton" / "Western Pendleton",
# "Eastern Monmouth" / "Western Monmouth"), a Lebanese village ("Kfar Shoba"),
# a Black Sea resort ("Arkhipo-Osipovka"), a South Atlantic island group
# ("Tristan da Cunha"), a Somali militant organisation ("al-Shabaab"), a
# competition ("Asian Championships"), a reduplicated common noun ("Media
# Media" / "Medya Medya") and an Uzbek prepositional phrase ("ochilish
# marosimidagi" — "at the opening ceremony").
#
# WHERE THEY COME FROM, read off the live payloads rather than guessed. Every
# one arrives through :func:`_classify_entity_text`, and two tiers let them in:
#
#   * the two-capitalised-tokens PERSON DEFAULT at the bottom of the ladder,
#     reached because GLiREL emitted them as endpoints of a locative predicate
#     ("Eastern Pendleton" / part of, "Kfar Shoba" / border with, "Media Media"
#     / located in) that the ladder has no reading for; and
#   * :data:`_PERSON_SUBJECT_PREDICATES`, which returns `person` for a SUBJECT
#     of "member of" before the cue ladder is ever consulted — which is how
#     "Asian Championships" and a lower-cased Uzbek phrase became people.
#
# WHAT WAS NOT DONE, and why. The obvious fix — treat the locative predicates
# ("part of", "located in", "border with") as a person veto — was measured
# against the live relation payloads first and abandoned: 18,304 relations have
# a person subject under "part of" and 13,399 under "located in". GLiREL puts
# real people on the subject of those predicates constantly, so that veto would
# have demoted thousands of genuine names to shrink a nine-row defect class.
# The gate below therefore rests only on the SURFACE, never on the predicate.
#
# THE RULES, each one measured:
#
#   1. LEADING DIRECTION. 431 active person rows begin with a compass or
#      positional token; a full read of all 431 found ZERO people. See
#      :func:`_entity_canon.leads_with_direction` — first token only, because
#      "Oliver North" is a person and his direction token is the surname.
#   2. NO CAPITALISED TOKEN. A Latin-script personal name has at least one.
#      This is what "ochilish marosimidagi" fails. It can only fire above the
#      bottom tier — the two-capitalised-tokens default is already unreachable
#      without a capital — so its whole blast radius is a surface that GLiREL
#      made the subject of a person predicate, or that carries a role cue.
#      MEASURED COST, stated rather than glossed: 346 active person rows are
#      all-lower-case multi-token, and a 60-row read found roughly a fifth of
#      them to be URL-slug renderings of real people ("javier - milei",
#      "jed spence") among four fifths of temporal and quantity junk ("almost
#      three decades", "just 86 minutes", "epidemiological week 30"). Those
#      slugs lose `person` and land in `entity`. Accepted: the slug surfaces are
#      a separate ingestion defect, they still carry the same class-agnostic
#      ``identity_fold`` key as the properly-cased row, and none of the labeled
#      sample's confirmed people is lower-case.
#   3. REDUPLICATION. Every token identical is a doubled common noun ("Media
#      Media"), never a name.
#   4. A POSITIVE NON-PERSON CUE already on the ladder (organization /
#      corporation / event / software). Only consulted at the person-subject-
#      predicate tier, which would otherwise return before those cues are read.
#
# A veto does NOT invent a class: it declines the person tier and lets the rest
# of the ladder run, so the surface lands on whatever cue or gazetteer tier does
# claim it and otherwise on the generic `entity` bucket. Nothing here can
# reclassify an existing row — this is the mint path, forward only.
# ---------------------------------------------------------------------------


def _non_person_surface(
    tokens: list[str], lower_tokens: set[str], *, check_cues: bool = False
) -> bool:
    """True when the SURFACE carries a positive signal that it is not a person.

    Conservative by construction — every rule fires on a property a personal
    name does not have, not on the absence of one. ``check_cues`` adds rule 4
    and is used only by the person-subject-predicate tier, which runs above the
    cue ladder and would otherwise never see those cues.
    """
    if not tokens:
        return False
    if leads_with_direction(" ".join(tokens)):                       # rule 1
        return True
    if not any(tok[:1].isupper() for tok in tokens):                 # rule 2
        return True
    if len(tokens) >= 2 and len({tok.lower() for tok in tokens}) == 1:  # rule 3
        return True
    if check_cues and lower_tokens & (                                # rule 4
        _ORGANIZATION_CUES | _CORPORATION_CUES | _EVENT_CUES | _SOFTWARE_CUES
    ):
        return True
    return False


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
        # W3-C — this tier runs ABOVE the cue ladder, so an unvetoed hit here
        # types "Asian Championships" (a competition, subject of "member of")
        # and a lower-cased Uzbek phrase as people before "championships" is
        # ever read as an event cue. ``check_cues`` closes exactly that window.
        if lo_pred in _PERSON_SUBJECT_PREDICATES and not _non_person_surface(
            tokens, lower_tokens, check_cues=True
        ):
            return "person"

    # Cue-token scan.
    if lower_tokens & _CORPORATION_CUES:
        return "corporation"
    if lower_tokens & _ORGANIZATION_CUES:
        return "organization"
    # W3-C — a ROLE cue is matched on any token, anywhere, so "Eastern DR Congo"
    # took `person` off "dr" and "East General Stewart Way" off "general". The
    # surface gate applies here too: a role word inside a surface that begins
    # with a compass bearing describes what is at that place, not who it is.
    if lower_tokens & _PERSON_CUES and not _non_person_surface(tokens, lower_tokens):
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
    #
    # W3-C — this default is where the NWS forecast zones and the reduplicated
    # common nouns entered the person table. The gate stays LOOSE on the name
    # shape itself (a count of capitalised tokens, not "every token
    # capitalised") because the live payloads carry "Ziyad al - Nakhalah",
    # "Khalil al - Hayya" and "Hong Myung - bo" — real people whose particles
    # and hyphens are lower-cased — and tightening the shape would have cost
    # more genuine names than it saved.
    title_tokens = [tok for tok in tokens if tok[:1].isupper()]
    if len(title_tokens) >= 2 and not _non_person_surface(tokens, lower_tokens):
        return "person"
    # Single token (capitalised or not) with no cues → "entity" — the
    # generic bucket. Geocoding (L-153) and other downstream enrichments
    # can refine if appropriate.
    return "entity"
