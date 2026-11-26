# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The instance profile: the one manifest a bring-up reads (PROFILE-1).

A Legba instance is the platform plus one *profile*: a directory under
``profiles/<id>/`` whose ``profile.yaml`` names everything instance-specific
the bring-up needs. The reference is ``profiles/geopolitics/``, whose paths point at
today's tree (``descriptors/``) and whose ``prompts/`` holds its own prose (the
six political-lean lenses, moved there by W9 SPLIT-2).

The manifest carries ten keys, and the model is CLOSED (an unknown key is
refused, at every level):

``id``
    The profile's slug; must equal its directory name.
``descriptors_dir``
    The directory the descriptor walker
    (``scripts/bringup_register_descriptors.py``) registers from. Its
    ``RETIRED.md`` (when present) is the walker's exclusion list.
``lifecycle``
    The live-state overlay (``LIFECYCLE.md``): the lifecycle state each
    descriptor holds on this instance when it is not the state its file ships.
    ``null`` = every descriptor registers at its shipped state.
``seeds``
    The knowledge-seed adapters ``deploy/deploy.sh --seed`` runs, in order
    (``LEGBA_SEED_SOURCES`` still overrides them).
``rosters``
    The instance's rosters as data: the subject-roster tag arrays, the
    bounded-unit roster (and its scored subset), the bring-up's source
    catalog file + credibility seed modules, and the gazetteer file. Each
    unit entry carries its own per-unit data inline (label, scope, search /
    title / reference terms — ROSTER-2, :class:`Unit`). The code reads them
    through ``legba.data.rosters`` (ROSTER-1), never through a literal; the
    gazetteer (the tables of names and codes the guards read) through
    ``legba.data.gazetteer`` (W10 CANON-1).
``prompts``
    The directory of the profile's own prompt prose, or ``null`` when it has
    none. It holds ONE top-level Python package named ``legba_<id>``
    (:func:`prompts_package_name`) — a name of its own beside ``legba``, so a
    profile's prose never shadows a platform module; a descriptor names a
    module in it as ``legba_<id>.<module>:ATTR``. The images carry it (the
    registry/runtime builds copy it into site-packages); a checkout puts the
    directory on ``PYTHONPATH``.
``top_floor``
    The producer id of the instance's top read, or ``none``.
``ui``
    The labels the workstation shows (UI-D1 §2.3), exported at build time to
    ``legba-ui-v3/src/lib/profile_ui.json`` by ``scripts/gen_profile_ui.py``.
``projection``
    The profile's read-time view (VIEW-1): ``view`` (the view file,
    ``projection/view.yaml``) and ``plane`` (the plane id its rows carry in
    ``profile_projection``), or absent when the profile declares none — every
    reader of that plane then refuses ``projection_disabled``. The view itself
    is loaded and resolved by ``legba.data.profile_projection``.
``constants``
    The instance constants the platform's mechanisms key on (SPLIT-3,
    :class:`Constants`): the external-audit drain's tier order, the
    external-truth stratum per producer, the subject frame, the frame-anchor
    and auto-merge entity classes, the translation languages, the adjacency
    edge families, the applicability tags and the reference subject tags
    (INST-1: the non-scored legs the correctness roster also covers). Read through
    ``legba.data.profile_constants`` (never a literal); a key left out is
    ``none`` / empty, and every reader then refuses by name.

Paths are relative to the profile directory. Selection is one argument:
``--profile <dir>`` beats the ``LEGBA_PROFILE`` environment variable, which
beats the default ``profiles/geopolitics`` (:func:`resolve_profile`).

**A leaf.** Nothing from ``legba`` is imported, so the registry's slim image,
the bring-up scripts and the UI exporter can all load a profile without the
analyst package.

CLI (used by ``deploy/deploy.sh``)::

    python -m legba.data.profile [--profile DIR] check    # validate + summary
    python -m legba.data.profile [--profile DIR] seeds    # the seed adapter list
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

__all__ = [
    "DEFAULT_PROFILE",
    "MANIFEST_NAME",
    "PROFILE_ENV",
    "UI_EXPORT_SCHEMA",
    "UI_UNIT_KEYS",
    "Constants",
    "DeskTier",
    "Profile",
    "Projection",
    "ProfileError",
    "export_ui",
    "load_profile",
    "prompts_package_name",
    "resolve_profile",
]

#: The environment variable that selects a profile when no ``--profile`` is given.
PROFILE_ENV = "LEGBA_PROFILE"
#: The profile a bring-up uses when nothing selects one (today's command line).
DEFAULT_PROFILE = "profiles/geopolitics"
#: The manifest file inside a profile directory.
MANIFEST_NAME = "profile.yaml"
#: The ``schema`` stamp of the exported ``profile_ui.json``.
UI_EXPORT_SCHEMA = "legba/profile_ui/1"
#: The spelling that declares "no top floor" in a manifest.
_NONE = "none"

_Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
_Text = Annotated[str, Field(min_length=1, max_length=400)]
_RelPath = Annotated[str, Field(min_length=1, max_length=512)]


def prompts_package_name(profile_id: str) -> str:
    """The one top-level package a profile's ``prompts`` directory holds."""
    return f"legba_{profile_id}"


class ProfileError(ValueError):
    """A manifest that cannot be loaded as declared (missing file, a path that
    names nothing, an id that is not its directory's name)."""


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _unique(ids: list[str], what: str) -> None:
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise ValueError(f"{what} ids repeat: {dup}")


# ---------------------------------------------------------------------------
# ui — the workstation's labels (UI-D1 §2.3)
# ---------------------------------------------------------------------------


class Noun(_Closed):
    """A display noun, as it renders in a title (``Desk`` / ``Desks``)."""

    singular: _Text
    plural: _Text


class SubjectKind(Noun):
    """One subject family, for an instance with more than one."""

    id: _Slug


class Subject(Noun):
    """What a target is called on this instance. ``kinds`` names each family
    when there is more than one (regimes and counterparties)."""

    kinds: tuple[SubjectKind, ...] = ()

    @model_validator(mode="after")
    def _kinds_unique(self) -> "Subject":
        _unique([k.id for k in self.kinds], "ui.subject.kinds")
        return self


class Frame(_Closed):
    """A frame kind: a record over several subjects. ``parent`` is the frame it
    rolls up into; ``producer`` the analyst id that writes its read."""

    id: _Slug
    label: _Text
    parent: _Slug | None = None
    producer: _Slug | None = None


class Stance(_Closed):
    """A workstation stance's label (the id is the shell's slot key)."""

    id: _Slug
    label: _Text
    question: _Text | None = None


class Mission(_Closed):
    """A mission's label (the id is the shell's ``#mission=`` key)."""

    id: _Slug
    label: _Text


class MapSpec(_Closed):
    """``geometry``: ``iso2`` (a country choropleth), ``declared`` (each subject
    declares its own), ``none`` (no map surface)."""

    geometry: Literal["iso2", "declared", "none"]
    title: _Text | None = None


class Labels(_Closed):
    """The remaining instance-worded strings the workstation renders."""

    read_title: _Text | None = None
    overview_title: _Text | None = None
    unscoped_subject: _Text | None = None
    #: roster tag -> its display label
    roster: dict[_Slug, _Text] = Field(default_factory=dict)


class Ui(_Closed):
    """Stances and missions are OVERRIDES keyed by the shell's ids: one the
    profile does not list keeps the shell's own label."""

    subject: Subject
    unit_noun: Noun | None = None
    frames: tuple[Frame, ...] = ()
    landing: _Slug | None = None
    stances: tuple[Stance, ...] = ()
    missions: tuple[Mission, ...] = ()
    map: MapSpec
    labels: Labels = Field(default_factory=Labels)

    @model_validator(mode="after")
    def _consistent(self) -> "Ui":
        frame_ids = [f.id for f in self.frames]
        _unique(frame_ids, "ui.frames")
        _unique([s.id for s in self.stances], "ui.stances")
        _unique([m.id for m in self.missions], "ui.missions")
        parents = {f.id: f.parent for f in self.frames}
        for f in self.frames:
            if f.parent is not None and f.parent not in parents:
                raise ValueError(f"ui.frames[{f.id}].parent names no frame: {f.parent}")
            seen, cur = {f.id}, f.parent
            while cur is not None:
                if cur in seen:
                    raise ValueError(f"ui.frames parent links cycle through {cur}")
                seen.add(cur)
                cur = parents[cur]
        return self


# ---------------------------------------------------------------------------
# rosters
# ---------------------------------------------------------------------------


_Term = Annotated[str, Field(min_length=1, max_length=64)]


class Unit(_Closed):
    """One bounded unit. ``question`` carries the question text only for a unit
    whose descriptor is not written yet (the descriptor's own
    ``method.bounded_question`` is the text once it is).

    The rest is the unit's per-unit data (ROSTER-2), every key optional — a
    unit that declares none degrades BY NAME wherever the data is read (the
    instrument skips and reports it, the workstation de-underscores its id),
    never with a ``KeyError``:

    ``label`` / ``short``
        The workstation's display label and its 1-4 character chip glyph.
    ``scope_tag``
        The one tag the unit's fan-out predicate narrows to (its descriptor's
        ``has_tag("<tag>")``); ``null`` = it fires on every scored subject. A
        scope tag that is itself an UNSCORED subject-roster tag puts the unit
        on that leg only (``legba.data.rosters.scored_subject_units``).
    ``scope_note``
        Why a scoped unit has no read on most subjects (its empty state).
    ``query_terms``
        The desk reference's web-search terms (``desk_reference``).
    ``title_terms``
        The vocabulary a title uses when it is ABOUT this unit, lower-case,
        matched as substrings (the title-frame gauge).
    ``reference_queries``
        The reference builder's query patterns; ``{subject}`` is the one
        substitution (``{country}`` is its compatibility spelling, filled from
        the same value — W10 SUBJECT-1).
    ``structural``
        The unit's status is a STANDING condition (X4 LEVEL, W11-F36), so its
        descriptor measures it with level-shaped indicators
        (``method.options.level_indicators``) — the carried-state rule's
        re-evidence for a status no change-shaped signpost renews. A test pins
        the flag to the descriptor; the carried-state census reads it.
    """

    id: _Slug
    question: _Text | None = None
    label: _Text | None = None
    short: Annotated[str, Field(min_length=1, max_length=4)] | None = None
    scope_tag: _Slug | None = None
    scope_note: _Text | None = None
    query_terms: _Text | None = None
    title_terms: tuple[_Term, ...] = ()
    reference_queries: tuple[_Text, ...] = ()
    structural: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> "Unit":
        where = f"rosters.units[{self.id}]"
        if self.scope_note is not None and self.scope_tag is None:
            raise ValueError(f"{where}.scope_note without a scope_tag")
        if self.query_terms is not None and self.query_terms != self.query_terms.strip():
            raise ValueError(f"{where}.query_terms carries surrounding whitespace")
        upper = [t for t in self.title_terms if t != t.lower()]
        if upper:
            raise ValueError(f"{where}.title_terms are matched lower-case: {upper}")
        _unique(list(self.title_terms), f"{where}.title_terms")
        for q in self.reference_queries:
            try:
                q.format(subject="", country="")
            except (KeyError, IndexError, ValueError):
                raise ValueError(
                    f"{where}.reference_queries: {q!r} substitutes something other than "
                    "{subject} (or its compatibility spelling {country})"
                ) from None
            if "{subject}" not in q and "{country}" not in q:
                raise ValueError(f"{where}.reference_queries: {q!r} names no {{subject}}")
        return self


class Rosters(_Closed):
    #: Every tag that puts a target on the subject roster (the desk-roster tag
    #: array; read by the absence and coverage denominators).
    subject_tags: tuple[_Slug, ...]
    #: The subset carrying the per-subject scorecard, baselines and
    #: baseline-deviation alerts.
    scored_subject_tags: tuple[_Slug, ...] = ()
    #: The bounded-unit roster, in display order.
    units: tuple[Unit, ...] = ()
    #: The subset of ``units`` that are fixed scorecard dimensions and that
    #: the correctness plane measures one by one (the units every scored
    #: subject fires), in their own order.
    scored_units: tuple[_Slug, ...] = ()
    #: The bring-up's source catalog: a YAML file of feed entries
    #: (``rosters/source_catalog.yaml``), or null.
    source_catalog: _RelPath | None = None
    #: The bring-up modules whose ``seed_credibility`` the walker runs.
    credibility_seeds: tuple[_Slug, ...] = ()
    #: The profile's gazetteer: a YAML file of name/code tables
    #: (``rosters/gazetteer.yaml``, read by ``legba.data.gazetteer`` — W10
    #: CANON-1), or null — every guard that needs one then answers
    #: not_applicable by name (SEAMS #67).
    gazetteer: _RelPath | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "Rosters":
        _unique(list(self.subject_tags), "rosters.subject_tags")
        _unique([u.id for u in self.units], "rosters.units")
        _unique(list(self.credibility_seeds), "rosters.credibility_seeds")
        _unique(list(self.scored_units), "rosters.scored_units")
        extra = sorted(set(self.scored_subject_tags) - set(self.subject_tags))
        if extra:
            raise ValueError(f"rosters.scored_subject_tags not in subject_tags: {extra}")
        stray = sorted(set(self.scored_units) - {u.id for u in self.units})
        if stray:
            raise ValueError(f"rosters.scored_units not in units: {stray}")
        scoped = sorted(u.id for u in self.units if u.id in self.scored_units and u.scope_tag)
        if scoped:
            raise ValueError(f"rosters.scored_units fire on every scored subject; scoped: {scoped}")
        return self


# ---------------------------------------------------------------------------
# subjects (W10 SUBJECT-1)
# ---------------------------------------------------------------------------

#: A separator glyph: no whitespace, and none of the characters the label's SQL
#: rendering (``unit_names.label_name_sql``) would have to escape.
_Glyph = Annotated[str, Field(min_length=1, max_length=3, pattern=r"^[^\s%_'\\()]+$")]
_LabelPart = Literal["tier", "name"]


class LabelStyle(_Closed):
    """How a subject's display name joins its tier and its name
    (``Watch — Pakistan``), read by ``unit_names.unit_label`` — the one
    producer and the one parser of the label, in Python and in SQL.

    ``separator`` joins the two parts with one space either side;
    ``separator_aliases`` are the other glyphs a parser accepts in its place
    (a label typed by hand); ``order`` is which part comes first."""

    separator: _Glyph = "\u2014"
    separator_aliases: tuple[_Glyph, ...] = ("\u2013", "-")
    order: tuple[_LabelPart, _LabelPart] = ("tier", "name")

    @model_validator(mode="after")
    def _consistent(self) -> "LabelStyle":
        if sorted(self.order) != ["name", "tier"]:
            raise ValueError(f"subjects.label.order must name tier and name once each: {self.order}")
        if self.separator in self.separator_aliases:
            raise ValueError("subjects.label.separator_aliases repeats the separator")
        _unique(list(self.separator_aliases), "subjects.label.separator_aliases")
        return self


class Subjects(_Closed):
    """The subject ladder's profile data (``legba.data.subject``): today the
    label style. Undeclared = the platform default style."""

    label: LabelStyle = LabelStyle()


# ---------------------------------------------------------------------------
# projection (VIEW-1)
# ---------------------------------------------------------------------------


class Projection(_Closed):
    """The profile's read-time view: the view file and the plane id its
    materialized rows carry (``legba.data.profile_projection``)."""

    view: _RelPath
    plane: _Slug


# ---------------------------------------------------------------------------
# constants (SPLIT-3)
# ---------------------------------------------------------------------------


_Lang = Annotated[str, Field(pattern=r"^[a-z]{2,3}$")]
_Code = Annotated[str, Field(pattern=r"^[A-Za-z0-9_]{1,64}$")]


class DeskTier(_Closed):
    """One producer's external-truth stratum (``external_truth``)."""

    analyst: _Slug
    tier: _Slug


class Constants(_Closed):
    """The constants the platform's mechanisms key on, as instance data.

    Every key is optional: one left out is ``none`` / empty, and the reader
    that needs it refuses BY NAME (no anchor, no auto-merge band, no drain
    rank, no adjacency spillover), never with another instance's value.
    ``legba.data.profile_constants`` is the one reader.

    ``audit_tier_order``
        The external-audit drain's tier order, highest first: ``ui.frames``
        ids whose ``producer`` is ranked (rank = count - position). A
        producer not listed ranks 0 and drains last.
    ``desk_tiers``
        The external-truth stratum each producer's grades fall in, in order
        (the record tiers, and ``voice`` for the authoring channel); any
        other producer is ``other``.
    ``subject_frame``
        The ``ui.frames`` id of the per-subject tier: its producer is the
        subject read (the scorecard reconciles against it), and a topic that
        carries the frame id as its id prefix names a subject's own target.
    ``anchor_class``
        The entity class a frame anchor must canonicalize to (the frame
        anchor's class gate, the coverage floor's fold), or ``none``.
    ``auto_merge_class``
        The entity class whose curated alias pairs merge without the
        adjudicator, or ``none`` (every alias pair stays gray).
    ``translation_languages``
        The source languages translated before NER (ISO 639 codes).
    ``adjacency``
        Edge families: family id -> undirected ``[a, b]`` pairs of subject
        codes (the desk baselines read ``land_border``).
    ``applicability_tags``
        The closed tag vocabulary a subject's own target file carries to
        narrow unit fan-out (the bring-up tagger merges it onto live heads).
    ``reference_subject_tags``
        The subject-roster tags whose subjects join the CORRECTNESS roster
        beside the scored ones (INST-1): the reference builder builds them a
        reference, the grader grades them, over the units scoped to the tag.
        Each must be a ``rosters.subject_tags`` entry.
    ``levels``
        The level words a desk's typed read may give its subject, lowest
        first (the desk-rows seat's ``severity``). ``none`` is reserved for
        "nothing in this window bears on the question" and is not a level.
        Left out ⇒ the seat that needs them refuses by name.
    """

    audit_tier_order: tuple[_Slug, ...] = ()
    desk_tiers: tuple[DeskTier, ...] = ()
    subject_frame: _Slug | None = None
    anchor_class: _Slug | None = None
    auto_merge_class: _Slug | None = None
    translation_languages: tuple[_Lang, ...] = ()
    adjacency: dict[_Slug, tuple[tuple[_Code, _Code], ...]] = Field(default_factory=dict)
    applicability_tags: tuple[_Slug, ...] = ()
    reference_subject_tags: tuple[_Slug, ...] = ()
    levels: tuple[_Slug, ...] = ()

    @field_validator("subject_frame", "anchor_class", "auto_merge_class", mode="before")
    @classmethod
    def _none_is_none(cls, v: Any) -> Any:
        return None if v == _NONE else v

    @model_validator(mode="after")
    def _consistent(self) -> "Constants":
        _unique(list(self.audit_tier_order), "constants.audit_tier_order")
        _unique([t.analyst for t in self.desk_tiers], "constants.desk_tiers")
        _unique(list(self.translation_languages), "constants.translation_languages")
        _unique(list(self.applicability_tags), "constants.applicability_tags")
        _unique(list(self.reference_subject_tags), "constants.reference_subject_tags")
        _unique(list(self.levels), "constants.levels")
        if _NONE in self.levels:
            raise ValueError("constants.levels: 'none' is reserved and is not a level")
        for family, edges in self.adjacency.items():
            loops = sorted({a for a, b in edges if a.upper() == b.upper()})
            if loops:
                raise ValueError(f"constants.adjacency[{family}] has self-edges: {loops}")
            keys = [tuple(sorted((a.upper(), b.upper()))) for a, b in edges]
            dup = sorted({"-".join(k) for k in keys if keys.count(k) > 1})
            if dup:
                raise ValueError(f"constants.adjacency[{family}] edges repeat: {dup}")
        return self


# ---------------------------------------------------------------------------
# the manifest
# ---------------------------------------------------------------------------


class Profile(_Closed):
    id: _Slug
    descriptors_dir: _RelPath
    lifecycle: _RelPath | None = None
    seeds: tuple[_Slug, ...] = ()
    rosters: Rosters
    prompts: _RelPath | None = None
    top_floor: _Slug | None = None
    ui: Ui
    projection: Projection | None = None
    constants: Constants = Field(default_factory=Constants)
    subjects: Subjects = Subjects()

    _root: Path | None = PrivateAttr(default=None)

    @field_validator("top_floor", mode="before")
    @classmethod
    def _none_is_no_top_floor(cls, v: Any) -> Any:
        return None if v == _NONE else v

    @model_validator(mode="after")
    def _consistent(self) -> "Profile":
        _unique(list(self.seeds), "seeds")
        producers = {f.producer for f in self.ui.frames if f.producer}
        if self.top_floor is not None and producers and self.top_floor not in producers:
            raise ValueError(
                f"top_floor {self.top_floor} is not the producer of any ui.frames entry"
            )
        frames = {f.id: f for f in self.ui.frames}
        c = self.constants
        for fid in c.audit_tier_order:
            if fid not in frames or frames[fid].producer is None:
                raise ValueError(
                    f"constants.audit_tier_order names {fid}, which is no ui.frames entry with a producer"
                )
        if c.subject_frame is not None and c.subject_frame not in frames:
            raise ValueError(f"constants.subject_frame names no ui.frames entry: {c.subject_frame}")
        stray = sorted(set(c.reference_subject_tags) - set(self.rosters.subject_tags))
        if stray:
            raise ValueError(f"constants.reference_subject_tags not in rosters.subject_tags: {stray}")
        return self

    # -- paths, resolved against the profile directory ----------------------

    @property
    def root(self) -> Path:
        if self._root is None:
            raise ProfileError("profile was not loaded from a directory (use load_profile)")
        return self._root

    def _path(self, rel: str | None) -> Path | None:
        return None if rel is None else (self.root / rel).resolve()

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    @property
    def descriptors_path(self) -> Path:
        return self._path(self.descriptors_dir)  # type: ignore[return-value]

    @property
    def retired_path(self) -> Path:
        return self.descriptors_path / "RETIRED.md"

    @property
    def lifecycle_path(self) -> Path | None:
        return self._path(self.lifecycle)

    @property
    def prompts_path(self) -> Path | None:
        return self._path(self.prompts)

    @property
    def prompts_package(self) -> str | None:
        """``legba_<id>``, the package inside :attr:`prompts_path`, or ``None``
        when the profile declares no prompts."""
        return None if self.prompts is None else prompts_package_name(self.id)

    @property
    def source_catalog_path(self) -> Path | None:
        return self._path(self.rosters.source_catalog)

    @property
    def gazetteer_path(self) -> Path | None:
        return self._path(self.rosters.gazetteer)

    @property
    def projection_view_path(self) -> Path | None:
        return None if self.projection is None else self._path(self.projection.view)

    @property
    def plane(self) -> str:
        """The plane id this profile's projection rows carry: the declared
        one, else the profile's own id (a plane with no view refuses)."""
        return self.projection.plane if self.projection is not None else self.id


def load_profile(path: str | os.PathLike[str]) -> Profile:
    """Load and validate ``<dir>/profile.yaml`` (or the manifest file itself).

    Raises :class:`ProfileError` for a missing manifest, a path that names
    nothing, or an ``id`` that is not the directory's name; pydantic's
    ``ValidationError`` (a ``ValueError``) for a malformed or unknown key.
    """
    p = Path(path)
    manifest = p / MANIFEST_NAME if p.is_dir() else p
    if not manifest.is_file():
        raise ProfileError(f"no profile manifest at {manifest}")
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ProfileError(f"{manifest} is not a mapping")
    profile = Profile.model_validate(data)
    root = manifest.parent.resolve()
    profile._root = root
    if profile.id != root.name:
        raise ProfileError(f"profile id {profile.id!r} is not its directory's name {root.name!r}")
    checks: list[tuple[str, Path | None, bool]] = [
        ("descriptors_dir", profile.descriptors_path, True),
        ("lifecycle", profile.lifecycle_path, False),
        ("prompts", profile.prompts_path, True),
        ("rosters.source_catalog", profile.source_catalog_path, False),
        ("rosters.gazetteer", profile.gazetteer_path, False),
        ("projection.view", profile.projection_view_path, False),
    ]
    for key, target, is_dir in checks:
        if target is None:
            continue
        if not (target.is_dir() if is_dir else target.is_file()):
            kind = "directory" if is_dir else "file"
            raise ProfileError(f"{manifest}: {key} names no {kind}: {target}")
    if profile.prompts_path is not None:
        pkg = profile.prompts_path / str(profile.prompts_package) / "__init__.py"
        if not pkg.is_file():
            raise ProfileError(
                f"{manifest}: prompts holds no package {profile.prompts_package} "
                f"(expected {pkg})"
            )
    return profile


def resolve_profile(
    cli: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    root: str | os.PathLike[str] | None = None,
) -> Path:
    """The profile directory to use: ``--profile`` beats ``LEGBA_PROFILE`` beats
    :data:`DEFAULT_PROFILE`. A relative choice resolves against ``root``
    (default: the working directory; deploy.sh runs from the repo root)."""
    env = os.environ if env is None else env
    choice = cli if cli not in (None, "") else (env.get(PROFILE_ENV) or DEFAULT_PROFILE)
    p = Path(choice)  # type: ignore[arg-type]
    if not p.is_absolute():
        p = Path(root if root is not None else Path.cwd()) / p
    return p


# ---------------------------------------------------------------------------
# the UI export
# ---------------------------------------------------------------------------


#: The unit keys the workstation export carries; the rest of a unit
#: (``query_terms``, ``title_terms``, ``reference_queries``) is instrument data.
UI_UNIT_KEYS: tuple[str, ...] = ("id", "question", "label", "short", "scope_tag", "scope_note")


def export_ui(profile: Profile) -> str:
    """The text of ``profile_ui.json`` for ``profile``.

    Undeclared optional keys are LEFT OUT (the workstation then renders its
    platform noun, never a blank and never another instance's word);
    ``top_floor`` is always present, ``null`` when the profile declares none.
    Keys sorted, two-space indent, trailing newline: deterministic bytes.
    """
    ui = profile.ui.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    rosters = profile.rosters.model_dump(
        mode="json",
        include={
            "subject_tags": True,
            "scored_subject_tags": True,
            # the workstation's half of a unit; its instrument terms ride the
            # packaged rosters export instead (legba.data.rosters)
            "units": {"__all__": set(UI_UNIT_KEYS)},
        },
        exclude_none=True,
        exclude_defaults=True,
    )
    rosters["subject_tags"] = list(profile.rosters.subject_tags)
    doc: dict[str, Any] = {
        "_generated": (
            "scripts/gen_profile_ui.py from profiles/"
            f"{profile.id}/{MANIFEST_NAME} - do not edit by hand"
        ),
        "schema": UI_EXPORT_SCHEMA,
        "profile": profile.id,
        # the projection plane the workstation reads (VIEW-1, UI-B2)
        "plane": profile.plane,
        "top_floor": profile.top_floor,
        "rosters": rosters,
        **ui,
    }
    return json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _summary(profile: Profile) -> str:
    r = profile.rosters
    return (
        f"profile {profile.id} ({profile.manifest_path})\n"
        f"  descriptors : {profile.descriptors_path}\n"
        f"  lifecycle   : {profile.lifecycle_path or 'none (shipped states)'}\n"
        f"  seeds       : {' '.join(profile.seeds) or 'none'}\n"
        f"  roster tags : {', '.join(r.subject_tags)}"
        f" (scored: {', '.join(r.scored_subject_tags) or 'none'})\n"
        f"  units       : {len(r.units)} (scored: {len(r.scored_units)})\n"
        f"  catalog     : {profile.source_catalog_path or 'none'}\n"
        f"  gazetteer   : {profile.gazetteer_path or 'none'}\n"
        f"  prompts     : {profile.prompts_package or 'none'}\n"
        f"  top floor   : {profile.top_floor or 'none'}\n"
        f"  map         : {profile.ui.map.geometry}\n"
        f"  projection  : "
        + (f"plane {profile.plane} ({profile.projection_view_path})" if profile.projection else "none")
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m legba.data.profile", description=__doc__.split("\n\n")[0])
    ap.add_argument("--profile", default=None, help=f"profile directory (default: ${PROFILE_ENV} or {DEFAULT_PROFILE})")
    ap.add_argument("command", choices=("check", "seeds"))
    args = ap.parse_args(argv)
    try:
        profile = load_profile(resolve_profile(args.profile))
    except (ProfileError, ValueError) as exc:
        print(f"profile: {exc}", file=sys.stderr)
        return 2
    if args.command == "seeds":
        print(" ".join(profile.seeds))
    else:
        print(_summary(profile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
