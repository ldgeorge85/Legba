# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The instance's rosters, read from its profile (ROSTER-1).

Every roster the code used to spell as a literal — the desk-roster tag array,
its scored subset, the bounded-unit roster and its scored subset, the source
catalog — is profile DATA (``profiles/<id>/profile.yaml`` ``rosters``,
docs/PROFILES.md). This module is the ONE reader the code calls:

:func:`subject_roster_tags`
    every tag that puts a target on the subject roster (the desk-coverage
    denominator; bound into SQL as ``?| $n::text[]``).
:func:`scored_roster_tags`
    the subset carrying the per-subject scorecard, baselines and
    baseline-deviation alerts.
:func:`unit_roster` / :func:`scored_unit_roster`
    the bounded units in display order, and the fixed scorecard dimensions.
:func:`scored_subject_units`
    the units that fire on the scored subjects (the scored dimensions plus the
    units narrowed within those legs) — ROSTER-2.
:func:`unit_terms` / :func:`terms_for` (and the per-instrument views)
    each unit's own data — search terms, title vocabulary, reference query
    patterns, scope tag — keyed by the roster (ROSTER-2). A unit that
    declares none reads as :data:`NO_TERMS`: the instrument skips it and says
    so by name, never a ``KeyError``.
:func:`source_catalog`
    the profile's source-catalog entries (a bring-up input; read from disk).
:func:`entity_classes`
    the registered ``entity_class`` vocabulary family, from the runtime's
    vocabulary snapshot, with a caller's literal as the documented fallback.

WHERE THE ROSTERS COME FROM. ``LEGBA_PROFILE`` set ⇒ that profile directory is
loaded (:func:`legba.data.profile.load_profile`; a profile that does not load
is an error, never a silent fallback). Unset ⇒ the PACKAGED export
``profile_rosters.json`` beside this file: the runtime and registry images
carry ``src/`` only (no ``profiles/``), so the selected profile's rosters ship
inside the package the same way its workstation labels ship inside the UI
build (``profile_ui.json``). ``scripts/gen_profile_rosters.py`` writes it; the
committed file is the default profile's export and
``tests/data_pkg/test_rosters.py`` fails when it is stale.

Cached per process; :func:`refresh` drops the cache (and can inject a profile —
a test, a bring-up script). Module constants that are built from a roster at
import (``scorecard_banding.DIMENSIONS``, ``absence_api.BOUNDED_UNITS``, …)
hold the roster the process started with; the SQL binds call the helpers at
run time and follow a refresh.

**A leaf.** Imports only ``legba.data.profile`` (itself a leaf) and the stdlib,
so the registry's slim image and the bring-up scripts can read it.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .profile import PROFILE_ENV, MANIFEST_NAME, Profile, ProfileError, load_profile, resolve_profile

__all__ = [
    "EXPORT_SCHEMA",
    "ENTITY_CLASS_FAMILY",
    "NO_TERMS",
    "PACKAGED_EXPORT",
    "RosterSet",
    "UnitTerms",
    "current",
    "entity_classes",
    "export_rosters",
    "load_source_catalog",
    "refresh",
    "roster_set",
    "scored_roster_tags",
    "scored_subject_units",
    "scored_unit_roster",
    "source_catalog",
    "subject_roster_tags",
    "terms_for",
    "unit_query_terms",
    "unit_reference_queries",
    "unit_roster",
    "unit_terms",
    "unit_title_terms",
]

#: The packaged copy of the selected profile's rosters (see the module doc).
PACKAGED_EXPORT = Path(__file__).with_name("profile_rosters.json")
#: The ``schema`` stamp of :data:`PACKAGED_EXPORT`.
EXPORT_SCHEMA = "legba/profile_rosters/2"
#: The registered vocabulary family :func:`entity_classes` reads.
ENTITY_CLASS_FAMILY = "entity_class"


@dataclass(frozen=True)
class UnitTerms:
    """One unit's instrument data (ROSTER-2; ``profile.Unit``). Every field is
    optional: :data:`NO_TERMS` is what a unit that declares none reads as."""

    #: The desk reference's web-search terms, or ``None``.
    query: str | None = None
    #: The title-frame gauge's vocabulary (lower-case substrings).
    title: tuple[str, ...] = ()
    #: The reference builder's query patterns (``{country}`` substituted).
    reference_queries: tuple[str, ...] = ()
    #: The tag the unit's fan-out narrows to, or ``None`` (every scored subject).
    scope_tag: str | None = None


#: A unit with no declared data — the by-name degradation, never a KeyError.
NO_TERMS = UnitTerms()


@dataclass(frozen=True)
class RosterSet:
    """One profile's rosters, as the code reads them."""

    profile: str
    subject_tags: tuple[str, ...]
    scored_subject_tags: tuple[str, ...]
    units: tuple[str, ...]
    scored_units: tuple[str, ...]
    #: Where it was read from: ``profile:<manifest>``, ``packaged:<file>``.
    origin: str
    #: unit id -> its declared data, for the units that declare any (ROSTER-2).
    unit_terms: tuple[tuple[str, UnitTerms], ...] = ()


def roster_set(profile: Profile, *, origin: str | None = None) -> RosterSet:
    """The :class:`RosterSet` of a loaded profile."""
    r = profile.rosters
    try:
        where = str(profile.manifest_path)
    except ProfileError:
        where = "<in-memory>"
    terms = tuple(
        (u.id, t)
        for u in r.units
        if (t := UnitTerms(
            query=u.query_terms,
            title=tuple(u.title_terms),
            reference_queries=tuple(u.reference_queries),
            scope_tag=u.scope_tag,
        )) != NO_TERMS
    )
    return RosterSet(
        profile=profile.id,
        subject_tags=tuple(r.subject_tags),
        scored_subject_tags=tuple(r.scored_subject_tags),
        units=tuple(u.id for u in r.units),
        scored_units=tuple(r.scored_units),
        origin=origin or f"profile:{where}",
        unit_terms=terms,
    )


def _terms_doc(t: UnitTerms) -> dict[str, Any]:
    doc: dict[str, Any] = {}
    if t.query is not None:
        doc["query"] = t.query
    if t.title:
        doc["title"] = list(t.title)
    if t.reference_queries:
        doc["reference_queries"] = list(t.reference_queries)
    if t.scope_tag is not None:
        doc["scope_tag"] = t.scope_tag
    return doc


def export_rosters(profile: Profile) -> str:
    """The text of :data:`PACKAGED_EXPORT` for ``profile``: keys sorted, two-space
    indent, trailing newline — deterministic bytes. Carries no workstation
    label (those ride ``profile_ui.json``)."""
    rs = roster_set(profile)
    doc = {
        "_generated": (
            "scripts/gen_profile_rosters.py from profiles/"
            f"{profile.id}/{MANIFEST_NAME} - do not edit by hand"
        ),
        "schema": EXPORT_SCHEMA,
        "profile": rs.profile,
        "subject_tags": list(rs.subject_tags),
        "scored_subject_tags": list(rs.scored_subject_tags),
        "units": list(rs.units),
        "scored_units": list(rs.scored_units),
        "unit_terms": {u: _terms_doc(t) for u, t in rs.unit_terms},
    }
    return json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def _from_export(path: Path) -> RosterSet:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema") != EXPORT_SCHEMA:
        raise ProfileError(f"{path} is not a {EXPORT_SCHEMA} export")

    def strs(val: Any, key: str) -> tuple[str, ...]:
        if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
            raise ProfileError(f"{path}: {key} is not a list of strings")
        return tuple(val)

    def opt_str(val: Any, key: str) -> str | None:
        if val is not None and not isinstance(val, str):
            raise ProfileError(f"{path}: {key} is not a string")
        return val

    units = strs(doc.get("units"), "units")
    raw_terms = doc.get("unit_terms")
    if not isinstance(raw_terms, dict):
        raise ProfileError(f"{path}: unit_terms is not a mapping")
    stray = sorted(set(raw_terms) - set(units))
    if stray:
        raise ProfileError(f"{path}: unit_terms name units off the roster: {stray}")
    terms: list[tuple[str, UnitTerms]] = []
    for unit in units:
        if unit not in raw_terms:
            continue
        t = raw_terms[unit]
        if not isinstance(t, dict) or set(t) - {"query", "title", "reference_queries", "scope_tag"}:
            raise ProfileError(f"{path}: unit_terms[{unit}] is not a terms mapping")
        terms.append((unit, UnitTerms(
            query=opt_str(t.get("query"), f"unit_terms[{unit}].query"),
            title=strs(t.get("title", []), f"unit_terms[{unit}].title"),
            reference_queries=strs(t.get("reference_queries", []), f"unit_terms[{unit}].reference_queries"),
            scope_tag=opt_str(t.get("scope_tag"), f"unit_terms[{unit}].scope_tag"),
        )))

    return RosterSet(
        profile=str(doc.get("profile") or ""),
        subject_tags=strs(doc.get("subject_tags"), "subject_tags"),
        scored_subject_tags=strs(doc.get("scored_subject_tags"), "scored_subject_tags"),
        units=units,
        scored_units=strs(doc.get("scored_units"), "scored_units"),
        origin=f"packaged:{path}",
        unit_terms=tuple(terms),
    )


def _select(env: Mapping[str, str] | None = None) -> RosterSet:
    env = os.environ if env is None else env
    if env.get(PROFILE_ENV):
        return roster_set(load_profile(resolve_profile(env=env)))
    return _from_export(PACKAGED_EXPORT)


_lock = threading.Lock()
_current: RosterSet | None = None


def current() -> RosterSet:
    """The process's rosters (read once, then cached)."""
    global _current
    rs = _current
    if rs is None:
        with _lock:
            if _current is None:
                _current = _select()
            rs = _current
    return rs


def refresh(profile: Profile | str | os.PathLike[str] | None = None) -> RosterSet:
    """Drop the cache and read again. ``profile`` (a loaded profile or its
    directory) is used instead of the selection rule; ``None`` re-applies it.
    Also drops the source-catalog cache."""
    global _current
    with _lock:
        if profile is None:
            _current = _select()
        elif isinstance(profile, Profile):
            _current = roster_set(profile)
        else:
            _current = roster_set(load_profile(profile))
        _CATALOG_CACHE.clear()
        return _current


def subject_roster_tags() -> tuple[str, ...]:
    """Every tag that puts a target on the subject roster."""
    return current().subject_tags


def scored_roster_tags() -> tuple[str, ...]:
    """The roster tags whose subjects carry the scorecard, baselines and
    baseline-deviation alerts."""
    return current().scored_subject_tags


def unit_roster() -> tuple[str, ...]:
    """The bounded units, in display order."""
    return current().units


def scored_unit_roster() -> tuple[str, ...]:
    """The fixed scorecard dimensions (the units the correctness plane
    measures one by one)."""
    return current().scored_units


def scored_subject_units() -> tuple[str, ...]:
    """The units that fire on the scored subjects, in roster order: every unit
    except one whose ``scope_tag`` is an UNSCORED subject-roster tag (a unit
    scoped to another leg — the exemplar's ``supply_chain`` lane unit). That
    is the scored dimensions plus the units narrowed WITHIN the scored legs
    (a ``scope_tag`` that is not a roster tag). The correctness grader and the
    reference builder grade these; the subject desk renders them."""
    rs = current()
    other_legs = set(rs.subject_tags) - set(rs.scored_subject_tags)
    scope = {u: t.scope_tag for u, t in rs.unit_terms}
    return tuple(u for u in rs.units if scope.get(u) not in other_legs)


def unit_terms() -> dict[str, UnitTerms]:
    """unit id -> its declared data, roster order, for the units that declare
    any. A roster unit that declares none is ABSENT here — read it through
    :func:`terms_for`, which degrades it by name."""
    return dict(current().unit_terms)


def terms_for(unit: str) -> UnitTerms:
    """One unit's data; :data:`NO_TERMS` for a unit that declares none or is
    off the roster (never a ``KeyError``)."""
    return dict(current().unit_terms).get(unit, NO_TERMS)


def unit_query_terms() -> dict[str, str]:
    """unit -> its desk-reference search terms, roster order, declared only."""
    return {u: t.query for u, t in current().unit_terms if t.query}


def unit_title_terms() -> dict[str, tuple[str, ...]]:
    """unit -> its title vocabulary, roster order, declared only."""
    return {u: t.title for u, t in current().unit_terms if t.title}


def unit_reference_queries() -> dict[str, tuple[str, ...]]:
    """unit -> its reference query patterns, roster order, declared only."""
    return {u: t.reference_queries for u, t in current().unit_terms if t.reference_queries}


# ---------------------------------------------------------------------------
# The source catalog (a bring-up input: read from the profile on disk)
# ---------------------------------------------------------------------------

_CATALOG_CACHE: dict[str, tuple[dict[str, Any], ...]] = {}


def load_source_catalog(path: str | os.PathLike[str]) -> tuple[dict[str, Any], ...]:
    """The entries of a source-catalog YAML file, in file order.

    The file is a mapping with one key, ``entries``: a list of mappings, each
    with a string ``id`` (unique). The FIELDS of an entry are the catalog
    loader's business (``scripts/bringup_register_source_catalog.py``
    ``CatalogEntry``, which refuses an unknown one). Raises
    :class:`ProfileError` on any other shape.
    """
    p = Path(path).resolve()
    key = str(p)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached
    if not p.is_file():
        raise ProfileError(f"no source catalog at {p}")
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or set(doc) != {"entries"}:
        raise ProfileError(f"{p}: a source catalog is a mapping with one key, entries")
    raw = doc["entries"] or []
    if not isinstance(raw, list):
        raise ProfileError(f"{p}: entries is not a list")
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for i, row in enumerate(raw):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise ProfileError(f"{p}: entries[{i}] is not a mapping with a string id")
        if row["id"] in seen:
            raise ProfileError(f"{p}: entries repeat id {row['id']}")
        seen.add(row["id"])
        rows.append(dict(row))
    out = tuple(rows)
    _CATALOG_CACHE[key] = out
    return out


def source_catalog(profile: Profile | None = None) -> tuple[dict[str, Any], ...]:
    """The profile's source-catalog entries; ``()`` when it declares none
    (``rosters.source_catalog: null``). ``None`` loads the selected profile
    from disk (``LEGBA_PROFILE``, else the default, relative to the working
    directory)."""
    if profile is None:
        profile = load_profile(resolve_profile())
    path = profile.source_catalog_path
    return () if path is None else load_source_catalog(path)


# ---------------------------------------------------------------------------
# entity_class — the registered family, through the runtime's snapshot
# ---------------------------------------------------------------------------


def entity_classes(index: Any, *, fallback: Iterable[str]) -> frozenset[str]:
    """The registered ``entity_class`` values when ``index`` (a
    ``legba.data.tags.VocabularyIndex`` — the runtime's ONE vocabulary
    snapshot, ``await tags.tag_vocabulary_index()``) has the family loaded;
    otherwise ``fallback`` — the caller's documented literal. No provider
    installed (the registry process, a script, a test) reads as unloaded."""
    if index is not None and index.family_loaded(ENTITY_CLASS_FAMILY):
        values = index.values.get(ENTITY_CLASS_FAMILY) or frozenset()
        if values:
            return frozenset(values)
    return frozenset(fallback)
