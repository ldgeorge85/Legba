# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The instance's GAZETTEER, read from its profile (W10 CANON-1).

Five modules used to carry the exemplar's tables of names and codes as code
literals — ``provenance/absence_slice.py`` (the V-B claim-scope resolver and the
M15 guard vocabulary), ``provenance/world_knowledge_guards.py`` +
``provenance/writes_supersession.py`` (the officeholder groups and offices, the
functional-role country side), ``_polity_match.py`` + ``_geo_routing.py`` (the
polity canon), ``_entity_canon.py`` (the alias / demonym / region maps) and
``runtime/grounding.py`` (the off-target guard and the RAG country name). On an
instance that is not about countries those tables are wrong, and a guard that
reads an empty one passes silently. They are now ONE profile file,
``profiles/<id>/rosters/gazetteer.yaml`` (declared by the manifest's
``rosters.gazetteer`` key, docs/PROFILES.md "The gazetteer"), behind this ONE
loader.

THE CONTRACT.

:func:`gazetteer`
    the process's :class:`Gazetteer` (read once, then cached; :func:`refresh`
    drops it). Each declared SECTION is held in the Python type the literal it
    replaced had — a ``dict`` in its file order, a ``tuple`` where order matters
    (the officeholder groups feed a regex alternation), a ``frozenset`` where it
    does not — so a consumer's behaviour is byte-identical (pinned per table by
    sha256 in ``tests/data_pkg/test_gazetteer.py``).
:meth:`Gazetteer.section`
    a section's value, or :class:`Absent` — a typed, falsy marker carrying the
    section's NAME. A section a profile does not declare is ABSENT, never an
    empty table that silently passes.
:func:`applicable`
    a GUARD's verdict on its own sections (:data:`GUARD_SECTIONS`): ``None`` when
    every one is declared, else a :class:`NotApplicable` naming the guard and
    the missing sections — counted in :func:`not_applicable_census`, logged
    once per process, and bumped by the verify fold onto the report's receipts
    counters (``gazetteer_not_applicable_<guard>``). SEAMS #67.
:func:`or_empty`
    for a consumer that is a canonicaliser rather than a guard (the entity
    canon's maps, the polity canon): the declared value, or the empty value of
    the section's type — with the absent read recorded by name
    (:func:`absent_reads`) and logged once.

WHERE IT COMES FROM. ``LEGBA_PROFILE`` set ⇒ that profile directory (a profile
that does not load is an error; one whose manifest declares no gazetteer reads
with EVERY section absent). Unset ⇒ the PACKAGED export
``profile_gazetteer.json`` beside this file — the images carry ``src/`` only —
written by ``scripts/gen_profile_gazetteer.py``; the committed file is the
default profile's export and ``tests/data_pkg/test_gazetteer.py`` fails when it
is stale. Module constants a consumer builds at import (``_entity_canon``'s
maps, ``_polity_match._SURFACES``) hold the gazetteer the process started with;
the guards read :func:`gazetteer` at call time and follow a refresh.

**A leaf.** Imports only ``legba.data.profile`` (itself a leaf), pydantic, yaml
and the stdlib, so the registry's slim image, verify and the bring-up scripts
can all read it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping, NamedTuple

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .profile import (
    MANIFEST_NAME,
    PROFILE_ENV,
    Profile,
    ProfileError,
    load_profile,
    resolve_profile,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ABSENT_REASON",
    "EXPORT_SCHEMA",
    "FILE_SCHEMA",
    "GUARD_SECTIONS",
    "PACKAGED_EXPORT",
    "SEAM",
    "SECTIONS",
    "Absent",
    "Gazetteer",
    "GazetteerFile",
    "NotApplicable",
    "PolityOffices",
    "PolityUniverse",
    "SubjectHomonymsFile",
    "absent_reads",
    "applicable",
    "export_gazetteer",
    "gazetteer",
    "load_gazetteer_file",
    "not_applicable_census",
    "or_empty",
    "refresh",
    "reset_census",
]

#: The packaged copy of the selected profile's gazetteer (see the module doc).
PACKAGED_EXPORT = Path(__file__).with_name("profile_gazetteer.json")
#: The ``schema`` stamp of :data:`PACKAGED_EXPORT`.
EXPORT_SCHEMA = "legba/profile_gazetteer/1"
#: The ``schema`` stamp of a profile's ``gazetteer.yaml``.
FILE_SCHEMA = "legba/gazetteer/1"
#: The docs/SEAMS.md entry every not-applicable guard names.
SEAM = 67
#: The reason a :class:`NotApplicable` carries.
ABSENT_REASON = "gazetteer_section_absent"


# ---------------------------------------------------------------------------
# The file model (closed: an unknown section is refused)
# ---------------------------------------------------------------------------

_Name = Annotated[str, Field(min_length=1, max_length=200)]
_Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
_Names = tuple[_Name, ...]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class DeskIds(_Closed):
    """The id convention that carries a subject's code: a target id containing
    ``marker`` is a per-subject desk whose trailing ``_<token>`` is its code."""

    marker: _Slug


class PolityOfficesFile(_Closed):
    role_predicates: dict[_Name, _Names]
    family_predicates: _Names
    leader_of: _Name


class PolitiesFile(_Closed):
    #: ``pycountry`` = derive the universe from the ISO-3166-1 records; null =
    #: the curated names alone.
    universe: Literal["pycountry"] | None = None
    curated: _Names = ()


class SubjectHomonymsFile(_Closed):
    """One subject's HOMONYM forms (B6, W11-F13): surface forms that LOOK like
    the subject (its code, or a name containing its name) but name something
    else — ``KP`` on the North Korea desk is Pakistan's Khyber Pakhtunkhwa in
    the Pakistani press. ``exclude`` are those forms; ``reads_as`` says what
    they actually name (for the audit); ``subject`` adds forms that name the
    subject beyond the gazetteer's own names for it (a capital, a leader).
    A form with no lowercase letter (``KP``, ``S.A.``) matches CASE-SENSITIVELY,
    every other form case-insensitively; both on ASCII-letter boundaries."""

    reads_as: _Names = Field(min_length=1)
    exclude: _Names = Field(min_length=1)
    subject: _Names = ()


class GazetteerFile(_Closed):
    """One profile's ``gazetteer.yaml``. Every section is optional; a section
    left out is ABSENT (``None`` here, :class:`Absent` through the loader)."""

    schema_: Literal["legba/gazetteer/1"] = Field(alias="schema")
    desk_ids: DeskIds | None = None
    # absence_slice
    desk_countries: dict[_Name, _Names] | None = None
    slice_geo_countries: _Names | None = None
    slug_to_country: dict[_Name, _Names] | None = None
    country_tokens: _Names | None = None
    slug_to_abbrev: dict[_Name, _Names] | None = None
    irregular_demonyms: dict[_Name, _Names] | None = None
    screen_stopwords: _Names | None = None
    classifier_patterns: dict[_Slug, _Name] | None = None
    # world_knowledge_guards
    officeholder_country_groups: tuple[_Names, ...] | None = None
    officeholder_acronyms: dict[_Name, _Name] | None = None
    polity_offices: PolityOfficesFile | None = None
    # writes_supersession
    functional_role_country_side: dict[_Name, Literal["subject", "value"]] | None = None
    # _polity_match / _geo_routing
    iso2_home_aliases: dict[_Name, _Names] | None = None
    polities: PolitiesFile | None = None
    # _entity_canon
    alias_map: dict[_Name, _Name] | None = None
    alias_org: _Names | None = None
    demonym_map: dict[_Name, _Name] | None = None
    region_adjective_map: dict[_Name, _Name] | None = None
    known_regions: _Names | None = None
    # grounding
    target_slug_names: dict[_Name, _Names] | None = None
    known_country_tokens: _Names | None = None
    # subject_homonyms (B6) — the slice's homonym exclusion, the critic's
    # off_subject check and the correctness grader's off_subject exclusion
    subject_homonyms: dict[_Slug, SubjectHomonymsFile] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "GazetteerFile":
        for name, pattern in (self.classifier_patterns or {}).items():
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"classifier_patterns.{name} does not compile: {exc}") from None
        if self.officeholder_acronyms is not None:
            if self.officeholder_country_groups is None:
                raise ValueError("officeholder_acronyms needs officeholder_country_groups")
            heads = [g[0] for g in self.officeholder_country_groups if g]
            stray = sorted(v for v in self.officeholder_acronyms.values() if v not in heads)
            if stray:
                raise ValueError(
                    f"officeholder_acronyms name no group's first spelling: {stray}"
                )
        if any(not g for g in self.officeholder_country_groups or ()):
            raise ValueError("officeholder_country_groups holds an empty group")
        return self


#: Every section a gazetteer file may declare, in file order.
SECTIONS: tuple[str, ...] = tuple(
    name for name in GazetteerFile.model_fields if name != "schema_"
)

#: Each GUARD and the sections it cannot run without. A guard whose sections a
#: profile does not all declare answers :class:`NotApplicable` (SEAMS #67).
GUARD_SECTIONS: dict[str, tuple[str, ...]] = {
    # provenance/world_knowledge_guards.cross_target_leak_span (M15)
    "cross_target_leak": ("desk_ids", "slug_to_country", "country_tokens"),
    # provenance/world_knowledge_guards.extract_officeholder_claims (E-1)
    "officeholder_vs_facts": ("officeholder_country_groups", "polity_offices"),
    # provenance/absence_slice._slice_scope_countries / _row_in_claim_scope (V-B W1(a))
    "absence_slice_scope": ("desk_countries", "slice_geo_countries"),
    # provenance/absence_slice._is_machine_structured_row's title-shape leg (W1(c))
    "machine_structured_title": ("classifier_patterns",),
    # provenance/writes_supersession.supersede_prior_functional_role_facts (FU3)
    "functional_role_supersession": ("functional_role_country_side",),
    # runtime/grounding.finding_is_off_target (D4 publish guard)
    "off_target": ("desk_ids", "target_slug_names", "known_country_tokens"),
    # runtime/actor_critic's subject-relevance check (B6, W11-F13)
    "off_subject": ("desk_ids", "subject_homonyms"),
}


# ---------------------------------------------------------------------------
# The runtime view
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Absent:
    """A section the profile does not declare. Falsy; carries its NAME."""

    section: str

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"<absent gazetteer section {self.section!r}>"


@dataclass(frozen=True)
class NotApplicable:
    """A guard that cannot run on this profile: it does not pass, it declares
    itself not applicable, by name (SEAMS #67)."""

    guard: str
    missing: tuple[str, ...]
    reason: str = ABSENT_REASON
    seam: int = SEAM

    def __bool__(self) -> bool:
        # Falsy, so a caller that reads a guard's answer as "flagged or not"
        # never mistakes not-applicable for a hit; a caller that must surface
        # it checks ``isinstance(x, NotApplicable)``.
        return False

    @property
    def counter(self) -> str:
        """The verify report's receipts counter for this verdict."""
        return f"gazetteer_not_applicable_{self.guard}"


class PolityOffices(NamedTuple):
    role_predicates: dict[str, tuple[str, ...]]
    family_predicates: tuple[str, ...]
    leader_of: str


class PolityUniverse(NamedTuple):
    #: ``"pycountry"`` or ``None`` (curated names only).
    universe: str | None
    curated: frozenset[str]


#: The sections held as frozensets (their literal was a set: order is noise).
_SET_SECTIONS = frozenset({
    "slice_geo_countries", "country_tokens", "screen_stopwords", "alias_org",
    "known_regions", "known_country_tokens",
})


@dataclass(frozen=True, eq=False)
class Gazetteer:
    """One profile's gazetteer as the code reads it. Hashable by identity, so a
    consumer can cache what it derives from one (a compiled regex, a union)."""

    profile: str
    #: Where it was read from: ``profile:<manifest>``, ``packaged:<file>``.
    origin: str
    _sections: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def declared(self) -> tuple[str, ...]:
        """The declared section names, in :data:`SECTIONS` order."""
        return tuple(s for s in SECTIONS if s in self._sections)

    def has(self, name: str) -> bool:
        _known(name)
        return name in self._sections

    def section(self, name: str) -> Any:
        """The section's value, or :class:`Absent` naming it."""
        _known(name)
        return self._sections.get(name, Absent(name))

    def get(self, name: str) -> Any:
        """The section's value, or ``None`` when absent (the guards' fast path:
        they pair it with :func:`applicable`, which records the absence)."""
        _known(name)
        return self._sections.get(name)


def _known(name: str) -> None:
    if name not in SECTIONS:
        raise KeyError(f"no gazetteer section {name!r} (sections: {', '.join(SECTIONS)})")


def _runtime_value(name: str, value: Any) -> Any:
    """The file value -> the Python type the replaced literal had."""
    if name in _SET_SECTIONS:
        return frozenset(value)
    if name == "desk_ids":
        return value.marker
    if name == "polity_offices":
        return PolityOffices(
            dict(value.role_predicates), tuple(value.family_predicates), value.leader_of,
        )
    if name == "polities":
        return PolityUniverse(value.universe, frozenset(value.curated))
    if isinstance(value, dict):
        return dict(value)
    return value


def build(model: GazetteerFile | None, *, profile: str, origin: str) -> Gazetteer:
    """The :class:`Gazetteer` of a validated file model (``None`` = a profile
    that declares no gazetteer: every section absent)."""
    sections: dict[str, Any] = {}
    if model is not None:
        for name in SECTIONS:
            value = getattr(model, name)
            if value is not None:
                sections[name] = _runtime_value(name, value)
        if "slice_geo_countries" in sections:
            # The pin's _SLICE_GEO_COUNTRIES: the desk names UNION the extras.
            desk_names = {
                n for names in (sections.get("desk_countries") or {}).values() for n in names
            }
            sections["slice_geo_countries"] = frozenset(desk_names) | sections["slice_geo_countries"]
        if "officeholder_acronyms" in sections:
            # Resolve each acronym to its GROUP (the very tuple object, as the
            # pin's ``_OFFICEHOLDER_ACRONYMS`` held ``GROUPS[i]``).
            by_head = {g[0]: g for g in sections["officeholder_country_groups"]}
            sections["officeholder_acronyms"] = {
                k: by_head[v] for k, v in sections["officeholder_acronyms"].items()
            }
    return Gazetteer(profile=profile, origin=origin, _sections=sections)


def load_gazetteer_file(path: str | os.PathLike[str]) -> GazetteerFile:
    """Load and validate one ``gazetteer.yaml``."""
    p = Path(path)
    if not p.is_file():
        raise ProfileError(f"no gazetteer at {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ProfileError(f"{p} is not a mapping")
    return GazetteerFile.model_validate(data)


def gazetteer_of(profile: Profile) -> Gazetteer:
    """The :class:`Gazetteer` a loaded profile declares."""
    try:
        where = str(profile.manifest_path)
    except ProfileError:
        where = "<in-memory>"
    path = profile.gazetteer_path
    model = None if path is None else load_gazetteer_file(path)
    origin = f"profile:{where}" + ("" if path is not None else " (no gazetteer)")
    return build(model, profile=profile.id, origin=origin)


# ---------------------------------------------------------------------------
# The packaged export
# ---------------------------------------------------------------------------


def export_gazetteer(profile: Profile) -> str:
    """The text of :data:`PACKAGED_EXPORT` for ``profile``: the validated file,
    absent sections LEFT OUT, in FILE order (not key-sorted: order is data
    here), two-space indent, trailing newline — deterministic bytes."""
    path = profile.gazetteer_path
    model = None if path is None else load_gazetteer_file(path)
    body = None if model is None else model.model_dump(
        mode="json", by_alias=True, exclude_none=True,
    )
    doc = {
        "_generated": (
            "scripts/gen_profile_gazetteer.py from profiles/"
            f"{profile.id}/{MANIFEST_NAME} - do not edit by hand"
        ),
        "schema": EXPORT_SCHEMA,
        "profile": profile.id,
        "gazetteer": body,
    }
    return json.dumps(doc, indent=2, ensure_ascii=True) + "\n"


def _from_export(path: Path) -> Gazetteer:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ProfileError(f"no packaged gazetteer at {path}") from None
    if not isinstance(doc, dict) or doc.get("schema") != EXPORT_SCHEMA:
        raise ProfileError(f"{path} is not a {EXPORT_SCHEMA} export")
    body = doc.get("gazetteer")
    model = None if body is None else GazetteerFile.model_validate(body)
    return build(model, profile=str(doc.get("profile") or ""), origin=f"packaged:{path}")


def _select(env: Mapping[str, str] | None = None) -> Gazetteer:
    env = os.environ if env is None else env
    if env.get(PROFILE_ENV):
        return gazetteer_of(load_profile(resolve_profile(env=env)))
    return _from_export(PACKAGED_EXPORT)


_lock = threading.Lock()
_current: Gazetteer | None = None


def gazetteer() -> Gazetteer:
    """The process's gazetteer (read once, then cached)."""
    global _current
    gz = _current
    if gz is None:
        with _lock:
            if _current is None:
                _current = _select()
            gz = _current
    return gz


def refresh(profile: Profile | Gazetteer | str | os.PathLike[str] | None = None) -> Gazetteer:
    """Drop the cache and read again. ``profile`` (a loaded profile, its
    directory, or a built :class:`Gazetteer`) is used instead of the selection
    rule; ``None`` re-applies it."""
    global _current
    with _lock:
        if profile is None:
            _current = _select()
        elif isinstance(profile, Gazetteer):
            _current = profile
        elif isinstance(profile, Profile):
            _current = gazetteer_of(profile)
        else:
            _current = gazetteer_of(load_profile(profile))
        return _current


# ---------------------------------------------------------------------------
# Not applicable, by name — the census
# ---------------------------------------------------------------------------

_census_lock = threading.Lock()
_NA_COUNTS: Counter[str] = Counter()
_ABSENT_READS: Counter[tuple[str, str]] = Counter()
_LOGGED: set[tuple[str, ...]] = set()


def applicable(
    guard: str, gz: Gazetteer | None = None, *, record: bool = True,
) -> NotApplicable | None:
    """``None`` when ``guard`` can run (every section it needs is declared);
    otherwise a :class:`NotApplicable` naming the missing sections — counted
    and logged once per process. ``record=False`` answers without counting (a
    fold that only LABELS the report for a guard that counts itself). Never
    raises for a known guard."""
    needs = GUARD_SECTIONS[guard]
    gz = gz if gz is not None else gazetteer()
    missing = tuple(s for s in needs if not gz.has(s))
    if not missing:
        return None
    verdict = NotApplicable(guard=guard, missing=missing)
    if not record:
        return verdict
    with _census_lock:
        _NA_COUNTS[guard] += 1
        first = ("na", guard, gz.profile) not in _LOGGED
        _LOGGED.add(("na", guard, gz.profile))
    if first:
        logger.warning(
            "gazetteer.not_applicable guard=%s missing=%s profile=%s origin=%s "
            "— the guard declares itself not applicable (SEAMS #%d), it does not pass",
            guard, ",".join(missing), gz.profile or "-", gz.origin, SEAM,
        )
    return verdict


_EMPTY: dict[str, Any] = {
    "desk_ids": None,
    "polity_offices": None,
    "polities": PolityUniverse(None, frozenset()),
}


def or_empty(name: str, *, consumer: str, gz: Gazetteer | None = None) -> Any:
    """The declared section, or the EMPTY value of its type — for a consumer
    that canonicalises rather than guards (an absent alias map means no alias
    collapses, not a passed check). The absent read is recorded by name and
    logged once per process."""
    gz = gz if gz is not None else gazetteer()
    if gz.has(name):
        return gz.get(name)
    with _census_lock:
        _ABSENT_READS[(consumer, name)] += 1
        first = ("absent", consumer, name, gz.profile) not in _LOGGED
        _LOGGED.add(("absent", consumer, name, gz.profile))
    if first:
        logger.info(
            "gazetteer.absent section=%s consumer=%s profile=%s — read as empty",
            name, consumer, gz.profile or "-",
        )
    if name in _EMPTY:
        return _EMPTY[name]
    if name in _SET_SECTIONS:
        return frozenset()
    if name in ("officeholder_country_groups",):
        return ()
    return {}


def not_applicable_census() -> dict[str, int]:
    """guard -> how many times it answered not applicable in this process."""
    with _census_lock:
        return dict(_NA_COUNTS)


def absent_reads() -> dict[str, int]:
    """``<consumer>:<section>`` -> absent reads in this process."""
    with _census_lock:
        return {f"{c}:{s}": n for (c, s), n in _ABSENT_READS.items()}


def reset_census() -> None:
    """Clear the counts and the log-once memory (tests)."""
    with _census_lock:
        _NA_COUNTS.clear()
        _ABSENT_READS.clear()
        _LOGGED.clear()
