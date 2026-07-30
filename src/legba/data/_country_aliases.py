# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Curated country-name alias gazetteer — official-vs-common name equivalences.

WHY THIS MODULE EXISTS (E4a recall lever, 2026-07-28)
-----------------------------------------------------
The human-labeled merge-quality pass measured entity-merge recall at 0.347 with
one recurring miss class being OFFICIAL vs COMMON country names: "Myanmar" and
"Burma" share no block-key token and are nowhere near each other in trigram
space, so the E3 probes can never even PROPOSE the pair — the adjudicator never
sees it. This module is a tiny, closed, hand-curated list of exact bidirectional
alias groups for exactly that class, consumed by
:func:`legba.data._entity_candidates.generate_candidates` as a third candidate
probe (class-gated to country/location/generic rows; persons/orgs untouched).

CURATION RULES (precision 1.000 must not move)
----------------------------------------------
* Only groups where EVERY member unambiguously denotes the SAME sovereign state
  (a renaming, an exonym, or a standard short form) are listed.
* Deliberately ABSENT — these are DISTINCT states / contested referents and
  must NEVER appear in any group (guarded by import-time asserts):
    - Sudan vs South Sudan;
    - any Korea pairing (Korea / North Korea / South Korea);
    - China vs Taiwan;
    - bare "Congo" (ambiguous between the two Congo republics — only the
      explicit Kinshasa/Brazzaville forms are grouped, in SEPARATE groups).
* Matching is EXACT on a normalized surface (lower + diacritic fold + punct→
  space + leading-article strip) — never fuzzy, never substring, so "South
  Sudan" can never fall into the "Sudan"-less groups by accident.

Layering: a pure leaf module (stdlib only) at ``legba.data``, importable by
``_entity_candidates`` without touching ``legba.data.analysts.*``.
"""

from __future__ import annotations

import re
import unicodedata

#: The curated groups. Each inner tuple is one referent; every pair of members
#: within a group is an exact bidirectional alias. Order within a group is
#: cosmetic except that the FIRST member's normalized form is the group's key
#: (used as the candidate's shared-key stamp in the probe).
_ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("Timor-Leste", "East Timor"),
    ("Democratic Republic of the Congo", "DR Congo", "DRC", "Congo-Kinshasa"),
    ("Republic of the Congo", "Congo-Brazzaville"),  # DISTINCT from the DRC group
    ("Myanmar", "Burma"),
    ("Côte d'Ivoire", "Ivory Coast"),
    ("Cabo Verde", "Cape Verde"),
    ("Eswatini", "Swaziland"),
    ("North Macedonia", "Macedonia"),
    ("Czechia", "Czech Republic"),
    ("Türkiye", "Turkey"),
    ("Holy See", "Vatican City"),
)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an) ")

#: The SQL expression computing the SAME normalization server-side (over
#: ``public.f_unaccent`` from migration 0088). ``{col}`` is the column to
#: normalize. Kept HERE, next to the Python twin, so the two can never drift
#: apart in separate files unnoticed; a DB-backed test asserts they agree on
#: every curated surface.
SQL_NORM_TEMPLATE = (
    "btrim(regexp_replace(btrim(regexp_replace("
    "lower(public.f_unaccent({col})), '[^a-z0-9]+', ' ', 'g')), "
    "'^(the|a|an) ', ''))"
)


def normalize_country_surface(name: str) -> str:
    """Normalize a surface form for exact gazetteer matching.

    lower + NFKD diacritic fold ("Türkiye" -> "turkiye", "Côte d'Ivoire" ->
    "cote d ivoire") + every non-alphanumeric run to ONE space + strip a single
    leading the/a/an ("the Czech Republic" -> "czech republic"). Pure,
    deterministic, idempotent; mirrors :data:`SQL_NORM_TEMPLATE` exactly.
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = _NON_ALNUM_RE.sub(" ", s.lower()).strip()
    s = _LEADING_ARTICLE_RE.sub("", s).strip()
    return s


#: normalized surface -> group index. Built once at import; the asserts make a
#: curation mistake (a surface in two groups, or a forbidden referent slipping
#: in) an import-time failure rather than a silent precision bug.
_GROUP_BY_SURFACE: dict[str, int] = {}
for _gi, _group in enumerate(_ALIAS_GROUPS):
    for _surface in _group:
        _key = normalize_country_surface(_surface)
        assert _key, f"alias surface normalized to empty: {_surface!r}"
        assert _GROUP_BY_SURFACE.setdefault(_key, _gi) == _gi, (
            f"alias surface in two groups: {_surface!r}"
        )

#: Referents that must NEVER match any group (see the curation rules above).
_FORBIDDEN_SURFACES: tuple[str, ...] = (
    "sudan", "south sudan",
    "korea", "north korea", "south korea",
    "china", "taiwan",
    "congo",  # bare form is ambiguous between the two Congo republics
)
for _f in _FORBIDDEN_SURFACES:
    assert _f not in _GROUP_BY_SURFACE, f"forbidden referent in gazetteer: {_f!r}"


def alias_group_id(name: str) -> int | None:
    """The gazetteer group index for ``name`` (exact normalized match), else
    ``None`` for any surface outside the curated list."""
    return _GROUP_BY_SURFACE.get(normalize_country_surface(name))


def alias_group_key(name: str) -> str | None:
    """The group's stable key (the normalized FIRST member) for ``name``, else
    ``None``. E.g. ``alias_group_key("Burma") == "myanmar"``."""
    gi = alias_group_id(name)
    if gi is None:
        return None
    return normalize_country_surface(_ALIAS_GROUPS[gi][0])


def are_country_aliases(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` are members of the SAME alias group but are
    not the same normalized surface (a name is not its own alias)."""
    ga = alias_group_id(a)
    if ga is None:
        return False
    if normalize_country_surface(a) == normalize_country_surface(b):
        return False
    return alias_group_id(b) == ga


def alias_surfaces() -> tuple[str, ...]:
    """Every normalized surface in the gazetteer, sorted — the SQL probe's
    ``ANY($1)`` filter."""
    return tuple(sorted(_GROUP_BY_SURFACE))


__all__ = [
    "SQL_NORM_TEMPLATE",
    "alias_group_id",
    "alias_group_key",
    "alias_surfaces",
    "are_country_aliases",
    "normalize_country_surface",
]
