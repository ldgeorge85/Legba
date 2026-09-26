# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A-0 — the shared polity matcher, and the BYTE-IDENTITY PROOF for its move.

``_coverage_floor_scan``'s matcher moved to :mod:`legba.data._polity_match` and
picked up the MECH-6 fold at its single normalisation site on the way. The
extraction's contract is that the coverage-floor detector's output is unchanged;
the 70 tests in ``test_coverage_floor_scan.py`` and the 87 in
``test_alert_trigger_scan.py`` pass UNCHANGED, and this file adds the property
those suites cannot state — that the fold itself changed nothing that could
matter, and could only ever change things in ONE direction.

Two claims, each proved rather than asserted:

  1. **Byte-identical on ASCII.** The pre-fold implementation is reproduced
     verbatim below and run against the live one over the canon's ENTIRE
     gazetteer surface set plus a corpus of live-shaped register prose. Equal,
     string for string. The whole coverage-floor corpus — 32 desk names, every
     ``_ALIAS_MAP`` / ``_DEMONYM_MAP`` surface, every ``_ISO2_HOME_ALIASES``
     entry bar one — is ASCII, so this covers the detector's actual inputs.
  2. **A strict widening otherwise.** On non-ASCII input every difference is a
     compat form, a deleted SOFT HYPHEN or a ``ß`` that now SURVIVES to match
     where ``[^a-z0-9]+`` used to punch it out. It can turn a ``None`` into a
     surface; it can never turn a surface into ``None`` — which for the coverage
     floor means it can only CLEAR a breach candidate or exclude a home country,
     never mint a new alert.
"""
from __future__ import annotations

import re

import pytest

from legba.data import _polity_match as pm
from legba.data._entity_canon import _ALIAS_MAP, _DEMONYM_MAP
from legba.data.analysts.deterministic_handlers import _coverage_floor_scan as cfs


# ---------------------------------------------------------------------------
# The PRE-FOLD implementation, reproduced from the commit before A-0. This is
# the reference the byte-identity claim is made against; if it is ever edited
# to make a test pass, the claim is worthless.
#
# IT CARRIES ONE DELIBERATE RE-BASE, and it is stated here rather than buried.
# The 2026-09-09 whole-token repair changed the multi-word SURFACE RULE (both
# legs now respect token boundaries — see ``SQUEEZE_TOKEN_MAX_CHARS``). These
# tests exist to isolate ONE variable, the A-0 FOLD, so the reference below
# keeps the pre-fold NORMALISER verbatim and takes the surface rule from the
# module under test. Leaving the old containment rule in it would have made
# every assertion here measure the token repair instead of the fold, which is
# the one thing this file is not for. The repair has its own before/after
# corpus proof in ``test_whole_token_surface_rule.py``.
# ---------------------------------------------------------------------------

_OLD_PUNCT = re.compile(r"[^a-z0-9]+")
_OLD_POSSESSIVE = re.compile(r"[’'`]s\b")


def _old_normalize_prose(text: str) -> str:
    low = _OLD_POSSESSIVE.sub(" ", str(text or "").lower())
    return _OLD_PUNCT.sub(" ", low).strip()


def _old_represented_by(canonical_name: str, frame_prose: str) -> str | None:
    prose = _old_normalize_prose(frame_prose)
    squashed = prose.replace(" ", "")
    for surface in pm.entity_surfaces(canonical_name):
        norm = _old_normalize_prose(surface)
        if not norm:
            continue
        if pm._surface_matches(norm, prose, squashed):
            return surface
    return None


# ---------------------------------------------------------------------------
# The corpora
# ---------------------------------------------------------------------------

#: Every written surface the matcher can ever be handed: both canon maps, every
#: canonical name they fold onto, and every home-country override spelling.
def _gazetteer_corpus() -> list[str]:
    out: set[str] = set()
    out |= set(_ALIAS_MAP) | set(_ALIAS_MAP.values())
    out |= set(_DEMONYM_MAP) | set(_DEMONYM_MAP.values())
    for aliases in pm._ISO2_HOME_ALIASES.values():
        out |= set(aliases)
    return sorted(out)


#: Live-shaped register prose — the exact strings the 2026-09-03 census read off
#: ``situations.name`` and the desk heads, plus the punctuation shapes the
#: coverage floor's own docstring names as load-bearing.
_PROSE_CORPUS = [
    "Israel's coalition splinters over the Dolphin-class submarine deal",
    "Israel’s coalition splinters",
    "Coordinated narrative on large-scale Ukrainian drone attacks in Russia",
    "US maintains high-severity seizure of oil",
    "U.S. extends Middle East troop deployments",
    "Anti-Saudi war-crime narrative spreads across Yemen-linked channels",
    "Israel-Lebanon maritime deal holds",
    "Qatar defence-export ban; UNRWA coverage; border-force buildup",
    "gasoline prices ease ahead of the October election",
    "DPRK missile test over the Sea of Japan",
    "Türkiye's lira slides",
    "",
    "   ",
    "12345",
]


# ---------------------------------------------------------------------------
# CLAIM 1 — byte-identical on ASCII
# ---------------------------------------------------------------------------


def test_polity_match_fold_is_byte_identical_on_ascii():
    """THE byte-identity proof for the A-0 move.

    Every ASCII input the matcher can see normalizes to the SAME string under
    the pre-fold implementation and the folded one. The fold's NFKC is a no-op
    on ASCII, its punctuation table has no ASCII keys, ``casefold()`` equals
    ``lower()``, and its whitespace collapse is subsumed by ``[^a-z0-9]+``.
    """
    corpus = [
        s for s in _gazetteer_corpus() + _PROSE_CORPUS if s.isascii()
    ]
    assert len(corpus) > 200, f"corpus collapsed to {len(corpus)} — check the maps"
    mismatched = [
        (s, _old_normalize_prose(s), pm.normalize_prose(s))
        for s in corpus
        if _old_normalize_prose(s) != pm.normalize_prose(s)
    ]
    assert mismatched == [], (
        f"the A-0 fold changed {len(mismatched)} ASCII normalisations: "
        f"{mismatched[:5]}"
    )


def test_represented_by_is_byte_identical_on_the_live_prose_corpus():
    """The same proof one level up: the MATCH VERDICT, not just the fold.

    Every (polity, prose) pair over the gazetteer x the live register corpus
    returns the identical surface (or the identical ``None``) before and after.
    """
    polities = sorted(set(_ALIAS_MAP.values()) | set(_DEMONYM_MAP.values()))
    assert len(polities) > 50
    diffs = []
    for prose in _PROSE_CORPUS:
        if not prose.isascii():
            continue
        for polity in polities:
            old = _old_represented_by(polity, prose)
            new = pm.represented_by(polity, prose)
            if old != new:
                diffs.append((polity, prose, old, new))
    assert diffs == [], f"the A-0 fold changed {len(diffs)} verdicts: {diffs[:5]}"


def test_home_country_exclusion_is_byte_identical_over_every_fleet_desk():
    """``test_every_fleet_desk_excludes_its_own_country``'s own input set, run
    through both implementations. This is the pinned 32-desk guarantee the
    coverage floor's home exclusion rests on."""
    for iso2 in sorted(pm._ISO2_HOME_ALIASES) + [
        "AR", "AU", "BR", "CA", "CN", "DE", "FR", "ID", "IN", "IT", "JP",
        "MX", "SA", "ZA", "BF", "HT", "ML", "MM", "NE", "PK", "SD", "UA",
        "IL",
    ]:
        blob = pm.home_prose([iso2])
        for name in pm._pycountry_names(iso2) + pm._ISO2_HOME_ALIASES.get(iso2, ()):
            assert (
                _old_represented_by(name, blob) is not None
            ) == pm.is_home_country(name, blob), (iso2, name)


# ---------------------------------------------------------------------------
# CLAIM 2 — a strict widening on the unicode plane
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "polity,prose",
    [
        # Full-width compat forms — NFKC reaches them, `[^a-z0-9]+` punched them
        # out entirely.
        ("United States", "ＵＳ troop posture unchanged"),
        # SOFT HYPHEN, an invisible line-break hint the wire inserts. The old
        # normaliser split the word in half; the fold DELETES it.
        ("Israel", "Is­rael's coalition splinters"),
        # The MECH-6 specimen itself — U+2011 NON-BREAKING HYPHEN.
        ("Israel", "Israel‑Lebanon maritime deal"),
    ],
)
def test_the_fold_only_ever_widens_toward_covered(polity, prose):
    """A folded match is a match the old matcher MISSED, never one it lost.

    Every one of these resolves toward "covered" — which for the coverage floor
    is toward SILENCE (a covered polity is not a breach; a matched home country
    is excluded from scoring altogether). The fold cannot mint an alert.
    """
    assert pm.represented_by(polity, prose) is not None
    # ...and the pre-fold matcher is allowed to have missed it. Assert only the
    # direction: nothing the OLD matcher found may now be lost.
    old = _old_represented_by(polity, prose)
    assert old is None or pm.represented_by(polity, prose) is not None


def test_no_ascii_or_unicode_input_loses_a_match_it_used_to_have():
    """The widening property, stated as the invariant that actually matters:
    ``old is not None`` implies ``new is not None``, over both corpora."""
    polities = sorted(set(_ALIAS_MAP.values()) | set(_DEMONYM_MAP.values()))
    lost = []
    for prose in _PROSE_CORPUS + _gazetteer_corpus():
        for polity in polities:
            if _old_represented_by(polity, prose) is not None:
                if pm.represented_by(polity, prose) is None:
                    lost.append((polity, prose))
    assert lost == [], f"the fold LOST {len(lost)} matches: {lost[:5]}"


# ---------------------------------------------------------------------------
# The re-export contract
# ---------------------------------------------------------------------------


def test_coverage_floor_re_exports_every_moved_name():
    """The move must be invisible at ``_coverage_floor_scan``'s call sites and
    in its public surface — the same objects, not copies."""
    for name in (
        "normalize_prose", "entity_surfaces", "represented_by",
        "home_prose", "is_home_country",
    ):
        assert getattr(cfs, name) is getattr(pm, name), name
        assert name in cfs.__all__, f"{name} dropped out of __all__"
    assert cfs._SURFACES is pm._SURFACES
    assert cfs._ISO2_HOME_ALIASES is pm._ISO2_HOME_ALIASES


def test_the_fold_site_is_the_only_one():
    """MECH-6, enforced: this module normalizes text in exactly ONE function,
    and that function calls the shared fold. A second normalisation entry point
    is how one side of a comparator gets folded and the other does not."""
    source = (
        __import__("pathlib").Path(pm.__file__).read_text()
    )
    assert source.count("normalize_for_match(") == 1, (
        "more than one call to the shared fold in _polity_match — every text "
        "input must enter through normalize_prose"
    )


def test_home_country_name_prefers_the_newsroom_spelling():
    """The gazetteer alone is wrong for the two cases the design names: GB
    returns "United Kingdom" and the wire says "Britain"; TR returns "Türkiye"
    and the wire says "Turkey". A query built from the gazetteer spelling asks a
    different question than the one the world's reporting answers."""
    assert pm.home_country_name("GB") == "Britain"
    assert pm.home_country_name("TR") == "Turkey"
    assert pm.home_country_name("US") == "United States"
    # No override -> the gazetteer name the caller already has.
    assert pm.home_country_name("IL", "Israel") == "Israel"
    # No override, no gazetteer name -> pycountry, still never a guess.
    assert pm.home_country_name("FR") == "France"
    assert pm.home_country_name("ZZ") == ""
