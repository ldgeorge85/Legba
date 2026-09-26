# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE SHARED POLITY MATCHER — one answer to "does this prose NAME that country?".

WHY THIS MODULE EXISTS (A-0, ``ATTENTION_MEASUREMENT_DESIGN_2026-09-05`` §4).
The coverage-floor detector (#82) had to answer that question and paid for a
real answer: alias and demonym expansion in BOTH directions off the shared
``_entity_canon`` maps, a word-boundary probe with an optional plural, a
phrase-or-squeezed rule for multi-word surfaces, and a home-country exclusion
that is a PROSE match rather than a fold containment. It is pinned over all 32
fleet desks by ``test_every_fleet_desk_excludes_its_own_country``.

The attention instrument asks the SAME question of different prose — a desk's
own head, a world reference item, an open situation frame — and a second
implementation of it would be a second set of false positives. §Appendix A.4 of
the design measured what the naive version reports instead: 3 of its 6
fleet-wide "gaps" are the desk's own country under a spelling the gazetteer does
not join (``britain`` for GB, ``argentine`` for AR, ``türkiye`` for TR).

So the matcher moves HERE, beside :mod:`legba.data._entity_canon` whose maps it
is built from, and ``_coverage_floor_scan`` imports it and re-exports every name
under its old module path. Nothing at that module's call sites changes.

THE ONE FOLD SITE (MECH-6, ``LAYER_REVIEW`` §A.3: *"every text comparator the
assembly design adds must fold unicode at one shared normalisation site"*).
:func:`normalize_prose` now runs its input through
:func:`legba.data.provenance.text_fold.normalize_for_match` before it does
anything else. That is the ONLY place any text enters this module, so both
sides of every comparison here are folded by construction — never one side and
not the other, which is the whole MECH-6 failure class.

WHY THAT IS SAFE FOR THE COVERAGE FLOOR, stated as a property rather than a
hope. ``_PUNCT`` (``[^a-z0-9]+``) runs AFTER the fold and collapses everything
the fold could have rewritten into punctuation, so:

  * on PURE-ASCII input the two implementations are byte-identical — the fold's
    NFKC is a no-op, its punctuation table has no ASCII keys, ``casefold()``
    equals ``lower()``, and its whitespace collapse is subsumed by ``_PUNCT``.
    ``test_polity_match_fold_is_byte_identical_on_ascii`` pins this over the
    canon's entire surface set;
  * on non-ASCII input every difference is a strict WIDENING — a compat form
    (``ＵＳ``, ``ﬁ``), a deleted SOFT HYPHEN, or ``ß`` → ``ss`` now survives to
    match where it used to be punched out to a space. It can turn a ``None``
    into a surface; it can never turn a surface into ``None``.

A widening resolves toward "covered", which is the direction this matcher
already documents itself as resolving (see :func:`represented_by`). For the
coverage floor that means toward SILENCE — a widened match can only clear a
breach candidate or exclude a home country, never mint a new alert.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Sequence

from ._entity_canon import _ALIAS_MAP, _DEMONYM_MAP
from .provenance.text_fold import normalize_for_match

_PUNCT = re.compile(r"[^a-z0-9]+")
_POSSESSIVE = re.compile(r"[’'`]s\b")

#: A squeezed surface AT OR BELOW this many characters is matched as a whole
#: TOKEN of the folded prose instead of as a substring of the squeezed prose.
#:
#: WHY THE SQUEEZED LEG NEEDS A FLOOR AT ALL. The leg exists for run-together
#: spellings — a hashtag or a headline that writes "SouthAfrica" as one token,
#: which the phrase leg cannot see because the fold leaves it a single word. A
#: LONG squeezed form ("southafrica", "unitedarabemirates") cannot occur inside
#: an ordinary word by accident, so containment is safe for it. A SHORT one
#: occurs constantly: ``United States`` carries the curated surface ``u.s``,
#: which folds to ``u s`` and squeezes to ``us`` — a substring of *russia*,
#: *australia*, *focus*, *thousands* and *Jerusalem* (``jer-USA-lem`` squeezes
#: past ``usa`` too). Measured on the live 72 h pool, 2026-09-09: the leg put
#: ``United States`` on 2,641 of 10,507 titles (25.1%) where a whole-token
#: reading finds 996, and it handed the ``country_g20_us`` desk's recovery leg
#: twenty Jerusalem/consulate rows in its thirty-row admission budget.
#:
#: THE FLOOR COSTS NO RECALL, and that is a property rather than a hope: every
#: short squeezed form the canon carries (``us``, ``usa``, ``uk``, ``uae``,
#: ``un``) is ALREADY a single-word surface in ``_SURFACES``, so the token this
#: leg now requires is the token the word-boundary leg above already matches.
#: The clause is kept — rather than the leg simply dropped for short forms —
#: so that a polity whose curated set ever carries only the dotted spelling
#: still resolves. Pinned by
#: ``test_the_short_squeezed_forms_are_exactly_the_ones_the_canon_carries`` and
#: ``test_every_spelling_a_newsroom_writes_still_names_its_polity``.
SQUEEZE_TOKEN_MAX_CHARS: int = 3


@lru_cache(maxsize=4096)
def _boundary_probe(norm: str) -> "re.Pattern[str]":
    """``\\b<norm>s?\\b`` — THE word-boundary probe, compiled once per surface.

    Both surface legs ask it, so a single-word surface and a multi-word phrase
    cannot end up with two different ideas of where a name may begin and end.
    ``normalize_prose`` emits a stream of ``[a-z0-9]+`` tokens separated by
    single spaces, so in that stream ``\\b`` is exactly "token boundary".
    """
    return re.compile(r"\b" + re.escape(norm) + r"s?\b")


def _surface_matches(norm: str, prose: str, squashed: str) -> bool:
    """Does this ONE normalized surface name the polity in this folded prose?

    The single decision both :func:`represented_by` and
    :class:`legba.data._frame_anchor.PolitySurfaceIndex` implement — stated
    once here so the two spellings of it cannot drift, which is the failure
    ``test_batched_matcher_agrees_with_represented_by`` exists to catch.

    ``squeezed in squashed`` IS A NECESSARY CONDITION FOR BOTH MULTI-WORD
    LEGS, and saying so first is what keeps this cheap: a phrase that occurs in
    the token stream occurs, spaces removed, in the squeezed stream, so a
    failure here refuses the surface without compiling or running anything.

    IT IS ALSO SUFFICIENT FOR A LONG SURFACE, which is why the repair's blast
    radius is exactly five surfaces. The pre-repair rule was
    ``phrase in prose or squeezed in squashed``, and the left disjunct implies
    the right — so for every surface it was already just the squeezed test, and
    for every surface longer than :data:`SQUEEZE_TOKEN_MAX_CHARS` it still is,
    character for character. Only ``us``, ``usa``, ``uk``, ``un`` and ``uae``
    take the whole-token clause below. Pinned by
    ``test_a_long_surface_decides_exactly_as_it_did_before_the_repair``.
    """
    if " " not in norm:
        return _boundary_probe(norm).search(prose) is not None
    squeezed = norm.replace(" ", "")
    if squeezed not in squashed:
        return False
    if len(squeezed) > SQUEEZE_TOKEN_MAX_CHARS:
        return True
    # A short squeezed form must be a WHOLE TOKEN — either the phrase itself on
    # token boundaries ("the u s military") or the squeezed form as one token
    # ("the US"). ``_boundary_probe(squeezed)`` is the regex spelling of the
    # set lookup ``PolitySurfaceIndex`` uses; the class docstring proves them
    # equivalent over a ``normalize_prose`` token stream.
    return (
        _boundary_probe(norm).search(prose) is not None
        or _boundary_probe(squeezed).search(prose) is not None
    )


#: canonical country name -> every curated surface the canon knows for it.
#: Built ONCE from the shared canon's own maps, never a second hand-kept list:
#: if the canon learns a demonym, this learns it in the same commit.
_SURFACES: dict[str, set[str]] = {}
for _surface, _canonical in list(_ALIAS_MAP.items()) + list(_DEMONYM_MAP.items()):
    _SURFACES.setdefault(_canonical, set()).add(_surface)


def normalize_prose(text: str) -> str:
    """Prose -> a flat lowercase token stream fit for name matching.

    THE ONE FOLD SITE. Every text that reaches this module passes through
    :func:`~legba.data.provenance.text_fold.normalize_for_match` here and
    nowhere else, so a comparison in this module can never have one side folded
    and the other not.

    Possessives are dropped BEFORE punctuation is collapsed, because the
    register writes "Israel's coalition splinters" and a naive strip leaves
    "israels", which no word-boundary probe for "israel" can match — the exact
    shape that would have made the coverage-floor detector lie about its own
    archetype. The fold maps U+2019 to an ASCII apostrophe on the way in, so the
    possessive rule sees one spelling instead of three.

    ``str(text or "")`` is kept ahead of the fold deliberately: the fold returns
    ``""`` for a non-string, and this function's contract (inherited from the
    coverage floor) is that a number stringifies rather than vanishing.
    """
    folded = normalize_for_match(str(text or ""))
    low = _POSSESSIVE.sub(" ", folded)
    return _PUNCT.sub(" ", low).strip()


def entity_surfaces(canonical_name: str) -> set[str]:
    """Every written form that counts as NAMING this polity."""
    return {canonical_name} | _SURFACES.get(canonical_name, set())


def represented_by(canonical_name: str, frame_prose: str) -> str | None:
    """The surface some prose used to name this polity, or None if none did.

    Single-word surfaces match on a word boundary with an optional plural
    ("Palestinians" counts as naming Palestine); multi-word surfaces match
    either as a phrase — on the SAME word boundaries, so "Israel-Lebanon deal"
    still reads as coverage once the fold has turned the hyphen into a space —
    or with their spaces squeezed out, which is the leg that sees a
    run-together "SouthAfrica". Deliberately GENEROUS: every ambiguity here
    resolves toward "covered", i.e. toward silence.

    THE ONE PLACE GENEROSITY WAS NOT GENEROSITY. Both multi-word legs used to
    be bare containment over a token stream, and containment has no idea where
    a word starts. That let the ``United States`` surface ``u.s`` name a polity
    in *Russia*, *Jerusalem* and *Netanyahu says*; the fix is that both legs
    now respect token boundaries (see :data:`SQUEEZE_TOKEN_MAX_CHARS` and
    :func:`_surface_matches`). It is a strict NARROWING — every prose that
    still matches, matched before — and it costs no real naming: every polity
    carrying a multi-word surface also carries its demonym as a single-word one
    ("south african", "sri lankan", "south korean"), which is the leg that was
    doing the work for "South Africans" all along. Measured over the live 72 h
    signal pool (10,507 titles × 160 canon polities) and the last five days of
    assembled reads: no row loses a polity that a whole-token reading of its
    prose supports. See ``planning/GEO_POLITY_INDEX_WHOLE_TOKEN_REPORT.md``.
    """
    prose = normalize_prose(frame_prose)
    squashed = prose.replace(" ", "")
    for surface in entity_surfaces(canonical_name):
        norm = normalize_prose(surface)
        if not norm:
            continue
        if _surface_matches(norm, prose, squashed):
            return surface
    return None


# ---------------------------------------------------------------------------
# HOME-COUNTRY EXCLUSION
#
# A desk's OWN country is in every one of its signals and (nearly) every one of
# its frames; it can never be the missing second story, and letting it through
# would make the loudest cluster on every desk a permanent false positive.
#
# The test is deliberately the SAME machinery as the frame check —
# ``represented_by(cluster_name, home_prose)`` — rather than a fold comparison.
# Fold containment was tried and is unsafe in both directions: "niger" is a
# substring of "nigeria" (Nigeria is a legitimate foreign polity for the Niger
# desk) and "sudan" of "south sudan", while "Russia" does NOT contain the ISO
# spelling "Russian Federation" that both gazetteers return. Running the
# canon's alias/demonym expansion over a prose blob of every home spelling gets
# all four cases right: Russia matches via the demonym "Russian", Nigeria and
# South Sudan match nothing, and the home country matches itself.
# ---------------------------------------------------------------------------

#: Extra home spellings for the ISO2s whose gazetteer name is NOT the form the
#: canon (or a newsroom) writes. Additive only, and additive in the SAFE
#: direction: an ISO2 missing from this map still gets its gazetteer names, so
#: a gap can at worst let a desk's own country through to the frame check —
#: which the desk's frames then almost always cover anyway. Pinned per fleet
#: desk by ``test_every_fleet_desk_excludes_its_own_country``.
_ISO2_HOME_ALIASES: dict[str, tuple[str, ...]] = {
    "CD": ("Democratic Republic of the Congo", "DR Congo", "DRC", "Congo"),
    "GB": ("Britain", "Great Britain", "England"),
    "IR": ("Iran", "Islamic Republic of Iran", "Persia"),
    "KP": ("North Korea", "DPRK"),
    "KR": ("South Korea", "ROK"),
    "RU": ("Russia", "Russian Federation"),
    "TR": ("Turkey", "Turkiye", "Türkiye"),
    "TW": ("Taiwan", "Republic of China"),
    "US": ("United States", "America", "USA"),
}


def _pycountry_names(iso2: str) -> tuple[str, ...]:
    """Every spelling pycountry holds for an ISO2 (empty if unavailable).

    pycountry is already the canon's gazetteer dependency; this read is the
    reason KR/KP/IR/TW need no override entry (their ``common_name`` is the
    newsroom spelling) while RU/TR do.
    """
    try:
        import pycountry
    except Exception:  # pragma: no cover — dependency-shape guard
        return ()
    record = pycountry.countries.get(alpha_2=iso2.upper())
    if record is None:
        return ()
    return tuple(
        str(n)
        for n in (
            getattr(record, "name", None),
            getattr(record, "common_name", None),
            getattr(record, "official_name", None),
        )
        if n
    )


def home_prose(iso2s: Sequence[str], gazetteer_names: Sequence[str] = ()) -> str:
    """The blob of every spelling of this desk's own country/countries."""
    parts: list[str] = [str(n) for n in gazetteer_names if n]
    for iso2 in iso2s:
        parts.extend(_pycountry_names(str(iso2)))
        parts.extend(_ISO2_HOME_ALIASES.get(str(iso2).upper(), ()))
    return " || ".join(dict.fromkeys(parts))


def is_home_country(canonical_name: str, home_blob: str) -> bool:
    """True when this cluster IS the desk's own country under any spelling."""
    return represented_by(canonical_name, home_blob) is not None


#: The newsroom spelling of a desk's own country — the FIRST home alias when the
#: canon carries an override, else the gazetteer's own name. The gazetteer alone
#: is wrong for the two cases the design names: ``GB`` returns "United Kingdom"
#: and the wire says "Britain"; ``TR`` returns "Türkiye" and the wire says
#: "Turkey". A search query built from the gazetteer spelling asks a different
#: question than the one the world's reporting answers.
def home_country_name(iso2: str, gazetteer_name: str = "") -> str:
    """The name to put in a search query for this ISO2, or ``""`` if unknown."""
    aliases = _ISO2_HOME_ALIASES.get(str(iso2).upper(), ())
    if aliases:
        return aliases[0]
    if gazetteer_name:
        return str(gazetteer_name)
    names = _pycountry_names(str(iso2))
    return names[0] if names else ""


__all__ = [
    "SQUEEZE_TOKEN_MAX_CHARS",
    "entity_surfaces",
    "home_country_name",
    "home_prose",
    "is_home_country",
    "normalize_prose",
    "represented_by",
]
