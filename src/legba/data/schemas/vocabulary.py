# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Vocabulary registry (per L-101 §8).

Operators add new `entity_class` / `relationship_type` / etc. values at
runtime without a schema migration. The seed set lives in
`legba.data.vocabulary` and is inserted by migration 0010; runtime entries
land via the registry CRUD which L-110 layers on top.

Families (W6 VOCAB-1). A family is either one of the platform's own
(`PLATFORM_FAMILIES` — a closed, PROTECTED set) or a profile's namespaced
`<profile>.<axis>` family (`sanctions.programme`, `sanctions.list`): lowercase,
dotted, at most three segments. `validate_family` is the one admission rule;
the `vocabulary_entries.family` column carries no CHECK (it never did), so
there is no migration.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: The platform's own vocabulary families — a CLOSED set, and PROTECTED: a
#: profile cannot redefine one (`family_protected`).
#:
#: `edge_family` (migration 0143) is the `entity_edges.edge_family` tier map;
#: the column carries its own CHECK, the row makes the tier readable through
#: the same registry every other closed vocabulary uses.
#:
#: `frame` (W7 FRAME-1) is a composition's subject groups — each entry a frame
#: with a `parent` link to its kind and its member rule in `aliases`
#: (`legba.data.frames`); the instance seeds the values, the family is the
#: platform's.
#:
#: `event_type` (W9 TAX-4) is `events.event_type`'s vocabulary, seeded from
#: descriptors/vocabulary_event_type.yaml; the column is also a CHECK, so the
#: family narrows it and never widens it (`legba.data.case_columns`).
PLATFORM_FAMILIES: frozenset[str] = frozenset({
    "entity_class",
    "relationship_type",
    "analyst_kind",
    "source_kind",
    "output_kind",
    "filter_kind",
    "enrichment_kind",
    "discovery_kind",
    "edge_family",
    # W7 TAXON-1 — four vocabularies that lived as code literals, seeded by
    # the bring-up walker from `legba.data.taxon_families` (the literal is the
    # fallback when a family is not loaded).
    "salience_class",
    "collection_gap_kind",
    "band",
    "source_layer",
    # W7 FRAME-1 — a composition's subject groups, seeded from
    # descriptors/vocabulary_frame.yaml by the walker's generic loader.
    "frame",
    # W9 TAX-4 — events.event_type, seeded from
    # descriptors/vocabulary_event_type.yaml by the walker's generic loader.
    "event_type",
    # W9 VIEW-1 (DEC-12) — the CROSSWALK: each entry maps a profile's value
    # (`value` = `<profile>.<axis>:<value>`) to the platform value it is
    # (`parent` = `<platform_family>:<value>`). Seeded from
    # descriptors/vocabulary_crosswalk.yaml by the same generic loader; read
    # by the profile projection (`legba.data.profile_projection`). An unmapped
    # profile value has no row — nothing is inferred.
    "crosswalk",
})

#: The crosswalk family (see :data:`PLATFORM_FAMILIES`).
CROSSWALK_FAMILY = "crosswalk"

#: Platform families a profile MAY carry its own copy of, as exactly
#: `<profile>.<family>` (`geopolitics.band`) — the case-shaped vocabularies
#: (W7 TAXON-1). The bare platform family stays protected (a profile cannot
#: redefine `band` itself); every other placement of the name in a profile
#: family (`p.band.sub`, `p.x.band`) is still `family_protected`. No reader
#: consults a profile's copy yet.
PROFILE_OVERRIDABLE_FAMILIES: frozenset[str] = frozenset({
    "salience_class",
    "collection_gap_kind",
    "band",
    "source_layer",
})

#: A profile-registered family: `<profile>.<axis>` (or `<profile>.<axis>.<sub>`)
#: — lowercase, dotted, 2..3 segments. The first segment is the profile.
NAMESPACED_FAMILY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,2}$")

#: Parent walk bound (an entry's `parent` chain is followed at most this far).
MAX_PARENT_DEPTH = 8

FAMILY_PROTECTED = "family_protected"
FAMILY_UNKNOWN = "family_unknown"
FAMILY_MALFORMED = "family_malformed"


class FamilyError(ValueError):
    """A vocabulary family the registry refuses. `code` is one of
    `family_protected` / `family_unknown` / `family_malformed`; `hint` says
    what to do about it."""

    def __init__(self, code: str, family: str, hint: str):
        super().__init__(f"{code}: {family!r} — {hint}")
        self.code = code
        self.family = family
        self.hint = hint


def validate_family(family: str, *, profile: str | None = None) -> str:
    """Admit a platform family or a `<profile>.<axis>` namespaced one.

    * a platform family is admitted as-is — unless a `profile` is registering
      (a profile cannot redefine a platform family): `family_protected`;
    * a dotted name must match the namespaced shape (`family_malformed`
      otherwise), and no segment may be a platform family name —
      `sanctions.entity_class` would shadow the platform axis:
      `family_protected`;
    * a bare name that is not a platform family is `family_unknown`, with the
      hint to namespace it;
    * when a `profile` is given, the family's first segment must be that
      profile (a profile registers only under its own namespace).
    """
    if not isinstance(family, str) or not family:
        raise FamilyError(
            FAMILY_MALFORMED, str(family), "family must be a non-empty string",
        )
    if family in PLATFORM_FAMILIES:
        if profile is not None:
            raise FamilyError(
                FAMILY_PROTECTED, family,
                f"{family!r} is a platform family and cannot be redefined by a "
                f"profile; register your own axis as {profile}.<axis>",
            )
        return family
    if "." not in family:
        if NAMESPACED_FAMILY_RE.match(f"x.{family}") is None:
            raise FamilyError(
                FAMILY_MALFORMED, family,
                "family names are lowercase letters, digits and underscores",
            )
        raise FamilyError(
            FAMILY_UNKNOWN, family,
            f"not a platform family; namespace it as <profile>.{family}",
        )
    if NAMESPACED_FAMILY_RE.match(family) is None:
        raise FamilyError(
            FAMILY_MALFORMED, family,
            "a namespaced family is <profile>.<axis> — lowercase, dotted, "
            "at most 3 segments",
        )
    segments = family.split(".")
    overriding = len(segments) == 2 and segments[1] in PROFILE_OVERRIDABLE_FAMILIES
    shadowed = [
        seg for i, seg in enumerate(segments)
        if seg in PLATFORM_FAMILIES and not (overriding and i == 1)
    ]
    if shadowed:
        raise FamilyError(
            FAMILY_PROTECTED, family,
            f"{shadowed[0]!r} is a platform family name and cannot appear in "
            f"a profile family",
        )
    if profile is not None and segments[0] != profile:
        raise FamilyError(
            FAMILY_PROTECTED, family,
            f"profile {profile!r} registers only under {profile}.<axis>",
        )
    return family


def qualify_family(profile: str, axis: str) -> str:
    """The family a profile registers for `axis`: `<profile>.<axis>`. An axis
    that is a platform family name is `family_protected` — unless it is one a
    profile may carry its own copy of (`PROFILE_OVERRIDABLE_FAMILIES`), which
    qualifies to `<profile>.<axis>`; one already qualified must lie under the
    profile's own namespace."""
    if axis in PLATFORM_FAMILIES and axis not in PROFILE_OVERRIDABLE_FAMILIES:
        return validate_family(axis, profile=profile)
    family = axis if "." in axis else f"{profile}.{axis}"
    return validate_family(family, profile=profile)


def split_parent(parent: str, *, default_family: str) -> tuple[str, str]:
    """Split a family-qualified parent id `<family>:<value>`. A bare value
    (no colon — the legacy shape) is a value of `default_family`."""
    if ":" in parent:
        fam, _, val = parent.partition(":")
        return fam, val
    return default_family, parent


class VocabularyEntry(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    #: A platform family (closed, protected) or a namespaced
    #: `<profile>.<axis>` family — admission is `validate_family`. This used
    #: to be a closed Literal, which made a family present in the TABLE but
    #: missing from the Literal fail the whole `VocabularyCache.refresh()`.
    family: str
    value: str
    schema_uri: str = Field(pattern=r"^legba/vocabulary/\d+\.\d+\.\d+$")
    introduced: datetime
    deprecated: datetime | None = None
    notes: str | None = None
    aliases: list[str] = Field(default_factory=list)
    #: A family-qualified id (`<family>:<value>`); a bare value is read as a
    #: value of this entry's own family. The resolver walks it.
    parent: str | None = None

    @field_validator("family")
    @classmethod
    def _family_admitted(cls, v: str) -> str:
        return validate_family(v)

    @field_validator("value")
    @classmethod
    def _value_shape(cls, v: str, info) -> str:
        family = info.data.get("family")
        if family == CROSSWALK_FAMILY:
            # `<profile>.<axis>:<value>` — the profile side of the pair. Its
            # family must be a namespaced profile family (a platform family's
            # value is already the platform's); its value keeps the profile's
            # own shape (a programme code may be upper-case).
            fam, sep, val = v.partition(":")
            if not sep or "." not in fam:
                raise ValueError("crosswalk value must be <profile>.<axis>:<value>")
            validate_family(fam)
            if not val or val != val.strip():
                raise ValueError(
                    "crosswalk value's profile value must be non-empty "
                    "without surrounding whitespace"
                )
            return v
        if family == "relationship_type":
            if not v[:1].isupper() or not v.isidentifier():
                raise ValueError("relationship_type must be PascalCase identifier")
        elif family in PLATFORM_FAMILIES:
            if v != v.lower() or " " in v:
                raise ValueError(f"{family} must be lowercase_snake_case")
        elif family is not None:
            # A namespaced family's values are the profile's own vocabulary
            # (a programme code may be upper-case); only emptiness and
            # surrounding whitespace are refused.
            if not v or v != v.strip():
                raise ValueError(
                    f"{family} value must be non-empty without surrounding whitespace"
                )
        return v

    @field_validator("parent")
    @classmethod
    def _parent_shape(cls, v: str | None, info) -> str | None:
        if v is None:
            return v
        family = info.data.get("family") or ""
        pfam, pval = split_parent(v, default_family=family)
        if not pval.strip() or ":" in pval:
            raise ValueError("parent must be <family>:<value> (or a bare value of this family)")
        if ":" in v:
            validate_family(pfam)
        if family == CROSSWALK_FAMILY and (
            ":" not in v or pfam not in PLATFORM_FAMILIES or pfam == CROSSWALK_FAMILY
        ):
            raise ValueError(
                "a crosswalk entry's parent is the platform value it maps to: "
                "<platform_family>:<value>"
            )
        return v

    @model_validator(mode="after")
    def _crosswalk_has_a_counterpart(self) -> "VocabularyEntry":
        # A crosswalk row with no platform counterpart says nothing: an
        # unmapped profile value is simply not a row.
        if self.family == CROSSWALK_FAMILY and not self.parent:
            raise ValueError("a crosswalk entry needs parent: <platform_family>:<value>")
        return self


class VocabularyRegistry(BaseModel):
    entries: list[VocabularyEntry]

    def values(self, family: str) -> set[str]:
        return {
            e.value
            for e in self.entries
            if e.family == family and e.deprecated is None
        }

    def index(self) -> dict[tuple[str, str], VocabularyEntry]:
        """Current (non-deprecated) entries keyed by (family, value)."""
        return {(e.family, e.value): e for e in self.entries if e.deprecated is None}


def walk_parents(
    index: dict[tuple[str, str], Any],
    family: str,
    value: str,
    *,
    max_depth: int = MAX_PARENT_DEPTH,
) -> list[tuple[str, str]]:
    """The ancestors of (family, value), nearest first, as (family, value)
    pairs. Bounded (`max_depth` hops) and cycle-safe (a node already seen
    ends the walk); a parent that is not a current entry ends it too — the
    chain stops at the last real node and never raises. `index` maps
    (family, value) to anything with `.family` and `.parent` attributes."""
    chain: list[tuple[str, str]] = []
    seen = {(family, value)}
    cur = index.get((family, value))
    while cur is not None and cur.parent and len(chain) < max_depth:
        nxt = split_parent(cur.parent, default_family=cur.family)
        if nxt in seen:
            break
        seen.add(nxt)
        node = index.get(nxt)
        if node is None:
            break
        chain.append(nxt)
        cur = node
    return chain
